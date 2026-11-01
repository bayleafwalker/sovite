"""Isolated synthetic storage checks; no provider or runtime authority."""
import os
import uuid

import pytest
from sqlalchemy import create_engine, insert, select
from sqlalchemy.exc import IntegrityError

from sovite.db import Base, Database, Job


@pytest.fixture(params=["sqlite", "postgresql"])
def schema(request, tmp_path):
    admin = None
    namespace = None
    if request.param == "sqlite":
        db = Database(f"sqlite:///{tmp_path}/draft.db")
    else:
        url = os.environ.get("SOVITE_TEST_POSTGRES_URL")
        if not url:
            if os.environ.get("SOVITE_LEDGER_SCHEMA_REQUIRE_PG") == "1":
                pytest.fail("SOVITE_TEST_POSTGRES_URL is required for ledger schema tests")
            pytest.skip("isolated PostgreSQL fixture not supplied")
        admin = create_engine(url)
        namespace = "ledger_draft_" + uuid.uuid4().hex
        with admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{namespace}"')
        db = Database(url, schema=namespace)
    db.migrate()
    metadata = Base.metadata
    h = "a" * 64
    from sovite.program_ledger_core import _source_identity_digest
    source_identity = _source_identity_digest("fixture", "fixture")
    with db.sessions.begin() as session:
        audit = db.audit(session, "test", "fixture", "fixture")
        session.flush()
        a = audit.sequence
        session.add(Job(id="job", receipt="job", idempotency_key="job", request_digest="job", caller="job",
                        grant_id="job", generation="job", resolved={}, task="fixture"))
        session.flush()
        t = metadata.tables
        session.execute(insert(t["program_budget_scopes"]), dict(id="scope", policy_revision=1,
            window_start_ms=1, window_end_ms=20, usd_limit_micro=10, allowance_unit="opencode-go-allowance",
            allowance_limit_micro=10, slot_limit=1, policy_digest=h))
        session.execute(insert(t["program_budget_subjects"]), dict(scope_id="scope", subject_id="job",
            purpose="work", binding_digest=h))
        session.execute(insert(t["program_reservations"]), dict(scope_id="scope", call_id="call", subject_id="job",
            purpose="work", effect_key="effect", reserved_usd_micro=1, reserved_allowance_micro=1,
            permit_digest=h, meter_digest=h, price_digest=h, permit_expires_ms=10, state="reserved", audit_sequence=a))
        session.execute(insert(t["program_slots"]), dict(scope_id="scope", slot_id="slot", call_id="call",
            subject_id="job", state="held", audit_sequence=a))
        session.execute(insert(t["program_policy_revisions"]), dict(scope_id="scope", revision=1, policy_digest=h,
            policy_canonical="{}", currency="USD", allowance_unit="opencode-go-allowance", window_start_ms=1, window_end_ms=20,
            usd_limit_micro=10, allowance_limit_micro=10, slot_limit=1, price_policy_digest=h,
            meter_policy_digest=h, permit_policy_digest=h, manifest_digest=h, source_identity_digest=source_identity, audit_sequence=a))
        session.execute(insert(t["program_subject_registry"]), dict(scope_id="scope", purpose="work", subject_id="job",
            binding_digest=h, audit_sequence=a))
        session.execute(insert(t["program_call_intents"]), dict(scope_id="scope", call_id="call", subject_id="job",
            purpose="work", effect_key="effect", idempotency_key="idempotency", request_digest=h, policy_revision=1,
            policy_digest=h, price_digest=h, meter_digest=h, permit_digest=h, source_code="fixture", issuer_code="fixture",
            source_identity_digest=source_identity, observed_ms=1, valid_until_ms=10,
            state="committed", work_job_id="job", work_attempt_id="attempt", work_lease_digest=h, audit_sequence=a))
    yield db, metadata.tables, a
    db.engine.dispose()
    if admin is not None:
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{namespace}" CASCADE')
        admin.dispose()


def test_evidence_precedes_receipt_with_same_call_and_digest(schema):
    db, t, a = schema
    h = "a" * 64
    e = dict(scope_id="scope", evidence_id="ev", kind="call", call_id="call", receipt_digest=h,
             usd_decimal="9223372036854775808", allowance_decimal="1", currency="USD", allowance_unit="units",
             source_digest=h, custody_ref="object", custody_bytes=1, observed_ms=1, audit_sequence=a)
    r = dict(scope_id="scope", call_id="call", receipt_id="receipt", receipt_digest=h, outcome="known",
             evidence_id="ev", currency="USD", allowance_unit="units", source_code="source", issuer_code="issuer",
             observed_ms=1, custody_ref="object", source_digest=h, source_identity_digest=h, custody_bytes=1, audit_sequence=a)
    with db.sessions.begin() as session:
        session.execute(insert(t["program_overflow_evidence"]), e)
        session.execute(insert(t["program_call_receipts"]), r)
    with db.sessions() as session:
        assert session.scalar(select(t["program_call_receipts"].c.evidence_id)) == "ev"
        assert session.scalar(select(t["program_gate_state"].c.scope_id)) is None


def test_partial_evidence_branch_is_rejected(schema):
    db, t, a = schema
    h = "a" * 64
    with pytest.raises(IntegrityError):
        with db.sessions.begin() as session:
            session.execute(insert(t["program_overflow_evidence"]), dict(scope_id="scope", evidence_id="ev",
                kind="call", call_id="call", receipt_digest=None, usd_decimal="1", allowance_decimal="1",
                currency="USD", allowance_unit="units", source_digest=h, custody_ref="object", custody_bytes=1,
                observed_ms=1, audit_sequence=a))


def test_draft_structure_passes_existing_verifier(schema):
    db, tables, _ = schema
    names = ("program_policy_revisions", "program_census_batches", "program_gate_state",
             "program_subject_registry", "program_call_intents", "program_overflow_evidence",
             "program_call_receipts", "program_legacy_links")
    with db.engine.connect() as connection:
        db._structure(connection, [tables[name] for name in names])


def test_duplicate_aggregate_snapshot_is_rejected_without_a_call(schema):
    db, t, a = schema
    h = "a" * 64
    value = dict(scope_id="scope", evidence_id="one", kind="aggregate", policy_revision=1,
        policy_digest=h, snapshot_digest=h, usd_decimal="9223372036854775808", allowance_decimal="1",
        currency="USD", allowance_unit="units", source_digest=h, custody_ref="object", custody_bytes=1,
        observed_ms=1, audit_sequence=a)
    with db.sessions.begin() as session:
        session.execute(insert(t["program_overflow_evidence"]), value)
    with pytest.raises(IntegrityError):
        with db.sessions.begin() as session:
            session.execute(insert(t["program_overflow_evidence"]), {**value, "evidence_id": "two"})


def test_receipt_cannot_reference_evidence_for_another_digest(schema):
    db, t, a = schema
    h = "a" * 64
    with db.sessions.begin() as session:
        session.execute(insert(t["program_overflow_evidence"]), dict(scope_id="scope", evidence_id="ev",
            kind="call", call_id="call", receipt_digest=h, usd_decimal="9223372036854775808", allowance_decimal="1",
            currency="USD", allowance_unit="units", source_digest=h, custody_ref="object", custody_bytes=1,
            observed_ms=1, audit_sequence=a))
    with pytest.raises(IntegrityError):
        with db.sessions.begin() as session:
            session.execute(insert(t["program_call_receipts"]), dict(scope_id="scope", call_id="call",
                receipt_id="receipt", receipt_digest="b" * 64, outcome="known", evidence_id="ev", currency="USD",
                allowance_unit="units", source_code="source", issuer_code="issuer", observed_ms=1,
                custody_ref="object", source_digest=h, source_identity_digest=h, custody_bytes=1, audit_sequence=a))


