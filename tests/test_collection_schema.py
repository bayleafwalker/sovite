"""Offline, disposable schema-8 persistence and invariant tests."""
import os
import uuid

import pytest
from sqlalchemy import create_engine, event, insert, select
from sqlalchemy.exc import IntegrityError

from sovite.db import Base, Database, DatabaseSchemaError, Job, MetadataRow, _COLLECTION_V8, _PROGRAM_LEDGER_V9


@pytest.fixture(params=["sqlite", "postgresql"])
def db(request, tmp_path):
    if request.param == "sqlite":
        database = Database(f"sqlite:///{tmp_path}/collection.db")
        yield database
        database.engine.dispose()
        return
    url = os.environ.get("SOVITE_TEST_POSTGRES_URL")
    if not url:
        if os.environ.get("SOVITE_COLLECTION_SCHEMA_REQUIRE_PG") == "1":
            pytest.fail("SOVITE_TEST_POSTGRES_URL is required for collection schema tests")
        pytest.skip("isolated PostgreSQL fixture not supplied")
    admin = create_engine(url)
    schema = "collection_" + uuid.uuid4().hex
    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    database = Database(url, schema=schema)
    try:
        yield database
    finally:
        database.engine.dispose()
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()


def rows(database):
    table = Base.metadata.tables
    h = "a" * 64
    with database.sessions.begin() as session:
        audit = database.audit(session, "fixture", "fixture", "fixture")
        session.flush()
        a = audit.sequence
        session.execute(insert(table["program_budget_scopes"]), dict(id="scope", policy_revision=1,
            window_start_ms=1, window_end_ms=2, usd_limit_micro=10, allowance_unit="units",
            allowance_limit_micro=10, slot_limit=1, policy_digest=h))
        session.execute(insert(table["program_budget_subjects"]), dict(scope_id="scope", subject_id="subject",
            purpose="collection", binding_digest=h))
        session.execute(insert(table["collection_sessions"]), dict(id="session", principal_id="principal",
            idempotency_key="session-key", scope_id="scope", workspace_id="workspace", repository_id="repository",
            schema_version="test/v1", purpose="operator_c0_collection", generation="sha256:" + h,
            suite_version="v1", suite_digest=h, seed_digest=h, base_sha=h, paths_digest=h, deployment_digest=h,
            permit_digest=h, immutable_digest=h, immutable_payload={}, deadline_ms=10, retain_until_ms=100,
            state="active", revision=1, current_epoch=1))
        session.execute(insert(table["collection_epochs"]), [dict(session_id="session", epoch=1,
            epoch_purpose="execution", principal_id="principal", authorization_digest=h, deadline_ms=10,
            retain_until_ms=100, state="active"), dict(session_id="session", epoch=2,
            epoch_purpose="recovery", principal_id="principal", authorization_digest=h, deadline_ms=30,
            retain_until_ms=100, state="authorized")])
        session.execute(insert(table["collection_enrollments"]), dict(id="enrollment", session_id="session",
            original_epoch=1, resource_digest=h, boot_digest=h, permit_digest=h, ack_key_digest=h,
            last_ack_sequence=0, retain_until_ms=100))
    return a


def operation(a, **changes):
    h = "a" * 64
    value = dict(session_id="session", operation_id="op", epoch=1, epoch_purpose="execution", ordinal=1,
        idempotency_key="key", recipe_id="recipe", enrollment_id="enrollment", verb="exec-v1", session_digest=h, request_digest=h,
        resource_digest=h, boot_digest=h, nonce="nonce", issuer_key_id="key", start_not_after_ms=5,
        execution_not_after_ms=10, authorization_claims="{}", authorization_claims_digest=h,
        state="intent", audit_sequence=a)
    value.update(changes)
    return value


