"""Recovery retries must match the stored producer bundle for the pinned generation."""
from __future__ import annotations

import copy
import time
import traceback
from types import SimpleNamespace

import pytest
from sqlalchemy import Text, cast, event, select, text

from sovite.db import BundleRevision, Job, MetadataRow, WorkerExecutionLease
from sovite.lease_service import LeaseService, _validate_recovery_recipe_generation
from sovite.policy import resolve_recipe
from sovite.recovery_cursor import cursor_key, peek_cursor
from sovite.service import PlatformError
from test_execution_leases import owner, submit  # noqa: F401
from test_migrations import database  # noqa: F401
from test_policy_v1alpha2 import bundle as gateway_bundle  # noqa: F401
from test_work_projection_rows import work_rows  # noqa: F401


def _expired_reserved_owner(owner, *, key, mutate=None):
    job = submit(owner, key=key)
    owner.clock[0] = max(owner.clock[0], time.time() + 1)
    reserved = owner.service.reserve(
        principal=owner.principal,
        instance_id=owner.instance,
        operation_id=f"{key}-reserve",
    )
    assert reserved["state"] == "reserved" and reserved["job"]["job_id"] == job.id
    now = owner.clock[0] + 20
    with owner.platform.db.sessions.begin() as session:
        row = session.get(Job, job.id)
        lease = session.get(WorkerExecutionLease, job.id)
        assert row is not None and lease is not None
        if mutate is not None:
            row.resolved = mutate(copy.deepcopy(row.resolved))
        row.lease_until = now - 1
        row.updated_at = now - 1
        lease.lease_until = now - 1
        lease.updated_at = now - 1
        captured = (row.status, row.resolved, row.lease_owner, row.lease_until,
                    row.updated_at, lease.phase, lease.fence,
                    lease.lease_until, lease.updated_at)
    owner.clock[0] = now
    return job.id, captured


def _owner_state(owner, job_id):
    with owner.platform.db.sessions() as session:
        row = session.get(Job, job_id)
        lease = session.get(WorkerExecutionLease, job_id)
        return (row.status, row.resolved, row.lease_owner, row.lease_until,
                row.updated_at, lease.phase, lease.fence,
                lease.lease_until, lease.updated_at)


@pytest.mark.parametrize("owner", ["direct", "gateway"], indirect=True)
def test_real_v1_direct_and_v2_gateway_recipes_recover_from_producer_bundle(owner):
    job_id, _ = _expired_reserved_owner(owner, key=f"producer-positive-{owner.model}")

    counts = owner.service.recover_expired()

    assert counts["retired"] == 1 and counts["invalid"] == 0
    with owner.platform.db.sessions() as session:
        row = session.get(Job, job_id)
        lease = session.get(WorkerExecutionLease, job_id)
        revision = session.get(BundleRevision, row.generation)
        assert revision is not None
        assert row.status == "retry_wait" and row.error_code == "lease_expired"
        assert row.lease_owner is None and row.lease_until is None
        assert lease.phase == "finished"


_RECIPE_MUTATIONS = (
    "model-destination-missing",
    "agent-id-missing",
    "harness-kind-missing",
    "grant-input-class-missing",
    "compute-id-missing",
)


@pytest.mark.parametrize("owner", ["direct", "gateway"], indirect=True)
@pytest.mark.parametrize("mutation", _RECIPE_MUTATIONS)
def test_partial_producer_recipe_is_held_candidate_locally(owner, mutation):
    def alter(resolved):
        path, field = {
            "model-destination-missing": ("model", "destination"),
            "agent-id-missing": ("agent", "id"),
            "harness-kind-missing": ("harness", "kind"),
            "grant-input-class-missing": ("grant", "input_class"),
            "compute-id-missing": ("compute", "id"),
        }[mutation]
        resolved[path].pop(field)
        return resolved

    job_id, before = _expired_reserved_owner(
        owner, key=f"partial-recipe-{owner.model}-{mutation}", mutate=alter
    )

    counts = owner.service.recover_expired()

    assert counts["invalid"] == 1 and counts["retired"] == 0
    assert _owner_state(owner, job_id) == before