def test_mark_unknown_retains_reservation_and_slot_and_is_idempotent(schema):
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger import _mark_unknown_in_transaction
    db, t, a = schema
    with db.sessions.begin() as session:
        session.execute(insert(t["program_gate_state"]), dict(scope_id="scope", policy_revision=1,
            policy_digest="a" * 64, mode="disabled", revision=0, audit_sequence=a))
    with owner_transaction(db) as session:
        _mark_unknown_in_transaction(db, session, t, scope_id="scope", call_id="call")
    with owner_transaction(db) as session:
        _mark_unknown_in_transaction(db, session, t, scope_id="scope", call_id="call")
        reservation = session.execute(select(t["program_reservations"])).mappings().one()
        slot = session.execute(select(t["program_slots"])).mappings().one()
        intent = session.execute(select(t["program_call_intents"])).mappings().one()
        assert (reservation.state, slot.state, intent.state) == ("unknown", "unknown", "unknown")
        assert reservation.reserved_usd_micro == reservation.reserved_allowance_micro == 1
        assert reservation.audit_sequence == slot.audit_sequence == intent.audit_sequence
        gate = session.execute(select(t["program_gate_state"])).mappings().one()
        assert (gate.mode, gate.revision, gate.audit_sequence) == ("inhibited", 1, reservation.audit_sequence)


def test_unknown_audit_failure_rolls_back_all_state(schema, monkeypatch):
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger import _mark_unknown_in_transaction
    db, t, _ = schema
    original = db.audit
    def fail_after_flush(session, *args, **kwargs):
        original(session, *args, **kwargs)
        session.flush()
        raise RuntimeError("injected audit failure")
    monkeypatch.setattr(db, "audit", fail_after_flush)
    with pytest.raises(RuntimeError, match="injected"):
        with owner_transaction(db) as session:
            _mark_unknown_in_transaction(db, session, t, scope_id="scope", call_id="call")
    with db.sessions() as session:
        assert session.scalar(select(t["program_reservations"].c.state)) == "reserved"
        assert session.scalar(select(t["program_slots"].c.state)) == "held"
        assert session.scalar(select(t["program_call_intents"].c.state)) == "committed"


def test_missing_gate_reports_no_durable_inhibition(schema):
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger import _mark_unknown_in_transaction
    db, t, _ = schema
    with owner_transaction(db) as session:
        assert _mark_unknown_in_transaction(db, session, t, scope_id="scope", call_id="call") == "unknown_gate_missing"
    with owner_transaction(db) as session:
        assert _mark_unknown_in_transaction(db, session, t, scope_id="scope", call_id="call") == "unknown_gate_missing"
        assert session.scalar(select(t["program_gate_state"].c.scope_id)) is None


def test_unknown_retry_rejects_incomplete_actual_pair(schema):
    from sqlalchemy import update
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger import _mark_unknown_in_transaction
    db, t, _ = schema
    with owner_transaction(db) as session:
        _mark_unknown_in_transaction(db, session, t, scope_id="scope", call_id="call")
    with db.sessions.begin() as session:
        session.execute(update(t["program_reservations"]).values(actual_usd_micro=1))
    with pytest.raises(ValueError, match="incomplete persisted actual pair"):
        with owner_transaction(db) as session:
            _mark_unknown_in_transaction(db, session, t, scope_id="scope", call_id="call")


def test_mixed_obligation_states_are_rejected(schema):
    from sqlalchemy import update
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger import _mark_unknown_in_transaction
    db, t, _ = schema
    with db.sessions.begin() as session:
        session.execute(update(t["program_slots"]).values(state="unknown"))
    with pytest.raises(ValueError, match="incoherent obligation state"):
        with owner_transaction(db) as session:
            _mark_unknown_in_transaction(db, session, t, scope_id="scope", call_id="call")
    with db.sessions() as session:
        assert session.scalar(select(t["program_reservations"].c.state)) == "reserved"


def test_unknown_update_failure_rolls_back_audit_and_obligation(schema):
    from sqlalchemy import event
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger import _mark_unknown_in_transaction
    db, t, _ = schema
    def fail_update(conn, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("UPDATE ") and "PROGRAM_SLOTS" in statement.upper():
            raise RuntimeError("injected slot update failure")
    event.listen(db.engine, "before_cursor_execute", fail_update)
    try:
        with pytest.raises(RuntimeError, match="slot update"):
            with owner_transaction(db) as session:
                _mark_unknown_in_transaction(db, session, t, scope_id="scope", call_id="call")
    finally:
        event.remove(db.engine, "before_cursor_execute", fail_update)
    with owner_transaction(db) as session:
        assert session.scalar(select(t["program_reservations"].c.state)) == "reserved"
        assert session.scalar(select(t["program_slots"].c.state)) == "held"
        assert session.scalar(select(t["program_call_intents"].c.state)) == "committed"


def test_concurrent_unknown_retries_serialize(schema):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger import _mark_unknown_in_transaction
    db, t, a = schema
    with db.sessions.begin() as session:
        session.execute(insert(t["program_gate_state"]), dict(scope_id="scope", policy_revision=1,
            policy_digest="a" * 64, mode="disabled", revision=0, audit_sequence=a))
    barrier = Barrier(2)
    def mark():
        barrier.wait(timeout=10)
        with owner_transaction(db) as session:
            return _mark_unknown_in_transaction(db, session, t, scope_id="scope", call_id="call")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(mark) for _ in range(2)]
        assert [future.result(timeout=20) for future in futures] == ["unknown_gate_row_inhibited_unverified", "unknown_gate_row_inhibited_unverified"]
    with owner_transaction(db) as session:
        gate = session.execute(select(t["program_gate_state"])).mappings().one()
        assert (gate.mode, gate.revision) == ("inhibited", 1)
        assert session.scalar(select(t["program_slots"].c.state)) == "unknown"


@pytest.mark.parametrize("already_unknown", [False, True])
def test_corrupt_gate_cutover_fails_before_update_or_retry(schema, already_unknown):
    from sqlalchemy import update
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger import _mark_unknown_in_transaction
    db, t, a = schema
    with db.sessions.begin() as session:
        session.execute(insert(t["program_gate_state"]), dict(scope_id="scope", policy_revision=1,
            policy_digest="a" * 64, mode="disabled", revision=0, audit_sequence=a))
    if already_unknown:
        with owner_transaction(db) as session:
            _mark_unknown_in_transaction(db, session, t, scope_id="scope", call_id="call")
    # Deliberately damage the disposable database to simulate a missed check.
    with db.engine.begin() as connection:
        if db.engine.dialect.name == "sqlite":
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        else:
            connection.exec_driver_sql(f'ALTER TABLE "{db.schema}".program_gate_state DROP CONSTRAINT ledger_gate_cutover_pair')
        connection.execute(update(t["program_gate_state"]).values(cutover_digest="b" * 64))
        if db.engine.dialect.name == "sqlite":
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
    with pytest.raises(ValueError, match="incomplete persisted gate cutover"):
        with owner_transaction(db) as session:
            _mark_unknown_in_transaction(db, session, t, scope_id="scope", call_id="call")
    with db.sessions() as session:
        assert session.scalar(select(t["program_reservations"].c.state)) == ("unknown" if already_unknown else "reserved")


def test_unknown_commit_failure_preserves_last_durable_hold(schema):
    from sqlalchemy import event
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger import _mark_unknown_in_transaction
    db, t, a = schema
    with db.sessions.begin() as session:
        session.execute(insert(t["program_gate_state"]), dict(scope_id="scope", policy_revision=1,
            policy_digest="a" * 64, mode="disabled", revision=0, audit_sequence=a))
    def fail_commit(session):
        raise RuntimeError("injected pre-commit failure")
    event.listen(db.sessions, "before_commit", fail_commit)
    try:
        with pytest.raises(RuntimeError, match="pre-commit failure"):
            with owner_transaction(db) as session:
                _mark_unknown_in_transaction(db, session, t, scope_id="scope", call_id="call")
    finally:
        event.remove(db.sessions, "before_commit", fail_commit)
    with owner_transaction(db) as session:
        assert session.scalar(select(t["program_reservations"].c.state)) == "reserved"
        assert session.scalar(select(t["program_slots"].c.state)) == "held"
        assert session.scalar(select(t["program_call_intents"].c.state)) == "committed"
        gate = session.execute(select(t["program_gate_state"])).mappings().one()
        assert (gate.mode, gate.revision) == ("disabled", 0)