def test_offline_upgrade_preserves_v7_and_refuses_old_reader(db):
    old_tables = [table for table in Base.metadata.sorted_tables if table not in _COLLECTION_V8 and table not in _PROGRAM_LEDGER_V9]
    Base.metadata.create_all(db.engine, tables=old_tables)
    with db.sessions.begin() as session:
        session.add(MetadataRow(key="schema_version", value={"version": 7}))
        session.add(MetadataRow(key="audit_head", value={"sequence": 0, "hash": "0" * 64}))
        session.add(Job(id="old", receipt="old", idempotency_key="old", request_digest="old", caller="old",
                        grant_id="old", generation="old", resolved={}, task="preserve"))
    statements = []
    def capture(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)
    event.listen(db.engine, "before_cursor_execute", capture)
    try:
        with pytest.raises(DatabaseSchemaError, match="older"):
            db.initialize("never")
    finally:
        event.remove(db.engine, "before_cursor_execute", capture)
    assert statements and not any(sql.lstrip().upper().startswith(("CREATE", "ALTER", "DROP", "INSERT", "UPDATE", "DELETE")) for sql in statements)
    db.migrate()
    db.check()
    with db.sessions() as session:
        assert session.get(Job, "old").task == "preserve"
        assert session.get(MetadataRow, "schema_version").value == {"version": Database.schema_version}
        assert all(not session.execute(select(table)).first() for table in _COLLECTION_V8)
    db.schema_version = 7
    with pytest.raises(DatabaseSchemaError, match="newer"):
        db.check()
    db.schema_version = Database.schema_version
    db.migrate()


@pytest.mark.parametrize("changes", [
    {"operation_id": "other", "idempotency_key": "other", "epoch": 2, "epoch_purpose": "recovery", "verb": "quiesce-v1"},
    {"operation_id": "other", "idempotency_key": "other", "nonce": "other", "epoch": 2, "epoch_purpose": "recovery", "verb": "exec-v1"},
    {"operation_id": "other", "idempotency_key": "other", "nonce": "other", "epoch": 2, "epoch_purpose": "execution"},
    {"operation_id": "other", "idempotency_key": "other", "nonce": "other", "authorization_envelope": "{}"},
])
def test_nonce_epoch_purpose_recovery_and_signed_pair(db, changes):
    db.migrate()
    audit = rows(db)
    table = Base.metadata.tables["collection_operations"]
    with db.sessions.begin() as session:
        session.execute(insert(table), operation(audit))
    with pytest.raises(IntegrityError):
        with db.sessions.begin() as session:
            session.execute(insert(table), operation(audit, **changes))


def test_retention_reference_prevents_checkpoint_and_audit_deletion(db):
    db.migrate()
    audit = rows(db)
    tables = Base.metadata.tables
    with db.sessions.begin() as session:
        session.execute(insert(tables["collection_checkpoints"]), dict(session_id="session", sequence=1,
            artifact_digest="a" * 64, custody_ref="fixture", retain_until_ms=100, audit_sequence=audit))
        session.execute(insert(tables["collection_retention_refs"]), dict(session_id="session", reference_id="hold",
            kind="checkpoint", object_digest="a" * 64, custody_ref="fixture", retain_until_ms=100,
            hold="active", audit_sequence=audit, checkpoint_sequence=1))
    for table, where in [(tables["collection_checkpoints"], tables["collection_checkpoints"].c.session_id == "session"),
                         (tables["audit_events"], tables["audit_events"].c.sequence == audit)]:
        with pytest.raises(IntegrityError):
            with db.sessions.begin() as session:
                session.execute(table.delete().where(where))


def test_budget_subject_binding_and_nonnegative_limits(db):
    db.migrate()
    audit = rows(db)
    table = Base.metadata.tables["program_reservations"]
    value = dict(scope_id="scope", call_id="call", subject_id="subject", purpose="work", effect_key="effect",
        reserved_usd_micro=1, reserved_allowance_micro=1, permit_digest="a" * 64, meter_digest="a" * 64,
        price_digest="a" * 64, permit_expires_ms=10, state="reserved", audit_sequence=audit)
    for change in ({}, {"purpose": "collection", "reserved_usd_micro": -1}):
        with pytest.raises(IntegrityError):
            with db.sessions.begin() as session:
                session.execute(insert(table), {**value, **change})


def test_recovery_quiesce_and_epoch_guard(db):
    db.migrate()
    audit = rows(db)
    tables = Base.metadata.tables
    with db.sessions.begin() as session:
        session.execute(insert(tables["collection_epoch_guards"]), dict(session_id="session", active_epoch=2, revision=2))
        session.execute(insert(tables["collection_operations"]), operation(audit, operation_id="recovery", epoch=2,
            epoch_purpose="recovery", verb="quiesce-v1"))
    with pytest.raises(IntegrityError):
        with db.sessions.begin() as session:
            session.execute(tables["collection_epochs"].delete().where(tables["collection_epochs"].c.epoch == 2))