@pytest.mark.parametrize("owner", ["gateway"], indirect=True)
@pytest.mark.parametrize("mutation", ["connection-removed", "connection-invalid"])
def test_gateway_connection_must_match_the_stored_v2_producer(owner, mutation):
    def alter(resolved):
        if mutation == "connection-removed":
            resolved["model"].pop("connection")
        else:
            resolved["model"]["connection"] = {"kind": "model-gateway"}
        return resolved

    job_id, before = _expired_reserved_owner(
        owner, key=f"gateway-connection-{mutation}", mutate=alter
    )

    counts = owner.service.recover_expired()

    assert counts["invalid"] == 1 and counts["retired"] == 0
    assert _owner_state(owner, job_id) == before


@pytest.mark.parametrize("bundle_state", ["missing", "malformed", "drifted"])
def test_unavailable_or_drifted_bundle_holds_expired_owner(owner, bundle_state):
    job_id, before = _expired_reserved_owner(
        owner, key=f"bundle-state-{owner.model}-{bundle_state}"
    )
    other_generation = None
    if bundle_state == "drifted":
        changed = copy.deepcopy(owner.bundle)
        changed["grants"][0]["name"] += " drift"
        other_generation = owner.platform.compile(changed)
    with owner.platform.db.sessions.begin() as session:
        job = session.get(Job, job_id)
        revision = session.get(BundleRevision, job.generation)
        assert revision is not None
        if bundle_state == "missing":
            session.delete(revision)
        elif bundle_state == "malformed":
            revision.bundle = ["not-a-policy-bundle"]
        else:
            other = session.get(BundleRevision, other_generation)
            assert other is not None and other_generation != job.generation
            revision.bundle = copy.deepcopy(other.bundle)

    counts = owner.service.recover_expired()

    assert counts["invalid"] == 1 and counts["retired"] == 0
    assert _owner_state(owner, job_id) == before


def test_bundle_read_failure_is_global_and_does_not_advance_or_reserve(owner):
    job_id, before = _expired_reserved_owner(
        owner, key=f"bundle-read-failure-{owner.model}"
    )
    calls = []

    def fail_bundle_read(conn, cursor, statement, parameters, context, executemany):
        if "bundle_revisions" in statement.lower() and statement.lstrip().lower().startswith("select"):
            calls.append(statement)
            raise RuntimeError("PRIVATE_BUNDLE_READ_SENTINEL")

    event.listen(owner.platform.db.engine, "before_cursor_execute", fail_bundle_read)
    try:
        with pytest.raises(PlatformError) as caught:
            owner.service.recover_expired()
    finally:
        event.remove(owner.platform.db.engine, "before_cursor_execute", fail_bundle_read)
    error = caught.value
    assert calls
    assert (error.code, error.status) == ("owner_unavailable", 503)
    assert "PRIVATE_BUNDLE_READ_SENTINEL" not in "".join(traceback.format_exception(error))
    assert error.__suppress_context__ is True
    assert _owner_state(owner, job_id) == before
    key = cursor_key(owner.platform.settings.compute_id, False)
    with owner.platform.db.sessions() as session:
        assert peek_cursor(session, owner.platform.settings.compute_id, False) is None
        assert session.get(MetadataRow, key) is None