def test_gate_cutover_composite_rejects_wrong_generation_and_digest(schema):
    from sqlalchemy import update
    db, t, a = schema
    h = "a" * 64
    with db.sessions.begin() as session:
        session.execute(insert(t["program_census_batches"]), dict(scope_id="scope", batch_id="batch",
            policy_revision=1, policy_digest=h, census_generation="c" * 32, snapshot_digest=h,
            fence_digest=h, owner_epoch=1, state="complete", source_count=0, mapped_count=0, unmatched_count=0,
            reserved_usd_micro=0, settled_usd_micro=0, unknown_usd_micro=0, reserved_allowance_micro=0,
            settled_allowance_micro=0, unknown_allowance_micro=0, held_slots=0, unknown_slots=0, audit_sequence=a))
        session.execute(insert(t["program_gate_state"]), dict(scope_id="scope", policy_revision=1, policy_digest=h,
            mode="active", revision=0, cutover_batch_id="batch", cutover_digest=h, cutover_generation="c" * 32,
            cutover_fence_digest=h, cutover_owner_epoch=1, audit_sequence=a))
    for changes in ({"cutover_generation": "d" * 32}, {"cutover_digest": "b" * 64},
                    {"cutover_fence_digest": "b" * 64}, {"cutover_owner_epoch": 2},
                    {"cutover_batch_id": None}, {"cutover_digest": None}, {"cutover_generation": None},
                    {"cutover_fence_digest": None}, {"cutover_owner_epoch": None}):
        with pytest.raises(IntegrityError):
            with db.sessions.begin() as session:
                session.execute(update(t["program_gate_state"]).values(**changes))


def test_gate_keeps_referenced_batch(schema):
    from sqlalchemy import delete
    db, t, a = schema
    h = "a" * 64
    with db.sessions.begin() as session:
        session.execute(insert(t["program_census_batches"]), dict(scope_id="scope", batch_id="batch",
            policy_revision=1, policy_digest=h, census_generation="c" * 32, snapshot_digest=h,
            fence_digest=h, owner_epoch=1, state="complete", source_count=0, mapped_count=0, unmatched_count=0,
            reserved_usd_micro=0, settled_usd_micro=0, unknown_usd_micro=0, reserved_allowance_micro=0,
            settled_allowance_micro=0, unknown_allowance_micro=0, held_slots=0, unknown_slots=0, audit_sequence=a))
        session.execute(insert(t["program_gate_state"]), dict(scope_id="scope", policy_revision=1, policy_digest=h,
            mode="active", revision=0, cutover_batch_id="batch", cutover_digest=h, cutover_generation="c" * 32,
            cutover_fence_digest=h, cutover_owner_epoch=1, audit_sequence=a))
    with pytest.raises(IntegrityError):
        with db.sessions.begin() as session:
            session.execute(delete(t["program_census_batches"]))


@pytest.mark.parametrize("duplicate", ["call", "slot"])
def test_legacy_source_call_and_slot_are_independently_unique(schema, duplicate):
    db, t, a = schema
    h = "a" * 64
    with db.sessions.begin() as session:
        session.execute(insert(t["program_census_batches"]), dict(scope_id="scope", batch_id="batch",
            policy_revision=1, policy_digest=h, census_generation="c" * 32, state="draft",
            source_count=2, mapped_count=0, unmatched_count=2, reserved_usd_micro=0, settled_usd_micro=0,
            unknown_usd_micro=0, reserved_allowance_micro=0, settled_allowance_micro=0,
            unknown_allowance_micro=0, held_slots=0, unknown_slots=0, audit_sequence=a))
        session.execute(insert(t["program_reservations"]), dict(scope_id="scope", call_id="call2", subject_id="job",
            purpose="work", effect_key="effect2", reserved_usd_micro=1, reserved_allowance_micro=1,
            permit_digest=h, meter_digest=h, price_digest=h, permit_expires_ms=10, state="reserved", audit_sequence=a))
        session.execute(insert(t["program_slots"]), dict(scope_id="scope", slot_id="slot2", call_id="call2",
            subject_id="job", state="held", audit_sequence=a))
        session.execute(insert(t["program_legacy_links"]), dict(scope_id="scope", batch_id="batch", link_id="one",
            job_id="job", original_attempt_id="attempt", original_call_id="orig-call", original_slot_id="orig-slot",
            original_digest=h, subject_id="job", call_id="call", slot_id="slot", baseline_state="reserved",
            baseline_slot_state="held", baseline_reserved_usd_micro=1, baseline_reserved_allowance_micro=1, audit_sequence=a))
    original_call = "orig-call" if duplicate == "call" else "other-call"
    original_slot = "orig-slot" if duplicate == "slot" else "other-slot"
    with pytest.raises(IntegrityError):
        with db.sessions.begin() as session:
            session.execute(insert(t["program_legacy_links"]), dict(scope_id="scope", batch_id="batch", link_id="two",
                job_id="job", original_attempt_id="attempt", original_call_id=original_call, original_slot_id=original_slot,
                original_digest=h, subject_id="job", call_id="call2", slot_id="slot2", baseline_state="reserved",
                baseline_slot_state="held", baseline_reserved_usd_micro=1, baseline_reserved_allowance_micro=1, audit_sequence=a))


def test_legacy_valid_fks_do_not_mask_cross_wired_slot(schema):
    from sovite.program_ledger import _validate_locked_legacy_binding
    db, t, a = schema
    h = "a" * 64
    with db.sessions.begin() as session:
        session.execute(insert(t["program_census_batches"]), dict(scope_id="scope", batch_id="batch",
            policy_revision=1, policy_digest=h, census_generation="c" * 32, state="draft",
            source_count=1, mapped_count=0, unmatched_count=1, reserved_usd_micro=0, settled_usd_micro=0,
            unknown_usd_micro=0, reserved_allowance_micro=0, settled_allowance_micro=0,
            unknown_allowance_micro=0, held_slots=0, unknown_slots=0, audit_sequence=a))
        session.execute(insert(t["program_reservations"]), dict(scope_id="scope", call_id="call2", subject_id="job",
            purpose="work", effect_key="effect2", reserved_usd_micro=1, reserved_allowance_micro=1,
            permit_digest=h, meter_digest=h, price_digest=h, permit_expires_ms=10, state="reserved", audit_sequence=a))
        session.execute(insert(t["program_slots"]), dict(scope_id="scope", slot_id="slot2", call_id="call2",
            subject_id="job", state="held", audit_sequence=a))
        session.execute(insert(t["program_legacy_links"]), dict(scope_id="scope", batch_id="batch", link_id="one",
            job_id="job", original_attempt_id="attempt", original_call_id="orig-call", original_slot_id="orig-slot",
            original_digest=h, subject_id="job", call_id="call", slot_id="slot2", baseline_state="reserved",
            baseline_slot_state="held", baseline_reserved_usd_micro=1, baseline_reserved_allowance_micro=1, audit_sequence=a))
    with db.sessions() as session:
        link = session.execute(select(t["program_legacy_links"])).mappings().one()
        batch = session.execute(select(t["program_census_batches"])).mappings().one()
        registry = session.execute(select(t["program_subject_registry"])).mappings().one()
        job = session.execute(select(t["jobs"])).mappings().one()
        reservation = session.execute(select(t["program_reservations"]).where(t["program_reservations"].c.call_id == "call")).mappings().one()
        slot = session.execute(select(t["program_slots"]).where(t["program_slots"].c.slot_id == "slot2")).mappings().one()
        with pytest.raises(ValueError, match="inconsistent legacy binding"):
            _validate_locked_legacy_binding(link, batch, registry, job, reservation, slot)


