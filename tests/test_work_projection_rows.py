"""Real-row synthetic checks for the dormant Work comparator."""
import asyncio
import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, delete, insert, make_url, select, text, update

from sovite.adapters import RuntimeFailure
from sovite.collection_transaction import owner_transaction
from sovite.db import Base, Database, Job, MetadataRow, digest
from sovite.lease_service import FinishRefused, LeaseService, execution_owner_key
from sovite.repository_runs import RUN_SCHEMA, RepositoryRunAttemptExecutor
from sovite.results import _projection
from sovite.service import Platform, PlatformError
from sovite.settings import Settings
from sovite.work_projection import (
    _WorkProjectionUnavailable,
    _compare_actual_work_projection,
    projection_digest,
)
from sovite.worker import Worker


def _local_test_url(value):
    url = make_url(value)
    if url.get_backend_name() != "postgresql":
        pytest.fail("SOVITE_TEST_POSTGRES_URL must select PostgreSQL")
    if any(key.startswith("PG") for key in os.environ):
        pytest.fail("Work projection tests refuse inherited libpq PG environment variables")
    if set(url.query) - {"host", "port"}:
        pytest.fail("Work projection PostgreSQL URL permits only local host and port options")
    query_hosts = url.query.get("host", ())
    if isinstance(query_hosts, str):
        query_hosts = (query_hosts,)
    socket_target = not url.host and len(query_hosts) == 1 and query_hosts[0].startswith("/tmp/")
    ci_service_target = (
        os.environ.get("SOVITE_TEST_POSTGRES_CI") == "1"
        and url.host == "127.0.0.1" and url.port == 5432
        and url.username == "sovite_resource_test"
        and url.database == "sovite_resource_test"
    )
    if not (socket_target or ci_service_target):
        pytest.fail("Work projection tests require a /tmp Unix socket or the opted-in local PG16 fixture")
    database_name = (url.database or "").lower()
    if not any(token in database_name for token in ("test", "oracle", "fixture")):
        pytest.fail("Work projection tests require a dedicated test/oracle/fixture database")
    return url


def _job_resolved_update(db):
    table = '"jobs"' if db.schema is None else f'"{db.schema}"."jobs"'
    return text(f"UPDATE {table} SET resolved = :resolved WHERE id = :job_id")


@pytest.fixture(params=("sqlite", "postgresql"), ids=("sqlite", "postgresql"))
def work_rows(request, tmp_path):
    backend = request.param
    admin = None
    db = None
    namespace = None
    owned_oid = None
    marker = None
    try:
        if backend == "sqlite":
            db = Database(f"sqlite:///{tmp_path / 'projection.db'}")
        else:
            if any(key.startswith("PG") for key in os.environ):
                pytest.fail("Work projection tests refuse inherited libpq PG environment variables")
            value = os.environ.get("SOVITE_TEST_POSTGRES_URL")
            if not value:
                if os.environ.get("SOVITE_WORK_PROJECTION_REQUIRE_PG") == "1":
                    pytest.fail("SOVITE_TEST_POSTGRES_URL is required for Work projection PostgreSQL tests")
                pytest.skip("local isolated PostgreSQL test database not supplied")
            url = _local_test_url(value)
            admin = create_engine(url)
            namespace = "work_projection_test_" + uuid.uuid4().hex
            marker = "sovite-work-projection-" + uuid.uuid4().hex
            with admin.begin() as connection:
                is_superuser = connection.exec_driver_sql(
                    "SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user"
                ).scalar_one()
                if is_superuser:
                    pytest.fail("Work projection tests refuse PostgreSQL superuser roles")
                server_uses_socket = connection.exec_driver_sql(
                    "SELECT inet_server_addr() IS NULL"
                ).scalar_one()
                ci_service_target = (
                    os.environ.get("SOVITE_TEST_POSTGRES_CI") == "1"
                    and url.host == "127.0.0.1" and url.port == 5432
                    and url.username == "sovite_resource_test"
                    and url.database == "sovite_resource_test"
                )
                query_hosts = url.query.get("host", ())
                if isinstance(query_hosts, str):
                    query_hosts = (query_hosts,)
                socket_target = (not url.host and len(query_hosts) == 1
                                 and query_hosts[0].startswith("/tmp/"))
                if (not socket_target and not ci_service_target
                        or socket_target and server_uses_socket is not True
                        or ci_service_target and server_uses_socket is not False):
                    pytest.fail("Work projection PostgreSQL transport does not match the approved local fixture")
                if connection.exec_driver_sql("SELECT current_database()").scalar_one() != url.database:
                    pytest.fail("Work projection tests require the explicitly supplied database")
                connection.exec_driver_sql(f'CREATE SCHEMA "{namespace}"')
                connection.exec_driver_sql(f'COMMENT ON SCHEMA "{namespace}" IS \'{marker}\'')
                owned_oid = connection.exec_driver_sql(
                    "SELECT oid FROM pg_catalog.pg_namespace WHERE nspname = %s", (namespace,)
                ).scalar_one()
                schema_owned = connection.exec_driver_sql(
                    "SELECT n.nspowner = r.oid FROM pg_catalog.pg_namespace n "
                    "JOIN pg_catalog.pg_roles r ON r.rolname = current_user WHERE n.nspname = %s",
                    (namespace,),
                ).scalar_one()
                assert schema_owned, "the UUID schema must be owned by the non-superuser test role"
            db = Database(url.render_as_string(hide_password=False), schema=namespace)

        db.migrate()

        h, policy_hash, request_hash = "a" * 64, "c" * 64, "b" * 64
        price_hash, meter_hash, permit_hash = "d" * 64, "e" * 64, "f" * 64
        generation, fence = "sha256:" + "1" * 64, "2" * 32
        lease_expiry = time.time() + 300
        fence_hash = hashlib.sha256(fence.encode()).hexdigest()
        identity_digest = digest({"source_code": "fixture", "issuer_code": "fixture"})
        lease_digest = digest({
            "job_id": "job", "attempt_id": "job-1", "workload_id": "workload",
            "instance_id": "instance", "generation": generation,
            "fence_sha256": fence_hash,
        })
        payload = {
            "schema_version": "sovite-work-projection/v1",
            "scope_id": "scope", "work_job_id": "job", "subject_id": "job",
            "attempt_id": "job-1", "call_id": "call", "slot_id": "slot",
            "effect_key": "effect", "receipt_id": None, "evidence_id": None,
            "purpose": "work", "lease_digest": lease_digest,
            "request_digest": request_hash, "policy_digest": policy_hash,
            "receipt_digest": None, "allowance_unit": "opencode-go-allowance",
            "policy_revision": "1", "period_start_ms": "100",
            "period_end_ms": "2000", "projection_revision": "1",
            "reserved_usd_micro": "10", "reserved_allowance_micro": "20",
            "audit_sequence": "1", "actual_usd_micro": None,
            "actual_allowance_micro": None, "intent_state": "committed",
            "reservation_state": "reserved", "slot_state": "held",
        }
        job_resolved = {
            "repository_run": {
                "schema_version": RUN_SCHEMA,
                "attempts": [{"id": "job-1", "state": "running",
                              "fence_sha256": fence_hash}],
                "program_ledger_projections_v1": [payload],
            },
        }
        raw_resolved = json.dumps(job_resolved, separators=(",", ":"))
        tables = Base.metadata.tables
        with db.sessions.begin() as session:
            audit = db.audit(session, "synthetic-test", "seed", "work-projection")
            session.flush()
            sequence = audit.sequence
            assert sequence == 1
            session.add(Job(
                id="job", receipt="job", idempotency_key="job",
                request_digest=request_hash, caller="fixture", grant_id="fixture",
                generation=generation, resolved={}, task="synthetic",
                status="running", lease_owner=execution_owner_key(
                    workload_id="workload", instance_id="instance"),
                lease_until=lease_expiry, attempts=1,
            ))
            session.flush()
            session.execute(_job_resolved_update(db), {"resolved": raw_resolved, "job_id": "job"})
            session.execute(insert(tables["program_budget_scopes"]), dict(
                id="scope", policy_revision=1, window_start_ms=100, window_end_ms=2000,
                usd_limit_micro=100, allowance_unit="opencode-go-allowance",
                allowance_limit_micro=100, slot_limit=10, policy_digest=policy_hash,
            ))
            session.execute(insert(tables["program_policy_revisions"]), dict(
                scope_id="scope", revision=1, policy_digest=policy_hash,
                policy_canonical="{}", currency="USD", allowance_unit="opencode-go-allowance",
                window_start_ms=100, window_end_ms=2000, usd_limit_micro=100,
                allowance_limit_micro=100, slot_limit=10,
                price_policy_digest=price_hash, meter_policy_digest=meter_hash,
                permit_policy_digest=permit_hash, manifest_digest=h,
                source_identity_digest=identity_digest, audit_sequence=sequence,
            ))
            session.execute(insert(tables["program_gate_state"]), dict(
                scope_id="scope", policy_revision=1, policy_digest=policy_hash,
                mode="disabled", revision=0, audit_sequence=sequence,
            ))
            session.execute(insert(tables["program_budget_subjects"]), dict(
                scope_id="scope", subject_id="job", purpose="work", binding_digest=h,
            ))
            session.execute(insert(tables["program_subject_registry"]), dict(
                scope_id="scope", subject_id="job", purpose="work",
                binding_digest=h, audit_sequence=sequence,
            ))
            session.execute(insert(tables["program_reservations"]), dict(
                scope_id="scope", call_id="call", subject_id="job", purpose="work",
                effect_key="effect", reserved_usd_micro=10, reserved_allowance_micro=20,
                permit_digest=permit_hash, meter_digest=meter_hash, price_digest=price_hash,
                permit_expires_ms=1500, state="reserved", audit_sequence=sequence,
            ))
            session.execute(insert(tables["program_slots"]), dict(
                scope_id="scope", slot_id="slot", subject_id="job", call_id="call",
                state="held", audit_sequence=sequence,
            ))
            session.execute(insert(tables["program_call_intents"]), dict(
                scope_id="scope", call_id="call", subject_id="job", purpose="work",
                effect_key="effect", idempotency_key="idempotency", request_digest=request_hash,
                policy_revision=1, policy_digest=policy_hash, price_digest=price_hash,
                meter_digest=meter_hash, permit_digest=permit_hash, source_code="fixture",
                issuer_code="fixture", source_identity_digest=identity_digest,
                observed_ms=200, valid_until_ms=1500, state="committed",
                work_job_id="job", work_attempt_id="job-1", work_lease_digest=lease_digest,
                audit_sequence=sequence,
            ))
            session.execute(insert(tables["worker_execution_leases"]), dict(
                job_id="job", workload_id="workload", instance_id="instance",
                fence=fence, generation=generation, phase="started", lease_until=lease_expiry,
                execution_deadline=lease_expiry, created_at=0.0, updated_at=0.0,
            ))
            session.execute(insert(tables["program_work_projection_bindings"]), dict(
                scope_id="scope", call_id="call", subject_id="job", work_job_id="job",
                attempt_id="job-1", slot_id="slot", purpose="work",
                policy_revision=1, policy_digest=policy_hash,
                projection_revision=1, projection_digest=projection_digest(payload),
                audit_sequence=sequence,
            ))

        yield db, payload, raw_resolved, tables["program_work_projection_bindings"]
    finally:
        if db is not None:
            db.engine.dispose()
        if admin is not None:
            if owned_oid is not None:
                with admin.begin() as connection:
                    matches = connection.exec_driver_sql(
                        "SELECT n.oid = %s AND n.nspowner = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = current_user) "
                        "AND obj_description(n.oid, 'pg_namespace') = %s "
                        "FROM pg_catalog.pg_namespace n WHERE n.nspname = %s",
                        (owned_oid, marker, namespace),
                    ).scalar_one_or_none()
                    if matches is True:
                        connection.exec_driver_sql(f'DROP SCHEMA "{namespace}" CASCADE')
            admin.dispose()


def test_comparator_agrees_only_from_locked_real_rows(work_rows):
    db, _, _, _ = work_rows
    with owner_transaction(db) as session:
        assert _compare_actual_work_projection(session, scope_id="scope", call_id="call") == "canonical_rows_agree"


def test_comparator_refuses_a_plain_session_and_cannot_accept_row_mappings(work_rows):
    db, _, _, _ = work_rows
    with db.sessions() as session:
        with pytest.raises(_WorkProjectionUnavailable):
            _compare_actual_work_projection(session, scope_id="scope", call_id="call")
        with pytest.raises(TypeError):
            _compare_actual_work_projection(session, scope_id="scope", call_id="call",
                                            rows={"reservation": {"state": "reserved"}})


@pytest.mark.parametrize("finish", ["commit", "rollback"])
def test_replaced_owner_transaction_is_refused(work_rows, finish):
    db, _, _, _ = work_rows
    with pytest.raises(RuntimeError, match="replaced or nested"):
        with owner_transaction(db) as session:
            getattr(session, finish)()
            session.execute(select(text("1")))
            with pytest.raises(_WorkProjectionUnavailable):
                _compare_actual_work_projection(session, scope_id="scope", call_id="call")


def test_nested_owner_transaction_is_refused(work_rows):
    db, _, _, _ = work_rows
    with owner_transaction(db) as session:
        with session.begin_nested():
            with pytest.raises(_WorkProjectionUnavailable):
                _compare_actual_work_projection(session, scope_id="scope", call_id="call")


@pytest.mark.parametrize("raw", [
    '{"padding":"' + 'x' * 1048576 + '"}',
    '{"nested":' + '[' * 33 + '0' + ']' * 33 + '}',
])
def test_stored_json_bounds_refuse_without_mutation(work_rows, raw):
    db, _, _, _ = work_rows
    with db.sessions.begin() as session:
        session.execute(_job_resolved_update(db), {"resolved": raw, "job_id": "job"})
    with owner_transaction(db) as session:
        with pytest.raises(_WorkProjectionUnavailable):
            _compare_actual_work_projection(session, scope_id="scope", call_id="call")


def _settle_synthetic_call(work_rows):
    db, original, _, binding = work_rows
    payload = dict(original)
    tables = Base.metadata.tables
    receipt_fields = dict(
        scope_id="scope", call_id="call", receipt_id="receipt",
        actual_usd="5", actual_allowance="10", currency="USD",
        allowance_unit=payload["allowance_unit"], source_code="fixture",
        issuer_code="fixture", source_digest="9" * 64,
        source_identity_digest=digest({"source_code": "fixture", "issuer_code": "fixture"}),
        custody_ref="fixture-object", custody_bytes=10, observed_ms=300,
    )
    receipt_hash = digest(receipt_fields)
    payload.update(
        receipt_id="receipt", receipt_digest=receipt_hash,
        actual_usd_micro="5", actual_allowance_micro="10",
        intent_state="known", reservation_state="settled", slot_state="released",
        audit_sequence="2",
    )
    job_value = {"repository_run": {
        "schema_version": RUN_SCHEMA,
        "attempts": [{"id": "job-1", "state": "running",
                      "fence_sha256": hashlib.sha256(("2" * 32).encode()).hexdigest()}],
        "program_ledger_projections_v1": [payload],
    }}
    with db.sessions.begin() as session:
        event = db.audit(session, "synthetic-test", "settle", "work-projection")
        session.flush()
        assert event.sequence == 2
        session.execute(update(tables["program_reservations"]).where(
            tables["program_reservations"].c.scope_id == "scope",
            tables["program_reservations"].c.call_id == "call",
        ).values(state="settled", actual_usd_micro=5, actual_allowance_micro=10,
                 audit_sequence=event.sequence))
        session.execute(update(tables["program_slots"]).where(
            tables["program_slots"].c.scope_id == "scope",
            tables["program_slots"].c.call_id == "call",
        ).values(state="released", audit_sequence=event.sequence))
        session.execute(update(tables["program_call_intents"]).where(
            tables["program_call_intents"].c.scope_id == "scope",
            tables["program_call_intents"].c.call_id == "call",
        ).values(state="known", audit_sequence=event.sequence))
        session.execute(insert(tables["program_call_receipts"]), dict(
            scope_id="scope", call_id="call", receipt_id="receipt",
            receipt_digest=receipt_hash, outcome="known", actual_usd_micro=5,
            actual_allowance_micro=10, evidence_id=None, currency="USD",
            allowance_unit=payload["allowance_unit"], source_code="fixture",
            issuer_code="fixture", observed_ms=300, custody_ref="fixture-object",
            source_digest="9" * 64, source_identity_digest=receipt_fields["source_identity_digest"],
            custody_bytes=10, audit_sequence=event.sequence,
        ))
        session.execute(update(binding).where(
            binding.c.scope_id == "scope", binding.c.call_id == "call",
        ).values(projection_digest=projection_digest(payload), audit_sequence=event.sequence))
        session.execute(_job_resolved_update(db), {
            "resolved": json.dumps(job_value, separators=(",", ":")), "job_id": "job",
        })
    return payload


