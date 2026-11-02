"""Disposable fixture-only read-only census tests; no live authority."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from threading import Event
import uuid

import pytest
from sqlalchemy import create_engine, event, insert, select

from sovite.db import Base, Database, Job
from sovite import census_report
from sovite.census_report import CensusIncomplete, _capture_fixture_report


@pytest.fixture(params=["sqlite", "postgresql"])
def db(request, tmp_path):
    admin = None
    namespace = None
    if request.param == "sqlite":
        database = Database(f"sqlite:///{tmp_path}/census.db")
    else:
        url = os.environ.get("SOVITE_TEST_POSTGRES_URL")
        if not url:
            if os.environ.get("SOVITE_CENSUS_REQUIRE_PG") == "1":
                pytest.fail("SOVITE_TEST_POSTGRES_URL is required for census tests")
            pytest.skip("isolated PostgreSQL fixture not supplied")
        admin = create_engine(url)
        namespace = "census_fixture_" + uuid.uuid4().hex
        with admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{namespace}"')
        database = Database(url, schema=namespace)
    database.migrate()
    try:
        yield database
    finally:
        database.engine.dispose()
        if admin is not None:
            with admin.begin() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA "{namespace}" CASCADE')
            admin.dispose()


def _job(identifier="private-job-canary", resolved=None):
    if resolved is None:
        resolved = {"repository_run": {"attempts": [{"id": "private-attempt-canary", "state": "running"}],
            "budget_reservations": [{"call_id": "private-call-canary", "reserved_usd": 1.5,
                                     "attempt_id": "private-attempt-canary", "slot_id": "private-slot-canary", "state": "reserved"}],
            "active_slots": ["private-slot-canary"]}}
    return Job(id=identifier, receipt=identifier, idempotency_key=identifier, request_digest=identifier,
               caller="fixture", grant_id="fixture", generation="fixture", resolved=resolved,
               task="private-task-canary", status="failed")


def _seed(db):
    t = Base.metadata.tables
    h = "a" * 64
    with db.sessions.begin() as session:
        audit = db.audit(session, "fixture", "seed", "fixture")
        session.flush()
        a = audit.sequence
        session.add(_job())
        session.flush()
        session.execute(insert(t["program_budget_scopes"]), dict(id="private-scope-canary", policy_revision=1,
            window_start_ms=1, window_end_ms=20, usd_limit_micro=10, allowance_unit="opencode-go-allowance",
            allowance_limit_micro=10, slot_limit=2, policy_digest=h))
        for subject, purpose in (("private-job-canary", "work"), ("private-collection-canary", "collection")):
            session.execute(insert(t["program_budget_subjects"]), dict(scope_id="private-scope-canary", subject_id=subject,
                purpose=purpose, binding_digest=h))
            session.execute(insert(t["program_subject_registry"]), dict(scope_id="private-scope-canary", subject_id=subject,
                purpose=purpose, binding_digest=h, audit_sequence=a))
        session.execute(insert(t["collection_sessions"]), dict(id="private-collection-canary", principal_id="principal",
            idempotency_key="key", scope_id="private-scope-canary", workspace_id="workspace", repository_id="repository",
            schema_version="test/v1", purpose="operator_c0_collection", generation="sha256:" + h,
            suite_version="v1", suite_digest=h, seed_digest=h, base_sha=h, paths_digest=h, deployment_digest=h,
            permit_digest=h, immutable_digest=h, immutable_payload={"scope_id":"private-scope-canary"},
            deadline_ms=20, retain_until_ms=100, state="active", revision=1, current_epoch=1))
        session.execute(insert(t["collection_epochs"]), dict(session_id="private-collection-canary", epoch=1,
            epoch_purpose="execution", principal_id="principal", authorization_digest=h, deadline_ms=20,
            retain_until_ms=100, state="active"))
        session.execute(insert(t["collection_epoch_guards"]), dict(session_id="private-collection-canary", active_epoch=1, revision=1))
        for subject, purpose, call, slot in (("private-job-canary", "work", "work-call", "work-slot"),
                                              ("private-collection-canary", "collection", "collection-call", "collection-slot")):
            session.execute(insert(t["program_reservations"]), dict(scope_id="private-scope-canary", call_id=call,
                subject_id=subject, purpose=purpose, effect_key=call, reserved_usd_micro=1, reserved_allowance_micro=1,
                permit_digest=h, meter_digest=h, price_digest=h, permit_expires_ms=10, state="reserved", audit_sequence=a))
            session.execute(insert(t["program_slots"]), dict(scope_id="private-scope-canary", slot_id=slot, subject_id=subject,
                call_id=call, state="held", audit_sequence=a))
        session.execute(insert(t["program_policy_revisions"]), dict(scope_id="private-scope-canary", revision=1,
            policy_digest=h, policy_canonical="{}", currency="USD", allowance_unit="opencode-go-allowance",
            window_start_ms=1, window_end_ms=20, usd_limit_micro=10, allowance_limit_micro=10, slot_limit=2,
            price_policy_digest=h, meter_policy_digest=h, permit_policy_digest=h, manifest_digest=h,
            source_identity_digest=h, audit_sequence=a))
        session.execute(insert(t["program_census_batches"]), dict(scope_id="private-scope-canary", batch_id="batch",
            policy_revision=1, policy_digest=h, census_generation="c" * 32, state="draft", source_count=1,
            mapped_count=0, unmatched_count=1, reserved_usd_micro=0, settled_usd_micro=0, unknown_usd_micro=0,
            reserved_allowance_micro=0, settled_allowance_micro=0, unknown_allowance_micro=0,
            held_slots=0, unknown_slots=0, audit_sequence=a))
        session.execute(insert(t["program_legacy_links"]), dict(scope_id="private-scope-canary", batch_id="batch", link_id="link",
            job_id="private-job-canary", original_attempt_id="private-attempt-canary", original_call_id="private-call-canary",
            original_slot_id="private-slot-canary", original_digest=h, subject_id="private-job-canary", call_id="work-call",
            slot_id="work-slot", baseline_state="reserved", baseline_slot_state="held", baseline_reserved_usd_micro=1,
            baseline_reserved_allowance_micro=1, audit_sequence=a))


def test_fixture_scan_is_deterministic_private_and_deduplicated(db):
    _seed(db)
    first = _capture_fixture_report(db, page_size=1)
    second = _capture_fixture_report(db, page_size=64)
    assert first == second
    assert first["scan_complete"] and first["cutover_ready"] is False
    assert (first["source_auth"], first["fence"], first["projection_verification"], first["external_custody"]) == (
        "unverified", "unverified", "unavailable", "uninspected")
    assert first["diagnostics"]["canonical_obligations"] == 2
    assert first["diagnostics"]["deduplicated_work_matches"] == 1
    assert first["diagnostics"]["unmatched_work_calls"] == 0
    assert first["diagnostics"]["issues"]["collection_obligations_without_job"] == 1
    assert first["diagnostics"]["issues"]["projection_unverifiable"] == 1
    assert "canary" not in json.dumps(first)
    assert "free_slots" not in json.dumps(first)


def test_empty_fixture_is_never_cutover_ready(db):
    report = _capture_fixture_report(db)
    assert report["stream_counts"]["jobs"] == 0
    assert report["cutover_ready"] is False and report["source_auth"] == "unverified"


def test_oversized_json_rejected_before_any_page_fetch(db, monkeypatch):
    with db.sessions.begin() as session:
        session.add(_job(resolved={"private-canary": "x" * (census_report._MAX_FIELD_BYTES + 1)}))
    called = []
    original = census_report._scan_stream
    def track(*args, **kwargs):
        called.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(census_report, "_scan_stream", track)
    with pytest.raises(CensusIncomplete, match="^census incomplete$") as error:
        _capture_fixture_report(db)
    assert not called and "canary" not in str(error.value)


def test_aggregate_bytes_rejected_before_any_page_fetch(db, monkeypatch):
    with db.sessions.begin() as session:
        for index in range(15):
            session.add(_job(identifier=f"job-{index:03}", resolved={"body": "x" * 200}))
    called = []
    monkeypatch.setattr(census_report, "_MAX_TOTAL_BYTES", 2000)
    monkeypatch.setattr(census_report, "_scan_stream", lambda *args, **kwargs: called.append(True))
    with pytest.raises(CensusIncomplete):
        _capture_fixture_report(db)
    assert not called


def test_invalid_page_size_is_rejected(db):
    for value in (0, -1, 129, True, "1"):
        with pytest.raises(CensusIncomplete):
            _capture_fixture_report(db, page_size=value)


def test_scan_uses_one_read_only_snapshot_during_writer_commit(db, monkeypatch):
    began, committed = Event(), Event()
    original = census_report._preflight
    first = True
    def barrier(*args, **kwargs):
        nonlocal first
        if first:
            first = False
            began.set()
            assert committed.wait(15)
        return original(*args, **kwargs)
    monkeypatch.setattr(census_report, "_preflight", barrier)
    def scan():
        return _capture_fixture_report(db)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(scan)
        assert began.wait(15)
        with db.sessions.begin() as session:
            session.add(_job(identifier="later-job", resolved={}))
        committed.set()
        report = future.result(timeout=30)
    assert report["stream_counts"]["jobs"] == 0
    with db.sessions() as session:
        assert len(session.execute(select(Job)).all()) == 1


def test_scan_does_not_write_or_rewire_product(db):
    statements = []
    def capture(conn, cursor, statement, parameters, context, many):
        statements.append(statement)
    event.listen(db.engine, "before_cursor_execute", capture)
    try:
        _capture_fixture_report(db)
    finally:
        event.remove(db.engine, "before_cursor_execute", capture)
    assert statements
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER")) for sql in statements)
    import ast
    from pathlib import Path
    import sovite
    for path in Path(sovite.__file__).parent.glob("*.py"):
        if path.name == "census_report.py":
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(not alias.name.endswith("census_report") for alias in node.names)
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").endswith("census_report")
                assert all(alias.name != "census_report" for alias in node.names)


def test_wrong_collection_scope_and_collision_are_reported(db):
    from sqlalchemy import update
    _seed(db)
    t = Base.metadata.tables
    with db.sessions.begin() as session:
        session.execute(insert(t["program_budget_scopes"]), dict(id="second-scope", policy_revision=1,
            window_start_ms=1, window_end_ms=20, usd_limit_micro=10, allowance_unit="opencode-go-allowance",
            allowance_limit_micro=10, slot_limit=2, policy_digest="a" * 64))
        session.execute(update(t["collection_sessions"]).values(scope_id="second-scope"))
        session.execute(insert(t["program_budget_subjects"]), dict(scope_id="second-scope",
            subject_id="private-job-canary", purpose="collection", binding_digest="a" * 64))
    report = _capture_fixture_report(db, page_size=1)
    assert report["diagnostics"]["issues"]["collection_scope_mismatch"] >= 1
    assert report["diagnostics"]["issues"]["subject_collisions"] == 1
    assert report["cutover_ready"] is False
    assert "private-job-canary" not in json.dumps(report)


def test_retention_reference_without_job_is_enumerated(db):
    _seed(db)
    t = Base.metadata.tables
    with db.sessions.begin() as session:
        session.execute(insert(t["collection_retention_refs"]), dict(session_id="private-collection-canary",
            reference_id="private-ref-canary", kind="resource", object_digest="a" * 64, custody_ref="private-custody-canary",
            retain_until_ms=100, hold="active", audit_sequence=1))
    report = _capture_fixture_report(db)
    assert report["stream_counts"]["collection_retention_refs"] == 1
    assert report["diagnostics"]["issues"]["collection_obligations_without_job"] == 1
    assert report["external_custody"] == "uninspected"
    assert "private-custody-canary" not in json.dumps(report)


def test_read_only_transaction_rejects_injected_write(db, monkeypatch):
    original = census_report._scan_stream
    def inject(connection, table, *args):
        if table.name == "jobs":
            connection.execute(insert(Base.metadata.tables["platform_metadata"]), dict(key="forbidden", value={}))
        return original(connection, table, *args)
    monkeypatch.setattr(census_report, "_scan_stream", inject)
    with pytest.raises(CensusIncomplete, match="^census incomplete$"):
        _capture_fixture_report(db)
    with db.sessions() as session:
        assert session.execute(select(Base.metadata.tables["platform_metadata"]).where(
            Base.metadata.tables["platform_metadata"].c.key == "forbidden")).first() is None


def test_query_only_state_restored_after_success_and_failure(db, monkeypatch):
    if db.engine.dialect.name != "sqlite":
        pytest.skip("SQLite-specific query_only state")
    with db.engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA query_only=ON")
        connection.commit()
    assert _capture_fixture_report(db)["scan_complete"]
    with db.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA query_only").scalar_one() == 1
    def fail(*args, **kwargs):
        raise RuntimeError("private-error-canary")
    monkeypatch.setattr(census_report, "_scan_stream", fail)
    with pytest.raises(CensusIncomplete, match="^census incomplete$") as error:
        _capture_fixture_report(db)
    assert "private-error-canary" not in str(error.value)
    with db.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA query_only").scalar_one() == 1
        connection.exec_driver_sql("PRAGMA query_only=OFF")
        connection.commit()


def test_sql_failure_is_sanitized_and_returns_no_partial_report(db, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("private-sql-canary")
    monkeypatch.setattr(census_report, "_scan_stream", fail)
    with pytest.raises(CensusIncomplete, match="^census incomplete$") as error:
        _capture_fixture_report(db)
    assert "private-sql-canary" not in str(error.value)
    assert _capture_fixture_report.__name__ not in str(error.value)


def test_binary_collation_keyset_is_deterministic_for_unicode(db):
    with db.sessions.begin() as session:
        for identifier in ("é-job", "a-job", "z-job", "Å-job"):
            session.add(_job(identifier=identifier, resolved={}))
    first = _capture_fixture_report(db, page_size=1)
    second = _capture_fixture_report(db, page_size=3)
    assert first == second
    assert first["stream_counts"]["jobs"] == 4
    assert first["dialect"] in {"sqlite", "postgresql"}


def test_bad_or_duplicate_json_is_never_emitted(db):
    with db.sessions.begin() as session:
        session.add(_job(resolved=[]))
    report = _capture_fixture_report(db)
    assert report["diagnostics"]["issues"]["malformed_work_json"] == 1
    assert "private-job-canary" not in json.dumps(report)


def test_unsupported_page_and_byte_limits_return_no_digest(db, monkeypatch):
    monkeypatch.setattr(census_report, "_MAX_TOTAL_ROWS", 0)
    with pytest.raises(CensusIncomplete) as error:
        _capture_fixture_report(db)
    assert str(error.value) == "census incomplete"


def test_cross_wired_source_slot_and_attempt_are_not_matched(db):
    from sqlalchemy import update
    _seed(db)
    resolved = {"repository_run": {
        "attempts": [{"id": "private-attempt-canary", "state": "running"}, {"id": "other-attempt", "state": "running"}],
        "budget_reservations": [{"call_id": "private-call-canary", "attempt_id": "other-attempt",
                                 "slot_id": "other-slot", "state": "reserved"}],
        "active_slots": ["private-slot-canary", "other-slot"]}}
    with db.sessions.begin() as session:
        session.execute(update(Job).values(resolved=resolved))
    report = _capture_fixture_report(db)
    assert report["diagnostics"]["deduplicated_work_matches"] == 0
    assert report["diagnostics"]["unmatched_work_calls"] == 1
    assert report["diagnostics"]["issues"]["legacy_link_unresolved"] == 1
    assert report["diagnostics"]["issues"]["unmatched_work_slots"] == 2


def test_ambiguous_markers_slots_and_numeric_values_are_unresolved(db):
    value = {"repository_run": {"attempts": [],
        "budget_reservations": [{"call_id": "call", "state": "bad", "reserved_usd": -1}],
        "active_slots": ["slot"], "draft_calls": [{"call_id": "draft", "state": "unknown"}]}}
    with db.sessions.begin() as session:
        session.add(_job(resolved=value))
    issues = _capture_fixture_report(db)["diagnostics"]["issues"]
    assert issues["legacy_numeric_ambiguous"] == 1
    assert issues["malformed_reservation_state"] == 1
    assert issues["patch_markers_unverified"] == 1
    assert issues["unmatched_work_slots"] == 1
    assert issues["work_calls_without_attempts"] == 1


@pytest.mark.parametrize("shape", ["depth", "nodes"])
def test_json_parser_bounds_are_enforced_before_report(db, shape):
    if shape == "depth":
        value = 0
        for _ in range(census_report._MAX_JSON_DEPTH + 1):
            value = [value]
    else:
        value = [0] * (census_report._MAX_JSON_NODES + 1)
    with db.sessions.begin() as session:
        session.add(_job(resolved=value))
    with pytest.raises(CensusIncomplete, match="^census incomplete$"):
        _capture_fixture_report(db)


def test_missing_collection_session_is_unresolved(db):
    _seed(db)
    with db.sessions.begin() as session:
        session.execute(insert(Base.metadata.tables["program_budget_subjects"]), dict(
            scope_id="private-scope-canary", subject_id="missing-session", purpose="collection", binding_digest="a" * 64))
    report = _capture_fixture_report(db)
    assert report["diagnostics"]["issues"]["collection_subject_without_session"] == 1
    assert report["cutover_ready"] is False


def test_cleanup_close_failure_is_sanitized(db, monkeypatch):
    connect = db.engine.connect
    class BrokenClose:
        def __init__(self, actual):
            self.actual = actual
        def __getattr__(self, name):
            return getattr(self.actual, name)
        def execution_options(self, **options):
            self.actual = self.actual.execution_options(**options)
            return self
        def close(self):
            self.actual.close()
            raise RuntimeError("private-close-canary")
    monkeypatch.setattr(db.engine, "connect", lambda: BrokenClose(connect()))
    with pytest.raises(CensusIncomplete, match="^census incomplete$") as error:
        _capture_fixture_report(db)
    assert "private-close-canary" not in str(error.value)


def test_query_only_restored_on_interruption(db, monkeypatch):
    if db.engine.dialect.name != "sqlite":
        pytest.skip("SQLite-specific query_only state")
    def stop(*args, **kwargs):
        raise KeyboardInterrupt()
    monkeypatch.setattr(census_report, "_scan_stream", stop)
    with pytest.raises(KeyboardInterrupt):
        _capture_fixture_report(db)
    with db.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA query_only").scalar_one() == 0


def test_json_bounds_stop_before_decoder(monkeypatch):
    value = '[' * (census_report._MAX_JSON_DEPTH + 1) + '0' + ']' * (census_report._MAX_JSON_DEPTH + 1)
    def decoder_must_not_run(*args, **kwargs):
        raise AssertionError('unbounded decoder was called')
    monkeypatch.setattr(census_report.json, 'loads', decoder_must_not_run)
    with pytest.raises(census_report._BoundExceeded):
        census_report._decode_json(value)


def test_disabled_sqlite_foreign_keys_inhibit_scan(db):
    if db.engine.dialect.name != 'sqlite':
        pytest.skip('SQLite-specific foreign-key state')
    with db.engine.connect() as connection:
        connection.exec_driver_sql('PRAGMA foreign_keys = OFF')
    try:
        with pytest.raises(CensusIncomplete, match='^census incomplete$'):
            _capture_fixture_report(db)
    finally:
        with db.engine.connect() as connection:
            connection.exec_driver_sql('PRAGMA foreign_keys = ON')


@pytest.mark.parametrize('mutation', ['none', 'scope', 'revision', 'digest', 'immutable', 'request_digest', 'workspace'])
def test_collection_owner_frozen_binding_and_policy(db, mutation):
    import importlib.util
    from pathlib import Path
    from sqlalchemy import update
    from sovite.collection_owner import _binding, immutable_payload_digest
    spec = importlib.util.spec_from_file_location('owner_fixture', Path(__file__).with_name('test_collection_owner_cancel.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.fixture(db)
    table = Base.metadata.tables
    h = '6' * 64
    with db.sessions.begin() as session:
        audit = db.audit(session, 'fixture', 'seed-policy', 'fixture')
        session.flush()
        session.execute(insert(table['program_policy_revisions']), dict(scope_id='scope', revision=1,
            policy_digest=h, policy_canonical='{}', currency='USD', allowance_unit='unit',
            window_start_ms=0, window_end_ms=1000, usd_limit_micro=0, allowance_limit_micro=0, slot_limit=0,
            price_policy_digest=h, meter_policy_digest=h, permit_policy_digest=h, manifest_digest=h,
            source_identity_digest=h, audit_sequence=audit.sequence))
        row = session.execute(select(table['collection_sessions'])).mappings().one()
        _binding(row)
        payload = dict(row['immutable_payload'])
        payload['policy'] = dict(payload['policy'])
        if mutation == 'scope':
            payload['policy']['scope_id'] = 'different'
        elif mutation == 'revision':
            payload['policy']['scope_policy_revision'] = 2
        elif mutation == 'digest':
            payload['policy']['scope_policy_digest'] = '7' * 64
        elif mutation == 'immutable':
            payload['policy']['seed_digest'] = 'b' * 64
        elif mutation == 'request_digest':
            payload['request_digest'] = '0' * 64
        elif mutation == 'workspace':
            request = json.loads(payload['request_canonical'])
            request['workspace_id'] = 'different'
            from sovite.collection_identity import create_request_identity
            _, payload['request_canonical'], payload['request_digest'] = create_request_identity(json.dumps(request).encode())
        if mutation != 'none':
            values = {'immutable_payload': payload}
            if mutation != 'immutable':
                values['immutable_digest'] = immutable_payload_digest(payload)
            session.execute(update(table['collection_sessions']).values(**values))
    issues = _capture_fixture_report(db)['diagnostics']['issues']
    assert issues.get('collection_immutable_mismatch', 0) == int(mutation in {'scope', 'immutable', 'request_digest', 'workspace'})
    assert issues.get('collection_policy_mismatch', 0) == int(mutation == 'digest')
    assert issues.get('collection_policy_unverified', 0) == int(mutation == 'revision')


def test_settled_without_actual_is_unresolved(db):
    from sqlalchemy import update
    _seed(db)
    with db.engine.begin() as connection:
        connection.execute(update(Base.metadata.tables['program_reservations']).where(
            Base.metadata.tables['program_reservations'].c.call_id == 'work-call').values(state='settled'))
    assert _capture_fixture_report(db)['diagnostics']['issues']['incoherent_reservation_state'] == 1


def test_corrupt_gate_and_receipt_branches_are_unresolved():
    data = {name: [] for name in census_report._FIELDS}
    data['program_gate_state'] = [dict(scope_id='x', mode='active', cutover_batch_id=None,
        cutover_generation=None, cutover_digest=None, cutover_fence_digest=None, cutover_owner_epoch=None)]
    data['program_call_receipts'] = [dict(scope_id='x', call_id='c', receipt_id='r', outcome='bogus',
        actual_usd_micro=None, actual_allowance_micro=None, evidence_id=None)]
    for name in ('program_gate_state', 'program_call_receipts'):
        for field in census_report._FIELDS[name]:
            data[name][0].setdefault(field, None)
    # This exercises malformed rows without altering the disposable schema.
    issues = census_report._classify(data)['issues']
    assert issues['invalid_gate_state'] == 1
    assert issues['invalid_receipt_branch'] == 1


def test_repair_lineage_detects_existing_wrong_root(db):
    def repair(parent, root):
        return {'repository_run': {'repair': {'parent_run_id': parent, 'root_run_id': root}}}
    with db.sessions.begin() as session:
        session.add_all([_job('root', {}), _job('other', {}), _job('child', repair('root', 'other')),
                         _job('grandchild', repair('child', 'root'))])
    issues = _capture_fixture_report(db)['diagnostics']['issues']
    assert issues['repair_root_mismatch'] == 1
    assert issues['repair_owner_unverified'] == 2


def test_metadata_depth_is_rejected_before_version_decoder(db, monkeypatch):
    from sqlalchemy import update
    value = 0
    for _ in range(census_report._MAX_JSON_DEPTH + 1):
        value = [value]
    metadata = Base.metadata.tables['platform_metadata']
    with db.engine.begin() as connection:
        connection.execute(update(metadata).where(metadata.c.key == 'schema_version').values(value=value))
    def forbidden(*args, **kwargs):
        raise AssertionError('version decoder called before bounds')
    monkeypatch.setattr(db, '_version', forbidden)
    with pytest.raises(CensusIncomplete, match='^census incomplete$'):
        _capture_fixture_report(db)


def test_rollback_failure_is_sanitized_and_connection_discarded(db, monkeypatch):
    connect = db.engine.connect
    class BrokenRollback:
        def __init__(self, actual):
            self.actual = actual
        def __getattr__(self, name):
            return getattr(self.actual, name)
        def execution_options(self, **options):
            self.actual = self.actual.execution_options(**options)
            return self
        def rollback(self):
            raise RuntimeError('private-rollback-canary')
    monkeypatch.setattr(db.engine, 'connect', lambda: BrokenRollback(connect()))
    with pytest.raises(CensusIncomplete, match='^census incomplete$') as error:
        _capture_fixture_report(db)
    assert 'private-rollback-canary' not in str(error.value)
    monkeypatch.setattr(db.engine, 'connect', connect)
    with db.engine.connect() as connection:
        assert connection.exec_driver_sql('SELECT 1').scalar_one() == 1


def test_incoherent_slot_intent_and_receipt_are_unresolved():
    data = {name: [] for name in census_report._FIELDS}
    def row(name, **values):
        return {field: values.get(field) for field in census_report._FIELDS[name]}
    data['program_reservations'] = [row('program_reservations', scope_id='s', call_id='c', subject_id='j',
        purpose='work', state='unknown', reserved_usd_micro=1, reserved_allowance_micro=1)]
    data['program_slots'] = [row('program_slots', scope_id='s', call_id='c', slot_id='slot', subject_id='j', state='released')]
    data['program_call_intents'] = [row('program_call_intents', scope_id='s', call_id='c', subject_id='j', purpose='work', state='known')]
    data['program_call_receipts'] = [row('program_call_receipts', scope_id='s', call_id='c', receipt_id=str(i), outcome='unknown') for i in range(2)]
    issues = census_report._classify(data)['issues']
    assert issues['incoherent_reservation_slot'] == 1
    assert issues['incoherent_intent_state'] == 1
    assert issues['incoherent_receipt_set'] == 1


@pytest.mark.parametrize('case', ['missing', 'different'])
def test_v9_settled_receipt_missing_or_mismatched_is_unresolved(case):
    data = {name: [] for name in census_report._FIELDS}
    def row(name, **values):
        return {field: values.get(field) for field in census_report._FIELDS[name]}
    data['program_reservations'] = [row('program_reservations', scope_id='s', call_id='c', subject_id='j',
        purpose='work', state='settled', reserved_usd_micro=2, reserved_allowance_micro=2,
        actual_usd_micro=1, actual_allowance_micro=1)]
    data['program_slots'] = [row('program_slots', scope_id='s', call_id='c', slot_id='slot', subject_id='j', state='released')]
    data['program_call_intents'] = [row('program_call_intents', scope_id='s', call_id='c', subject_id='j', purpose='work', state='known')]
    if case == 'different':
        data['program_call_receipts'] = [row('program_call_receipts', scope_id='s', call_id='c', receipt_id='r',
            outcome='known', actual_usd_micro=2, actual_allowance_micro=1)]
    issues = census_report._classify(data)['issues']
    assert issues.get('incoherent_receipt_set', 0) == int(case == 'missing')
    assert issues.get('receipt_reservation_amount_mismatch', 0) == int(case == 'different')


def test_settled_overrun_is_unresolved():
    data = {name: [] for name in census_report._FIELDS}
    data['program_reservations'] = [{field: value for field, value in zip(census_report._FIELDS['program_reservations'],
        ('s', 'c', 'j', 'work', 'settled', 1, 1, 2, 1, 1))}]
    assert census_report._classify(data)['issues']['settled_reservation_overrun'] == 1


def test_settled_without_intent_does_not_invent_receipt():
    data = {name: [] for name in census_report._FIELDS}
    data['program_reservations'] = [{field: value for field, value in zip(census_report._FIELDS['program_reservations'],
        ('s', 'c', 'j', 'work', 'settled', 1, 1, 1, 1, 1))}]
    report = census_report._classify(data)
    assert report['issues']['settled_without_intent_unverified'] == 1
    assert report['issues'].get('incoherent_receipt_set', 0) == 0


def test_linked_settled_legacy_without_intent_is_unverified_in_snapshot(db):
    from sqlalchemy import update
    _seed(db)
    t = Base.metadata.tables
    with db.engine.begin() as connection:
        connection.execute(update(t['program_reservations']).where(t['program_reservations'].c.call_id == 'work-call')
                           .values(state='settled', actual_usd_micro=1, actual_allowance_micro=1))
        connection.execute(update(t['program_slots']).where(t['program_slots'].c.call_id == 'work-call').values(state='released'))
    report = _capture_fixture_report(db)
    assert report['diagnostics']['deduplicated_work_matches'] == 1
    assert report['diagnostics']['issues']['settled_without_intent_unverified'] == 1
    assert report['stream_counts']['program_call_receipts'] == 0


@pytest.mark.parametrize('case', ['missing', 'different', 'overrun', 'allowance_overrun', 'both_overrun', 'valid', 'overflow'])
def test_receipt_and_overrun_states_in_database_snapshot(db, case):
    from sqlalchemy import update
    _seed(db)
    t = Base.metadata.tables
    h = 'a' * 64
    with db.sessions.begin() as session:
        a = session.scalar(select(t['audit_events'].c.sequence))
        is_overflow = case == 'overflow'
        amounts = {'actual_usd_micro': None, 'actual_allowance_micro': None} if is_overflow else {
            'actual_usd_micro': 2 if case in {'overrun', 'both_overrun'} else 1, 'actual_allowance_micro': 2 if case in {'allowance_overrun', 'both_overrun'} else 1}
        session.execute(update(t['program_reservations']).where(t['program_reservations'].c.call_id == 'work-call')
                        .values(state='unknown' if is_overflow else 'settled', **amounts))
        session.execute(update(t['program_slots']).where(t['program_slots'].c.call_id == 'work-call')
                        .values(state='unknown' if is_overflow else 'released'))
        session.execute(insert(t['program_call_intents']), dict(scope_id='private-scope-canary', call_id='work-call',
            subject_id='private-job-canary', purpose='work', effect_key='work-call', idempotency_key='i', request_digest=h,
            policy_revision=1, policy_digest=h, price_digest=h, meter_digest=h, permit_digest=h,
            source_code='fixture', issuer_code='fixture', source_identity_digest=h, observed_ms=1, valid_until_ms=10,
            state='unknown' if is_overflow else 'known', work_job_id='private-job-canary',
            work_attempt_id='private-attempt-canary', work_lease_digest=h, audit_sequence=a))
        if is_overflow:
            session.execute(insert(t['program_overflow_evidence']), dict(scope_id='private-scope-canary',
                evidence_id='ev', kind='call', call_id='work-call', receipt_digest=h,
                usd_decimal='9223372036854775808', allowance_decimal='1', currency='USD', allowance_unit='unit',
                source_digest=h, custody_ref='object', custody_bytes=1, observed_ms=1, audit_sequence=a))
        if case != 'missing':
            session.execute(insert(t['program_call_receipts']), dict(scope_id='private-scope-canary', call_id='work-call',
                receipt_id='receipt', receipt_digest=h, outcome='known', evidence_id='ev' if is_overflow else None,
                actual_usd_micro=None if is_overflow else (2 if case in {'different', 'overrun', 'both_overrun'} else 1),
                actual_allowance_micro=None if is_overflow else (2 if case in {'allowance_overrun', 'both_overrun'} else 1), currency='USD', allowance_unit='unit',
                source_code='fixture', issuer_code='fixture', observed_ms=1, custody_ref='object',
                source_digest=h, source_identity_digest=h, custody_bytes=1, audit_sequence=a))
    report = _capture_fixture_report(db)
    issues = report['diagnostics']['issues']
    assert issues.get('incoherent_receipt_set', 0) == int(case == 'missing')
    assert issues.get('receipt_reservation_amount_mismatch', 0) == int(case == 'different')
    assert issues.get('settled_reservation_overrun', 0) == int(case in {'overrun', 'allowance_overrun', 'both_overrun'})
    if is_overflow:
        assert issues.get('missing_selected_fk', 0) == 0
        assert report['stream_counts']['program_overflow_evidence'] == 1


def test_duplicate_key_json_is_reported_without_content(db):
    from sqlalchemy import text, update
    with db.sessions.begin() as session:
        session.add(_job())
    raw = '{"repository_run":{},"repository_run":{"private-canary":1}}'
    expression = 'CAST(:raw AS JSON)' if db.engine.dialect.name == 'postgresql' else ':raw'
    with db.engine.begin() as connection:
        connection.execute(update(Job).values(resolved=text(expression)), {'raw': raw})
    report = _capture_fixture_report(db)
    assert report['diagnostics']['issues']['malformed_work_json'] == 1
    assert 'private-canary' not in json.dumps(report)


def test_oversized_multibyte_primary_key_rejected_before_stream(db, monkeypatch):
    with db.sessions.begin() as session:
        session.add(_job(identifier='é' * 49, resolved={}))
    called = []
    monkeypatch.setattr(census_report, '_scan_stream', lambda *args, **kwargs: called.append(True))
    with pytest.raises(CensusIncomplete, match='^census incomplete$'):
        _capture_fixture_report(db)
    assert not called


def test_snapshot_does_not_include_concurrent_update(db, monkeypatch):
    from sqlalchemy import update
    with db.sessions.begin() as session:
        session.add(_job(resolved={}))
    before = _capture_fixture_report(db)['stream_digests']['jobs']
    began, committed = Event(), Event()
    original = census_report._preflight
    first = True
    def barrier(*args, **kwargs):
        nonlocal first
        if first:
            first = False
            began.set()
            assert committed.wait(15)
        return original(*args, **kwargs)
    monkeypatch.setattr(census_report, '_preflight', barrier)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_capture_fixture_report, db)
        assert began.wait(15)
        with db.sessions.begin() as session:
            session.execute(update(Job).values(resolved={'changed': True}))
        committed.set()
        report = future.result(timeout=30)
    assert report['stream_digests']['jobs'] == before
    assert _capture_fixture_report(db)['stream_digests']['jobs'] != before


def test_missing_retention_checkpoint_reference_is_counted(db):
    from sqlalchemy import text
    _seed(db)
    params = dict(session_id='private-collection-canary', reference_id='bad-ref', kind='resource',
        object_digest='a' * 64, custody_ref='private-custody-canary', retain_until_ms=100,
        hold='active', checkpoint_sequence=999, audit_sequence=1)
    if db.engine.dialect.name == 'sqlite':
        with db.engine.connect() as connection:
            connection.exec_driver_sql('PRAGMA foreign_keys=OFF')
            try:
                connection.execute(insert(Base.metadata.tables['collection_retention_refs']), params)
                connection.commit()
            finally:
                connection.exec_driver_sql('PRAGMA foreign_keys=ON')
                connection.commit()
    else:
        admin_url = os.environ.get('SOVITE_TEST_POSTGRES_ROLE_ADMIN_URL')
        if not admin_url:
            pytest.skip('admin fixture URL required to simulate persisted FK corruption')
        admin = create_engine(admin_url)
        try:
            with admin.begin() as connection:
                connection.exec_driver_sql('SET LOCAL session_replication_role = replica')
                statement = text(f'INSERT INTO "{db.schema}".collection_retention_refs '
                    '(session_id, reference_id, kind, object_digest, custody_ref, retain_until_ms, hold, checkpoint_sequence, audit_sequence) '
                    'VALUES (:session_id, :reference_id, :kind, :object_digest, :custody_ref, :retain_until_ms, :hold, :checkpoint_sequence, :audit_sequence)')
                connection.execute(statement, params)
        finally:
            admin.dispose()
    report = _capture_fixture_report(db)
    assert report['diagnostics']['issues']['missing_selected_fk'] >= 1
    assert 'private-custody-canary' not in json.dumps(report)


def test_partial_retention_ack_is_counted_after_check_bypass(db):
    if db.engine.dialect.name != 'sqlite':
        pytest.skip('SQLite controlled CHECK-bypass fixture')
    _seed(db)
    with db.engine.connect() as connection:
        connection.exec_driver_sql('PRAGMA ignore_check_constraints=ON')
        try:
            connection.execute(insert(Base.metadata.tables['collection_retention_refs']), dict(
                session_id='private-collection-canary', reference_id='partial-ref', kind='resource',
                object_digest='a' * 64, custody_ref='private-custody-canary', retain_until_ms=100,
                hold='active', ack_enrollment_id='private-enrollment-canary', ack_sequence=None, audit_sequence=1))
            connection.commit()
        finally:
            connection.exec_driver_sql('PRAGMA ignore_check_constraints=OFF')
            connection.commit()
    report = _capture_fixture_report(db)
    assert report['diagnostics']['issues']['partial_retention_ack'] == 1
    assert 'private-enrollment-canary' not in json.dumps(report)