def _seed_prepare_ready(schema):
    from sqlalchemy import update
    import hashlib
    from sovite.program_accounting import SUPPORTED_ALLOWANCE_UNIT
    from sovite.lease_service import execution_owner_key
    from sovite.db import digest
    db, t, a = schema
    h = "a" * 64
    snapshot = digest([])
    with db.sessions.begin() as session:
        session.execute(update(t["jobs"]).values(status="running", generation="sha256:" + h,
            lease_owner=execution_owner_key(workload_id="workload", instance_id="instance"), lease_until=10.0,
            resolved={"repository_run": {"attempts": [{"id": "attempt", "state": "running",
                "fence_sha256": hashlib.sha256(("d" * 32).encode()).hexdigest()}]}}))
        session.execute(update(t["program_budget_scopes"]).values(allowance_unit=SUPPORTED_ALLOWANCE_UNIT,
            usd_limit_micro=2, allowance_limit_micro=2, slot_limit=2))
        session.execute(update(t["program_policy_revisions"]).values(allowance_unit=SUPPORTED_ALLOWANCE_UNIT,
            usd_limit_micro=2, allowance_limit_micro=2, slot_limit=2))
        session.execute(insert(t["worker_execution_leases"]), dict(job_id="job", workload_id="workload",
            instance_id="instance", fence="d" * 32, generation="sha256:" + h, phase="started", lease_until=10.0,
            execution_deadline=10.0, created_at=0.0, updated_at=0.0))
        session.execute(insert(t["program_census_batches"]), dict(scope_id="scope", batch_id="batch",
            policy_revision=1, policy_digest=h, census_generation="c" * 32, snapshot_digest=snapshot,
            fence_digest=h, owner_epoch=1, state="complete", source_count=0, mapped_count=0, unmatched_count=0,
            reserved_usd_micro=0, settled_usd_micro=0, unknown_usd_micro=0, reserved_allowance_micro=0,
            settled_allowance_micro=0, unknown_allowance_micro=0, held_slots=0, unknown_slots=0, audit_sequence=a))
        session.execute(insert(t["program_gate_state"]), dict(scope_id="scope", policy_revision=1, policy_digest=h,
            mode="active", revision=0, cutover_batch_id="batch", cutover_digest=snapshot, cutover_generation="c" * 32,
            cutover_fence_digest=h, cutover_owner_epoch=1, audit_sequence=a))


def _prepare_args(suffix):
    import hashlib
    from sovite.db import digest
    h = "a" * 64
    from sovite.program_ledger_core import _source_identity_digest
    source_identity = _source_identity_digest("fixture", "fixture")
    lease_digest = digest({"job_id": "job", "attempt_id": "attempt", "workload_id": "workload",
                           "instance_id": "instance", "generation": "sha256:" + h,
                           "fence_sha256": hashlib.sha256(("d" * 32).encode()).hexdigest()})
    return dict(scope_id="scope", subject_id="job", purpose="work", call_id="new-call-" + suffix,
        slot_id="new-slot-" + suffix, effect_key="new-effect-" + suffix, idempotency_key="new-key-" + suffix,
        request_digest=h, usd_micro=1, allowance_micro=1, observed_ms=2, valid_until_ms=9,
        price_digest=h, meter_digest=h, permit_digest=h, source_code="fixture", issuer_code="fixture",
        source_identity_digest=source_identity, context={"attempt_id": "attempt", "lease_digest": lease_digest})


def test_private_prepare_atomic_and_idempotent(schema):
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger_core import _prepare_in_transaction
    db, t, _ = schema
    _seed_prepare_ready(schema)
    args = _prepare_args("a")
    with owner_transaction(db) as session:
        assert _prepare_in_transaction(db, session, t, **args) == ("created", "new-call-a")
    with owner_transaction(db) as session:
        assert _prepare_in_transaction(db, session, t, **args) == ("existing", "new-call-a")
    with pytest.raises(ValueError, match="idempotency conflict"):
        with owner_transaction(db) as session:
            _prepare_in_transaction(db, session, t, **{**args, "usd_micro": 2})
    with db.sessions() as session:
        assert len(session.execute(select(t["program_reservations"])).all()) == 2
        assert len(session.execute(select(t["program_slots"])).all()) == 2
        assert len(session.execute(select(t["program_call_intents"])).all()) == 2


def test_private_prepare_last_capacity_is_serialized(schema):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger_core import _prepare_in_transaction
    db, t, _ = schema
    _seed_prepare_ready(schema)
    barrier = Barrier(2)
    def run(suffix):
        barrier.wait(timeout=10)
        try:
            with owner_transaction(db) as session:
                return _prepare_in_transaction(db, session, t, **_prepare_args(suffix))[0]
        except ValueError as error:
            assert str(error) == "capacity unavailable"
            return "denied"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ("a", "b")))
    assert sorted(results) == ["created", "denied"]
    with db.sessions() as session:
        assert len(session.execute(select(t["program_reservations"])).all()) == 2
        assert len(session.execute(select(t["program_slots"])).all()) == 2
        assert len(session.execute(select(t["program_call_intents"])).all()) == 2


def _receipt_args(actual_usd, actual_allowance):
    from sovite.db import digest
    from sovite.program_accounting import SUPPORTED_ALLOWANCE_UNIT
    from sovite.program_ledger_core import _source_identity_digest
    value = dict(scope_id="scope", call_id="call", receipt_id="receipt", actual_usd=actual_usd,
        actual_allowance=actual_allowance, currency="USD", allowance_unit=SUPPORTED_ALLOWANCE_UNIT,
        source_code="fixture", issuer_code="fixture", source_digest="b" * 64, source_identity_digest=_source_identity_digest("fixture", "fixture"),
        custody_ref="fixture-object", custody_bytes=10, observed_ms=30)
    return {**value, "receipt_digest": digest(value)}


@pytest.mark.parametrize("actual_usd,actual_allowance,unresolved,overflow", [
    ("0", "0", False, False), ("2", "1", True, False),
    ("1", "2", True, False), ("2", "2", True, False),
    (str(1 << 63), "1", True, True),
])
def test_private_known_receipt_preserves_truth_and_obligation(schema, actual_usd, actual_allowance, unresolved, overflow):
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger_core import _record_known_in_transaction
    db, t, a = schema
    with db.sessions.begin() as session:
        session.execute(insert(t["program_gate_state"]), dict(scope_id="scope", policy_revision=1,
            policy_digest="a" * 64, mode="disabled", revision=0, audit_sequence=a))
    args = _receipt_args(actual_usd, actual_allowance)
    with owner_transaction(db) as session:
        result, _ = _record_known_in_transaction(db, session, t, **args)
        assert result == ("known_unresolved_unverified" if unresolved else "known_settled_unverified")
    with owner_transaction(db) as session:
        assert _record_known_in_transaction(db, session, t, **args) == ("existing", "receipt")
        receipt = session.execute(select(t["program_call_receipts"])).mappings().one()
        reservation = session.execute(select(t["program_reservations"])).mappings().one()
        slot = session.execute(select(t["program_slots"])).mappings().one()
        gate = session.execute(select(t["program_gate_state"])).mappings().one()
        assert receipt.outcome == "known"
        assert reservation.state == ("unknown" if unresolved else "settled")
        assert slot.state == ("unknown" if unresolved else "released")
        assert gate.mode == ("inhibited" if unresolved else "disabled")
        if overflow:
            evidence = session.execute(select(t["program_overflow_evidence"])).mappings().one()
            assert (evidence.usd_decimal, evidence.allowance_decimal) == (actual_usd, actual_allowance)
            assert receipt.actual_usd_micro is None
        else:
            assert (receipt.actual_usd_micro, receipt.actual_allowance_micro) == (int(actual_usd), int(actual_allowance))


def test_late_known_receipt_does_not_clear_existing_unknown(schema):
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger import _mark_unknown_in_transaction
    from sovite.program_ledger_core import _record_known_in_transaction
    db, t, _ = schema
    with owner_transaction(db) as session:
        _mark_unknown_in_transaction(db, session, t, scope_id="scope", call_id="call")
    with owner_transaction(db) as session:
        result, _ = _record_known_in_transaction(db, session, t, **_receipt_args("0", "0"))
        assert result == "known_unresolved_unverified"
        assert session.scalar(select(t["program_reservations"].c.state)) == "unknown"
        assert session.scalar(select(t["program_slots"].c.state)) == "unknown"


def test_late_receipt_cannot_overwrite_unknown_actual(schema):
    from sqlalchemy import update
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger import _mark_unknown_in_transaction
    from sovite.program_ledger_core import _record_known_in_transaction
    db, t, _ = schema
    with owner_transaction(db) as session:
        _mark_unknown_in_transaction(db, session, t, scope_id="scope", call_id="call")
        session.execute(update(t["program_reservations"]).values(actual_usd_micro=100, actual_allowance_micro=100))
    with pytest.raises(ValueError, match="conflicting prior actual"):
        with owner_transaction(db) as session:
            _record_known_in_transaction(db, session, t, **_receipt_args("2", "2"))
    with db.sessions() as session:
        row = session.execute(select(t["program_reservations"])).mappings().one()
        assert (row.actual_usd_micro, row.actual_allowance_micro, row.state) == (100, 100, "unknown")
        assert session.execute(select(t["program_call_receipts"])).first() is None