@pytest.mark.parametrize("escape_case", [
    "nul-compute-value", "surrogate-compute-value",
    "nul-compute-key", "surrogate-compute-key",
    "nul-root-key", "surrogate-root-key",
])
@pytest.mark.parametrize("owner", ["direct"], indirect=True)
def test_pg_unsafe_unicode_escapes_in_selector_are_candidate_local(owner, escape_case):
    if owner.platform.db.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL JSON escape behavior is dialect-specific")

    escape = "\x00" if escape_case.startswith("nul-") else "\ud800"
    def raw_mutation(resolved):
        if escape_case.endswith("compute-value"):
            resolved["compute"]["id"] = f"bad{escape}id"
        elif escape_case.endswith("compute-key"):
            resolved["compute"][f"bad{escape}key"] = "x"
        else:
            resolved[f"bad{escape}root-key"] = "x"
        return resolved

    job_id, before = _expired_reserved_owner(
        owner, key=f"unsafe-json-{escape_case}", mutate=raw_mutation
    )
    expected = _owner_state(owner, job_id)
    assert expected[1] == before[1]
    # Confirm SQLAlchemy's JSON serializer stored actual unsafe code points as
    # escaped JSON text before testing the recovery selector expressions.
    with owner.platform.db.sessions() as session:
        raw = session.execute(select(cast(Job.__table__.c.resolved, Text)).where(
            Job.id == job_id)).scalar_one()
    assert "\\u0000" in raw if escape_case.startswith("nul-") else "\\ud800" in raw

    counts = owner.service.recover_expired()

    if escape_case.endswith("root-key"):
        # Unknown top-level fields are ignored by producer consumers, but the
        # raw selector must still inspect the row without decoding the unsafe
        # JSON key inside its candidate filter.
        assert counts["examined"] == 1
        if counts["invalid"]:
            assert _owner_state(owner, job_id) == expected
        else:
            assert counts["retired"] == 1
            with owner.platform.db.sessions() as session:
                assert session.get(Job, job_id).status == "retry_wait"
    else:
        assert counts["invalid"] == 1 and counts["retired"] == 0
        assert _owner_state(owner, job_id) == expected


@pytest.mark.parametrize("valid_extra", [
    "literal-backslash-u",
    "valid-surrogate-pair",
])
@pytest.mark.parametrize("scope", ["foreign", "local"])
@pytest.mark.parametrize("owner", ["direct"], indirect=True)
def test_valid_escaped_json_and_genuine_foreign_compute_are_classified(owner, valid_extra, scope):
    if owner.platform.db.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL JSON escape behavior is dialect-specific")
    def annotate(resolved):
        if scope == "foreign":
            resolved["compute"]["id"] = "foreign-compute"
        resolved["probe_text"] = (
            r"literal \\u0000 content" if valid_extra == "literal-backslash-u" else "emoji 😀"
        )
        return resolved

    job_id, expected = _expired_reserved_owner(
        owner, key=f"scope-json-{scope}-{valid_extra}", mutate=annotate
    )

    counts = owner.service.recover_expired()

    if scope == "foreign":
        assert counts["examined"] == 0 and counts["retired"] == 0
        assert _owner_state(owner, job_id) == expected
    else:
        assert counts["examined"] == 1 and counts["retired"] == 1
        with owner.platform.db.sessions() as session:
            assert session.get(Job, job_id).status == "retry_wait"


def test_malformed_bundle_does_not_block_uncertain_work_closure(work_rows, monkeypatch):
    import sovite.lease_service as lease_module
    from sovite.collection_transaction import owner_transaction
    from sovite.db import Base
    from sovite.lease_service import execution_owner_key

    db, _, _, _ = work_rows
    with db.sessions.begin() as session:
        job = session.get(Job, "job")
        job.resolved = {**job.resolved, "compute": {"id": "fixture"}}
        expiry = session.get(WorkerExecutionLease, "job").lease_until
        session.add(BundleRevision(generation=job.generation, bundle=["malformed but unused for uncertain work"]))
    monkeypatch.setattr(lease_module.time, "time", lambda: expiry + 1)
    service = LeaseService(
        SimpleNamespace(db=db, settings=SimpleNamespace(compute_id="fixture", max_attempts=3)),
        execution_owner_key(workload_id="workload", instance_id="instance"),
        {},
    )

    with owner_transaction(db) as session:
        assert service._close_owner("job", recovery_session=session)

    with db.sessions() as session:
        job = session.get(Job, "job")
        lease = session.get(WorkerExecutionLease, "job")
        tables = Base.metadata.tables
        reservation = session.execute(select(tables["program_reservations"].c.state)).scalar_one()
        slot = session.execute(select(tables["program_slots"].c.state)).scalar_one()
        assert (job.status, job.error_code, job.attempts) == ("blocked", "execution_uncertain", 1)
        assert lease.phase == "finished"
        assert reservation == "unknown" and slot == "unknown"