def test_comparator_matches_an_actual_known_receipt_row(work_rows):
    db, _, _, _ = work_rows
    _settle_synthetic_call(work_rows)
    with owner_transaction(db) as session:
        assert _compare_actual_work_projection(session, scope_id="scope", call_id="call") == "canonical_rows_agree"


def test_changed_receipt_row_is_unavailable(work_rows):
    db, _, _, _ = work_rows
    _settle_synthetic_call(work_rows)
    with db.sessions.begin() as session:
        session.execute(update(Base.metadata.tables["program_call_receipts"]).where(
            Base.metadata.tables["program_call_receipts"].c.call_id == "call",
        ).values(actual_usd_micro=6))
    with pytest.raises(_WorkProjectionUnavailable):
        with owner_transaction(db) as session:
            _compare_actual_work_projection(session, scope_id="scope", call_id="call")


@pytest.mark.parametrize("mutation", ["reservation", "binding", "lease", "duplicate-json", "unsorted-array"])
def test_any_stale_or_malformed_row_refuses_without_comparator_mutation(work_rows, mutation):
    db, payload, raw_resolved, binding = work_rows
    tables = Base.metadata.tables
    with db.sessions.begin() as session:
        if mutation == "reservation":
            session.execute(update(tables["program_reservations"]).where(
                tables["program_reservations"].c.call_id == "call").values(reserved_usd_micro=11))
        elif mutation == "binding":
            session.execute(update(binding).where(binding.c.call_id == "call").values(
                projection_digest="0" * 64))
        elif mutation == "lease":
            session.execute(update(tables["worker_execution_leases"]).where(
                tables["worker_execution_leases"].c.job_id == "job").values(phase="expired"))
        elif mutation == "duplicate-json":
            session.execute(_job_resolved_update(db), {
                "resolved": '{"repository_run":{"program_ledger_projections_v1":[], '
                            '"program_ledger_projections_v1":[]}}', "job_id": "job",
            })
        else:
            other = dict(payload, call_id="call-z", effect_key="effect-z")
            # Valid individual payloads in descending UTF-8 call order.
            raw = json.dumps({"repository_run": {"program_ledger_projections_v1": [
                other, payload,
            ]}}, separators=(",", ":"))
            session.execute(_job_resolved_update(db), {"resolved": raw, "job_id": "job"})

    with db.sessions() as session:
        before_state = session.scalar(select(tables["program_reservations"].c.state))
        before_audits = session.scalar(select(text("count(*)")).select_from(tables["audit_events"]))
    with pytest.raises(_WorkProjectionUnavailable):
        with owner_transaction(db) as session:
            _compare_actual_work_projection(session, scope_id="scope", call_id="call")
    with db.sessions() as session:
        assert session.scalar(select(tables["program_reservations"].c.state)) == before_state
        assert session.scalar(select(text("count(*)")).select_from(tables["audit_events"])) == before_audits


def test_absent_binding_relation_refuses_and_does_not_create_it(work_rows):
    db, _, _, binding = work_rows
    binding.drop(db.engine)
    with pytest.raises(_WorkProjectionUnavailable):
        with owner_transaction(db) as session:
            _compare_actual_work_projection(session, scope_id="scope", call_id="call")
    assert "program_work_projection_bindings" not in set(
        __import__("sqlalchemy").inspect(db.engine).get_table_names(schema=db.schema))


def test_explicit_uncertain_attempt_holds_projection_in_same_owner_transaction(work_rows, tmp_path):
    db, original, _, binding = work_rows
    fence = "2" * 32
    executor = RepositoryRunAttemptExecutor(
        type("Platform", (), {"db": db})(), object(), tmp_path, lambda *args, **kwargs: None
    )

    def uncertain(run):
        run["attempts"][-1].update(state="uncertain", finished_at="2026-11-03T00:00:00+00:00")

    executor._persist("job", fence, uncertain, uncertain_attempt_id="job-1")

    tables = Base.metadata.tables
    with db.sessions() as session:
        reservation = session.execute(select(tables["program_reservations"])).mappings().one()
        slot = session.execute(select(tables["program_slots"])).mappings().one()
        intent = session.execute(select(tables["program_call_intents"])).mappings().one()
        gate = session.execute(select(tables["program_gate_state"])).mappings().one()
        binding_row = session.execute(select(binding)).mappings().one()
        job = session.get(Job, "job")
        projection = job.resolved["repository_run"]["program_ledger_projections_v1"][0]

    assert (reservation["state"], slot["state"], intent["state"]) == ("unknown", "unknown", "unknown")
    assert (reservation["reserved_usd_micro"], reservation["reserved_allowance_micro"]) == (10, 20)
    assert gate["mode"] == "inhibited"
    assert projection["intent_state"] == projection["reservation_state"] == projection["slot_state"] == "unknown"
    assert projection["projection_revision"] == "2"
    assert binding_row["projection_revision"] == 2
    for field in ("scope_id", "call_id", "subject_id", "work_job_id", "attempt_id",
                  "slot_id", "purpose", "policy_revision", "policy_digest"):
        assert binding_row[field] == (int(original[field]) if field == "policy_revision"
                                      else original[field])
    assert job.resolved["repository_run"]["attempts"][-1]["state"] == "uncertain"


def _hand_back_service(db, *, captured_fence=None):
    return LeaseService(
        SimpleNamespace(db=db),
        execution_owner_key(workload_id="workload", instance_id="instance"),
        {"job": "2" * 32} if captured_fence is None else {"job": captured_fence},
    )


def _set_hand_back_attempt_state(db, state):
    with db.sessions() as session:
        job = session.get(Job, "job")
        resolved = json.loads(json.dumps(job.resolved))
    resolved["repository_run"]["attempts"][-1]["state"] = state
    with db.sessions.begin() as session:
        session.execute(_job_resolved_update(db), {
            "resolved": json.dumps(resolved, separators=(",", ":")), "job_id": "job",
        })


def test_hand_back_holds_possible_dispatch_and_inhibits_gate_atomically(work_rows):
    db, original, _, binding = work_rows
    service = _hand_back_service(db)

    assert service.hand_back("job", fence="2" * 32) is True

    tables = Base.metadata.tables
    with db.sessions() as session:
        job = session.get(Job, "job")
        lease = session.execute(select(tables["worker_execution_leases"])).mappings().one()
        reservation = session.execute(select(tables["program_reservations"])).mappings().one()
        slot = session.execute(select(tables["program_slots"])).mappings().one()
        intent = session.execute(select(tables["program_call_intents"])).mappings().one()
        gate = session.execute(select(tables["program_gate_state"])).mappings().one()
        binding_row = session.execute(select(binding)).mappings().one()
        projection = job.resolved["repository_run"]["program_ledger_projections_v1"][0]

    assert (job.status, job.error_code, job.attempts) == ("blocked", "execution_uncertain", 1)
    assert job.lease_owner is None and job.lease_until is None
    assert lease.phase == "finished"
    assert (reservation["state"], slot["state"], intent["state"]) == (
        "unknown", "unknown", "unknown",
    )
    assert (reservation["reserved_usd_micro"], reservation["reserved_allowance_micro"]) == (10, 20)
    assert gate["mode"] == "inhibited"
    assert (projection["intent_state"], projection["reservation_state"], projection["slot_state"]) == (
        "unknown", "unknown", "unknown",
    )
    assert projection["projection_revision"] == "2"
    assert binding_row["projection_revision"] == 2
    assert binding_row["projection_digest"] == projection_digest(projection)
    for field in ("scope_id", "call_id", "subject_id", "work_job_id", "attempt_id",
                  "slot_id", "purpose", "policy_revision", "policy_digest"):
        assert binding_row[field] == (int(original[field]) if field == "policy_revision"
                                      else original[field])
    assert db.verify_audit()["valid"]


@pytest.mark.parametrize("attempt_state", ["interrupted", "cancelled", "activation_cancelled"])
def test_hand_back_conservatively_holds_nonactive_shutdown_attempt(work_rows, attempt_state):
    db, _, _, binding = work_rows
    _set_hand_back_attempt_state(db, attempt_state)

    with owner_transaction(db) as session:
        with pytest.raises(_WorkProjectionUnavailable):
            _compare_actual_work_projection(session, scope_id="scope", call_id="call")
        if attempt_state == "interrupted":
            assert _compare_actual_work_projection(
                session, scope_id="scope", call_id="call", allow_interrupted_attempt=True,
            ) == "canonical_rows_agree"
        else:
            assert _compare_actual_work_projection(
                session, scope_id="scope", call_id="call", allow_preentry_cancelled_attempt=True,
            ) == "canonical_rows_agree"

    assert _hand_back_service(db).hand_back("job", fence="2" * 32) is True

    tables = Base.metadata.tables
    with db.sessions() as session:
        job = session.get(Job, "job")
        reservation = session.execute(select(tables["program_reservations"])).mappings().one()
        slot = session.execute(select(tables["program_slots"])).mappings().one()
        intent = session.execute(select(tables["program_call_intents"])).mappings().one()
        gate = session.execute(select(tables["program_gate_state"])).mappings().one()
        binding_row = session.execute(select(binding)).mappings().one()
        projection = job.resolved["repository_run"]["program_ledger_projections_v1"][0]

    assert (job.status, job.error_code, job.attempts) == ("blocked", "execution_uncertain", 1)
    assert job.resolved["repository_run"]["attempts"][-1]["state"] == attempt_state
    assert (reservation["state"], slot["state"], intent["state"]) == ("unknown", "unknown", "unknown")
    assert gate["mode"] == "inhibited"
    assert (projection["intent_state"], projection["reservation_state"], projection["slot_state"]) == (
        "unknown", "unknown", "unknown",
    )
    assert binding_row["projection_revision"] == 2
    assert db.verify_audit()["valid"]


@pytest.mark.parametrize("attempt_state", ["completed", "failed", "uncertain"])
def test_hand_back_does_not_admit_other_nonactive_attempt_states(work_rows, attempt_state):
    db, _, _, _ = work_rows
    _set_hand_back_attempt_state(db, attempt_state)
    tables = Base.metadata.tables
    with db.sessions() as session:
        job = session.get(Job, "job")
        before = (job.status, job.attempts, job.lease_owner, job.lease_until, job.resolved)
        audits = session.scalar(select(text("count(*)")).select_from(tables["audit_events"]))

    with pytest.raises(PlatformError) as caught:
        _hand_back_service(db).hand_back("job", fence="2" * 32)
    assert caught.value.code == "execution_owner_state_unavailable"

    with db.sessions() as session:
        job = session.get(Job, "job")
        assert (job.status, job.attempts, job.lease_owner, job.lease_until, job.resolved) == before
        assert session.scalar(select(text("count(*)")).select_from(tables["audit_events"])) == audits


@pytest.mark.parametrize("captured_fence", [None, "", "3" * 32])
def test_projected_hand_back_requires_explicit_captured_fence_without_mutation(work_rows, captured_fence):
    db, _, _, _ = work_rows
    service = _hand_back_service(db, captured_fence=captured_fence)
    tables = Base.metadata.tables

    with db.sessions() as session:
        before_job = session.get(Job, "job")
        before_job_state = (before_job.status, before_job.attempts, before_job.lease_owner,
                            before_job.lease_until, before_job.resolved)
        before_audits = session.scalar(select(text("count(*)")).select_from(tables["audit_events"]))
        before_gate = session.scalar(select(tables["program_gate_state"].c.mode))

    with pytest.raises(PlatformError) as caught:
        service.hand_back("job", fence=captured_fence)
    assert caught.value.code == "execution_fenced"
    assert caught.value.status == 409

    with db.sessions() as session:
        after_job = session.get(Job, "job")
        assert (after_job.status, after_job.attempts, after_job.lease_owner,
                after_job.lease_until, after_job.resolved) == before_job_state
        assert session.scalar(select(tables["worker_execution_leases"].c.phase).where(
            tables["worker_execution_leases"].c.job_id == "job")) == "started"
        assert session.scalar(select(text("count(*)")).select_from(tables["audit_events"])) == before_audits
        assert session.scalar(select(tables["program_gate_state"].c.mode)) == before_gate


def test_missing_projection_array_with_relational_rows_refuses_legacy_hand_back(work_rows):
    db, _, _, _ = work_rows
    service = _hand_back_service(db)
    raw = json.dumps({"repository_run": {
        "schema_version": RUN_SCHEMA,
        "attempts": [{"id": "job-1", "state": "running",
                      "fence_sha256": hashlib.sha256(("2" * 32).encode()).hexdigest()}],
    }}, separators=(",", ":"))
    with db.engine.begin() as connection:
        connection.execute(_job_resolved_update(db), {"resolved": raw, "job_id": "job"})
    tables = Base.metadata.tables
    with db.sessions() as session:
        before_job = session.get(Job, "job")
        before_job_state = (before_job.status, before_job.attempts, before_job.lease_owner,
                            before_job.lease_until, before_job.resolved)
        before_audits = session.scalar(select(text("count(*)")).select_from(tables["audit_events"]))

    with pytest.raises(PlatformError) as caught:
        service.hand_back("job", fence="2" * 32)
    assert caught.value.code == "execution_owner_state_unavailable"
    assert caught.value.status == 503

    with db.sessions() as session:
        after_job = session.get(Job, "job")
        assert (after_job.status, after_job.attempts, after_job.lease_owner,
                after_job.lease_until, after_job.resolved) == before_job_state
        assert session.scalar(select(tables["worker_execution_leases"].c.phase).where(
            tables["worker_execution_leases"].c.job_id == "job")) == "started"
        assert session.scalar(select(text("count(*)")).select_from(tables["audit_events"])) == before_audits


def test_work_intent_attributed_by_subject_cannot_disappear_from_empty_proof(work_rows):
    db, _, _, _ = work_rows
    if db.engine.dialect.name != "sqlite":
        pytest.skip("the PostgreSQL ledger_intent_context CHECK forbids subject/work_job_id asymmetry")
    tables = Base.metadata.tables
    raw = json.dumps({
        "harness": {"kind": "tool-agent", "idempotent": True},
        "tools": [],
    }, separators=(",", ":"))
    connection = db.engine.raw_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.execute("PRAGMA ignore_check_constraints=ON")
        cursor.execute("DELETE FROM program_work_projection_bindings WHERE work_job_id = 'job'")
        cursor.execute("DELETE FROM program_slots WHERE subject_id = 'job'")
        cursor.execute("DELETE FROM program_reservations WHERE subject_id = 'job'")
        cursor.execute("UPDATE program_call_intents SET work_job_id = 'other-job' WHERE subject_id = 'job'")
        cursor.execute("UPDATE jobs SET resolved = ? WHERE id = 'job'", (raw,))
        connection.commit()
        cursor.execute("PRAGMA ignore_check_constraints=OFF")
        cursor.execute("PRAGMA foreign_keys=ON")
        connection.commit()
    finally:
        connection.close()
    service = _hand_back_service(db)

    with db.sessions() as session:
        before_job = session.get(Job, "job")
        before_job_state = (before_job.status, before_job.attempts, before_job.lease_owner,
                            before_job.lease_until, before_job.resolved)
        before_audits = session.scalar(select(text("count(*)")).select_from(tables["audit_events"]))

    with pytest.raises(PlatformError) as caught:
        service.hand_back("job", fence="2" * 32)
    assert caught.value.code == "execution_owner_state_unavailable"
    assert caught.value.status == 503

    with db.sessions() as session:
        after_job = session.get(Job, "job")
        assert (after_job.status, after_job.attempts, after_job.lease_owner,
                after_job.lease_until, after_job.resolved) == before_job_state
        assert session.scalar(select(tables["worker_execution_leases"].c.phase).where(
            tables["worker_execution_leases"].c.job_id == "job")) == "started"
        assert session.scalar(select(text("count(*)")).select_from(tables["audit_events"])) == before_audits