def test_receipt_retry_rejects_changed_immutable_source(schema):
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger_core import _record_known_in_transaction
    db, t, _ = schema
    args = _receipt_args("0", "0")
    with owner_transaction(db) as session:
        _record_known_in_transaction(db, session, t, **args)
    from sovite.db import digest
    changed = {**args, "issuer_code": "different", "source_identity_digest": digest(
        {"source_code": "fixture", "issuer_code": "different"})}
    changed["receipt_digest"] = digest({key: value for key, value in changed.items() if key != "receipt_digest"})
    with pytest.raises(ValueError, match="receipt source or policy unavailable"):
        with owner_transaction(db) as session:
            _record_known_in_transaction(db, session, t, **changed)


def test_prepare_rejects_settled_actual_receipt_mismatch(schema):
    from sqlalchemy import update
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger_core import _prepare_in_transaction, _record_known_in_transaction
    db, t, _ = schema
    _seed_prepare_ready(schema)
    with owner_transaction(db) as session:
        _record_known_in_transaction(db, session, t, **_receipt_args("0", "0"))
    with db.sessions.begin() as session:
        session.execute(update(t["program_reservations"]).values(actual_usd_micro=1))
    with pytest.raises(ValueError, match="settled receipt mismatch"):
        with owner_transaction(db) as session:
            _prepare_in_transaction(db, session, t, **_prepare_args("a"))


def test_prepare_rejects_stale_work_lease_generation(schema):
    from sqlalchemy import update
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger_core import _prepare_in_transaction
    db, t, _ = schema
    _seed_prepare_ready(schema)
    with db.sessions.begin() as session:
        session.execute(update(t["worker_execution_leases"]).values(generation="stale"))
    with pytest.raises(ValueError, match="work context unavailable"):
        with owner_transaction(db) as session:
            _prepare_in_transaction(db, session, t, **_prepare_args("a"))


def test_prepare_fails_closed_on_nonempty_unreconciled_baseline(schema):
    from sqlalchemy import update
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger_core import _prepare_in_transaction
    db, t, _ = schema
    _seed_prepare_ready(schema)
    with db.sessions.begin() as session:
        session.execute(update(t["program_census_batches"]).values(source_count=1, mapped_count=1))
    with pytest.raises(ValueError, match="census source binding mismatch"):
        with owner_transaction(db) as session:
            _prepare_in_transaction(db, session, t, **_prepare_args("a"))


def test_duplicate_receipt_rejects_settled_null_actuals(schema):
    from sqlalchemy import update
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger_core import _record_known_in_transaction
    db, t, _ = schema
    args = _receipt_args("0", "0")
    with owner_transaction(db) as session:
        _record_known_in_transaction(db, session, t, **args)
    with db.sessions.begin() as session:
        session.execute(update(t["program_reservations"]).values(actual_usd_micro=None, actual_allowance_micro=None))
    with pytest.raises(ValueError, match="settled obligation lacks actuals"):
        with owner_transaction(db) as session:
            _record_known_in_transaction(db, session, t, **args)


def _seed_nonempty_fixture_baseline(schema):
    from sqlalchemy import update
    from sovite.db import digest
    db, t, a = schema
    _seed_prepare_ready(schema)
    payload = dict(scope_id="scope", batch_id="batch", link_id="link", job_id="job", original_attempt_id="attempt", original_call_id="legacy-call",
        original_slot_id="legacy-slot", subject_id="job", call_id="call", slot_id="slot",
        baseline_state="reserved", baseline_slot_state="held", baseline_reserved_usd_micro=1,
        baseline_reserved_allowance_micro=1, baseline_actual_usd_micro=None, baseline_actual_allowance_micro=None)
    snapshot = digest([payload])
    with db.sessions.begin() as session:
        session.execute(update(t["program_gate_state"]).values(mode="disabled", cutover_batch_id=None,
            cutover_digest=None, cutover_generation=None, cutover_fence_digest=None, cutover_owner_epoch=None))
        session.execute(insert(t["program_legacy_links"]), dict(original_digest=digest(payload), audit_sequence=a, **payload))
        session.execute(update(t["program_census_batches"]).values(snapshot_digest=snapshot, source_count=1,
            mapped_count=1, unmatched_count=0, reserved_usd_micro=1, reserved_allowance_micro=1, held_slots=1))
        session.execute(update(t["program_gate_state"]).values(mode="active", cutover_batch_id="batch",
            cutover_digest=snapshot, cutover_generation="c" * 32, cutover_fence_digest="a" * 64, cutover_owner_epoch=1))


def test_nonempty_baseline_counts_current_obligation_once(schema):
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger_core import _prepare_in_transaction
    db, t, _ = schema
    _seed_nonempty_fixture_baseline(schema)
    with owner_transaction(db) as session:
        assert _prepare_in_transaction(db, session, t, **_prepare_args("a")) == ("created", "new-call-a")


def test_nonempty_baseline_uses_later_settled_actual_not_baseline_twice(schema):
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger_core import _prepare_in_transaction, _record_known_in_transaction
    db, t, _ = schema
    _seed_nonempty_fixture_baseline(schema)
    with owner_transaction(db) as session:
        _record_known_in_transaction(db, session, t, **_receipt_args("0", "0"))
    with owner_transaction(db) as session:
        assert _prepare_in_transaction(db, session, t, **_prepare_args("a")) == ("created", "new-call-a")


def test_nonempty_baseline_rejects_tampered_mapping(schema):
    from sqlalchemy import update
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger_core import _prepare_in_transaction
    db, t, _ = schema
    _seed_nonempty_fixture_baseline(schema)
    with db.sessions.begin() as session:
        session.execute(update(t["program_legacy_links"]).values(original_call_id="different"))
    with pytest.raises(ValueError, match="census source binding mismatch"):
        with owner_transaction(db) as session:
            _prepare_in_transaction(db, session, t, **_prepare_args("a"))


def test_nonempty_baseline_retains_legacy_without_new_intent(schema):
    from sqlalchemy import delete
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger_core import _prepare_in_transaction
    db, t, _ = schema
    _seed_nonempty_fixture_baseline(schema)
    with db.sessions.begin() as session:
        session.execute(delete(t["program_call_intents"]).where(t["program_call_intents"].c.call_id == "call"))
    with owner_transaction(db) as session:
        assert _prepare_in_transaction(db, session, t, **_prepare_args("a")) == ("created", "new-call-a")


def test_nonempty_baseline_rejects_incorrect_totals(schema):
    from sqlalchemy import update
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger_core import _prepare_in_transaction
    db, t, _ = schema
    _seed_nonempty_fixture_baseline(schema)
    with db.sessions.begin() as session:
        session.execute(update(t["program_census_batches"]).values(reserved_usd_micro=0))
    with pytest.raises(ValueError, match="census total mismatch"):
        with owner_transaction(db) as session:
            _prepare_in_transaction(db, session, t, **_prepare_args("a"))


def test_nonempty_baseline_rejects_changed_reserve_even_with_intent(schema):
    from sqlalchemy import update
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger_core import _prepare_in_transaction
    db, t, _ = schema
    _seed_nonempty_fixture_baseline(schema)
    with db.sessions.begin() as session:
        session.execute(update(t["program_reservations"]).values(reserved_usd_micro=0))
    with pytest.raises(ValueError, match="legacy reserve changed"):
        with owner_transaction(db) as session:
            _prepare_in_transaction(db, session, t, **_prepare_args("a"))


