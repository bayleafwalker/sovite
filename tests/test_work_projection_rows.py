"""Real-row synthetic checks for the dormant Work comparator."""
import hashlib
import json
import os
import time
import uuid

import pytest
from sqlalchemy import create_engine, insert, make_url, select, text, update

from sovite.collection_transaction import owner_transaction
from sovite.db import Base, Database, Job, MetadataRow, digest
from sovite.lease_service import execution_owner_key
from sovite.repository_runs import RUN_SCHEMA, RepositoryRunAttemptExecutor
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