@pytest.mark.parametrize("owner", ["direct", "gateway"], indirect=True)
def test_large_programmatic_bundle_snapshot_has_no_invented_json_cap(owner):
    bundle = copy.deepcopy(owner.bundle)
    for index in range(70):
        bundle["tools"].append({
            "id": f"recovery-large-unreferenced-{index}",
            "name": "β" * 16000,
            "description": "β" * 16000,
            "kind": "kubernetes-read",
            "input_class": "C3",
            "output_class": "C3",
            "destination": "private-runtime",
            "allowed_operations": ["list-pods"],
            "read_only": True,
        })
    generation = owner.platform.compile(bundle)
    with owner.platform.db.sessions() as session:
        revision = session.get(BundleRevision, generation)
        raw = session.execute(select(cast(BundleRevision.__table__.c.bundle, Text)).where(
            BundleRevision.generation == generation)).scalar_one()
        producer_resolved = resolve_recipe(revision.bundle, "incident-forensics")
    assert len(raw.encode("utf-8")) > 2_000_000

    _validate_recovery_recipe_generation(
        producer_resolved, raw, generation, "incident-forensics"
    )


def test_hosted_generation_uses_installed_envelope_and_rejects_drift(tmp_path):
    import json
    from pathlib import Path
    from test_hosting import build_hosted
    from sovite.lease_service import _HandBackOwnerUnavailable

    settings, platform, bundle, envelope = build_hosted(tmp_path)
    generation = platform.active()[1]
    grant_id = bundle['grants'][0]['id']
    producer = resolve_recipe(bundle, grant_id)
    producer['generation'] = generation
    raw = json.dumps(bundle)
    _validate_recovery_recipe_generation(producer, raw, generation, grant_id, settings.hosting_envelope_path)

    envelope['revision'] += 1
    Path(settings.hosting_envelope_path).write_text(json.dumps(envelope))
    with pytest.raises(_HandBackOwnerUnavailable):
        _validate_recovery_recipe_generation(producer, raw, generation, grant_id, settings.hosting_envelope_path)
    Path(settings.hosting_envelope_path).unlink()
    with pytest.raises(OSError):
        _validate_recovery_recipe_generation(producer, raw, generation, grant_id, settings.hosting_envelope_path)


@pytest.mark.parametrize('owner', ['gateway'], indirect=True)
@pytest.mark.parametrize('drift', [False, True])
def test_hosted_reserved_owner_retry_uses_installed_binding(owner, tmp_path, drift):
    import json
    from pathlib import Path
    from dataclasses import replace
    from sovite.collection_transaction import owner_transaction
    from test_hosting import build_hosted

    (tmp_path / 'hosted').mkdir()
    settings, hosted_platform, bundle, envelope = build_hosted(tmp_path / 'hosted')
    generation = hosted_platform.active()[1]
    grant_id = bundle['grants'][0]['id']
    job_id, _ = _expired_reserved_owner(owner, key=f'hosted-{drift}')
    producer = resolve_recipe(bundle, grant_id)
    producer['generation'] = generation
    with owner.platform.db.sessions.begin() as session:
        row = session.get(Job, job_id)
        lease = session.get(WorkerExecutionLease, job_id)
        row.resolved = {**row.resolved, **producer}
        row.generation = generation
        row.grant_id = grant_id
        lease.generation = generation
        session.add(BundleRevision(generation=generation, bundle=bundle))
    owner.platform.settings = replace(owner.platform.settings, hosting_envelope_path=settings.hosting_envelope_path)
    if drift:
        envelope['revision'] += 1
        Path(settings.hosting_envelope_path).write_text(json.dumps(envelope))
    before = _owner_state(owner, job_id)
    if drift:
        from sovite.lease_service import _HandBackOwnerUnavailable
        with pytest.raises(_HandBackOwnerUnavailable):
            with owner_transaction(owner.platform.db) as session:
                owner.service._close_owner(job_id, recovery_session=session)
        assert _owner_state(owner, job_id) == before
    else:
        with owner_transaction(owner.platform.db) as session:
            assert owner.service._close_owner(job_id, recovery_session=session)
        assert _owner_state(owner, job_id)[0] == 'retry_wait'