def test_nonempty_unknown_baseline_cannot_regress_actual(schema):
    from sqlalchemy import update
    from sovite.db import digest
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger_core import _prepare_in_transaction
    db, t, _ = schema
    _seed_nonempty_fixture_baseline(schema)
    with db.sessions.begin() as session:
        session.execute(update(t["program_gate_state"]).values(mode="disabled", cutover_batch_id=None,
            cutover_digest=None, cutover_generation=None, cutover_fence_digest=None, cutover_owner_epoch=None))
        link = session.execute(select(t["program_legacy_links"])).mappings().one()
        from sovite.program_ledger_core import _baseline_payload
        payload = _baseline_payload(link)
        payload.update(baseline_state="unknown", baseline_slot_state="unknown",
                       baseline_actual_usd_micro=100, baseline_actual_allowance_micro=100)
        snapshot = digest([payload])
        session.execute(update(t["program_legacy_links"]).values(original_digest=digest(payload), **payload))
        session.execute(update(t["program_census_batches"]).values(snapshot_digest=snapshot,
            reserved_usd_micro=0, reserved_allowance_micro=0, held_slots=0,
            unknown_usd_micro=100, unknown_allowance_micro=100, unknown_slots=1))
        session.execute(update(t["program_gate_state"]).values(mode="active", cutover_batch_id="batch",
            cutover_digest=snapshot, cutover_generation="c" * 32, cutover_fence_digest="a" * 64, cutover_owner_epoch=1))
        session.execute(update(t["program_reservations"]).values(state="unknown", actual_usd_micro=2, actual_allowance_micro=2))
        session.execute(update(t["program_slots"]).values(state="unknown"))
        session.execute(update(t["program_call_intents"]).values(state="unknown"))
    with pytest.raises(ValueError, match="unknown baseline actual regressed"):
        with owner_transaction(db) as session:
            _prepare_in_transaction(db, session, t, **_prepare_args("a"))


def test_private_prepare_work_and_collection_compete_for_last_slot(schema):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sovite.db import digest
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger_core import _prepare_in_transaction
    db, t, _ = schema
    _seed_prepare_ready(schema)
    h = "a" * 64
    with db.sessions.begin() as session:
        session.execute(insert(t["program_budget_subjects"]), dict(scope_id="scope", subject_id="collect",
            purpose="collection", binding_digest=h))
        session.execute(insert(t["program_subject_registry"]), dict(scope_id="scope", subject_id="collect",
            purpose="collection", binding_digest=h, audit_sequence=1))
        session.execute(insert(t["collection_sessions"]), dict(id="collect", principal_id="principal",
            idempotency_key="collection-key", scope_id="scope", workspace_id="workspace", repository_id="repository",
            schema_version="test/v1", purpose="operator_c0_collection", generation="sha256:" + h,
            suite_version="v1", suite_digest=h, seed_digest=h, base_sha=h, paths_digest=h, deployment_digest=h,
            permit_digest=h, immutable_digest=h, immutable_payload={}, deadline_ms=20, retain_until_ms=100,
            state="active", revision=1, current_epoch=1))
        session.execute(insert(t["collection_epochs"]), dict(session_id="collect", epoch=1,
            epoch_purpose="execution", principal_id="principal", authorization_digest=h, deadline_ms=20,
            retain_until_ms=100, state="active"))
        session.execute(insert(t["collection_epoch_guards"]), dict(session_id="collect", active_epoch=1, revision=1))
    collection_args = _prepare_args("collection")
    collection_args.update(subject_id="collect", purpose="collection", context={"epoch": 1, "context_digest": digest(
        {"scope_id": "scope", "session_id": "collect", "epoch": 1, "session_digest": h, "authorization_digest": h})})
    barrier = Barrier(2)
    def run(args):
        barrier.wait(timeout=10)
        try:
            with owner_transaction(db) as session:
                return _prepare_in_transaction(db, session, t, **args)[0]
        except ValueError as error:
            assert str(error) == "capacity unavailable"
            return "denied"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (_prepare_args("work"), collection_args)))
    assert sorted(results) == ["created", "denied"]
    with db.sessions() as session:
        assert len(session.execute(select(t["program_slots"])).all()) == 2
        assert len(session.execute(select(t["program_reservations"])).all()) == 2