def test_unknown_reservation_survives_restrictive_references(db):
    db.migrate()
    audit = rows(db)
    tables = Base.metadata.tables
    with db.sessions.begin() as session:
        session.execute(insert(tables["program_reservations"]), dict(scope_id="scope", call_id="call",
            subject_id="subject", purpose="collection", effect_key="effect", reserved_usd_micro=1,
            reserved_allowance_micro=1, permit_digest="a" * 64, meter_digest="a" * 64, price_digest="a" * 64,
            permit_expires_ms=10, state="unknown", audit_sequence=audit))
        session.execute(insert(tables["program_slots"]), dict(scope_id="scope", slot_id="slot", call_id="call",
            subject_id="subject", state="unknown", audit_sequence=audit))
    with pytest.raises(IntegrityError):
        with db.sessions.begin() as session:
            session.execute(tables["program_reservations"].delete())


def test_cancel_and_retention_hold_require_bound_records(db):
    db.migrate()
    audit = rows(db)
    tables = Base.metadata.tables
    with pytest.raises(IntegrityError):
        with db.sessions.begin() as session:
            session.execute(tables["collection_sessions"].update().values(state="cancel_pending"))
    with db.sessions.begin() as session:
        session.execute(tables["collection_sessions"].update().values(state="cancel_pending", cancel_revision=2, cancel_at_ms=2))
    with pytest.raises(IntegrityError):
        with db.sessions.begin() as session:
            session.execute(insert(tables["collection_retention_refs"]), dict(session_id="session", reference_id="hold",
                kind="checkpoint", object_digest="a" * 64, custody_ref="fixture", retain_until_ms=100,
                hold="active", audit_sequence=audit))


def test_retained_ack_cannot_be_orphaned(db):
    db.migrate()
    audit = rows(db)
    t = Base.metadata.tables
    h = "a" * 64
    with db.sessions.begin() as session:
        session.execute(insert(t["collection_operations"]), operation(audit))
        session.execute(insert(t["collection_acknowledgements"]), dict(enrollment_id="enrollment", sequence=1,
            session_id="session", operation_id="op", ack_digest=h, key_id="ack-key", canonical_payload="{}",
            signature="fixture", custody_ref="fixture", event="STARTED", outcome="known", audit_sequence=audit))
        session.execute(insert(t["collection_retention_refs"]), dict(session_id="session", reference_id="ack-hold",
            kind="receipt", object_digest=h, custody_ref="fixture", retain_until_ms=100, hold="active",
            audit_sequence=audit, ack_enrollment_id="enrollment", ack_sequence=1))
    with pytest.raises(IntegrityError):
        with db.sessions.begin() as session:
            session.execute(t["collection_acknowledgements"].delete())


def test_window_and_retention_order_constraints(db):
    db.migrate()
    t = Base.metadata.tables
    h = "a" * 64
    with pytest.raises(IntegrityError):
        with db.sessions.begin() as session:
            session.execute(insert(t["program_budget_scopes"]), dict(id="bad", policy_revision=1,
                window_start_ms=10, window_end_ms=10, usd_limit_micro=0, allowance_unit="units",
                allowance_limit_micro=0, slot_limit=0, policy_digest=h))
    rows(db)
    for name in ("collection_sessions", "collection_epochs"):
        with pytest.raises(IntegrityError):
            with db.sessions.begin() as session:
                session.execute(t[name].update().values(retain_until_ms=1))


def test_operation_deadline_order_and_reconciliation_refusal(db):
    db.migrate()
    audit = rows(db)
    t = Base.metadata.tables
    with db.sessions.begin() as session:
        session.execute(insert(t["collection_epochs"]), dict(session_id="session", epoch=3,
            epoch_purpose="reconciliation", principal_id="principal", authorization_digest="a" * 64,
            deadline_ms=30, retain_until_ms=100, state="authorized"))
    for changes in ({"start_not_after_ms": 11, "execution_not_after_ms": 10},
                    {"epoch": 3, "epoch_purpose": "reconciliation", "verb": "quiesce-v1"}):
        with pytest.raises(IntegrityError):
            with db.sessions.begin() as session:
                session.execute(insert(t["collection_operations"]), operation(audit, **changes))