def test_historical_bundle_retry_does_not_reapply_new_unrelated_policy(gateway_bundle):
    import json
    from sovite.lease_service import _HandBackOwnerUnavailable
    from sovite.policy import PolicyError, bundle_generation, validate_bundle

    # Accepted by c9248d6^; its new destination-wide rule rejects this unused
    # provider cache even though it is outside the selected producer recipe.
    historical = copy.deepcopy(gateway_bundle)
    unused = copy.deepcopy(historical['destinations'][0])
    unused['id'] = 'historical-unused-provider-cache'
    unused['retention']['in_memory_cache']['scope'] = 'provider'
    historical['destinations'].append(unused)
    with pytest.raises(PolicyError):
        validate_bundle(historical)
    generation = bundle_generation(historical)
    grant_id = historical['grants'][0]['id']
    producer = resolve_recipe(historical, grant_id)
    raw = json.dumps(historical)
    _validate_recovery_recipe_generation(producer, raw, generation, grant_id)

    tampered = copy.deepcopy(producer)
    tampered['model']['id'] = 'other-model'
    with pytest.raises(_HandBackOwnerUnavailable):
        _validate_recovery_recipe_generation(tampered, raw, generation, grant_id)
    with pytest.raises(_HandBackOwnerUnavailable):
        _validate_recovery_recipe_generation(producer, raw, 'sha256:' + '0' * 64, grant_id)


@pytest.mark.parametrize('owner', ['direct'], indirect=True)
@pytest.mark.parametrize('safe', [True, False])
def test_legacy_running_owner_without_lease_recovered_conservatively(owner, safe):
    job_id, _ = _expired_reserved_owner(owner, key=f'legacy-no-lease-{safe}')
    with owner.platform.db.sessions.begin() as session:
        row = session.get(Job, job_id)
        row.attempts = 1
        if not safe:
            resolved = copy.deepcopy(row.resolved)
            resolved['harness']['kind'] = 'agent-http'
            row.resolved = resolved
        session.delete(session.get(WorkerExecutionLease, job_id))
    result = owner.service.recover_expired()
    assert result['retired'] == 1 and result['invalid'] == 0
    with owner.platform.db.sessions() as session:
        row = session.get(Job, job_id)
        assert (row.status, row.error_code) == (('retry_wait', 'lease_expired') if safe else ('blocked', 'execution_uncertain'))
        assert row.lease_owner is None and row.lease_until is None
        assert session.get(WorkerExecutionLease, job_id) is None


