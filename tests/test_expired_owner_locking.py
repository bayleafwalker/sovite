"""Recovery-only nonblocking Job/lease acquisition contract."""
from __future__ import annotations

import os
import threading
import time
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select, text

from sovite.collection_transaction import owner_transaction
from sovite.db import Database, Job, WorkerExecutionLease
from sovite.recovery_cursor import advance_cursor, read_cursor
from sovite.work_projection import (
    _WorkProjectionBusy, _WorkProjectionUnavailable, lock_work_projection_set,
)


def _owned_pg_url():
    value = os.environ.get("SOVITE_EXPIRED_OWNER_TEST_POSTGRES_URL")
    if not value:
        pytest.skip("isolated expired-owner PostgreSQL fixture not configured")
    if value != "postgresql+psycopg://sovite:sovite-test-only@127.0.0.1:55439/sovite_test":
        pytest.fail("expired-owner test requires its separately labeled local PostgreSQL fixture")
    return value


def test_recovery_lock_classifies_skip_locked_and_retries_after_release():
    url = _owned_pg_url()
    admin = create_engine(url)
    schema = "expired_owner_" + uuid.uuid4().hex
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    db = Database(url, schema=schema)
    try:
        db.migrate()
        now = 100.0
        with db.sessions.begin() as session:
            session.add(Job(
                id="expired-job", receipt="r" * 96, idempotency_key="i" * 128,
                request_digest="d" * 80, caller="caller", grant_id="grant",
                generation="generation", resolved={"compute": {"id": "compute"}}, task="task",
                status="running", lease_owner="owner", lease_until=now - 1,
            ))
            session.add(WorkerExecutionLease(
                job_id="expired-job", workload_id="workload", instance_id="i" * 64,
                fence="f" * 64, generation="generation", phase="started",
                lease_until=now - 1, execution_deadline=now + 10,
                created_at=now - 20, updated_at=now - 1,
            ))
        ledger_tables = __import__("sovite.db", fromlist=["Base"]).Base.metadata.tables
        with db.sessions.begin() as session:
            session.execute(ledger_tables["program_budget_scopes"].insert().values(
                id="lock-scope", policy_revision=1, window_start_ms=0, window_end_ms=1000,
                usd_limit_micro=10, allowance_unit="unit", allowance_limit_micro=10,
                slot_limit=1, policy_digest="p" * 64,
            ))

        holder = db.sessions()
        try:
            holder.execute(text("BEGIN"))
            holder.execute(select(Job).where(Job.id == "expired-job").with_for_update()).scalar_one()
            with pytest.raises(_WorkProjectionBusy):
                with owner_transaction(db) as session:
                    lock_work_projection_set(
                        session, (), nonblocking_owner_ids=("expired-job",),
                    )
        finally:
            holder.rollback()
            holder.close()

        lease_holder = db.sessions()
        try:
            lease_holder.execute(text("BEGIN"))
            lease_holder.execute(select(WorkerExecutionLease).where(
                WorkerExecutionLease.job_id == "expired-job"
            ).with_for_update()).scalar_one()
            with pytest.raises(_WorkProjectionBusy):
                with owner_transaction(db) as session:
                    lock_work_projection_set(
                        session, (), nonblocking_owner_ids=("expired-job",),
                    )
        finally:
            lease_holder.rollback()
            lease_holder.close()

        with pytest.raises(_WorkProjectionUnavailable):
            with owner_transaction(db) as session:
                lock_work_projection_set(session, (), nonblocking_owner_ids=("missing-job",))
        with db.sessions.begin() as session:
            session.add(Job(
                id="job-without-lease", receipt="s" * 96, idempotency_key="j" * 128,
                request_digest="e" * 80, caller="caller", grant_id="grant",
                generation="generation", resolved={"compute": {"id": "compute"}}, task="task",
                status="running", lease_owner="owner", lease_until=now - 1,
            ))
        with pytest.raises(_WorkProjectionUnavailable):
            with owner_transaction(db) as session:
                lock_work_projection_set(session, (), nonblocking_owner_ids=("job-without-lease",))

        # A refusal after earlier ordered-class locks and a tentative write
        # rolls the whole candidate transaction back.
        projection = SimpleNamespace(
            scope_id="lock-scope", call_id="call", slot_id="slot", subject_id="expired-job",
            purpose="work", work_job_id="expired-job", policy_revision=1,
        )
        rollback_holder = db.sessions()
        rollback_holder.execute(text("BEGIN"))
        rollback_holder.execute(select(Job).where(Job.id == "expired-job").with_for_update()).scalar_one()
        try:
            with pytest.raises(_WorkProjectionBusy):
                with owner_transaction(db) as session:
                    session.execute(ledger_tables["program_budget_scopes"].update().where(
                        ledger_tables["program_budget_scopes"].c.id == "lock-scope"
                    ).values(usd_limit_micro=5))
                    session.flush()
                    lock_work_projection_set(
                        session, (projection,), expected_job_id="expired-job",
                        nonblocking_owner_ids=("expired-job",),
                    )
        finally:
            rollback_holder.rollback()
            rollback_holder.close()
        with db.sessions() as session:
            assert session.execute(select(
                ledger_tables["program_budget_scopes"].c.usd_limit_micro
            ).where(ledger_tables["program_budget_scopes"].c.id == "lock-scope")).scalar_one() == 10

        with owner_transaction(db) as session:
            lock_work_projection_set(session, (), nonblocking_owner_ids=("expired-job",))

        # Ordinary finish/hand-back closure must retain its blocking owner lock.
        projection = SimpleNamespace(
            scope_id="scope", call_id="call", slot_id="slot", subject_id="expired-job",
            purpose="work", work_job_id="expired-job", policy_revision=1,
        )
        blocking_holder = db.sessions()
        blocking_holder.execute(text("BEGIN"))
        blocking_holder.execute(select(Job).where(Job.id == "expired-job").with_for_update()).scalar_one()
        started = threading.Event()
        outcomes = []

        def ordinary_lock():
            try:
                with owner_transaction(db) as session:
                    started.set()
                    lock_work_projection_set(session, (projection,), expected_job_id="expired-job")
            except Exception as error:
                outcomes.append(type(error).__name__)
            else:
                outcomes.append("complete")

        thread = threading.Thread(target=ordinary_lock, daemon=True)
        try:
            thread.start()
            assert started.wait(3)
            time.sleep(0.1)
            assert thread.is_alive(), "default owner closure must wait for a locked Job"
        finally:
            blocking_holder.rollback()
            blocking_holder.close()
        thread.join(5)
        assert not thread.is_alive(), "default owner closure did not resume after lock release"
        assert outcomes == ["complete"]
    finally:
        db.engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_cursor_json_cas_on_owned_postgresql():
    url = _owned_pg_url()
    admin = create_engine(url)
    schema = "expired_cursor_" + uuid.uuid4().hex
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    db = Database(url, schema=schema)
    try:
        db.migrate()
        with owner_transaction(db) as session:
            initial = read_cursor(session, "compute", False)
            assert advance_cursor(session, initial, "job-1", phase="forward",
                                  scan_started_at=1.0, wrap_until_job_id=None)
            assert not advance_cursor(session, initial, "job-2", phase="forward",
                                      scan_started_at=1.0, wrap_until_job_id=None)
        with owner_transaction(db) as session:
            assert read_cursor(session, "compute", False).last_job_id == "job-1"
    finally:
        db.engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