def test_prepare_insert_failure_rolls_back_all_rows(schema):
    from sqlalchemy import event
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger_core import _prepare_in_transaction
    db, t, _ = schema
    _seed_prepare_ready(schema)
    def fail_intent(conn, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("INSERT INTO ") and "PROGRAM_CALL_INTENTS" in statement.upper():
            raise RuntimeError("injected intent failure")
    event.listen(db.engine, "before_cursor_execute", fail_intent)
    try:
        with pytest.raises(RuntimeError, match="intent failure"):
            with owner_transaction(db) as session:
                _prepare_in_transaction(db, session, t, **_prepare_args("a"))
    finally:
        event.remove(db.engine, "before_cursor_execute", fail_intent)
    with owner_transaction(db) as session:
        assert len(session.execute(select(t["program_reservations"])).all()) == 1
        assert len(session.execute(select(t["program_slots"])).all()) == 1
        assert len(session.execute(select(t["program_call_intents"])).all()) == 1


def test_known_receipt_overlong_input_preserves_hold(schema):
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger_core import _record_known_in_transaction
    db, t, _ = schema
    with pytest.raises(ValueError, match="canonical"):
        with owner_transaction(db) as session:
            _record_known_in_transaction(db, session, t, **_receipt_args("9" * 129, "1"))
    with db.sessions() as session:
        assert session.scalar(select(t["program_reservations"].c.state)) == "reserved"
        assert session.scalar(select(t["program_slots"].c.state)) == "held"
        assert session.execute(select(t["program_call_receipts"])).first() is None


def test_conflicting_second_known_receipt_does_not_replace_first(schema):
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger_core import _record_known_in_transaction
    db, t, _ = schema
    with owner_transaction(db) as session:
        _record_known_in_transaction(db, session, t, **_receipt_args("0", "0"))
    with pytest.raises(ValueError, match="conflicting prior actual"):
        with owner_transaction(db) as session:
            _record_known_in_transaction(db, session, t, **_receipt_args("2", "2"))
    with db.sessions() as session:
        row = session.execute(select(t["program_call_receipts"])).mappings().one()
        assert (row.actual_usd_micro, row.actual_allowance_micro) == (0, 0)


def test_late_known_after_unknown_receipt_preserves_both_and_hold(schema):
    from sovite.collection_transaction import owner_transaction
    from sovite.db import digest
    from sovite.program_accounting import SUPPORTED_ALLOWANCE_UNIT
    from sovite.program_ledger import _mark_unknown_in_transaction
    from sovite.program_ledger_core import _record_known_in_transaction
    db, t, a = schema
    unknown = dict(scope_id="scope", call_id="call", receipt_id="ambiguous", actual_usd=None,
        actual_allowance=None, currency="USD", allowance_unit=SUPPORTED_ALLOWANCE_UNIT, source_code="fixture",
        issuer_code="fixture", source_digest="c" * 64, source_identity_digest=digest({"source_code": "fixture", "issuer_code": "fixture"}), custody_ref="unknown-object", custody_bytes=10, observed_ms=15)
    with owner_transaction(db) as session:
        _mark_unknown_in_transaction(db, session, t, scope_id="scope", call_id="call")
        session.execute(insert(t["program_call_receipts"]), dict(scope_id="scope", call_id="call",
            receipt_id=unknown["receipt_id"], receipt_digest=digest(unknown), outcome="unknown", currency="USD",
            allowance_unit=SUPPORTED_ALLOWANCE_UNIT, source_code="fixture", issuer_code="fixture",
            source_digest=unknown["source_digest"], source_identity_digest=unknown["source_identity_digest"], custody_ref="unknown-object", custody_bytes=10,
            observed_ms=15, audit_sequence=a))
    with owner_transaction(db) as session:
        assert _record_known_in_transaction(db, session, t, **_receipt_args("0", "0"))[0] == "known_unresolved_unverified"
    with db.sessions() as session:
        assert {row.outcome for row in session.execute(select(t["program_call_receipts"])).mappings()} == {"unknown", "known"}
        assert session.scalar(select(t["program_reservations"].c.state)) == "unknown"
        assert session.scalar(select(t["program_slots"].c.state)) == "unknown"


def test_known_retry_checks_later_sorted_unknown_receipt(schema):
    from sqlalchemy import update
    from sovite.collection_transaction import owner_transaction
    from sovite.db import digest
    from sovite.program_accounting import SUPPORTED_ALLOWANCE_UNIT
    from sovite.program_ledger import _mark_unknown_in_transaction
    from sovite.program_ledger_core import _record_known_in_transaction
    db, t, a = schema
    unknown = dict(scope_id="scope", call_id="call", receipt_id="z-unknown", actual_usd=None,
        actual_allowance=None, currency="USD", allowance_unit=SUPPORTED_ALLOWANCE_UNIT, source_code="fixture",
        issuer_code="fixture", source_digest="c" * 64, source_identity_digest=digest({"source_code": "fixture", "issuer_code": "fixture"}), custody_ref="unknown-object", custody_bytes=10, observed_ms=15)
    with owner_transaction(db) as session:
        _mark_unknown_in_transaction(db, session, t, scope_id="scope", call_id="call")
        session.execute(insert(t["program_call_receipts"]), dict(scope_id="scope", call_id="call",
            receipt_id=unknown["receipt_id"], receipt_digest=digest(unknown), outcome="unknown", currency="USD",
            allowance_unit=SUPPORTED_ALLOWANCE_UNIT, source_code="fixture", issuer_code="fixture",
            source_digest=unknown["source_digest"], source_identity_digest=unknown["source_identity_digest"], custody_ref="unknown-object", custody_bytes=10,
            observed_ms=15, audit_sequence=a))
    args = _receipt_args("0", "0")
    with owner_transaction(db) as session:
        _record_known_in_transaction(db, session, t, **args)
    with db.sessions.begin() as session:
        session.execute(update(t["program_call_receipts"]).where(t["program_call_receipts"].c.receipt_id == "z-unknown")
                        .values(issuer_code="different"))
    with pytest.raises(ValueError, match="invalid unknown receipt"):
        with owner_transaction(db) as session:
            _record_known_in_transaction(db, session, t, **args)


def _seed_observed_aggregate_overflow(schema):
    from sqlalchemy import update
    from sovite.db import canonical, digest
    db, t, a = schema
    _seed_prepare_ready(schema)
    maximum = (1 << 63) - 1
    with db.sessions.begin() as session:
        session.execute(update(t["program_budget_scopes"]).values(usd_limit_micro=maximum, allowance_limit_micro=maximum, slot_limit=3))
        session.execute(update(t["program_policy_revisions"]).values(usd_limit_micro=maximum, allowance_limit_micro=maximum, slot_limit=3))
        h = "a" * 64
        session.execute(insert(t["program_reservations"]), dict(scope_id="scope", call_id="overflow-call", subject_id="job",
            purpose="work", effect_key="overflow-effect", reserved_usd_micro=maximum, reserved_allowance_micro=1,
            permit_digest=h, meter_digest=h, price_digest=h, permit_expires_ms=10, state="reserved", audit_sequence=a))
        session.execute(insert(t["program_slots"]), dict(scope_id="scope", slot_id="overflow-slot", subject_id="job",
            call_id="overflow-call", state="held", audit_sequence=a))
        session.execute(insert(t["program_call_intents"]), dict(scope_id="scope", call_id="overflow-call", subject_id="job",
            purpose="work", effect_key="overflow-effect", idempotency_key="overflow-key", request_digest=h, policy_revision=1,
            policy_digest=h, price_digest=h, meter_digest=h, permit_digest=h, source_code="fixture", issuer_code="fixture",
            source_identity_digest=digest({"source_code": "fixture", "issuer_code": "fixture"}), observed_ms=1, valid_until_ms=10,
            state="committed", work_job_id="job", work_attempt_id="attempt", work_lease_digest=h, audit_sequence=a))
    rows = [("call", "reserved", "1", "1", None, None),
            ("overflow-call", "reserved", str(maximum), "1", None, None)]
    snapshot = digest(rows)
    return dict(evidence_id="aggregate", snapshot_digest=snapshot, source_digest=snapshot,
                custody_ref="fixture-aggregate", custody_bytes=len(canonical(rows)))


def test_existing_aggregate_overflow_records_exact_evidence_and_inhibits(schema):
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger_core import _prepare_in_transaction
    db, t, _ = schema
    custody = _seed_observed_aggregate_overflow(schema)
    with owner_transaction(db) as session:
        assert _prepare_in_transaction(db, session, t, **_prepare_args("new"), aggregate_custody=custody) == (
            "aggregate_overflow_unverified", None)
    with db.sessions() as session:
        evidence = session.execute(select(t["program_overflow_evidence"])).mappings().one()
        gate = session.execute(select(t["program_gate_state"])).mappings().one()
        assert evidence.kind == "aggregate" and evidence.call_id is None and evidence.receipt_digest is None
        assert (evidence.usd_decimal, evidence.allowance_decimal) == (str(1 << 63), "2")
        assert gate.mode == "inhibited"
        assert len(session.execute(select(t["program_reservations"])).all()) == 2
        assert len(session.execute(select(t["program_slots"])).all()) == 2


def test_aggregate_overflow_evidence_failure_preserves_held_rows(schema):
    from sqlalchemy import event
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger_core import _prepare_in_transaction
    db, t, _ = schema
    custody = _seed_observed_aggregate_overflow(schema)
    def fail_evidence(conn, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("INSERT INTO ") and "PROGRAM_OVERFLOW_EVIDENCE" in statement.upper():
            raise RuntimeError("injected evidence failure")
    event.listen(db.engine, "before_cursor_execute", fail_evidence)
    try:
        with pytest.raises(RuntimeError, match="evidence failure"):
            with owner_transaction(db) as session:
                _prepare_in_transaction(db, session, t, **_prepare_args("new"), aggregate_custody=custody)
    finally:
        event.remove(db.engine, "before_cursor_execute", fail_evidence)
    with owner_transaction(db) as session:
        assert session.scalar(select(t["program_gate_state"].c.mode)) == "active"
        assert session.execute(select(t["program_overflow_evidence"])).first() is None
        assert len(session.execute(select(t["program_reservations"])).all()) == 2
        assert len(session.execute(select(t["program_slots"])).all()) == 2


def test_aggregate_overflow_without_custody_does_not_claim_inhibition(schema):
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger_core import _prepare_in_transaction
    db, t, _ = schema
    _seed_observed_aggregate_overflow(schema)
    with pytest.raises(ValueError, match="overflow custody unavailable"):
        with owner_transaction(db) as session:
            _prepare_in_transaction(db, session, t, **_prepare_args("new"))
    with owner_transaction(db) as session:
        assert session.scalar(select(t["program_gate_state"].c.mode)) == "active"
        assert session.execute(select(t["program_overflow_evidence"])).first() is None
        assert len(session.execute(select(t["program_slots"])).all()) == 2


@pytest.mark.parametrize("field", ["custody_bytes", "snapshot_digest", "source_digest"])
def test_aggregate_overflow_rejects_changed_custody_binding(schema, field):
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger_core import _prepare_in_transaction
    db, t, _ = schema
    custody = _seed_observed_aggregate_overflow(schema)
    custody[field] = custody[field] + 1 if field == "custody_bytes" else "f" * 64
    with pytest.raises(ValueError, match="overflow custody unavailable"):
        with owner_transaction(db) as session:
            _prepare_in_transaction(db, session, t, **_prepare_args("new"), aggregate_custody=custody)
    with db.sessions() as session:
        assert session.scalar(select(t["program_gate_state"].c.mode)) == "active"
        assert session.execute(select(t["program_overflow_evidence"])).first() is None


def test_prospective_overflow_does_not_record_fake_observation(schema):
    from sqlalchemy import update
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger_core import _prepare_in_transaction
    db, t, _ = schema
    _seed_prepare_ready(schema)
    maximum = (1 << 63) - 1
    with db.sessions.begin() as session:
        session.execute(update(t["program_budget_scopes"]).values(usd_limit_micro=maximum))
        session.execute(update(t["program_policy_revisions"]).values(usd_limit_micro=maximum))
        session.execute(update(t["program_reservations"]).values(reserved_usd_micro=maximum))
    with pytest.raises(ValueError, match="post-reservation total overflow"):
        with owner_transaction(db) as session:
            _prepare_in_transaction(db, session, t, **_prepare_args("new"))
    with db.sessions() as session:
        assert session.scalar(select(t["program_gate_state"].c.mode)) == "active"
        assert session.execute(select(t["program_overflow_evidence"])).first() is None
        assert len(session.execute(select(t["program_reservations"])).all()) == 1


def test_known_receipt_insert_failure_rolls_back_evidence_and_hold(schema):
    from sqlalchemy import event
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger_core import _record_known_in_transaction
    db, t, _ = schema
    def fail_receipt(conn, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("INSERT INTO ") and "PROGRAM_CALL_RECEIPTS" in statement.upper():
            raise RuntimeError("injected receipt failure")
    event.listen(db.engine, "before_cursor_execute", fail_receipt)
    try:
        with pytest.raises(RuntimeError, match="receipt failure"):
            with owner_transaction(db) as session:
                _record_known_in_transaction(db, session, t, **_receipt_args(str(1 << 63), "1"))
    finally:
        event.remove(db.engine, "before_cursor_execute", fail_receipt)
    with owner_transaction(db) as session:
        assert session.scalar(select(t["program_reservations"].c.state)) == "reserved"
        assert session.scalar(select(t["program_slots"].c.state)) == "held"
        assert session.execute(select(t["program_call_receipts"])).first() is None
        assert session.execute(select(t["program_overflow_evidence"])).first() is None


@pytest.mark.parametrize("operation", ["prepare", "receipt"])
def test_private_operation_commit_failure_retains_last_durable_state(schema, operation):
    from sqlalchemy import event
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger_core import _prepare_in_transaction, _record_known_in_transaction
    db, t, _ = schema
    if operation == "prepare":
        _seed_prepare_ready(schema)
    def fail_commit(session):
        raise RuntimeError("injected commit failure")
    event.listen(db.sessions, "before_commit", fail_commit)
    try:
        with pytest.raises(RuntimeError, match="commit failure"):
            with owner_transaction(db) as session:
                if operation == "prepare":
                    _prepare_in_transaction(db, session, t, **_prepare_args("new"))
                else:
                    _record_known_in_transaction(db, session, t, **_receipt_args("0", "0"))
    finally:
        event.remove(db.sessions, "before_commit", fail_commit)
    with owner_transaction(db) as session:
        assert len(session.execute(select(t["program_reservations"])).all()) == 1
        assert session.scalar(select(t["program_reservations"].c.state)) == "reserved"
        assert session.scalar(select(t["program_slots"].c.state)) == "held"
        assert session.execute(select(t["program_call_receipts"])).first() is None


@pytest.mark.parametrize("table,changes", [
    ("jobs", {"lease_owner": "wrong-owner"}),
    ("jobs", {"lease_until": 9.0}),
    ("worker_execution_leases", {"fence": "bad-fence"}),
    ("worker_execution_leases", {"instance_id": "bad instance"}),
    ("worker_execution_leases", {"workload_id": "bad workload"}),
])
def test_prepare_rejects_mismatched_or_malformed_work_owner(schema, table, changes):
    from sqlalchemy import update
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger_core import _prepare_in_transaction
    db, t, _ = schema
    _seed_prepare_ready(schema)
    with db.sessions.begin() as session:
        session.execute(update(t[table]).values(**changes))
    with pytest.raises(ValueError, match="work context unavailable"):
        with owner_transaction(db) as session:
            _prepare_in_transaction(db, session, t, **_prepare_args("new"))


def test_observed_aggregate_overflow_records_despite_unknown_receipt(schema):
    from sqlalchemy import update
    from sovite.db import canonical, digest
    from sovite.program_accounting import SUPPORTED_ALLOWANCE_UNIT
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger_core import _prepare_in_transaction
    db, t, a = schema
    custody = _seed_observed_aggregate_overflow(schema)
    unknown = dict(scope_id="scope", call_id="call", receipt_id="ambiguous", actual_usd=None,
        actual_allowance=None, currency="USD", allowance_unit=SUPPORTED_ALLOWANCE_UNIT, source_code="fixture",
        issuer_code="fixture", source_digest="c" * 64, source_identity_digest=digest({"source_code": "fixture", "issuer_code": "fixture"}), custody_ref="unknown-object", custody_bytes=10, observed_ms=1)
    with db.sessions.begin() as session:
        session.execute(update(t["program_reservations"]).where(t["program_reservations"].c.call_id == "call").values(state="unknown"))
        session.execute(update(t["program_slots"]).where(t["program_slots"].c.call_id == "call").values(state="unknown"))
        session.execute(update(t["program_call_intents"]).where(t["program_call_intents"].c.call_id == "call").values(state="unknown"))
        session.execute(insert(t["program_call_receipts"]), dict(scope_id="scope", call_id="call",
            receipt_id="ambiguous", receipt_digest=digest(unknown), outcome="unknown", currency="USD",
            allowance_unit=SUPPORTED_ALLOWANCE_UNIT, source_code="fixture", issuer_code="fixture", source_digest="c" * 64, source_identity_digest=unknown["source_identity_digest"],
            custody_ref="unknown-object", custody_bytes=10, observed_ms=1, audit_sequence=a))
    rows = [("call", "unknown", "1", "1", None, None),
            ("overflow-call", "reserved", str((1 << 63) - 1), "1", None, None)]
    custody.update(snapshot_digest=digest(rows), source_digest=digest(rows), custody_bytes=len(canonical(rows)))
    with owner_transaction(db) as session:
        assert _prepare_in_transaction(db, session, t, **_prepare_args("new"), aggregate_custody=custody) == (
            "aggregate_overflow_unverified", None)
    with db.sessions() as session:
        assert session.scalar(select(t["program_gate_state"].c.mode)) == "inhibited"
        assert session.scalar(select(t["program_overflow_evidence"].c.usd_decimal)) == str(1 << 63)


def test_prepare_rejects_nonnumeric_persisted_gate_revision(schema):
    from sqlalchemy import update
    from sqlalchemy.exc import DataError
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger_core import _prepare_in_transaction
    db, t, _ = schema
    _seed_prepare_ready(schema)
    if db.engine.dialect.name == "postgresql":
        with pytest.raises(DataError):
            with db.sessions.begin() as session:
                session.execute(update(t["program_gate_state"]).values(revision="invalid"))
        return
    with db.engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        connection.execute(update(t["program_gate_state"]).values(revision="invalid"))
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
    with pytest.raises(ValueError, match="invalid policy or gate revision"):
        with owner_transaction(db) as session:
            _prepare_in_transaction(db, session, t, **_prepare_args("new"))


def test_policy_manifest_and_source_identity_remain_distinct(schema):
    from sovite.program_ledger_core import _source_identity_digest
    db, t, _ = schema
    with db.sessions() as session:
        policy = session.execute(select(t["program_policy_revisions"])).mappings().one()
        intent = session.execute(select(t["program_call_intents"])).mappings().one()
        assert policy.manifest_digest == "a" * 64
        assert policy.source_identity_digest == _source_identity_digest("fixture", "fixture")
        assert policy.source_identity_digest != policy.manifest_digest
        assert intent.source_identity_digest == policy.source_identity_digest


def test_prepare_rejects_mutated_historical_intent_source_even_if_receipt_matches(schema):
    from sqlalchemy import update
    from sovite.db import digest
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger_core import _prepare_in_transaction, _record_known_in_transaction
    db, t, _ = schema
    _seed_prepare_ready(schema)
    args = _receipt_args("0", "0")
    with owner_transaction(db) as session:
        _record_known_in_transaction(db, session, t, **args)
    changed_identity = digest({"source_code": "fixture", "issuer_code": "different"})
    altered = {**args, "issuer_code": "different", "source_identity_digest": changed_identity}
    changed_digest = digest({key: value for key, value in altered.items() if key != "receipt_digest"})
    with db.sessions.begin() as session:
        session.execute(update(t["program_call_intents"]).values(issuer_code="different", source_identity_digest=changed_identity))
        session.execute(update(t["program_call_receipts"]).values(issuer_code="different", source_identity_digest=changed_identity,
                                                                    receipt_digest=changed_digest))
    with pytest.raises(ValueError, match="historical intent policy binding mismatch"):
        with owner_transaction(db) as session:
            _prepare_in_transaction(db, session, t, **_prepare_args("new"))


@pytest.mark.parametrize("field", ["price_digest", "meter_digest", "permit_digest"])
def test_late_receipt_rejects_corrupted_original_policy_binding(schema, field):
    from sqlalchemy import update
    from sovite.collection_transaction import owner_transaction
    from sovite.program_ledger_core import _record_known_in_transaction
    db, t, _ = schema
    with db.sessions.begin() as session:
        session.execute(update(t["program_call_intents"]).values({field: "f" * 64}))
    with pytest.raises(ValueError, match="receipt source or policy unavailable"):
        with owner_transaction(db) as session:
            _record_known_in_transaction(db, session, t, **_receipt_args("0", "0"))
    with db.sessions() as session:
        assert session.scalar(select(t["program_reservations"].c.state)) == "reserved"
        assert session.scalar(select(t["program_slots"].c.state)) == "held"
        assert session.execute(select(t["program_call_receipts"])).first() is None