def test_v4_running_job_without_lease_survives_migration_and_recovers(database, tmp_path):
    from sovite.policy import load_bundle
    from sovite.service import Platform
    from sovite.settings import Settings
    from test_migrations import _create_historical_schema

    _create_historical_schema(database, 4)
    bundle, generation = load_bundle('config/platform.local.yaml')
    grant_id = 'incident-forensics'
    recipe = resolve_recipe(bundle, grant_id)
    with database.sessions.begin() as session:
        session.add(BundleRevision(generation=generation, bundle=bundle))
        session.add(MetadataRow(key='active_generation', value={'generation': generation}))
        session.add(Job(id='legacy-v4', receipt='legacy-receipt', idempotency_key='legacy-key',
                        request_digest='legacy-digest', caller='operations', grant_id=grant_id,
                        generation=generation, resolved=recipe, task='legacy', status='running',
                        attempts=1, lease_owner='legacy-worker', lease_until=time.time() - 10))
    database.migrate()
    settings = Settings(database_url=database.engine.url.render_as_string(hide_password=False),
                        database_schema=database.schema, artifact_path=str(tmp_path / 'artifacts'),
                        compute_id=recipe['compute']['id'], require_qualification=False)
    platform = Platform(settings)
    platform.db.engine.dispose()
    platform.db = database
    with database.sessions() as session:
        assert session.get(WorkerExecutionLease, 'legacy-v4') is None
    result = LeaseService(platform, 'new-worker', {}).recover_expired()
    assert result['retired'] == 1
    with database.sessions() as session:
        job = session.get(Job, 'legacy-v4')
        assert (job.status, job.error_code) == ('retry_wait', 'lease_expired')
        assert job.lease_owner is None and job.lease_until is None
        assert session.get(WorkerExecutionLease, 'legacy-v4') is None


@pytest.mark.parametrize('owner', ['direct'], indirect=True)
def test_legacy_snapshot_refuses_lease_appearing_before_owner_lock(owner, monkeypatch):
    import sovite.work_projection as projections
    job_id, _ = _expired_reserved_owner(owner, key='legacy-lease-drift')
    with owner.platform.db.sessions.begin() as session:
        row = session.get(Job, job_id)
        row.attempts = 1
        lease = session.get(WorkerExecutionLease, job_id)
        saved = {col.name: getattr(lease, col.name) for col in WorkerExecutionLease.__table__.columns}
        session.delete(lease)
    from test_execution_leases import snapshot
    before = snapshot(owner.platform)
    original = projections.lock_work_projection_set
    reached = []
    def insert_before_lock(session, *args, **kwargs):
        reached.append(True)
        session.add(WorkerExecutionLease(**saved))
        session.flush()
        return original(session, *args, **kwargs)
    monkeypatch.setattr(projections, 'lock_work_projection_set', insert_before_lock)
    result = owner.service.recover_expired()
    assert reached and result['invalid'] == 1 and result['retired'] == 0
    after = snapshot(owner.platform)
    assert [row for row in after['jobs'] if row['id'] == job_id] == [row for row in before['jobs'] if row['id'] == job_id]
    with owner.platform.db.sessions() as session:
        assert session.get(WorkerExecutionLease, job_id) is None


@pytest.mark.parametrize('owner', ['direct'], indirect=True)
def test_recovery_denies_live_conflicting_lease(owner):
    job_id, _ = _expired_reserved_owner(owner, key='legacy-live-conflict')
    with owner.platform.db.sessions.begin() as session:
        session.get(WorkerExecutionLease, job_id).lease_until = owner.clock[0] + 100
    before = _owner_state(owner, job_id)
    result = owner.service.recover_expired()
    assert result['invalid'] == 1 and result['retired'] == 0
    assert _owner_state(owner, job_id) == before


@pytest.mark.parametrize('owner', ['direct'], indirect=True)
def test_legacy_missing_owner_is_not_recovered(owner):
    job_id, _ = _expired_reserved_owner(owner, key='legacy-missing-owner')
    with owner.platform.db.sessions.begin() as session:
        row = session.get(Job, job_id)
        row.attempts = 1
        row.lease_owner = None
        session.delete(session.get(WorkerExecutionLease, job_id))
    result = owner.service.recover_expired()
    assert result['invalid'] == 1 and result['retired'] == 0
    with owner.platform.db.sessions() as session:
        assert session.get(Job, job_id).status == 'running'
        assert session.get(WorkerExecutionLease, job_id) is None