def test_postgresql_rejects_subject_work_job_asymmetry_by_schema_constraint(work_rows):
    db, _, _, _ = work_rows
    if db.engine.dialect.name != "postgresql":
        pytest.skip("ledger_intent_context enforcement evidence requires PostgreSQL")
    from sqlalchemy.exc import IntegrityError
    tables = Base.metadata.tables
    with db.sessions.begin() as session:
        session.add(Job(
            id="other-job", receipt="other-receipt", idempotency_key="other-idempotency",
            request_digest="f" * 64, caller="fixture", grant_id="fixture",
            generation="sha256:" + "3" * 64, resolved={}, task="synthetic",
            status="queued", attempts=0,
        ))
        session.flush()
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.execute(update(tables["program_call_intents"]).where(
                    tables["program_call_intents"].c.call_id == "call").values(work_job_id="other-job"))


def test_account_only_work_subject_does_not_disable_legacy_empty_hand_back(work_rows):
    db, _, raw, binding = work_rows
    tables = Base.metadata.tables
    with db.sessions.begin() as session:
        session.execute(delete(binding).where(binding.c.work_job_id == "job"))
        session.execute(delete(tables["program_call_intents"]).where(
            tables["program_call_intents"].c.work_job_id == "job"))
        session.execute(delete(tables["program_slots"]).where(
            tables["program_slots"].c.subject_id == "job"))
        session.execute(delete(tables["program_reservations"]).where(
            tables["program_reservations"].c.subject_id == "job"))
        session.execute(_job_resolved_update(db), {"resolved": json.dumps({
            "repository_run": {"schema_version": RUN_SCHEMA, "attempts": [
                {"id": "job-1", "state": "running",
                 "fence_sha256": hashlib.sha256(("2" * 32).encode()).hexdigest()},
            ]},
        }, separators=(",", ":")), "job_id": "job"})
    service = _hand_back_service(db)

    assert service.hand_back("job", fence="2" * 32) is True

    with db.sessions() as session:
        job = session.get(Job, "job")
    assert (job.status, job.error_code, job.attempts) == ("blocked", "execution_uncertain", 1)


def _seed_retained_legacy_history(db, *, target_call="legacy-target", target_slot="legacy-target-slot",
                                  include_current_projection=True, include_target_intent=False,
                                  link_job_id="job", gate_cutover=False):
    tables = Base.metadata.tables
    baseline = dict(
        scope_id="scope", batch_id="history-batch", link_id="history-link",
        job_id=link_job_id, original_attempt_id="job-0", original_call_id="source-call",
        original_slot_id="source-slot", subject_id="job", call_id=target_call,
        slot_id=target_slot, baseline_state="reserved", baseline_slot_state="held",
        baseline_reserved_usd_micro=4, baseline_reserved_allowance_micro=5,
        baseline_actual_usd_micro=None, baseline_actual_allowance_micro=None,
    )
    snapshot = digest([baseline])
    with db.sessions() as session:
        current_projection = session.execute(select(tables["jobs"].c.resolved).where(
            tables["jobs"].c.id == "job")).scalar_one()["repository_run"]["program_ledger_projections_v1"][0]
    resolved = {
        "repository_run": {
            "schema_version": RUN_SCHEMA,
            "attempts": [
                {"id": "job-0", "state": "completed"},
                {"id": "job-1", "state": "running",
                 "fence_sha256": hashlib.sha256(("2" * 32).encode()).hexdigest()},
            ],
            "budget_reservations": [{"call_id": "source-call", "attempt_id": "job-0",
                                     "slot_id": "source-slot", "state": "settled"}],
            "active_slots": ["source-slot"],
            "program_ledger_projections_v1": [current_projection] if include_current_projection else [],
        },
    }
    with db.sessions.begin() as session:
        if link_job_id != "job":
            session.add(Job(
                id=link_job_id, receipt="other-receipt", idempotency_key="other-idempotency",
                request_digest="f" * 64, caller="fixture", grant_id="fixture",
                generation="sha256:" + "3" * 64, resolved={}, task="synthetic",
                status="queued", attempts=0,
            ))
            session.flush()
        if not include_current_projection:
            session.execute(delete(tables["program_work_projection_bindings"]).where(
                tables["program_work_projection_bindings"].c.call_id == "call"))
            session.execute(delete(tables["program_call_intents"]).where(
                tables["program_call_intents"].c.call_id == "call"))
            session.execute(delete(tables["program_slots"]).where(tables["program_slots"].c.call_id == "call"))
            session.execute(delete(tables["program_reservations"]).where(
                tables["program_reservations"].c.call_id == "call"))
        session.execute(_job_resolved_update(db), {"resolved": json.dumps(resolved, separators=(",", ":")), "job_id": "job"})
        session.execute(insert(tables["program_census_batches"]), dict(
            scope_id="scope", batch_id="history-batch", policy_revision=1,
            policy_digest="c" * 64, census_generation="a" * 32,
            snapshot_digest=snapshot, fence_digest="a" * 64, owner_epoch=1,
            state="complete", source_count=1, mapped_count=1, unmatched_count=0,
            reserved_usd_micro=4, settled_usd_micro=0, unknown_usd_micro=0,
            reserved_allowance_micro=5, settled_allowance_micro=0,
            unknown_allowance_micro=0, held_slots=1, unknown_slots=0, audit_sequence=1,
        ))
        if gate_cutover:
            session.execute(update(tables["program_gate_state"]).where(
                tables["program_gate_state"].c.scope_id == "scope").values(
                    mode="active", cutover_batch_id="history-batch", cutover_digest=snapshot,
                    cutover_generation="a" * 32, cutover_fence_digest="a" * 64,
                    cutover_owner_epoch=1,
                ))
        if target_call != "call":
            session.execute(insert(tables["program_reservations"]), dict(
                scope_id="scope", call_id=target_call, subject_id="job", purpose="work",
                effect_key="legacy-effect", reserved_usd_micro=4, reserved_allowance_micro=5,
                permit_digest="f" * 64, meter_digest="e" * 64, price_digest="d" * 64,
                permit_expires_ms=1500, state="reserved", audit_sequence=1,
            ))
            session.execute(insert(tables["program_slots"]), dict(
                scope_id="scope", slot_id=target_slot, subject_id="job", call_id=target_call,
                state="held", audit_sequence=1,
            ))
        if include_target_intent:
            source_intent = session.execute(select(tables["program_call_intents"]).where(
                tables["program_call_intents"].c.call_id == "call")).mappings().one()
            intent = dict(source_intent)
            intent.update(call_id=target_call, effect_key="legacy-effect", idempotency_key="legacy-idempotency",
                          work_attempt_id="job-0", work_lease_digest="b" * 64)
            session.execute(insert(tables["program_call_intents"]), intent)
        session.execute(insert(tables["program_legacy_links"]), dict(
            **baseline, original_digest=digest(baseline), audit_sequence=1,
        ))
    return baseline


def test_hand_back_preserves_valid_prior_legacy_link_and_target(work_rows):
    db, _, _, _ = work_rows
    baseline = _seed_retained_legacy_history(db, include_target_intent=True, gate_cutover=True)
    tables = Base.metadata.tables
    with db.sessions() as session:
        before_link = session.execute(select(tables["program_legacy_links"])).mappings().one()
        before_target = session.execute(select(tables["program_reservations"]).where(
            tables["program_reservations"].c.call_id == "legacy-target")).mappings().one()
        before_slot = session.execute(select(tables["program_slots"]).where(
            tables["program_slots"].c.slot_id == "legacy-target-slot")).mappings().one()

    assert _hand_back_service(db).hand_back("job", fence="2" * 32) is True

    with db.sessions() as session:
        after_link = session.execute(select(tables["program_legacy_links"])).mappings().one()
        after_target = session.execute(select(tables["program_reservations"]).where(
            tables["program_reservations"].c.call_id == "legacy-target")).mappings().one()
        after_slot = session.execute(select(tables["program_slots"]).where(
            tables["program_slots"].c.slot_id == "legacy-target-slot")).mappings().one()
        job = session.get(Job, "job")
    assert dict(after_link) == dict(before_link)
    assert dict(after_target) == dict(before_target)
    assert dict(after_slot) == dict(before_slot)
    assert after_link["original_digest"] == digest(baseline)
    assert job.attempts == 1


def test_hand_back_history_only_path_locks_and_keeps_attempt_monotonic(work_rows):
    db, _, _, _ = work_rows
    _seed_retained_legacy_history(db, include_current_projection=False)
    assert _hand_back_service(db).hand_back("job", fence="2" * 32) is True
    with db.sessions() as session:
        job = session.get(Job, "job")
        target = session.execute(select(Base.metadata.tables["program_reservations"]).where(
            Base.metadata.tables["program_reservations"].c.call_id == "legacy-target")).mappings().one()
    assert (job.status, job.attempts) == ("blocked", 1)
    assert target["state"] == "reserved"


def test_hand_back_rejects_historical_target_overlapping_current_projection(work_rows):
    db, _, _, _ = work_rows
    _seed_retained_legacy_history(db, target_call="call", target_slot="slot")
    before = None
    with db.sessions() as session:
        job = session.get(Job, "job")
        before = (job.status, job.attempts, job.lease_owner, job.lease_until, job.resolved)
    with pytest.raises(PlatformError) as caught:
        _hand_back_service(db).hand_back("job", fence="2" * 32)
    assert caught.value.code == "execution_owner_state_unavailable"
    with db.sessions() as session:
        job = session.get(Job, "job")
        assert (job.status, job.attempts, job.lease_owner, job.lease_until, job.resolved) == before


def test_hand_back_rejects_corrupt_historical_batch_snapshot_without_mutation(work_rows):
    db, _, _, _ = work_rows
    _seed_retained_legacy_history(db)
    tables = Base.metadata.tables
    with db.sessions.begin() as session:
        session.execute(update(tables["program_census_batches"]).where(
            tables["program_census_batches"].c.batch_id == "history-batch").values(snapshot_digest="9" * 64))
    with db.sessions() as session:
        job = session.get(Job, "job")
        before = (job.status, job.attempts, job.lease_owner, job.lease_until, job.resolved)
        audits = session.scalar(select(text("count(*)")).select_from(tables["audit_events"]))

    with pytest.raises(PlatformError) as caught:
        _hand_back_service(db).hand_back("job", fence="2" * 32)
    assert caught.value.code == "execution_owner_state_unavailable"

    with db.sessions() as session:
        job = session.get(Job, "job")
        assert (job.status, job.attempts, job.lease_owner, job.lease_until, job.resolved) == before
        assert session.scalar(select(tables["worker_execution_leases"].c.phase)) == "started"
        assert session.scalar(select(text("count(*)")).select_from(tables["audit_events"])) == audits


def test_hand_back_finds_legacy_link_selected_by_subject_even_with_wrong_job(work_rows):
    db, _, _, _ = work_rows
    _seed_retained_legacy_history(db, link_job_id="other-job")
    with db.sessions() as session:
        job = session.get(Job, "job")
        before = (job.status, job.attempts, job.lease_owner, job.lease_until, job.resolved)
        audits = session.scalar(select(text("count(*)")).select_from(Base.metadata.tables["audit_events"]))
    with pytest.raises(PlatformError) as caught:
        _hand_back_service(db).hand_back("job", fence="2" * 32)
    assert caught.value.code == "execution_owner_state_unavailable"
    with db.sessions() as session:
        job = session.get(Job, "job")
        assert (job.status, job.attempts, job.lease_owner, job.lease_until, job.resolved) == before
        assert session.scalar(select(text("count(*)")).select_from(Base.metadata.tables["audit_events"])) == audits


def test_hand_back_refuses_active_gate_without_cutover_in_history_only_branch(work_rows):
    db, _, _, _ = work_rows
    if db.engine.dialect.name != "sqlite":
        pytest.skip("the PostgreSQL ledger_gate_active_batch CHECK forbids this stored row")
    _seed_retained_legacy_history(db, include_current_projection=False)
    connection = db.engine.raw_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("PRAGMA ignore_check_constraints=ON")
        cursor.execute("UPDATE program_gate_state SET mode = 'active' WHERE scope_id = 'scope'")
        connection.commit()
        cursor.execute("PRAGMA ignore_check_constraints=OFF")
        connection.commit()
    finally:
        connection.close()
    tables = Base.metadata.tables
    with db.sessions() as session:
        job = session.get(Job, "job")
        before = (job.status, job.attempts, job.lease_owner, job.lease_until, job.resolved)
        audits = session.scalar(select(text("count(*)")).select_from(tables["audit_events"]))
    with pytest.raises(PlatformError) as caught:
        _hand_back_service(db).hand_back("job", fence="2" * 32)
    assert caught.value.code == "execution_owner_state_unavailable"
    with db.sessions() as session:
        job = session.get(Job, "job")
        assert (job.status, job.attempts, job.lease_owner, job.lease_until, job.resolved) == before
        assert session.scalar(select(text("count(*)")).select_from(tables["audit_events"])) == audits


def test_hand_back_refuses_gate_policy_digest_drift_in_history_only_branch(work_rows):
    db, _, _, _ = work_rows
    if db.engine.dialect.name != "sqlite":
        pytest.skip("the PostgreSQL ledger_gate_policy_fk forbids this stored row")
    _seed_retained_legacy_history(db, include_current_projection=False)
    connection = db.engine.raw_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.execute("UPDATE program_gate_state SET policy_digest = ? WHERE scope_id = 'scope'", ("b" * 64,))
        connection.commit()
        cursor.execute("PRAGMA foreign_keys=ON")
        connection.commit()
    finally:
        connection.close()
    tables = Base.metadata.tables
    with db.sessions() as session:
        job = session.get(Job, "job")
        before = (job.status, job.attempts, job.lease_owner, job.lease_until, job.resolved)
        audits = session.scalar(select(text("count(*)")).select_from(tables["audit_events"]))
    with pytest.raises(PlatformError) as caught:
        _hand_back_service(db).hand_back("job", fence="2" * 32)
    assert caught.value.code == "execution_owner_state_unavailable"
    with db.sessions() as session:
        job = session.get(Job, "job")
        assert (job.status, job.attempts, job.lease_owner, job.lease_until, job.resolved) == before
        assert session.scalar(select(text("count(*)")).select_from(tables["audit_events"])) == audits


def test_hand_back_refuses_gate_audit_sequence_above_canonical_integer_bound(work_rows):
    db, _, _, _ = work_rows
    if db.engine.dialect.name != "sqlite":
        pytest.skip("PostgreSQL Integer enforces the canonical audit sequence bound")
    _seed_retained_legacy_history(db, include_current_projection=False)
    tables = Base.metadata.tables
    oversized = (1 << 31)
    with db.engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO audit_events "
            "SELECT :oversized, created_at, actor, action, target, detail, previous_hash, hash "
            "FROM audit_events WHERE sequence = (SELECT audit_sequence FROM program_gate_state "
            "WHERE scope_id = 'scope')"), {"oversized": oversized})
        connection.execute(text(
            "UPDATE program_gate_state SET audit_sequence = :oversized WHERE scope_id = 'scope'"),
            {"oversized": oversized})
    with db.sessions() as session:
        job = session.get(Job, "job")
        before = (job.status, job.attempts, job.lease_owner, job.lease_until, job.resolved)
        audits = session.scalar(select(text("count(*)")).select_from(tables["audit_events"]))
    with pytest.raises(PlatformError) as caught:
        _hand_back_service(db).hand_back("job", fence="2" * 32)
    assert caught.value.code == "execution_owner_state_unavailable"
    with db.sessions() as session:
        job = session.get(Job, "job")
        assert (job.status, job.attempts, job.lease_owner, job.lease_until, job.resolved) == before
        assert session.scalar(select(text("count(*)")).select_from(tables["audit_events"])) == audits


