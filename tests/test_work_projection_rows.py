"""Real-row synthetic checks for the dormant Work comparator."""
import hashlib
import json
import os
import uuid

import pytest
from sqlalchemy import create_engine, insert, make_url, select, text, update

from sovite.collection_transaction import owner_transaction
from sovite.db import Base, Database, Job, digest
from sovite.lease_service import execution_owner_key
from sovite.work_projection import (
    _WorkProjectionUnavailable,
    _compare_actual_work_projection,
    projection_digest,
)


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
    if url.host or len(query_hosts) != 1 or not query_hosts[0].startswith("/tmp/"):
        pytest.fail("Work projection tests require an explicit Unix socket under /tmp")
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
                if connection.exec_driver_sql("SELECT inet_server_addr() IS NULL").scalar_one() is not True:
                    pytest.fail("Work projection tests require a Unix socket connection")
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
        fence_hash = hashlib.sha256(fence.encode()).hexdigest()
        identity_digest = digest({"source_code": "fixture", "issuer_code": "fixture"})
        lease_digest = digest({
            "job_id": "job", "attempt_id": "attempt", "workload_id": "workload",
            "instance_id": "instance", "generation": generation,
            "fence_sha256": fence_hash,
        })
        payload = {
            "schema_version": "sovite-work-projection/v1",
            "scope_id": "scope", "work_job_id": "job", "subject_id": "job",
            "attempt_id": "attempt", "call_id": "call", "slot_id": "slot",
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
                "attempts": [{"id": "attempt", "state": "running",
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
                lease_until=10.0,
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
                work_job_id="job", work_attempt_id="attempt", work_lease_digest=lease_digest,
                audit_sequence=sequence,
            ))
            session.execute(insert(tables["worker_execution_leases"]), dict(
                job_id="job", workload_id="workload", instance_id="instance",
                fence=fence, generation=generation, phase="started", lease_until=10.0,
                execution_deadline=10.0, created_at=0.0, updated_at=0.0,
            ))
            session.execute(insert(tables["program_work_projection_bindings"]), dict(
                scope_id="scope", call_id="call", subject_id="job", work_job_id="job",
                attempt_id="attempt", slot_id="slot", purpose="work",
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
        "attempts": [{"id": "attempt", "state": "running",
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