def test_hand_back_rolls_back_all_ledger_and_owner_changes_after_projection_failure(work_rows, monkeypatch):
    db, _, _, binding = work_rows
    from sovite import program_ledger
    original = program_ledger._mark_work_projection_unknown_in_transaction

    def fail_after_work_write(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("synthetic post-write failure")

    monkeypatch.setattr(program_ledger, "_mark_work_projection_unknown_in_transaction", fail_after_work_write)
    with db.sessions() as session:
        job = session.get(Job, "job")
        before_job = (job.status, job.attempts, job.lease_owner, job.lease_until, job.resolved)
        before_audits = session.scalar(select(text("count(*)")).select_from(Base.metadata.tables["audit_events"]))

    with pytest.raises(PlatformError) as caught:
        _hand_back_service(db).hand_back("job", fence="2" * 32)
    assert caught.value.code == "execution_owner_state_unavailable"

    with db.sessions() as session:
        job = session.get(Job, "job")
        assert (job.status, job.attempts, job.lease_owner, job.lease_until, job.resolved) == before_job
        assert session.scalar(select(Base.metadata.tables["worker_execution_leases"].c.phase)) == "started"
        assert session.scalar(select(Base.metadata.tables["program_reservations"].c.state)) == "reserved"
        assert session.scalar(select(Base.metadata.tables["program_slots"].c.state)) == "held"
        assert session.scalar(select(Base.metadata.tables["program_call_intents"].c.state)) == "committed"
        assert session.scalar(select(Base.metadata.tables["program_gate_state"].c.mode)) == "disabled"
        assert session.scalar(select(binding.c.projection_revision)) == 1
        assert session.scalar(select(text("count(*)")).select_from(Base.metadata.tables["audit_events"])) == before_audits


def _seed_hand_back_known_receipt(work_rows, *, actual_usd, actual_allowance):
    db, _, _, binding = work_rows
    tables = Base.metadata.tables
    source_identity = digest({"source_code": "fixture", "issuer_code": "fixture"})
    receipt_body = dict(
        scope_id="scope", call_id="call", receipt_id="receipt", actual_usd=actual_usd,
        actual_allowance=actual_allowance, currency="USD", allowance_unit="opencode-go-allowance",
        source_code="fixture", issuer_code="fixture", source_digest="b" * 64,
        source_identity_digest=source_identity, custody_ref="fixture-object", custody_bytes=10,
        observed_ms=300,
    )
    args = {**receipt_body, "receipt_digest": digest(receipt_body)}
    from sovite.program_ledger_core import _record_known_in_transaction
    with owner_transaction(db) as session:
        result, _ = _record_known_in_transaction(db, session, tables, **args)
        assert result == ("known_unresolved_unverified" if int(actual_usd) > 10
                          or int(actual_allowance) > 20 else "known_settled_unverified")
    with db.sessions.begin() as session:
        job = session.get(Job, "job")
        resolved = dict(job.resolved)
        run = dict(resolved["repository_run"])
        entries = [dict(item) for item in run["program_ledger_projections_v1"]]
        payload = entries[0]
        state = "unknown" if int(actual_usd) > 10 or int(actual_allowance) > 20 else "known"
        payload.update(
            receipt_id="receipt", receipt_digest=args["receipt_digest"],
            actual_usd_micro=actual_usd, actual_allowance_micro=actual_allowance,
            intent_state=state, reservation_state="unknown" if state == "unknown" else "settled",
            slot_state="unknown" if state == "unknown" else "released",
        )
        sequence = session.execute(select(tables["program_call_intents"].c.audit_sequence).where(
            tables["program_call_intents"].c.call_id == "call")).scalar_one()
        payload["audit_sequence"] = str(sequence)
        run["program_ledger_projections_v1"] = entries
        resolved["repository_run"] = run
        job.resolved = resolved
        session.execute(update(binding).where(binding.c.call_id == "call").values(
            projection_digest=projection_digest(payload), audit_sequence=sequence))
    return args, binding


def test_hand_back_preserves_known_settled_receipt_and_projection(work_rows):
    db, _, _, _ = work_rows
    _seed_hand_back_known_receipt(work_rows, actual_usd="0", actual_allowance="0")
    tables = Base.metadata.tables
    with db.sessions() as session:
        before_receipt = dict(session.execute(select(tables["program_call_receipts"])).mappings().one())
        before_reservation = dict(session.execute(select(tables["program_reservations"])).mappings().one())
        before_slot = dict(session.execute(select(tables["program_slots"])).mappings().one())
        before_intent = dict(session.execute(select(tables["program_call_intents"])).mappings().one())
        before_projection = session.get(Job, "job").resolved["repository_run"]["program_ledger_projections_v1"][0]

    assert _hand_back_service(db).hand_back("job", fence="2" * 32) is True

    with db.sessions() as session:
        assert dict(session.execute(select(tables["program_call_receipts"])).mappings().one()) == before_receipt
        assert dict(session.execute(select(tables["program_reservations"])).mappings().one()) == before_reservation
        assert dict(session.execute(select(tables["program_slots"])).mappings().one()) == before_slot
        assert dict(session.execute(select(tables["program_call_intents"])).mappings().one()) == before_intent
        job = session.get(Job, "job")
        assert job.resolved["repository_run"]["program_ledger_projections_v1"][0] == before_projection
        assert job.attempts == 1


def test_hand_back_preserves_known_receipt_while_unsettled_capacity_stays_held(work_rows):
    db, _, _, _ = work_rows
    _seed_hand_back_known_receipt(work_rows, actual_usd="11", actual_allowance="1")
    tables = Base.metadata.tables
    with db.sessions() as session:
        before_receipt = dict(session.execute(select(tables["program_call_receipts"])).mappings().one())
        before_projection = session.get(Job, "job").resolved["repository_run"]["program_ledger_projections_v1"][0]

    assert _hand_back_service(db).hand_back("job", fence="2" * 32) is True

    with db.sessions() as session:
        receipt = dict(session.execute(select(tables["program_call_receipts"])).mappings().one())
        reservation = session.execute(select(tables["program_reservations"])).mappings().one()
        slot = session.execute(select(tables["program_slots"])).mappings().one()
        intent = session.execute(select(tables["program_call_intents"])).mappings().one()
        gate = session.execute(select(tables["program_gate_state"])).mappings().one()
        job = session.get(Job, "job")
    assert receipt == before_receipt
    assert (reservation["state"], reservation["actual_usd_micro"], reservation["actual_allowance_micro"]) == (
        "unknown", 11, 1,
    )
    assert (slot["state"], intent["state"], gate["mode"]) == ("unknown", "unknown", "inhibited")
    assert job.resolved["repository_run"]["program_ledger_projections_v1"][0] == before_projection
    assert (job.status, job.attempts) == ("blocked", 1)


def test_hand_back_waits_for_locked_reservation_before_mutating_owner(work_rows):
    db, _, _, _ = work_rows
    if db.engine.dialect.name != "postgresql":
        pytest.skip("row-lock contention evidence requires the owned PostgreSQL fixture")
    from sqlalchemy import event
    reached = threading.Event()
    finished = threading.Event()
    result = []

    def observe_reservation_lock(conn, cursor, statement, parameters, context, executemany):
        if (threading.current_thread().name == "hand-back-lock-contention"
                and "program_reservations" in statement.lower()
                and "for update" in statement.lower()):
            reached.set()

    event.listen(db.engine, "before_cursor_execute", observe_reservation_lock)
    try:
        with db.sessions.begin() as locker:
            locker.execute(select(Base.metadata.tables["program_reservations"]).where(
                Base.metadata.tables["program_reservations"].c.call_id == "call").with_for_update()).first()

            def hand_back():
                try:
                    result.append(_hand_back_service(db).hand_back("job", fence="2" * 32))
                except Exception as error:  # surfaced below as assertion data
                    result.append(error)
                finally:
                    finished.set()

            worker = threading.Thread(target=hand_back, daemon=True, name="hand-back-lock-contention")
            worker.start()
            assert reached.wait(5), "hand_back never reached its reservation lock"
            assert not finished.wait(0.2), "hand_back passed a reservation row held by another transaction"
        worker.join(5)
        assert not worker.is_alive()
        assert result == [True]
    finally:
        event.remove(db.engine, "before_cursor_execute", observe_reservation_lock)


def test_empty_hand_back_rescans_work_membership_after_job_lock(work_rows):
    db, _, _, binding = work_rows
    if db.engine.dialect.name != "postgresql":
        pytest.skip("Job-lock interleaving evidence requires the owned PostgreSQL fixture")
    tables = Base.metadata.tables
    with db.sessions.begin() as session:
        session.execute(delete(binding).where(binding.c.work_job_id == "job"))
        session.execute(delete(tables["program_call_intents"]).where(tables["program_call_intents"].c.work_job_id == "job"))
        session.execute(delete(tables["program_slots"]).where(tables["program_slots"].c.subject_id == "job"))
        session.execute(delete(tables["program_reservations"]).where(tables["program_reservations"].c.subject_id == "job"))
        session.execute(_job_resolved_update(db), {"resolved": json.dumps({
            "repository_run": {"schema_version": RUN_SCHEMA, "attempts": [
                {"id": "job-1", "state": "running",
                 "fence_sha256": hashlib.sha256(("2" * 32).encode()).hexdigest()},
            ]},
        }, separators=(",", ":")), "job_id": "job"})

    from sqlalchemy import event
    reached_job_lock = threading.Event()
    finished = threading.Event()
    result = []

    def observe_job_lock(conn, cursor, statement, parameters, context, executemany):
        if (threading.current_thread().name == "hand-back-empty-membership"
                and "for update" in statement.lower()):
            reached_job_lock.set()

    event.listen(db.engine, "before_cursor_execute", observe_job_lock)
    locker = db.sessions()
    locker.begin()
    try:
        locker.execute(select(Job).where(Job.id == "job").with_for_update()).first()

        def hand_back():
            try:
                result.append(_hand_back_service(db).hand_back("job", fence="2" * 32))
            except Exception as error:  # surfaced below as assertion data
                result.append(error)
            finally:
                finished.set()

        worker = threading.Thread(target=hand_back, daemon=True, name="hand-back-empty-membership")
        worker.start()
        assert reached_job_lock.wait(5), "empty hand_back never reached the Job lock"
        assert not finished.wait(0.2), "empty hand_back passed a Job row held by another transaction"
        locker.execute(insert(tables["program_reservations"]), dict(
            scope_id="scope", call_id="late-call", subject_id="job", purpose="work",
            effect_key="late-effect", reserved_usd_micro=1, reserved_allowance_micro=1,
            permit_digest="f" * 64, meter_digest="e" * 64, price_digest="d" * 64,
            permit_expires_ms=1500, state="reserved", audit_sequence=1,
        ))
        locker.execute(insert(tables["program_slots"]), dict(
            scope_id="scope", slot_id="late-slot", subject_id="job", call_id="late-call",
            state="held", audit_sequence=1,
        ))
        locker.commit()
        worker.join(5)
        assert not worker.is_alive()
        assert len(result) == 1 and isinstance(result[0], PlatformError)
        assert result[0].code == "execution_owner_state_unavailable"
    finally:
        if locker.in_transaction():
            locker.rollback()
        locker.close()
        event.remove(db.engine, "before_cursor_execute", observe_job_lock)

    with db.sessions() as session:
        job = session.get(Job, "job")
        assert (job.status, job.attempts) == ("running", 1)
        assert session.scalar(select(tables["program_reservations"].c.state).where(
            tables["program_reservations"].c.call_id == "late-call")) == "reserved"


@pytest.mark.parametrize("change", ["attempt", "expired"])
def test_uncertain_attempt_refuses_stale_job_or_expired_live_lease(work_rows, tmp_path, change):
    db, _, _, _ = work_rows
    executor = RepositoryRunAttemptExecutor(
        type("Platform", (), {"db": db})(), object(), tmp_path, lambda *args, **kwargs: None
    )
    tables = Base.metadata.tables
    with db.sessions.begin() as session:
        if change == "attempt":
            session.execute(update(Job).where(Job.id == "job").values(attempts=2))
        else:
            session.execute(update(Job).where(Job.id == "job").values(lease_until=1.0))
            session.execute(update(tables["worker_execution_leases"]).where(
                tables["worker_execution_leases"].c.job_id == "job").values(
                    lease_until=1.0, execution_deadline=1.0))

    with pytest.raises(Exception) as exc_info:
        executor._persist("job", "2" * 32,
                          lambda run: run["attempts"][-1].update(state="uncertain"),
                          uncertain_attempt_id="job-1")
    assert getattr(exc_info.value, "code", None) == "execution_fenced"
    with db.sessions() as session:
        assert session.scalar(select(tables["program_reservations"].c.state)) == "reserved"
        assert session.scalar(select(tables["program_call_intents"].c.state)) == "committed"
        assert session.scalar(select(tables["program_gate_state"].c.mode)) == "disabled"


def test_uncertain_no_projection_path_stays_job_only(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'no-projection.db'}")
    db.migrate()
    executor = RepositoryRunAttemptExecutor(
        type("Platform", (), {"db": db})(), object(), tmp_path, lambda *args, **kwargs: None
    )
    now = time.time()
    fence = "3" * 32
    fence_hash = hashlib.sha256(fence.encode()).hexdigest()
    resolved = {"repository_run": {
        "schema_version": RUN_SCHEMA,
        "attempts": [{"id": "job-1", "state": "running", "fence_sha256": fence_hash}],
        "program_ledger_projections_v1": [],
    }}
    with db.sessions.begin() as session:
        db.audit(session, "synthetic-test", "seed", "job")
        session.add(Job(
            id="job", receipt="job", idempotency_key="job", request_digest="b" * 64,
            caller="fixture", grant_id="fixture", generation="sha256:" + "1" * 64,
            resolved=resolved, task="synthetic", status="running",
            lease_owner=execution_owner_key(workload_id="workload", instance_id="instance"),
            lease_until=now + 300, attempts=1,
        ))
        session.execute(insert(Base.metadata.tables["worker_execution_leases"]).values(
            job_id="job", workload_id="workload", instance_id="instance", fence=fence,
            generation="sha256:" + "1" * 64, phase="started", lease_until=now + 300,
            execution_deadline=now + 300, created_at=now, updated_at=now,
        ))

    executor._persist("job", fence,
                      lambda run: run["attempts"][-1].update(state="uncertain"),
                      uncertain_attempt_id="job-1")
    with db.sessions() as session:
        assert session.get(Job, "job").resolved[
            "repository_run"]["attempts"][-1]["state"] == "uncertain"


def test_uncertain_cancel_requested_still_records_unknown_hold(work_rows, tmp_path):
    db, _, _, _ = work_rows
    with db.sessions.begin() as session:
        session.execute(update(Job).where(Job.id == "job").values(cancel_requested=True))
    executor = RepositoryRunAttemptExecutor(
        type("Platform", (), {"db": db})(), object(), tmp_path, lambda *args, **kwargs: None
    )
    executor._persist("job", "2" * 32,
                      lambda run: run["attempts"][-1].update(state="uncertain"),
                      uncertain_attempt_id="job-1")
    with db.sessions() as session:
        assert session.scalar(select(Base.metadata.tables["program_reservations"].c.state)) == "unknown"
        assert session.scalar(select(Job.cancel_requested).where(Job.id == "job")) is True


def test_uncertain_known_settled_projection_and_receipt_remain_known(work_rows, tmp_path):
    db, _, _, _ = work_rows
    _settle_synthetic_call(work_rows)
    executor = RepositoryRunAttemptExecutor(
        type("Platform", (), {"db": db})(), object(), tmp_path, lambda *args, **kwargs: None
    )
    executor._persist("job", "2" * 32,
                      lambda run: run["attempts"][-1].update(state="uncertain"),
                      uncertain_attempt_id="job-1")
    tables = Base.metadata.tables
    with db.sessions() as session:
        assert session.scalar(select(tables["program_reservations"].c.state)) == "settled"
        assert session.scalar(select(tables["program_call_intents"].c.state)) == "known"
        assert session.scalar(select(tables["program_slots"].c.state)) == "released"
        assert session.scalar(select(tables["program_call_receipts"].c.receipt_id)) == "receipt"
        gate = session.execute(select(tables["program_gate_state"])).mappings().one()
        assert (gate["mode"], gate["revision"]) == ("inhibited", 1)


def test_uncertain_binding_mismatch_rolls_back_all_owner_writes(work_rows, tmp_path):
    db, _, _, binding = work_rows
    with db.sessions.begin() as session:
        session.execute(update(binding).where(binding.c.scope_id == "scope", binding.c.call_id == "call")
                        .values(projection_digest="0" * 64))
    executor = RepositoryRunAttemptExecutor(
        type("Platform", (), {"db": db})(), object(), tmp_path, lambda *args, **kwargs: None
    )
    with pytest.raises(Exception) as exc_info:
        executor._persist("job", "2" * 32,
                          lambda run: run["attempts"][-1].update(state="uncertain"),
                          uncertain_attempt_id="job-1")
    assert getattr(exc_info.value, "code", None) == "execution_projection_unavailable"
    tables = Base.metadata.tables
    with db.sessions() as session:
        assert session.scalar(select(tables["program_reservations"].c.state)) == "reserved"
        assert session.scalar(select(tables["program_gate_state"].c.mode)) == "disabled"
        assert session.get(Job, "job").resolved["repository_run"]["attempts"][-1]["state"] == "running"


def _add_second_projection_scope(work_rows, *, corrupt_binding=False):
    db, original, _, binding = work_rows
    tables = Base.metadata.tables
    second = dict(original, scope_id="scope-z", call_id="call-z", slot_id="slot-z",
                  effect_key="effect-z")
    with db.sessions.begin() as session:
        def copied(table_name, key_name, key_value, **changes):
            table = tables[table_name]
            row = dict(session.execute(select(table).where(getattr(table.c, key_name) == key_value))
                       .mappings().one())
            row.update(changes)
            session.execute(insert(table), row)

        second["scope_id"] = "scope"
        copied("program_reservations", "scope_id", "scope", call_id="call-z", effect_key="effect-z")
        copied("program_slots", "scope_id", "scope", slot_id="slot-z", call_id="call-z")
        copied("program_call_intents", "scope_id", "scope", call_id="call-z",
               effect_key="effect-z", idempotency_key="idempotency-z")
        copied("program_work_projection_bindings", "scope_id", "scope", call_id="call-z",
               slot_id="slot-z",
               projection_digest="0" * 64 if corrupt_binding else projection_digest(second))

        job = session.get(Job, "job")
        job_value = dict(job.resolved)
        run = dict(job_value["repository_run"])
        run["program_ledger_projections_v1"] = [original, second]
        job_value["repository_run"] = run
        job.resolved = job_value
    return db


def test_uncertain_prelocks_multiple_calls_and_updates_all_projections(work_rows, tmp_path):
    db, _, _, _ = work_rows
    _add_second_projection_scope(work_rows)
    executor = RepositoryRunAttemptExecutor(
        type("Platform", (), {"db": db})(), object(), tmp_path, lambda *args, **kwargs: None
    )
    executor._persist("job", "2" * 32,
                      lambda run: run["attempts"][-1].update(state="uncertain"),
                      uncertain_attempt_id="job-1")
    tables = Base.metadata.tables
    with db.sessions() as session:
        states = session.execute(select(tables["program_reservations"].c.scope_id,
                                        tables["program_reservations"].c.state)
                                 .order_by(tables["program_reservations"].c.scope_id)).all()
        job = session.get(Job, "job")
    assert states == [("scope", "unknown"), ("scope", "unknown")]
    assert [entry["reservation_state"] for entry in
            job.resolved["repository_run"]["program_ledger_projections_v1"]] == ["unknown", "unknown"]


def test_uncertain_second_call_mismatch_rolls_back_first_call(work_rows, tmp_path):
    db, _, _, _ = work_rows
    _add_second_projection_scope(work_rows, corrupt_binding=True)
    executor = RepositoryRunAttemptExecutor(
        type("Platform", (), {"db": db})(), object(), tmp_path, lambda *args, **kwargs: None
    )
    with pytest.raises(Exception) as exc_info:
        executor._persist("job", "2" * 32,
                          lambda run: run["attempts"][-1].update(state="uncertain"),
                          uncertain_attempt_id="job-1")
    assert getattr(exc_info.value, "code", None) == "execution_projection_unavailable"
    with db.sessions() as session:
        states = session.scalars(select(Base.metadata.tables["program_reservations"].c.state)
                                 .order_by(Base.metadata.tables["program_reservations"].c.scope_id)).all()
        assert states == ["reserved", "reserved"]


def test_already_unknown_inhibited_projection_is_idempotent(work_rows, tmp_path):
    db, original, _, binding = work_rows
    tables = Base.metadata.tables
    payload = dict(original, intent_state="unknown", reservation_state="unknown",
                   slot_state="unknown", projection_revision="2", audit_sequence="2")
    with db.sessions.begin() as session:
        event = db.audit(session, "synthetic-test", "mark-unknown", "call",
                         {"scope_id": "scope"})
        session.flush()
        assert event.sequence == 2
        session.execute(update(tables["program_reservations"]).where(
            tables["program_reservations"].c.call_id == "call").values(
                state="unknown", audit_sequence=event.sequence))
        session.execute(update(tables["program_slots"]).where(
            tables["program_slots"].c.call_id == "call").values(
                state="unknown", audit_sequence=event.sequence))
        session.execute(update(tables["program_call_intents"]).where(
            tables["program_call_intents"].c.call_id == "call").values(
                state="unknown", audit_sequence=event.sequence))
        session.execute(update(tables["program_gate_state"]).where(
            tables["program_gate_state"].c.scope_id == "scope").values(
                mode="inhibited", revision=1, audit_sequence=event.sequence))
        session.execute(update(binding).where(binding.c.call_id == "call").values(
            projection_revision=2, projection_digest=projection_digest(payload),
            audit_sequence=event.sequence))
        session.execute(_job_resolved_update(db), {
            "resolved": json.dumps({"repository_run": {
                "schema_version": RUN_SCHEMA,
                "attempts": [{"id": "job-1", "state": "running",
                              "fence_sha256": hashlib.sha256(("2" * 32).encode()).hexdigest()}],
                "program_ledger_projections_v1": [payload],
            }}, separators=(",", ":")),
            "job_id": "job",
        })

    executor = RepositoryRunAttemptExecutor(
        type("Platform", (), {"db": db})(), object(), tmp_path, lambda *args, **kwargs: None
    )
    executor._persist("job", "2" * 32,
                      lambda run: run["attempts"][-1].update(state="uncertain"),
                      uncertain_attempt_id="job-1")
    with db.sessions() as session:
        assert session.scalar(select(text("count(*)")).select_from(tables["audit_events"])) == 2
        assert session.scalar(select(tables["program_reservations"].c.state)) == "unknown"
        assert session.get(Job, "job").resolved["repository_run"]["attempts"][-1]["state"] == "uncertain"


def test_audit_verification_failure_rolls_back_uncertain_attempt(work_rows, tmp_path):
    db, _, _, _ = work_rows
    with db.sessions.begin() as session:
        session.execute(update(MetadataRow).where(MetadataRow.key == "audit_head").values(
            value={"sequence": 1, "hash": "0" * 64}))
    executor = RepositoryRunAttemptExecutor(
        type("Platform", (), {"db": db})(), object(), tmp_path, lambda *args, **kwargs: None
    )
    with pytest.raises(Exception) as exc_info:
        executor._persist("job", "2" * 32,
                          lambda run: run["attempts"][-1].update(state="uncertain"),
                          uncertain_attempt_id="job-1")
    assert getattr(exc_info.value, "code", None) == "execution_projection_unavailable"
    with db.sessions() as session:
        assert session.scalar(select(Base.metadata.tables["program_reservations"].c.state)) == "reserved"
        assert session.get(Job, "job").resolved["repository_run"]["attempts"][-1]["state"] == "running"


def test_projection_revision_exhaustion_rolls_back_unknown_transition(work_rows, tmp_path):
    db, original, _, binding = work_rows
    tables = Base.metadata.tables
    maximum = str((1 << 63) - 1)
    payload = dict(original, projection_revision=maximum)
    with db.sessions.begin() as session:
        session.execute(update(binding).where(binding.c.call_id == "call").values(
            projection_revision=int(maximum), projection_digest=projection_digest(payload)))
        session.execute(_job_resolved_update(db), {
            "resolved": json.dumps({"repository_run": {
                "schema_version": RUN_SCHEMA,
                "attempts": [{"id": "job-1", "state": "running",
                              "fence_sha256": hashlib.sha256(("2" * 32).encode()).hexdigest()}],
                "program_ledger_projections_v1": [payload],
            }}, separators=(",", ":")),
            "job_id": "job",
        })
    executor = RepositoryRunAttemptExecutor(
        type("Platform", (), {"db": db})(), object(), tmp_path, lambda *args, **kwargs: None
    )
    with pytest.raises(Exception) as exc_info:
        executor._persist("job", "2" * 32,
                          lambda run: run["attempts"][-1].update(state="uncertain"),
                          uncertain_attempt_id="job-1")
    assert getattr(exc_info.value, "code", None) == "execution_projection_unavailable"
    with db.sessions() as session:
        assert session.scalar(select(tables["program_reservations"].c.state)) == "reserved"
        assert session.scalar(select(text("count(*)")).select_from(tables["audit_events"])) == 1


@pytest.mark.parametrize("mismatch", ["json", "binding", "intent"])
def test_uncertain_refuses_asymmetric_projection_binding_intent_sets(work_rows, tmp_path, mismatch):
    db, _, _, binding = work_rows
    tables = Base.metadata.tables
    with db.sessions.begin() as session:
        if mismatch == "json":
            session.execute(_job_resolved_update(db), {
                "resolved": json.dumps({"repository_run": {
                    "schema_version": RUN_SCHEMA,
                    "attempts": [{"id": "job-1", "state": "running",
                                  "fence_sha256": hashlib.sha256(("2" * 32).encode()).hexdigest()}],
                    "program_ledger_projections_v1": [],
                }}, separators=(",", ":")), "job_id": "job",
            })
        elif mismatch == "binding":
            session.execute(binding.delete().where(binding.c.scope_id == "scope", binding.c.call_id == "call"))
        else:
            session.execute(update(tables["program_call_intents"]).where(
                tables["program_call_intents"].c.scope_id == "scope",
                tables["program_call_intents"].c.call_id == "call",
            ).values(work_attempt_id="job-2"))
    executor = RepositoryRunAttemptExecutor(
        type("Platform", (), {"db": db})(), object(), tmp_path, lambda *args, **kwargs: None
    )
    with pytest.raises(Exception) as exc_info:
        executor._persist("job", "2" * 32,
                          lambda run: run["attempts"][-1].update(state="uncertain"),
                          uncertain_attempt_id="job-1")
    assert getattr(exc_info.value, "code", None) == "execution_projection_unavailable"
    with db.sessions() as session:
        assert session.scalar(select(tables["program_reservations"].c.state)) == "reserved"
        assert session.scalar(select(tables["program_gate_state"].c.mode)) == "disabled"
        assert session.get(Job, "job").resolved["repository_run"]["attempts"][-1]["state"] == "running"


def _platform_for_projection_test(db):
    platform = object.__new__(Platform)
    platform.db = db
    return platform


@pytest.mark.parametrize("attempt_state", ["preparing", "awaiting_activation", "running"])
def test_cancel_possible_dispatch_inhibits_current_projection(work_rows, attempt_state):
    db, _, _, binding = work_rows
    with db.sessions.begin() as session:
        job = session.get(Job, "job")
        value = dict(job.resolved)
        run = dict(value["repository_run"])
        attempts = [dict(item) for item in run["attempts"]]
        attempts[-1]["state"] = attempt_state
        run["attempts"] = attempts
        value["repository_run"] = run
        job.resolved = value

    cancelled = _platform_for_projection_test(db).cancel("job", actor="operator")

    tables = Base.metadata.tables
    with db.sessions() as session:
        job = session.get(Job, "job")
        assert job.cancel_requested is True
        assert job.status == "running"
        assert job.resolved["repository_run"]["attempts"][-1]["state"] == attempt_state
        projection = job.resolved["repository_run"]["program_ledger_projections_v1"][0]
        assert (projection["intent_state"], projection["reservation_state"],
                projection["slot_state"]) == ("unknown", "unknown", "unknown")
        assert session.scalar(select(tables["program_reservations"].c.state)) == "unknown"
        assert session.scalar(select(tables["program_call_intents"].c.state)) == "unknown"
        assert session.scalar(select(tables["program_slots"].c.state)) == "unknown"
        gate = session.execute(select(tables["program_gate_state"])).mappings().one()
        assert (gate["mode"], gate["revision"]) == ("inhibited", 1)
        binding_row = session.execute(select(binding)).mappings().one()
        assert binding_row["projection_revision"] == 2
        assert binding_row["attempt_id"] == "job-1"
    assert cancelled.cancel_requested is True


def test_cancel_with_no_projection_preserves_running_job_behavior(work_rows):
    db, _, _, binding = work_rows
    tables = Base.metadata.tables
    with db.sessions.begin() as session:
        session.execute(binding.delete().where(binding.c.scope_id == "scope", binding.c.call_id == "call"))
        session.execute(tables["program_call_intents"].delete().where(
            tables["program_call_intents"].c.scope_id == "scope",
            tables["program_call_intents"].c.call_id == "call"))
        job = session.get(Job, "job")
        value = dict(job.resolved)
        run = dict(value["repository_run"])
        run["program_ledger_projections_v1"] = []
        value["repository_run"] = run
        job.resolved = value

    cancelled = _platform_for_projection_test(db).cancel("job", actor="operator")

    with db.sessions() as session:
        job = session.get(Job, "job")
        assert cancelled.cancel_requested is True
        assert job.cancel_requested is True and job.status == "running"
        assert session.scalar(select(tables["program_reservations"].c.state)) == "reserved"
        assert session.scalar(select(tables["program_slots"].c.state)) == "held"
        assert job.resolved["repository_run"]["attempts"][-1]["state"] == "running"


def test_cancel_legacy_json_marker_text_is_not_an_orphan_projection(work_rows):
    db, _, _, binding = work_rows
    tables = Base.metadata.tables
    with db.sessions.begin() as session:
        session.execute(binding.delete().where(binding.c.work_job_id == "job"))
        session.execute(tables["program_call_intents"].delete().where(
            tables["program_call_intents"].c.work_job_id == "job"))
        job = session.get(Job, "job")
        value = dict(job.resolved)
        value.pop("repository_run")
        value["note"] = "program_ledger_projections_v1 appears here as ordinary text"
        job.resolved = value

    cancelled = _platform_for_projection_test(db).cancel("job", actor="operator")

    assert cancelled.cancel_requested is True
    with db.sessions() as session:
        job = session.get(Job, "job")
        assert job.status == "running" and job.cancel_requested is True


def test_cancel_missing_attempt_id_checks_all_job_bindings(work_rows):
    db, _, _, _ = work_rows
    with db.sessions.begin() as session:
        job = session.get(Job, "job")
        value = dict(job.resolved)
        run = dict(value["repository_run"])
        run["attempts"] = []
        run["program_ledger_projections_v1"] = []
        value["repository_run"] = run
        job.resolved = value

    with pytest.raises(PlatformError) as exc_info:
        _platform_for_projection_test(db).cancel("job", actor="operator")

    assert exc_info.value.code == "cancel_projection_unavailable"
    with db.sessions() as session:
        job = session.get(Job, "job")
        assert job.status == "running" and job.cancel_requested is True
        assert session.scalar(select(Base.metadata.tables["program_reservations"].c.state)) == "reserved"


@pytest.mark.parametrize("drift", ["status", "attempts", "malformed_attempts"])
def test_cancel_locked_job_snapshot_change_falls_back_without_inhibition(work_rows, monkeypatch, drift):
    db, _, _, _ = work_rows
    import sovite.work_projection as work_projection

    original = work_projection.lock_work_projection_set

    def change_status_after_projection_locks(session, projections):
        original(session, projections)
        job = session.get(Job, "job")
        if drift == "status":
            job.status = "queued"
        elif drift == "attempts":
            value = dict(job.resolved)
            run = dict(value["repository_run"])
            attempts = [dict(item) for item in run["attempts"]]
            attempts[-1]["state"] = "awaiting_activation"
            run["attempts"] = attempts
            value["repository_run"] = run
            job.resolved = value
        else:
            value = dict(job.resolved)
            run = dict(value["repository_run"])
            run["attempts"] = {"invalid": "not-a-list"}
            value["repository_run"] = run
            job.resolved = value

    monkeypatch.setattr(work_projection, "lock_work_projection_set", change_status_after_projection_locks)
    with pytest.raises(PlatformError) as exc_info:
        _platform_for_projection_test(db).cancel("job", actor="operator")

    assert exc_info.value.code == "cancel_projection_unavailable"
    with db.sessions() as session:
        job = session.get(Job, "job")
        # The injected drift shares the failed owner transaction and is rolled
        # back; the durable fallback records only the cancel request.
        assert job.status == "running" and job.cancel_requested is True
        assert session.scalar(select(Base.metadata.tables["program_reservations"].c.state)) == "reserved"


def test_cancel_after_worker_running_persist_inhibits_same_projection(work_rows, monkeypatch):
    db, _, _, _ = work_rows
    import sovite.work_projection as work_projection

    with db.sessions.begin() as session:
        job = session.get(Job, "job")
        value = dict(job.resolved)
        run = dict(value["repository_run"])
        attempts = [dict(item) for item in run["attempts"]]
        attempts[-1]["state"] = "preparing"
        run["attempts"] = attempts
        value["repository_run"] = run
        job.resolved = value

    original = work_projection.lock_work_projection_set

    def worker_wins_before_job_reread(session, projections):
        job = session.get(Job, "job")
        value = dict(job.resolved)
        run = dict(value["repository_run"])
        attempts = [dict(item) for item in run["attempts"]]
        attempts[-1]["state"] = "running"
        run["attempts"] = attempts
        value["repository_run"] = run
        job.resolved = value
        original(session, projections)

    monkeypatch.setattr(work_projection, "lock_work_projection_set", worker_wins_before_job_reread)
    cancelled = _platform_for_projection_test(db).cancel("job", actor="operator")

    tables = Base.metadata.tables
    with db.sessions() as session:
        job = session.get(Job, "job")
        assert cancelled.cancel_requested is True
        assert job.cancel_requested is True and job.status == "running"
        assert job.resolved["repository_run"]["attempts"][-1]["state"] == "running"
        assert session.scalar(select(tables["program_reservations"].c.state)) == "unknown"
        gate = session.execute(select(tables["program_gate_state"])).mappings().one()
        assert gate["mode"] == "inhibited"


def test_cancel_malformed_sqlite_job_json_is_explicitly_unavailable(work_rows):
    db, _, _, _ = work_rows
    if db.engine.dialect.name != "sqlite":
        pytest.skip("PostgreSQL JSON columns reject syntactically invalid JSON")
    with db.sessions.begin() as session:
        session.execute(text("UPDATE jobs SET resolved = :raw WHERE id = :job_id"),
                        {"raw": "{malformed", "job_id": "job"})

    with pytest.raises(PlatformError) as exc_info:
        _platform_for_projection_test(db).cancel("job", actor="operator")

    assert exc_info.value.code == "cancel_owner_state_unavailable"
    with db.sessions() as session:
        assert not session.execute(text(
            "SELECT cancel_requested FROM jobs WHERE id = 'job'"
        )).scalar_one()


def test_cancel_repeat_does_not_duplicate_audit_or_projection_revision(work_rows):
    db, _, _, binding = work_rows
    tables = Base.metadata.tables
    platform = _platform_for_projection_test(db)
    platform.cancel("job", actor="operator")
    with db.sessions() as session:
        audit_before = session.scalar(select(text("count(*)")).select_from(tables["audit_events"]))
        projection_before = session.execute(select(binding.c.projection_revision)).scalar_one()
        gate_before = session.execute(select(tables["program_gate_state"].c.revision)).scalar_one()
    platform.cancel("job", actor="operator")
    with db.sessions() as session:
        job = session.get(Job, "job")
        assert session.scalar(select(text("count(*)")).select_from(tables["audit_events"])) == audit_before
        assert session.execute(select(binding.c.projection_revision)).scalar_one() == projection_before
        assert session.execute(select(tables["program_gate_state"].c.revision)).scalar_one() == gate_before
        assert job.cancel_requested is True and job.status == "running"


def test_cancel_projection_mismatch_commits_request_but_not_inhibition(work_rows):
    db, _, _, _ = work_rows
    tables = Base.metadata.tables
    with db.sessions.begin() as session:
        job = session.get(Job, "job")
        value = dict(job.resolved)
        run = dict(value["repository_run"])
        run["program_ledger_projections_v1"] = []
        value["repository_run"] = run
        job.resolved = value

    with pytest.raises(PlatformError) as exc_info:
        _platform_for_projection_test(db).cancel("job", actor="operator")

    assert exc_info.value.code == "cancel_projection_unavailable"
    assert exc_info.value.status == 503
    with db.sessions() as session:
        job = session.get(Job, "job")
        assert job.cancel_requested is True and job.status == "running"
        assert session.scalar(select(tables["program_reservations"].c.state)) == "reserved"
        assert session.scalar(select(tables["program_slots"].c.state)) == "held"
        assert session.scalar(select(text("count(*)")).select_from(tables["audit_events"])) == 2


def test_cancel_queued_malformed_active_join_only_records_request(work_rows):
    db, _, _, _ = work_rows
    with db.sessions.begin() as session:
        session.execute(update(Job).where(Job.id == "job").values(status="queued"))

    with pytest.raises(PlatformError) as exc_info:
        _platform_for_projection_test(db).cancel("job", actor="operator")

    assert exc_info.value.code == "cancel_projection_unavailable"
    with db.sessions() as session:
        job = session.get(Job, "job")
        assert job.status == "queued" and job.cancel_requested is True
        assert session.scalar(select(Base.metadata.tables["program_reservations"].c.state)) == "reserved"


def test_cancel_invalid_schema_metadata_fails_closed(work_rows):
    db, _, _, _ = work_rows
    with db.sessions.begin() as session:
        session.execute(update(MetadataRow).where(MetadataRow.key == "schema_version").values(
            value={"version": "10"}))

    with pytest.raises(PlatformError) as exc_info:
        _platform_for_projection_test(db).cancel("job", actor="operator")

    assert exc_info.value.code == "cancel_owner_state_unavailable"
    with db.sessions() as session:
        assert session.get(Job, "job").cancel_requested is False


def test_cancel_known_settled_rows_only_inhibits_gate(work_rows):
    db, _, _, binding = work_rows
    payload = _settle_synthetic_call(work_rows)
    tables = Base.metadata.tables

    _platform_for_projection_test(db).cancel("job", actor="operator")

    with db.sessions() as session:
        job = session.get(Job, "job")
        assert job.cancel_requested is True
        current = job.resolved["repository_run"]["program_ledger_projections_v1"][0]
        assert current == payload
        assert session.scalar(select(tables["program_reservations"].c.state)) == "settled"
        assert session.scalar(select(tables["program_call_intents"].c.state)) == "known"
        assert session.scalar(select(tables["program_slots"].c.state)) == "released"
        assert session.scalar(select(tables["program_call_receipts"].c.receipt_id)) == "receipt"
        gate = session.execute(select(tables["program_gate_state"])).mappings().one()
        assert (gate["mode"], gate["revision"]) == ("inhibited", 1)
        binding_row = session.execute(select(binding)).mappings().one()
        assert binding_row["projection_revision"] == 1


def test_cancel_updates_all_current_projection_calls_atomically(work_rows):
    db, _, _, _ = work_rows
    _add_second_projection_scope(work_rows)
    tables = Base.metadata.tables

    _platform_for_projection_test(db).cancel("job", actor="operator")

    with db.sessions() as session:
        reservations = session.scalars(select(tables["program_reservations"].c.state).order_by(
            tables["program_reservations"].c.call_id)).all()
        job = session.get(Job, "job")
        gate = session.execute(select(tables["program_gate_state"])).mappings().one()
    assert reservations == ["unknown", "unknown"]
    assert [item["reservation_state"] for item in
            job.resolved["repository_run"]["program_ledger_projections_v1"]] == ["unknown", "unknown"]
    assert gate["mode"] == "inhibited"


def test_cancel_late_revision_failure_rolls_back_every_projection_call(work_rows):
    db, _, _, binding = work_rows
    _add_second_projection_scope(work_rows)
    tables = Base.metadata.tables
    maximum = str((1 << 63) - 1)
    with db.sessions.begin() as session:
        job = session.get(Job, "job")
        value = dict(job.resolved)
        run = dict(value["repository_run"])
        entries = [dict(entry) for entry in run["program_ledger_projections_v1"]]
        second = dict(entries[1], projection_revision=maximum)
        entries[1] = second
        run["program_ledger_projections_v1"] = entries
        value["repository_run"] = run
        job.resolved = value
        session.execute(update(binding).where(binding.c.call_id == "call-z").values(
            projection_revision=int(maximum), projection_digest=projection_digest(second)))

    with pytest.raises(PlatformError) as exc_info:
        _platform_for_projection_test(db).cancel("job", actor="operator")

    assert exc_info.value.code == "cancel_projection_unavailable"
    with db.sessions() as session:
        job = session.get(Job, "job")
        states = session.scalars(select(tables["program_reservations"].c.state).order_by(
            tables["program_reservations"].c.call_id)).all()
        gate = session.execute(select(tables["program_gate_state"])).mappings().one()
        assert job.cancel_requested is True
        assert states == ["reserved", "reserved"]
        assert gate["mode"] == "disabled"
        assert session.scalar(select(text("count(*)")).select_from(tables["audit_events"])) == 2


def test_cancel_stale_lease_records_request_without_projection_mutation(work_rows):
    db, _, _, _ = work_rows
    tables = Base.metadata.tables
    with db.sessions.begin() as session:
        session.execute(update(tables["worker_execution_leases"]).where(
            tables["worker_execution_leases"].c.job_id == "job").values(lease_until=0))
        session.execute(update(Job).where(Job.id == "job").values(lease_until=0))

    with pytest.raises(PlatformError) as exc_info:
        _platform_for_projection_test(db).cancel("job", actor="operator")

    assert exc_info.value.code == "cancel_projection_unavailable"
    with db.sessions() as session:
        job = session.get(Job, "job")
        assert job.cancel_requested is True
        assert session.scalar(select(tables["program_reservations"].c.state)) == "reserved"
        assert job.resolved["repository_run"]["attempts"][-1]["state"] == "running"


def test_cancel_audit_verification_failure_does_not_persist_request(work_rows):
    db, _, _, _ = work_rows
    with db.sessions.begin() as session:
        session.execute(update(MetadataRow).where(MetadataRow.key == "audit_head").values(
            value={"sequence": 1, "hash": "0" * 64}))

    with pytest.raises(PlatformError) as exc_info:
        _platform_for_projection_test(db).cancel("job", actor="operator")

    assert exc_info.value.code == "cancel_owner_state_unavailable"
    with db.sessions() as session:
        assert session.get(Job, "job").cancel_requested is False


def test_repository_running_persist_rejects_prior_cancel_request(work_rows, tmp_path):
    db, _, _, _ = work_rows
    with db.sessions.begin() as session:
        session.execute(update(Job).where(Job.id == "job").values(cancel_requested=True))
    executor = RepositoryRunAttemptExecutor(
        type("Platform", (), {"db": db})(), object(), tmp_path, lambda *args, **kwargs: None
    )

    with pytest.raises(Exception) as exc_info:
        executor._persist(
            "job", "2" * 32,
            lambda run: run["attempts"][-1].update(state="running"),
            reject_cancel_requested=True,
        )

    assert getattr(exc_info.value, "code", None) == "execution_cancel_requested"
    with db.sessions() as session:
        assert session.get(Job, "job").resolved["repository_run"]["attempts"][-1]["state"] == "running"


def _finish_service(db, *, deny=False):
    def authorize(session, job, *, purpose):
        if deny:
            raise PlatformError("execution_scope_denied", "denied", 403)
        return {}

    platform = SimpleNamespace(
        db=db, settings=SimpleNamespace(max_attempts=3),
        authorize_existing_job=authorize,
    )
    return LeaseService(platform,
        execution_owner_key(workload_id="workload", instance_id="instance"),
        {"job": "2" * 32})


def _finish_snapshot(db):
    with db.engine.connect() as connection:
        return {
            table.name: [dict(row) for row in connection.execute(
                select(table).order_by(*table.primary_key.columns)).mappings()]
            for table in Base.metadata.sorted_tables
        }


def _clear_work_history(db):
    tables = Base.metadata.tables
    with db.sessions.begin() as session:
        for table_name in (
            "program_work_projection_bindings", "program_call_intents",
            "program_slots", "program_reservations", "program_subject_registry",
            "program_budget_subjects",
        ):
            session.execute(delete(tables[table_name]))
        job = session.get(Job, "job")
        job.resolved = {"repository_run": {
            "schema_version": RUN_SCHEMA, "attempts": [],
            "program_ledger_projections_v1": [],
        }}


def _set_native_awaiting_activation(db, *, valid=True):
    with db.sessions() as session:
        job = session.get(Job, "job")
        resolved = json.loads(json.dumps(job.resolved))
        lease = session.execute(select(Base.metadata.tables["worker_execution_leases"]).where(
            Base.metadata.tables["worker_execution_leases"].c.job_id == "job")).mappings().one()
        execution_deadline = lease.execution_deadline
        generation, caller = job.generation, job.caller
    run = resolved["repository_run"]
    run.update(
        workspace_id="workspace", repository_id="repository", principal_id=caller,
        deadline=datetime.fromtimestamp(execution_deadline, timezone.utc).isoformat(),
    )
    attempt = run["attempts"][-1]
    attempt.update(state="awaiting_activation", packet_sha256="a" * 64)
    activation = {
        "schema_version": "sovite-native-activation/v1",
        "job_id": "job", "attempt_id": "job-1", "workspace_id": "workspace",
        "repository_id": "repository", "principal_id": caller, "generation": generation,
        "fence_sha256": attempt["fence_sha256"], "packet_sha256": "a" * 64,
        "deadline": execution_deadline - 1,
    }
    if not valid:
        activation["fence_sha256"] = "0" * 64
    attempt["activation"] = activation
    with db.sessions.begin() as session:
        session.execute(_job_resolved_update(db), {
            "resolved": json.dumps(resolved, separators=(",", ":")), "job_id": "job",
        })


def _private_artifact_route(db):
    from pathlib import Path

    from sovite.api import create_app

    settings = Settings(
        database_url="sqlite:///:memory:",
        bundle_path=str(Path(__file__).resolve().parents[1] / "config/platform.local.yaml"),
        artifact_path="/tmp/sovite-private-artifact-test",
        operator_token="private-artifact-operator-token-000000",
        caller_tokens={}, require_qualification=False,
    )
    platform = Platform(settings)
    platform.db.engine.dispose()
    platform.db = db
    reads = []
    platform.store = SimpleNamespace(get=lambda key, sha256: reads.append((key, sha256)) or b"private")
    app = create_app(settings, platform, repository_run_owner=object())
    route = next(route for route in app.routes
                 if getattr(route, "path", None) == "/api/jobs/{job_id}/artifact")
    return route.endpoint, reads


def test_finish_completed_releases_only_after_known_settlement(work_rows):
    db, _, _, _ = work_rows
    _seed_hand_back_known_receipt(work_rows, actual_usd="0", actual_allowance="0")
    _set_hand_back_attempt_state(db, "completed")

    assert _finish_service(db).finish(
        "job", artifact=("private-object", "a" * 64), fence="2" * 32) is True

    with db.sessions() as session:
        job = session.get(Job, "job")
        lease = session.execute(select(Base.metadata.tables["worker_execution_leases"])).mappings().one()
        assert job.status == "completed"
        assert (job.artifact_key, job.artifact_sha256) == ("private-object", "a" * 64)
        assert job.public_projection == {"receipt": "job", "status": "available"}
        assert job.released_at is not None
        assert _projection(job) == {"receipt": "job", "status": "available"}
        assert lease["phase"] == "finished"


def test_finish_denial_records_private_custody_without_public_release(work_rows, monkeypatch):
    db, _, _, _ = work_rows
    _seed_hand_back_known_receipt(work_rows, actual_usd="0", actual_allowance="0")
    _set_hand_back_attempt_state(db, "completed")

    assert _finish_service(db, deny=True).finish(
        "job", artifact=("private-object", "a" * 64), fence="2" * 32) is True

    with db.sessions() as session:
        job = session.get(Job, "job")
        assert (job.status, job.error_code) == ("blocked", "execution_scope_denied")
        assert (job.artifact_key, job.artifact_sha256) == ("private-object", "a" * 64)
        assert job.public_projection is None
        assert job.released_at is None
        assert _projection(job)["status"] != "available"
    endpoint, reads = _private_artifact_route(db)

    async def direct_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", direct_to_thread)
    response = asyncio.run(endpoint("job", auth=("operator", "fixture")))
    assert response.body == b"private"
    assert reads == [("private-object", "a" * 64)]


@pytest.mark.parametrize("attempt_state", ("uncertain", "interrupted"))
def test_finish_explicit_uncertainty_inhibits_known_projection(work_rows, attempt_state):
    db, _, _, _ = work_rows
    _seed_hand_back_known_receipt(work_rows, actual_usd="0", actual_allowance="0")
    _set_hand_back_attempt_state(db, attempt_state)

    assert _finish_service(db).finish(
        "job", error=RuntimeFailure("external_execution_uncertain"), fence="2" * 32) is True

    tables = Base.metadata.tables
    with db.sessions() as session:
        job = session.get(Job, "job")
        gate = session.execute(select(tables["program_gate_state"])).mappings().one()
        reservation = session.execute(select(tables["program_reservations"])).mappings().one()
        assert (job.status, job.error_code) == ("blocked", "execution_uncertain")
        assert gate["mode"] == "inhibited"
        assert reservation["state"] == "settled"
        assert job.public_projection is None


def test_finish_uncertainty_with_empty_work_set_has_no_gate_to_inhibit(work_rows):
    db, _, _, _ = work_rows
    _set_hand_back_attempt_state(db, "uncertain")
    tables = Base.metadata.tables
    with db.sessions.begin() as session:
        job = session.get(Job, "job")
        root = dict(job.resolved)
        run = dict(root["repository_run"])
        run["program_ledger_projections_v1"] = []
        root["repository_run"] = run
        job.resolved = root
        for name in (
            "program_work_projection_bindings", "program_call_intents",
            "program_slots", "program_reservations", "program_subject_registry",
            "program_budget_subjects", "program_gate_state",
            "program_policy_revisions", "program_budget_scopes",
        ):
            table = tables[name]
            key = table.c.id if name == "program_budget_scopes" else table.c.scope_id
            value = "scope"
            session.execute(delete(table).where(key == value))

    assert _finish_service(db).finish(
        "job", error=RuntimeFailure("external_execution_uncertain"),
        fence="2" * 32) is True

    with db.sessions() as session:
        job = session.get(Job, "job")
        gates = session.execute(select(tables["program_gate_state"])).mappings().all()
        assert (job.status, job.error_code) == ("blocked", "execution_uncertain")
        assert gates == [] and job.public_projection is None


def test_finish_ineligible_is_nonrelease(work_rows):
    db, _, _, _ = work_rows
    _seed_hand_back_known_receipt(work_rows, actual_usd="0", actual_allowance="0")
    _set_hand_back_attempt_state(db, "ineligible")

    assert _finish_service(db).finish(
        "job", error=RuntimeFailure("repository_candidate_ineligible"),
        fence="2" * 32) is True

    with db.sessions() as session:
        job = session.get(Job, "job")
        assert (job.status, job.error_code) == ("failed", "repository_candidate_ineligible")
        assert job.public_projection is None and job.released_at is None


def test_finish_preentry_cancel_preserves_known_projection(work_rows):
    db, _, _, _ = work_rows
    _seed_hand_back_known_receipt(work_rows, actual_usd="0", actual_allowance="0")
    _set_hand_back_attempt_state(db, "activation_cancelled")

    assert _finish_service(db).finish("job", cancelled=True, fence="2" * 32) is True

    with db.sessions() as session:
        job = session.get(Job, "job")
        gate = session.execute(select(Base.metadata.tables["program_gate_state"])).mappings().one()
        reservation = session.execute(select(Base.metadata.tables["program_reservations"])).mappings().one()
        assert (job.status, job.error_code) == ("cancelled", "cancelled")
        assert gate["mode"] == "disabled"
        assert reservation["state"] == "settled"
        assert job.public_projection is None


def test_finish_unsettled_artifact_is_held_and_not_released(work_rows):
    db, _, _, _ = work_rows
    _set_hand_back_attempt_state(db, "completed")

    assert _finish_service(db).finish(
        "job", artifact=("private-object", "a" * 64), fence="2" * 32) is True

    tables = Base.metadata.tables
    with db.sessions() as session:
        job = session.get(Job, "job")
        reservation = session.execute(select(tables["program_reservations"])).mappings().one()
        slot = session.execute(select(tables["program_slots"])).mappings().one()
        intent = session.execute(select(tables["program_call_intents"])).mappings().one()
        assert (job.status, job.error_code) == ("blocked", "execution_uncertain")
        assert (job.artifact_key, job.artifact_sha256) == ("private-object", "a" * 64)
        assert job.public_projection is None and job.released_at is None
        assert (reservation["state"], slot["state"], intent["state"]) == (
            "unknown", "unknown", "unknown")


def test_finish_rejects_inconsistent_attempt_outcome_pair(work_rows):
    db, _, _, _ = work_rows
    _seed_hand_back_known_receipt(work_rows, actual_usd="0", actual_allowance="0")
    _set_hand_back_attempt_state(db, "completed")

    with pytest.raises(FinishRefused):
        _finish_service(db).finish(
            "job", error=RuntimeFailure("repository_candidate_ineligible"),
            fence="2" * 32)

    with db.sessions() as session:
        job = session.get(Job, "job")
        lease = session.execute(select(Base.metadata.tables["worker_execution_leases"])).mappings().one()
        assert job.status == "running" and job.public_projection is None
        assert job.artifact_key is None and lease["phase"] == "started"


def test_finish_accepts_only_explicit_post_execution_store_error(work_rows):
    db, _, _, _ = work_rows
    _seed_hand_back_known_receipt(work_rows, actual_usd="0", actual_allowance="0")
    _set_hand_back_attempt_state(db, "completed")

    assert _finish_service(db).finish(
        "job", error=RuntimeFailure("storage_adapter_failure"), fence="2" * 32,
        execution_completed=True) is True

    with db.sessions() as session:
        job = session.get(Job, "job")
        lease = session.execute(select(Base.metadata.tables["worker_execution_leases"])).mappings().one()
        assert (job.status, job.error_code) == ("blocked", "storage_adapter_failure")
        assert job.artifact_key is None and job.public_projection is None and job.released_at is None
        assert lease["phase"] == "finished"


def test_finish_preserves_awaiting_activation_persist_error_with_valid_metadata(work_rows):
    db, _, _, _ = work_rows
    _seed_hand_back_known_receipt(work_rows, actual_usd="0", actual_allowance="0")
    _set_native_awaiting_activation(db)

    assert _finish_service(db).finish(
        "job", error=RuntimeFailure("fixture_persist_refused"), fence="2" * 32) is True

    with db.sessions() as session:
        job = session.get(Job, "job")
        lease = session.execute(select(Base.metadata.tables["worker_execution_leases"])).mappings().one()
        gate = session.execute(select(Base.metadata.tables["program_gate_state"])).mappings().one()
        reservation = session.execute(select(Base.metadata.tables["program_reservations"])).mappings().one()
        assert (job.status, job.error_code) == ("failed", "fixture_persist_refused")
        assert job.resolved["repository_run"]["attempts"][-1]["state"] == "awaiting_activation"
        assert job.artifact_key is None and job.public_projection is None and job.released_at is None
        assert lease["phase"] == "finished"
        assert gate["mode"] == "disabled" and reservation["state"] == "settled"


def test_finish_holds_unresolved_work_after_awaiting_activation_persist_error(work_rows):
    db, _, _, _ = work_rows
    _set_native_awaiting_activation(db)

    assert _finish_service(db).finish(
        "job", error=RuntimeFailure("storage_adapter_failure"), fence="2" * 32) is True

    tables = Base.metadata.tables
    with db.sessions() as session:
        job = session.get(Job, "job")
        reservation = session.execute(select(tables["program_reservations"])).mappings().one()
        slot = session.execute(select(tables["program_slots"])).mappings().one()
        intent = session.execute(select(tables["program_call_intents"])).mappings().one()
        gate = session.execute(select(tables["program_gate_state"])).mappings().one()
        assert (job.status, job.error_code) == ("blocked", "execution_uncertain")
        assert (reservation["state"], slot["state"], intent["state"]) == (
            "unknown", "unknown", "unknown")
        assert gate["mode"] == "inhibited"
        assert job.artifact_key is None and job.public_projection is None and job.released_at is None


def test_finish_refuses_malformed_awaiting_activation_metadata(work_rows):
    db, _, _, _ = work_rows
    _set_native_awaiting_activation(db, valid=False)
    before = _finish_snapshot(db)

    with pytest.raises(FinishRefused):
        _finish_service(db).finish(
            "job", error=RuntimeFailure("fixture_persist_refused"), fence="2" * 32)

    assert _finish_snapshot(db) == before


def test_finish_accepts_live_preentry_timeout_activation_cancel_marker(work_rows):
    db, _, _, _ = work_rows
    _seed_hand_back_known_receipt(work_rows, actual_usd="0", actual_allowance="0")
    _set_native_awaiting_activation(db)
    _set_hand_back_attempt_state(db, "activation_cancelled")

    assert _finish_service(db).finish(
        "job", error=RuntimeFailure("execution_timeout", False), fence="2" * 32) is True

    with db.sessions() as session:
        job = session.get(Job, "job")
        lease = session.execute(select(Base.metadata.tables["worker_execution_leases"])).mappings().one()
        assert (job.status, job.error_code) == ("failed", "execution_timeout")
        assert job.artifact_key is None and job.public_projection is None and job.released_at is None
        assert lease["phase"] == "finished"


def test_finish_refuses_expired_preentry_timeout_owner_without_mutation(work_rows):
    db, _, _, _ = work_rows
    _seed_hand_back_known_receipt(work_rows, actual_usd="0", actual_allowance="0")
    _set_native_awaiting_activation(db)
    _set_hand_back_attempt_state(db, "activation_cancelled")
    with db.sessions.begin() as session:
        leases = Base.metadata.tables["worker_execution_leases"]
        session.execute(update(leases).where(leases.c.job_id == "job").values(
            execution_deadline=time.time() - 1))
    before = _finish_snapshot(db)

    with pytest.raises(FinishRefused):
        _finish_service(db).finish(
            "job", error=RuntimeFailure("execution_timeout", False), fence="2" * 32)

    assert _finish_snapshot(db) == before


def test_finish_execution_completed_hint_cannot_replace_attempt_marker(work_rows):
    db, _, _, _ = work_rows
    before = _finish_snapshot(db)

    with pytest.raises(FinishRefused):
        _finish_service(db).finish(
            "job", error=RuntimeFailure("storage_adapter_failure"), fence="2" * 32,
            execution_completed=True)

    assert _finish_snapshot(db) == before


def test_finish_completed_hint_cannot_be_combined_with_uncertainty(work_rows):
    db, _, _, _ = work_rows
    _set_hand_back_attempt_state(db, "interrupted")
    before = _finish_snapshot(db)

    with pytest.raises(FinishRefused):
        _finish_service(db).finish(
            "job", error=RuntimeFailure("external_execution_uncertain"), fence="2" * 32,
            execution_completed=True)

    assert _finish_snapshot(db) == before


def test_finish_completed_error_requires_worker_completion_hint(work_rows):
    db, _, _, _ = work_rows
    _set_hand_back_attempt_state(db, "completed")
    before = _finish_snapshot(db)

    with pytest.raises(FinishRefused):
        _finish_service(db).finish(
            "job", error=RuntimeFailure("internal_execution_error"), fence="2" * 32)

    assert _finish_snapshot(db) == before


def test_worker_storage_runtime_failure_finishes_completed_attempt_without_release(work_rows, monkeypatch):
    db, _, _, _ = work_rows
    _seed_hand_back_known_receipt(work_rows, actual_usd="0", actual_allowance="0")
    _set_hand_back_attempt_state(db, "completed")

    class Store:
        def put(self, job_id, result):
            raise RuntimeFailure("storage_adapter_failure")

    class Stop:
        def is_set(self):
            return False

        async def wait(self):
            return False

    class Executor:
        async def run(self, job, *, fence, authorization_check):
            return {"kind": "repository-candidate"}

        def check_authorization(self, job):
            return None

    worker = Worker.__new__(Worker)
    worker.leases = _finish_service(db)
    worker.platform = SimpleNamespace(
        settings=SimpleNamespace(job_timeout_seconds=5, lease_seconds=1), store=Store())
    worker.repository_run_executor = Executor()
    worker.runner = SimpleNamespace(authorization_check=None)
    worker.stop = Stop()
    worker.heartbeat = lambda job_id=None: None
    worker.renew = lambda job_id, fence=None: True
    async def direct_to_thread(function, *args):
        return function(*args)
    monkeypatch.setattr(asyncio, "to_thread", direct_to_thread)
    with db.sessions() as session:
        resolved = json.loads(json.dumps(session.get(Job, "job").resolved))
    resolved["grant"] = {"timeout_seconds": 5}
    job = SimpleNamespace(
        id="job", task="synthetic", execution_fence="2" * 32,
        execution_deadline=time.time() + 300, resolved=resolved,
    )

    asyncio.run(worker.execute(job))

    with db.sessions() as session:
        row = session.get(Job, "job")
        lease = session.execute(select(Base.metadata.tables["worker_execution_leases"])).mappings().one()
        assert (row.status, row.error_code) == ("blocked", "storage_adapter_failure")
        assert row.artifact_key is None and row.public_projection is None and row.released_at is None
        assert lease["phase"] == "finished"


def test_retryable_storage_failure_blocks_instead_of_replaying_completed_repository_attempt(work_rows):
    db, _, _, _ = work_rows
    _seed_hand_back_known_receipt(work_rows, actual_usd="0", actual_allowance="0")
    _set_hand_back_attempt_state(db, "completed")
    with db.sessions.begin() as session:
        job = session.get(Job, "job")
        resolved = json.loads(json.dumps(job.resolved))
        resolved["grant"] = {"on_outage": "queue", "max_attempts": 3}
        resolved["tools"] = []
        job.resolved = resolved
        prior_next_attempt_at = job.next_attempt_at

    assert _finish_service(db).finish(
        "job", error=RuntimeFailure("storage_adapter_failure", retryable=True),
        fence="2" * 32, execution_completed=True) is True

    with db.sessions() as session:
        job = session.get(Job, "job")
        assert (job.status, job.error_code) == ("blocked", "storage_adapter_failure")
        assert job.next_attempt_at == prior_next_attempt_at and job.attempts == 1
        assert job.artifact_key is None and job.public_projection is None and job.released_at is None


def test_worker_completed_hint_keeps_permitted_legacy_retry_without_repository_marker(work_rows, monkeypatch):
    db, _, _, _ = work_rows
    _clear_work_history(db)
    with db.sessions.begin() as session:
        job_row = session.get(Job, "job")
        job_row.resolved = {
            "grant": {"timeout_seconds": 5, "on_outage": "queue", "max_attempts": 3},
            "tools": [],
        }

    class Store:
        def put(self, job_id, result):
            raise RuntimeFailure("storage_adapter_failure", retryable=True)

    class Stop:
        def is_set(self):
            return False

        async def wait(self):
            return False

    class Runner:
        authorization_check = None

        async def run(self, resolved, task, job_id):
            return {"answer": "read-only result"}

    async def direct_to_thread(function, *args):
        return function(*args)
    monkeypatch.setattr(asyncio, "to_thread", direct_to_thread)

    worker = Worker.__new__(Worker)
    worker.leases = _finish_service(db)
    worker.platform = SimpleNamespace(settings=SimpleNamespace(
        job_timeout_seconds=5, lease_seconds=1, max_attempts=3), store=Store())
    worker.runner = Runner()
    worker.repository_run_executor = None
    worker.stop = Stop()
    worker.heartbeat = lambda job_id=None: None
    worker.renew = lambda job_id, fence=None: True
    job = SimpleNamespace(
        id="job", task="synthetic", execution_fence="2" * 32,
        execution_deadline=time.time() + 300,
        resolved={"grant": {"timeout_seconds": 5, "on_outage": "queue", "max_attempts": 3},
                  "tools": []},
    )

    asyncio.run(worker.execute(job))

    with db.sessions() as session:
        row = session.get(Job, "job")
        assert (row.status, row.error_code) == ("retry_wait", "storage_adapter_failure")
        assert row.attempts == 1 and row.next_attempt_at > time.time()
        assert row.artifact_key is None and row.public_projection is None and row.released_at is None


@pytest.mark.parametrize("attempt_state", ["failed", "cancelled", "activation_cancelled"])
def test_finish_accepts_repository_binding_revocation_producer_states(work_rows, attempt_state):
    db, _, _, _ = work_rows
    _set_hand_back_attempt_state(db, attempt_state)

    assert _finish_service(db).finish(
        "job", error=RuntimeFailure("repository_binding_revoked"), fence="2" * 32) is True

    with db.sessions() as session:
        job = session.get(Job, "job")
        assert job.public_projection is None and job.released_at is None
        assert job.status in {"failed", "blocked"}


def test_finish_refuses_unrelated_interrupted_binding_revocation_pair(work_rows):
    db, _, _, _ = work_rows
    _set_hand_back_attempt_state(db, "interrupted")
    before = _finish_snapshot(db)

    with pytest.raises(FinishRefused):
        _finish_service(db).finish(
            "job", error=RuntimeFailure("repository_binding_revoked"), fence="2" * 32)

    assert _finish_snapshot(db) == before


def test_worker_repository_authorization_revocation_finishes_cancel_marker(work_rows):
    db, _, _, _ = work_rows

    class Stop:
        def is_set(self):
            return False

        async def wait(self):
            await asyncio.Event().wait()

    class Executor:
        async def run(self, job, *, fence, authorization_check):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                with db.sessions.begin() as session:
                    row = session.get(Job, job.id)
                    resolved = json.loads(json.dumps(row.resolved))
                    resolved["repository_run"]["attempts"][-1]["state"] = "activation_cancelled"
                    row.resolved = resolved
                raise

        def check_authorization(self, job):
            raise RuntimeFailure("repository_binding_revoked")

        def effect_started(self, job_id):
            return False

    worker = Worker.__new__(Worker)
    worker.leases = _finish_service(db)
    worker.platform = SimpleNamespace(settings=SimpleNamespace(
        job_timeout_seconds=5, lease_seconds=1))
    worker.repository_run_executor = Executor()
    worker.runner = SimpleNamespace(authorization_check=None)
    worker.stop = Stop()
    worker.heartbeat = lambda job_id=None: None
    worker.renew = lambda job_id, fence=None: True
    with db.sessions() as session:
        row = session.get(Job, "job")
        resolved = dict(row.resolved)
    resolved["grant"] = {"timeout_seconds": 5}
    job = SimpleNamespace(
        id="job", task="synthetic", execution_fence="2" * 32,
        execution_deadline=time.time() + 300, resolved=resolved,
    )

    asyncio.run(worker.execute(job))

    with db.sessions() as session:
        row = session.get(Job, "job")
        assert row.resolved["repository_run"]["attempts"][-1]["state"] == "activation_cancelled"
        assert row.public_projection is None and row.released_at is None
        assert (row.status, row.error_code) == ("blocked", "execution_uncertain")


@pytest.mark.parametrize("error_code", [
    "repository_binding_revoked", "repository_binding_unavailable",
    "repository_binding_mismatch",
])
def test_finish_allows_verified_empty_work_preappend_error(work_rows, error_code):
    db, _, _, _ = work_rows
    _clear_work_history(db)

    assert _finish_service(db).finish(
        "job", error=RuntimeFailure(error_code), fence="2" * 32) is True

    with db.sessions() as session:
        job = session.get(Job, "job")
        lease = session.execute(select(Base.metadata.tables["worker_execution_leases"])).mappings().one()
        assert (job.status, job.error_code) == ("failed", error_code)
        assert job.artifact_key is None and job.public_projection is None and job.released_at is None
        assert lease["phase"] == "finished"


def test_finish_allows_requested_cancel_before_preappend_with_empty_work(work_rows):
    db, _, _, _ = work_rows
    _clear_work_history(db)
    with db.sessions.begin() as session:
        session.get(Job, "job").cancel_requested = True

    assert _finish_service(db).finish("job", cancelled=True, fence="2" * 32) is True

    with db.sessions() as session:
        job = session.get(Job, "job")
        lease = session.execute(select(Base.metadata.tables["worker_execution_leases"])).mappings().one()
        assert (job.status, job.error_code) == ("cancelled", "cancelled")
        assert job.resolved["repository_run"]["attempts"] == []
        assert job.artifact_key is None and job.artifact_sha256 is None
        assert job.public_projection is None and job.released_at is None
        assert lease["phase"] == "finished"


@pytest.mark.parametrize("case", ["not_requested", "workful"])
def test_finish_refuses_preappend_cancel_without_empty_requested_closure(work_rows, case):
    db, _, _, _ = work_rows
    if case == "not_requested":
        _clear_work_history(db)
    else:
        with db.sessions.begin() as session:
            session.get(Job, "job").cancel_requested = True
    before = _finish_snapshot(db)

    with pytest.raises(FinishRefused):
        _finish_service(db).finish("job", cancelled=True, fence="2" * 32)

    assert _finish_snapshot(db) == before


@pytest.mark.parametrize("extra", [
    {"cancelled": True}, {"execution_completed": True},
])
def test_finish_refuses_contradictory_empty_work_preappend_hints(work_rows, extra):
    db, _, _, _ = work_rows
    _clear_work_history(db)
    before = _finish_snapshot(db)

    with pytest.raises(FinishRefused):
        _finish_service(db).finish(
            "job", error=RuntimeFailure("repository_binding_unavailable"),
            fence="2" * 32, **extra)

    assert _finish_snapshot(db) == before


def test_finish_preappend_error_preserves_prior_attempt_history(work_rows):
    db, _, _, _ = work_rows
    _clear_work_history(db)
    with db.sessions.begin() as session:
        job = session.get(Job, "job")
        job.attempts = 2
        job.resolved = {"repository_run": {
            "schema_version": RUN_SCHEMA,
            "attempts": [{"id": "job-1", "state": "failed", "fence_sha256": "3" * 64}],
            "program_ledger_projections_v1": [],
        }}

    assert _finish_service(db).finish(
        "job", error=RuntimeFailure("repository_binding_unavailable"), fence="2" * 32) is True

    with db.sessions() as session:
        job = session.get(Job, "job")
        assert job.resolved["repository_run"]["attempts"] == [
            {"id": "job-1", "state": "failed", "fence_sha256": "3" * 64},
        ]
        assert job.error_code == "repository_binding_unavailable"


def test_worker_missing_repository_executor_finishes_empty_work_job(work_rows):
    db, _, _, _ = work_rows
    _clear_work_history(db)
    worker = Worker.__new__(Worker)
    worker.leases = _finish_service(db)
    worker.platform = SimpleNamespace(settings=SimpleNamespace(repository_worker_only=False))
    worker.repository_run_executor = None
    worker.heartbeat = lambda job_id=None: None
    job = SimpleNamespace(
        id="job", task="synthetic", execution_fence="2" * 32,
        resolved={"repository_run": {"schema_version": RUN_SCHEMA, "attempts": []}},
    )

    asyncio.run(worker.execute(job))

    with db.sessions() as session:
        row = session.get(Job, "job")
        assert (row.status, row.error_code) == ("failed", "repository_profile_unavailable")
        assert row.artifact_key is None and row.public_projection is None
        lease = session.execute(select(Base.metadata.tables["worker_execution_leases"])).mappings().one()
        assert lease["phase"] == "finished"


def test_finish_refuses_missing_current_attempt_when_work_history_exists(work_rows):
    db, _, _, _ = work_rows
    with db.sessions.begin() as session:
        job = session.get(Job, "job")
        resolved = dict(job.resolved)
        run = dict(resolved["repository_run"])
        run["attempts"] = []
        resolved["repository_run"] = run
        job.resolved = resolved
    before = _finish_snapshot(db)

    with pytest.raises(FinishRefused):
        _finish_service(db).finish(
            "job", error=RuntimeFailure("repository_binding_revoked"), fence="2" * 32)

    assert _finish_snapshot(db) == before


def test_finish_refuses_artifact_without_current_attempt_even_when_work_is_empty(work_rows):
    db, _, _, _ = work_rows
    _clear_work_history(db)
    before = _finish_snapshot(db)

    with pytest.raises(FinishRefused):
        _finish_service(db).finish(
            "job", artifact=("prewritten-unreferenced-object", "a" * 64), fence="2" * 32)

    assert _finish_snapshot(db) == before


def test_finish_requires_exact_projection_closure_before_custody_write(work_rows):
    db, _, _, binding = work_rows
    _seed_hand_back_known_receipt(work_rows, actual_usd="0", actual_allowance="0")
    _set_hand_back_attempt_state(db, "completed")
    with db.sessions.begin() as session:
        session.execute(delete(binding).where(binding.c.call_id == "call"))

    with pytest.raises(FinishRefused):
        _finish_service(db).finish(
            "job", artifact=("already-written-object", "a" * 64), fence="2" * 32)

    with db.sessions() as session:
        job = session.get(Job, "job")
        assert job.status == "running"
        assert job.artifact_key is None and job.public_projection is None


def test_comparator_keeps_terminal_attempts_opt_in_only(work_rows):
    db, _, _, _ = work_rows
    _set_hand_back_attempt_state(db, "completed")
    with owner_transaction(db) as session:
        with pytest.raises(_WorkProjectionUnavailable):
            _compare_actual_work_projection(session, scope_id="scope", call_id="call")
        assert _compare_actual_work_projection(
            session, scope_id="scope", call_id="call",
            finish_attempt_states=frozenset({"completed"}),
        ) == "canonical_rows_agree"


def test_finish_stale_fence_refuses_without_owner_mutation(work_rows):
    db, _, _, _ = work_rows
    before = _hand_back_service(db)

    with pytest.raises(FinishRefused):
        before.finish("job", artifact=("object", "a" * 64), fence="3" * 32)

    with db.sessions() as session:
        job = session.get(Job, "job")
        lease = session.execute(select(Base.metadata.tables["worker_execution_leases"])).mappings().one()
        assert job.status == "running"
        assert job.artifact_key is None and job.public_projection is None
        assert lease["phase"] == "started"


def test_worker_handles_one_refused_finish_without_reclassification():
    class Stop:
        def is_set(self):
            return False

        async def wait(self):
            return False

    completed = []

    class Runner:
        authorization_check = None

        async def run(self, resolved, task, job_id):
            completed.append(job_id)
            return {"answer": "ok"}

    worker = Worker.__new__(Worker)
    worker.platform = SimpleNamespace(
        settings=SimpleNamespace(job_timeout_seconds=5, lease_seconds=1),
        store=SimpleNamespace(put=lambda job_id, result: ("object", "a" * 64)),
    )
    worker.runner = Runner()
    worker.repository_run_executor = None
    worker.stop = Stop()
    worker.heartbeat = lambda job_id=None: None
    calls = []

    def refuse(*args, **kwargs):
        calls.append(kwargs)
        raise FinishRefused("owner refusal")

    worker.finish = refuse
    job = SimpleNamespace(
        execution_fence="2" * 32, id="job", task="task",
        resolved={"grant": {"timeout_seconds": 5}, "tools": []},
    )
    asyncio.run(worker.execute(job))
    assert completed == ["job"]
    assert len(calls) == 1 and "artifact" in calls[0] and "error" not in calls[0]


def test_worker_handles_refusal_inside_store_error_path_once(monkeypatch):
    failure = RuntimeFailure("storage_adapter_failure")
    completed = []

    class Stop:
        def is_set(self):
            return False

        async def wait(self):
            return False

    class Runner:
        authorization_check = None

        async def run(self, resolved, task, job_id):
            completed.append(job_id)
            return {"answer": "ok"}

    async def fail_store(_put, job_id, result):
        raise failure

    monkeypatch.setattr(asyncio, "to_thread", fail_store)
    worker = Worker.__new__(Worker)
    worker.platform = SimpleNamespace(
        settings=SimpleNamespace(job_timeout_seconds=5, lease_seconds=1),
        store=SimpleNamespace(put=lambda job_id, result: None),
    )
    worker.runner = Runner()
    worker.repository_run_executor = None
    worker.stop = Stop()
    worker.heartbeat = lambda job_id=None: None
    calls = []

    def refuse(*args, **kwargs):
        calls.append(kwargs)
        raise FinishRefused("owner refusal")

    worker.finish = refuse
    job = SimpleNamespace(
        execution_fence="2" * 32, id="job", task="task",
        resolved={"grant": {"timeout_seconds": 5}, "tools": []},
    )
    asyncio.run(worker.execute(job))
    assert completed == ["job"]
    assert len(calls) == 1
    assert calls[0]["error"].code == "storage_adapter_failure"
    assert calls[0]["execution_completed"] is True


@pytest.mark.parametrize(("failure", "expected_code"), [
    (OSError("private storage detail"), "internal_execution_error"),
    (RuntimeFailure("storage_adapter_failure"), "storage_adapter_failure"),
])
def test_worker_routes_store_failure_to_one_finish_call(monkeypatch, failure, expected_code):
    assert isinstance(failure, (OSError, RuntimeFailure))
    completed = []

    class Stop:
        def is_set(self):
            return False

        async def wait(self):
            return False

    class Runner:
        authorization_check = None

        async def run(self, resolved, task, job_id):
            completed.append(job_id)
            return {"answer": "ok"}

    async def fail_store(_put, job_id, result):
        raise failure
    monkeypatch.setattr(asyncio, "to_thread", fail_store)

    worker = Worker.__new__(Worker)
    worker.platform = SimpleNamespace(
        settings=SimpleNamespace(job_timeout_seconds=5, lease_seconds=1),
        store=SimpleNamespace(put=lambda job_id, result: None),
    )
    worker.runner = Runner()
    worker.repository_run_executor = None
    worker.stop = Stop()
    worker.heartbeat = lambda job_id=None: None
    calls = []
    worker.finish = lambda *args, **kwargs: calls.append(kwargs)
    job = SimpleNamespace(
        execution_fence="2" * 32, id="job", task="task",
        resolved={"grant": {"timeout_seconds": 5}, "tools": []},
    )

    asyncio.run(worker.execute(job))

    assert completed == ["job"]
    assert len(calls) == 1
    assert calls[0]["error"].code == expected_code
    assert calls[0]["fence"] == "2" * 32
    assert calls[0]["execution_completed"] is True
