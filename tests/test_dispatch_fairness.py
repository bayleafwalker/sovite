"""Fair dispatch must not be starved by earlier foreign-pool work."""
import copy

import pytest

from sovite.db import Job, WorkerExecutionLease
from test_execution_leases import owner, submit  # noqa: F401
from test_migrations import database  # noqa: F401
from test_policy_v1alpha2 import bundle as gateway_bundle  # noqa: F401


@pytest.mark.parametrize('owner', ['direct'], indirect=True)
def test_foreign_pool_backlog_does_not_hide_local_job(owner):
    bundle = copy.deepcopy(owner.bundle)
    foreign_compute = copy.deepcopy(bundle['compute'][0])
    foreign_compute['id'] = 'foreign-pool'
    bundle['compute'].append(foreign_compute)
    foreign_grant = copy.deepcopy(bundle['grants'][0])
    foreign_grant['id'] = 'foreign-grant'
    foreign_grant['compute'] = 'foreign-pool'
    bundle['grants'].append(foreign_grant)
    generation = owner.platform.compile(bundle)
    owner.platform.activate(generation)
    foreign_jobs = [owner.platform.submit('foreign-grant', 'foreign', 'operations',
                                          f'foreign-key-{index}') for index in range(101)]
    job = submit(owner, key='fair-local')
    with owner.platform.db.sessions.begin() as session:
        local = session.get(Job, job.id)
        local.created_at, local.next_attempt_at = 1000, 0
        for foreign in foreign_jobs:
            row = session.get(Job, foreign.id)
            row.created_at, row.next_attempt_at = 1, 0
    result = owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                                   operation_id='fair-local-reserve')
    assert result['state'] == 'reserved' and result['job']['job_id'] == job.id
    with owner.platform.db.sessions() as session:
        for foreign in foreign_jobs:
            row = session.get(Job, foreign.id)
            assert row.status == 'queued' and row.lease_owner is None
            assert session.get(WorkerExecutionLease, foreign.id) is None


@pytest.mark.parametrize('owner', ['direct'], indirect=True)
def test_repository_worker_never_blocks_ordinary_unsafe_unicode_job(owner):
    from sovite.lease_service import LeaseService
    if owner.platform.db.engine.dialect.name != 'postgresql':
        pytest.skip('PostgreSQL json and jsonb have distinct unicode escape handling')
    job = submit(owner, key='ordinary-unsafe-json')
    with owner.platform.db.sessions.begin() as session:
        row = session.get(Job, job.id)
        resolved = copy.deepcopy(row.resolved)
        resolved['irrelevant_probe'] = '\x00'
        row.resolved = resolved
        row.next_attempt_at = 0
    restricted = LeaseService(owner.platform, 'repository-only', {}, repository_worker_only=True)
    result = restricted.reserve(principal=owner.principal, instance_id=owner.instance,
                                operation_id='repository-only-unsafe')
    assert result['state'] == 'idle'
    with owner.platform.db.sessions() as session:
        row = session.get(Job, job.id)
        assert row.status == 'queued' and row.error_code is None
        assert session.get(WorkerExecutionLease, job.id) is None


@pytest.mark.parametrize('owner', ['direct'], indirect=True)
@pytest.mark.parametrize('unsafe', [False, True])
def test_repository_worker_admits_safe_producer_job_and_holds_unsafe(owner, tmp_path, unsafe):
    from sovite.lease_service import LeaseService
    from test_repository_runs import BindingResolver, make_owner, make_repo, packet, submit as submit_run
    if unsafe and owner.platform.db.engine.dialect.name != 'postgresql':
        pytest.skip('PostgreSQL json and jsonb have distinct unicode escape handling')
    mirror, base = make_repo(tmp_path)
    resolver = BindingResolver(mirror, base, owner.platform.active()[1])
    repository_owner = make_owner(owner.platform, resolver, executor=lambda *_: None)
    run = submit_run(repository_owner, packet(base), principal_id='cloud-codex',
                     idempotency_key='repository-fairness-probe')
    with owner.platform.db.sessions.begin() as session:
        row = session.get(Job, run['id'])
        row.next_attempt_at = 0
        if unsafe:
            resolved = copy.deepcopy(row.resolved)
            resolved['irrelevant_probe'] = '\x00'
            row.resolved = resolved
    service = LeaseService(owner.platform, 'repository-worker', {},
                           repository_worker_only=True, repository_run_profile=True)
    result = service.reserve(principal=owner.principal, instance_id=owner.instance,
                             operation_id='repository-fairness-pull')
    if unsafe:
        assert result['state'] == 'idle'
        with owner.platform.db.sessions() as session:
            row = session.get(Job, run['id'])
            assert row.status == 'queued' and row.error_code is None
            assert session.get(WorkerExecutionLease, run['id']) is None
    else:
        assert result['state'] == 'reserved' and result['job']['job_id'] == run['id']


@pytest.mark.parametrize('owner', ['direct'], indirect=True)
@pytest.mark.parametrize('kind', ['duplicate-root', 'duplicate-schema', 'malformed'])
def test_repository_worker_holds_ambiguous_json_without_denial(owner, kind):
    import json
    from sqlalchemy import text
    from sovite.lease_service import LeaseService
    from sovite.repository_runs import RUN_SCHEMA
    if kind == 'malformed' and owner.platform.db.engine.dialect.name != 'sqlite':
        pytest.skip('PostgreSQL json storage rejects malformed JSON')
    job = submit(owner, key=f'ambiguous-{kind}')
    with owner.platform.db.sessions.begin() as session:
        row = session.get(Job, job.id)
        row.next_attempt_at = 0
        raw = json.dumps(row.resolved)
        if kind == 'malformed':
            raw = '{'
        elif kind == 'duplicate-root':
            raw = raw[:-1] + ',"repository_run":null,"repository_run":{"schema_version":' + json.dumps(RUN_SCHEMA) + '}}'
        else:
            raw = raw[:-1] + ',"repository_run":{"schema_version":"other","schema_version":' + json.dumps(RUN_SCHEMA) + '}}'
        table = f'"{owner.platform.db.schema}".jobs' if owner.platform.db.schema else 'jobs'
        value = 'CAST(:raw AS JSON)' if owner.platform.db.schema else ':raw'
        session.execute(text(f'UPDATE {table} SET resolved={value} WHERE id=:id'), {'raw': raw, 'id': job.id})
    restricted = LeaseService(owner.platform, 'repository-only', {}, repository_worker_only=True)
    result = restricted.reserve(principal=owner.principal, instance_id=owner.instance,
                                operation_id=f'repository-{kind}')
    assert result['state'] == 'idle'
    with owner.platform.db.sessions() as session:
        table = f'"{owner.platform.db.schema}".jobs' if owner.platform.db.schema else 'jobs'
        row = session.execute(text(f'SELECT status, error_code FROM {table} WHERE id=:id'), {'id': job.id}).one()
        assert tuple(row) == ('queued', None)
        assert session.get(WorkerExecutionLease, job.id) is None


@pytest.mark.parametrize('owner', ['direct'], indirect=True)
def test_foreign_repository_backlog_does_not_hide_local_repository_job(owner, tmp_path):
    from sovite.lease_service import LeaseService
    from test_repository_runs import BindingResolver, make_owner, make_repo, packet, submit as submit_run
    bundle = copy.deepcopy(owner.bundle)
    foreign_compute = copy.deepcopy(bundle['compute'][0])
    foreign_compute['id'] = 'foreign-pool'
    bundle['compute'].append(foreign_compute)
    foreign_grant = copy.deepcopy(bundle['grants'][0])
    foreign_grant['id'], foreign_grant['compute'] = 'foreign-grant', 'foreign-pool'
    bundle['grants'].append(foreign_grant)
    generation = owner.platform.compile(bundle)
    owner.platform.activate(generation)
    mirror, base = make_repo(tmp_path)
    resolver = BindingResolver(mirror, base, generation)
    repository_owner = make_owner(owner.platform, resolver, executor=lambda *_: None)
    resolver.grant_id = 'foreign-grant'
    foreign = [submit_run(repository_owner, packet(base), principal_id='cloud-codex',
                          idempotency_key=f'foreign-repository-{index:04}') for index in range(101)]
    resolver.grant_id = 'incident-forensics'
    local = submit_run(repository_owner, packet(base), principal_id='cloud-codex',
                       idempotency_key='local-repository-probe')
    with owner.platform.db.sessions.begin() as session:
        for item in foreign:
            row = session.get(Job, item['id'])
            row.created_at, row.next_attempt_at = 1, 0
        row = session.get(Job, local['id'])
        row.created_at, row.next_attempt_at = 1000, 0
    service = LeaseService(owner.platform, 'repository-worker', {},
                           repository_worker_only=True, repository_run_profile=True)
    result = service.reserve(principal=owner.principal, instance_id=owner.instance,
                             operation_id='repository-pool-fairness')
    assert result['state'] == 'reserved' and result['job']['job_id'] == local['id']
    with owner.platform.db.sessions() as session:
        for item in foreign:
            row = session.get(Job, item['id'])
            assert row.status == 'queued' and row.lease_owner is None
            assert session.get(WorkerExecutionLease, item['id']) is None


@pytest.mark.parametrize('owner', ['direct'], indirect=True)
def test_local_worker_progresses_past_incompatible_window_after_restart(owner):
    from sovite.lease_service import LeaseService
    job = submit(owner, key='fair-profile-local')
    with owner.platform.db.sessions.begin() as session:
        local = session.get(Job, job.id)
        local.created_at, local.next_attempt_at = 1000, 0
        for index in range(101):
            resolved = copy.deepcopy(local.resolved)
            resolved['repository_run'] = {}
            session.add(Job(id=f'profile-{index:03}', receipt=f'profile-receipt-{index}',
                idempotency_key=f'profile-key-{index}', request_digest='profile-digest',
                caller=local.caller, grant_id=local.grant_id, generation=local.generation,
                resolved=resolved, task='repository', status='queued', attempts=0,
                created_at=1, next_attempt_at=0))
    first = owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                                  operation_id='fair-profile-first')
    assert first['state'] == 'idle'
    restarted = LeaseService(owner.platform, 'restarted', {})
    replay = restarted.reserve(principal=owner.principal, instance_id=owner.instance,
                               operation_id='fair-profile-first')
    assert replay == first
    later = restarted.reserve(principal=owner.principal, instance_id=owner.instance,
                              operation_id='fair-profile-second')
    assert later['state'] == 'reserved' and later['job']['job_id'] == job.id


def _profile_backlog(owner):
    job = submit(owner, key='cursor-controls-target')
    with owner.platform.db.sessions.begin() as session:
        local = session.get(Job, job.id)
        local.created_at, local.next_attempt_at = 1000, 0
        for index in range(101):
            resolved = copy.deepcopy(local.resolved)
            resolved['repository_run'] = {}
            session.add(Job(id=f'control-{index:03}', receipt=f'control-receipt-{index}',
                idempotency_key=f'control-key-{index}', request_digest='control-digest',
                caller=local.caller, grant_id=local.grant_id, generation=local.generation,
                resolved=resolved, task='repository', status='queued', attempts=0,
                created_at=1, next_attempt_at=0))
    return job


def _external_profile_backlog(owner):
    from test_worker_remote_boundary import admit_agent_http_owner

    admit_agent_http_owner(owner)
    target = owner.platform.submit('native-code-review', 'external fairness target',
                                  'operations', 'external-fairness-target', input_class='C2')
    # Keep owner authorization valid while making only the external recipe
    # incompatible. This exercises v2's authorize-then-audit progression.
    with owner.platform.db.sessions.begin() as session:
        local = session.get(Job, target.id)
        local.created_at, local.next_attempt_at = 1000, 0
        for index in range(101):
            resolved = copy.deepcopy(local.resolved)
            resolved['harness']['kind'] = 'unsupported-external-profile'
            session.add(Job(id=f'external-control-{index:03}', receipt=f'external-control-receipt-{index}',
                idempotency_key=f'external-control-key-{index}', request_digest='external-control-digest',
                caller=local.caller, grant_id=local.grant_id, generation=local.generation,
                resolved=resolved, task='external fairness incompatible task', status='queued', attempts=0,
                created_at=1, next_attempt_at=0))
    return target


@pytest.mark.parametrize('owner', ['direct'], indirect=True)
def test_dispatch_cursor_and_idle_journal_roll_back_together(owner, monkeypatch):
    from sqlalchemy import select
    from sovite.db import MetadataRow, WorkerExecutionOperation
    _profile_backlog(owner)
    original = owner.platform.db.audit
    def fail_idle(session, actor, action, target, *args, **kwargs):
        if action == 'task.execution_idle':
            raise RuntimeError('synthetic idle audit failure')
        return original(session, actor, action, target, *args, **kwargs)
    monkeypatch.setattr(owner.platform.db, 'audit', fail_idle)
    with pytest.raises(RuntimeError, match='synthetic idle audit failure'):
        owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                              operation_id='fair-idle-failure')
    with owner.platform.db.sessions() as session:
        assert not session.scalars(select(MetadataRow).where(MetadataRow.key.like('dispatch_fair:%'))).all()
        assert session.get(WorkerExecutionOperation, (owner.principal.workload_id,
                           owner.instance, 'fair-idle-failure')) is None


@pytest.mark.parametrize('database', ['postgresql'], indirect=True)
@pytest.mark.parametrize('owner', ['direct'], indirect=True)
def test_dispatch_cursor_serializes_competing_workers(owner, monkeypatch):
    from test_execution_leases import _pg_race
    job = _profile_backlog(owner)
    values = _pg_race(owner, monkeypatch,
        lambda: owner.service.reserve(principal=owner.principal, instance_id='worker-a', operation_id='cursor-a'),
        lambda: owner.service.reserve(principal=owner.principal, instance_id='worker-b', operation_id='cursor-b'))
    assert sorted(value['state'] for value in values) == ['idle', 'reserved']
    assert next(value for value in values if value['state'] == 'reserved')['job']['job_id'] == job.id


@pytest.mark.parametrize('owner', ['direct'], indirect=True)
def test_dispatch_cursor_wraps_to_new_earlier_job(owner):
    target = _profile_backlog(owner)
    first = owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                                  operation_id='cursor-wrap-first')
    assert first['state'] == 'idle'
    with owner.platform.db.sessions.begin() as session:
        session.get(Job, target.id).created_at = 0
    second = owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                                   operation_id='cursor-wrap-second')
    assert second['state'] == 'reserved' and second['job']['job_id'] == target.id


@pytest.mark.parametrize('owner', ['direct'], indirect=True)
def test_corrupt_dispatch_cursor_refuses_without_idle_or_owner_changes(owner):
    from sqlalchemy import select
    from sovite.db import MetadataRow, WorkerExecutionOperation
    from sovite.service import PlatformError
    _profile_backlog(owner)
    assert owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                                 operation_id='cursor-corrupt-seed')['state'] == 'idle'
    with owner.platform.db.sessions.begin() as session:
        cursor = session.scalars(select(MetadataRow).where(MetadataRow.key.like('dispatch_fair:%'))).one()
        cursor.value = {'schema': 'unknown'}
    with pytest.raises(PlatformError) as error:
        owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                              operation_id='cursor-corrupt-read')
    assert error.value.code == 'owner_unavailable'
    with owner.platform.db.sessions() as session:
        assert session.get(WorkerExecutionOperation, (owner.principal.workload_id,
                           owner.instance, 'cursor-corrupt-read')) is None
        cursor = session.scalars(select(MetadataRow).where(MetadataRow.key.like('dispatch_fair:%'))).one()
        assert cursor.value == {'schema': 'unknown'}


@pytest.mark.parametrize('owner', ['direct'], indirect=True)
def test_cursor_progresses_past_capacity_saturated_jobs(owner):
    bundle = copy.deepcopy(owner.bundle)
    bundle['models'][0]['max_concurrency'] = 1
    spare_model = copy.deepcopy(bundle['models'][0]); spare_model['id'] = 'spare-model'
    bundle['models'].append(spare_model)
    spare_agent = copy.deepcopy(bundle['agents'][0]); spare_agent['id'] = 'spare-agent'; spare_agent['model'] = spare_model['id']
    bundle['agents'].append(spare_agent)
    spare_grant = copy.deepcopy(bundle['grants'][0]); spare_grant['id'] = 'spare-grant'; spare_grant['agent'] = spare_agent['id']
    bundle['grants'].append(spare_grant)
    owner.platform.activate(owner.platform.compile(bundle))
    busy = submit(owner, key='cursor-capacity-busy')
    with owner.platform.db.sessions.begin() as session:
        session.get(Job, busy.id).next_attempt_at = 0
    assert owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                                 operation_id='cursor-capacity-busy')['state'] == 'reserved'
    waiting = [submit(owner, key=f'cursor-capacity-{index}') for index in range(101)]
    target = owner.platform.submit('spare-grant', 'target', 'operations', 'cursor-capacity-spare')
    with owner.platform.db.sessions.begin() as session:
        for item in waiting:
            row = session.get(Job, item.id); row.created_at, row.next_attempt_at = 1, 0
        row = session.get(Job, target.id); row.created_at, row.next_attempt_at = 1000, 0
    first = owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                                  operation_id='cursor-capacity-first')
    assert first['state'] == 'idle'
    second = owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                                   operation_id='cursor-capacity-second')
    assert second['state'] == 'reserved' and second['job']['job_id'] == target.id


@pytest.mark.parametrize('owner', ['direct'], indirect=True)
def test_cursor_preserves_known_denial_before_advancing(owner, monkeypatch):
    from sovite.service import PlatformError
    from sovite.db import AuditEvent
    from sqlalchemy import select
    target = _profile_backlog(owner)
    denied = submit(owner, key='cursor-known-denial')
    with owner.platform.db.sessions.begin() as session:
        row = session.get(Job, denied.id); row.created_at, row.next_attempt_at = 0, 0
    original = owner.service._authorize_current
    def deny_selected(session, row, now):
        if row.id == denied.id:
            raise PlatformError('caller_denied', 'synthetic denial', 403)
        return original(session, row, now)
    monkeypatch.setattr(owner.service, '_authorize_current', deny_selected)
    assert owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                                 operation_id='cursor-denial-first')['state'] == 'idle'
    with owner.platform.db.sessions() as session:
        row = session.get(Job, denied.id)
        assert (row.status, row.error_code) == ('blocked', 'caller_denied')
        assert session.scalar(select(AuditEvent).where(AuditEvent.target == denied.id,
                                                       AuditEvent.action == 'task.dispatch_denied')) is not None
    result = owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                                   operation_id='cursor-denial-second')
    assert result['state'] == 'reserved' and result['job']['job_id'] == target.id


@pytest.mark.parametrize('owner', ['direct'], indirect=True)
def test_external_profile_denial_does_not_write_dispatch_cursor(owner):
    from sqlalchemy import select
    from sovite.db import MetadataRow, WorkerExecutionOperation
    from sovite.service import PlatformError
    _profile_backlog(owner)
    with pytest.raises(PlatformError) as error:
        owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                              operation_id='cursor-external-denial', external_profile='agent-http-v1')
    assert error.value.code == 'execution_profile_unavailable'
    with owner.platform.db.sessions() as session:
        assert not session.scalars(select(MetadataRow).where(MetadataRow.key.like('dispatch_fair:%'))).all()
        assert session.get(WorkerExecutionOperation, (owner.principal.workload_id,
                           owner.instance, 'cursor-external-denial')) is None


@pytest.mark.parametrize('owner', ['direct'], indirect=True)
def test_external_v2_scans_in_bounded_batches_and_replays_progress(owner):
    from sqlalchemy import select
    from sovite.db import MetadataRow, WorkerExecutionOperation
    from sovite.remote_execution import RemoteExecution
    from sovite.service import PlatformError
    from sovite.lease_service import LeaseService

    target = _external_profile_backlog(owner)
    first = owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                                  operation_id='external-v2-first', external_profile='agent-http-v1',
                                  pull_protocol=2)
    assert first == {'state': 'scan_advanced'}
    restarted = LeaseService(owner.platform, 'external-v2-restarted', {})
    assert restarted.reserve(principal=owner.principal, instance_id=owner.instance,
                             operation_id='external-v2-first', external_profile='agent-http-v1',
                             pull_protocol=2) == first
    with pytest.raises(PlatformError) as conflict:
        restarted.reserve(principal=owner.principal, instance_id=owner.instance,
                          operation_id='external-v2-first', external_profile='agent-http-v1')
    assert conflict.value.code == 'idempotency_conflict'
    lookup = RemoteExecution(owner.platform).lookup(
        owner.principal, instance_id=owner.instance, operation_id='external-v2-first', pull_protocol=2)
    assert lookup['state'] == 'found' and lookup['result_state'] == first['state']
    with pytest.raises(PlatformError) as error:
        RemoteExecution(owner.platform).lookup(
            owner.principal, instance_id=owner.instance, operation_id='external-v2-first')
    assert error.value.code == 'owner_unavailable'

    second = owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                                   operation_id='external-v2-second', external_profile='agent-http-v1',
                                   pull_protocol=2)
    assert second['state'] == 'reserved' and second['job']['job_id'] == target.id
    with owner.platform.db.sessions() as session:
        cursor = session.scalars(select(MetadataRow).where(
            MetadataRow.key.like('external_dispatch_fair_v2:%'))).all()
        assert len(cursor) == 1
        assert cursor[0].value['schema'] == 'sovite-external-dispatch-fair/v2'
        assert cursor[0].value['scope'] == {
            'workload_id': owner.principal.workload_id,
            'compute_id': owner.principal.compute_id,
            'profile': 'agent-http-v1',
            'repository_worker_only': False,
            'repository_run_profile': False,
        }
        row = session.get(WorkerExecutionOperation, (owner.principal.workload_id,
                             owner.instance, 'external-v2-first'))
        assert row.result == first and row.job_id is None and row.fence is None


@pytest.mark.parametrize('owner', ['direct'], indirect=True)
def test_external_v2_journals_profile_unavailable_and_resets_scan(owner):
    from sqlalchemy import select
    from sovite.db import MetadataRow, WorkerExecutionOperation
    from sovite.lease_service import LeaseService
    from sovite.remote_execution import RemoteExecution

    target = _external_profile_backlog(owner)
    # The ordinary row from the helper is also made profile-incompatible.
    with owner.platform.db.sessions.begin() as session:
        target_row = session.get(Job, target.id)
        resolved = copy.deepcopy(target_row.resolved)
        resolved.pop('repository_run', None)
        resolved['harness']['kind'] = 'unsupported-external-profile'
        target_row.resolved = resolved
        target_row.created_at = 1000
    first = owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                                  operation_id='external-v2-unavailable-first', external_profile='agent-http-v1',
                                  pull_protocol=2)
    assert first == {'state': 'scan_advanced'}
    second = owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                                   operation_id='external-v2-unavailable-second', external_profile='agent-http-v1',
                                   pull_protocol=2)
    assert second == {'state': 'profile_unavailable'}
    with owner.platform.db.sessions() as session:
        assert not session.scalars(select(MetadataRow).where(
            MetadataRow.key.like('external_dispatch_fair_v2:%'))).all()
    later_target = owner.platform.submit('native-code-review', 'after unavailable journal',
                                         'operations', 'external-fairness-after-unavailable', input_class='C2')
    with owner.platform.db.sessions.begin() as session:
        row = session.get(Job, later_target.id)
        row.created_at, row.next_attempt_at = 0, 0
    assert owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                                 operation_id='external-v2-unavailable-second', external_profile='agent-http-v1',
                                 pull_protocol=2) == second
    restarted = LeaseService(owner.platform, 'external-v2-restarted', {})
    next_scan = restarted.reserve(principal=owner.principal, instance_id=owner.instance,
                                  operation_id='external-v2-after-unavailable', external_profile='agent-http-v1',
                                  pull_protocol=2)
    assert next_scan['state'] == 'reserved' and next_scan['job']['job_id'] == later_target.id
    lookup = RemoteExecution(owner.platform).lookup(
        owner.principal, instance_id=owner.instance,
        operation_id='external-v2-unavailable-second', pull_protocol=2)
    assert lookup['state'] == 'found' and lookup['result_state'] == second['state']
    with owner.platform.db.sessions() as session:
        cursor = session.scalars(select(MetadataRow).where(
            MetadataRow.key.like('external_dispatch_fair_v2:%'))).all()
        assert len(cursor) == 1 and cursor[0].value['id'] == later_target.id
        row = session.get(WorkerExecutionOperation, (owner.principal.workload_id,
                             owner.instance, 'external-v2-unavailable-second'))
        assert row.result == second and row.job_id is None and row.fence is None


@pytest.mark.parametrize('owner', ['direct'], indirect=True)
def test_external_v2_corrupt_cursor_fails_closed_without_journaling(owner):
    from sqlalchemy import select
    from sovite.db import MetadataRow, WorkerExecutionOperation
    from sovite.service import PlatformError

    _external_profile_backlog(owner)
    first = owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                                  operation_id='external-v2-corrupt-seed', external_profile='agent-http-v1',
                                  pull_protocol=2)
    assert first == {'state': 'scan_advanced'}
    with owner.platform.db.sessions.begin() as session:
        cursor = session.scalar(select(MetadataRow).where(
            MetadataRow.key.like('external_dispatch_fair_v2:%')))
        cursor.value = {'schema': 'sovite-external-dispatch-fair/v2', 'scope': {},
                        'created_at': float('nan'), 'id': 'corrupt'}
    with pytest.raises(PlatformError) as error:
        owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                              operation_id='external-v2-corrupt-next', external_profile='agent-http-v1',
                              pull_protocol=2)
    assert error.value.code == 'owner_unavailable'
    with owner.platform.db.sessions() as session:
        assert session.get(WorkerExecutionOperation, (owner.principal.workload_id,
                           owner.instance, 'external-v2-corrupt-next')) is None


@pytest.mark.parametrize('owner', ['direct'], indirect=True)
def test_success_advances_past_continuously_replenished_early_jobs(owner):
    earlier = submit(owner, key='cursor-success-earlier')
    later = submit(owner, key='cursor-success-later')
    with owner.platform.db.sessions.begin() as session:
        row = session.get(Job, earlier.id); row.created_at, row.next_attempt_at = 10, 0
        row = session.get(Job, later.id); row.created_at, row.next_attempt_at = 20, 0
    first = owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                                  operation_id='cursor-success-first')
    assert first['job']['job_id'] == earlier.id
    replenished = submit(owner, key='cursor-success-replenished')
    with owner.platform.db.sessions.begin() as session:
        row = session.get(Job, replenished.id); row.created_at, row.next_attempt_at = 0, 0
    second = owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                                   operation_id='cursor-success-second')
    assert second['job']['job_id'] == later.id


@pytest.mark.parametrize('owner', ['direct'], indirect=True)
def test_dispatch_cursor_rejects_duplicate_raw_keys(owner):
    import json
    from sqlalchemy import select, text
    from sovite.db import MetadataRow
    from sovite.service import PlatformError
    _profile_backlog(owner)
    assert owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                                 operation_id='cursor-dupe-seed')['state'] == 'idle'
    with owner.platform.db.sessions.begin() as session:
        cursor = session.scalars(select(MetadataRow).where(MetadataRow.key.like('dispatch_fair:%'))).one()
        raw = json.dumps(cursor.value)
        raw = '{"schema":"wrong",' + raw[1:]
        table = f'"{owner.platform.db.schema}".platform_metadata' if owner.platform.db.schema else 'platform_metadata'
        value = 'CAST(:raw AS JSON)' if owner.platform.db.schema else ':raw'
        session.execute(text(f'UPDATE {table} SET value={value} WHERE key=:key'), {'raw': raw, 'key': cursor.key})
    with pytest.raises(PlatformError) as error:
        owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                              operation_id='cursor-dupe-read')
    assert error.value.code == 'owner_unavailable'


@pytest.mark.parametrize('database', ['postgresql'], indirect=True)
@pytest.mark.parametrize('owner', ['direct'], indirect=True)
def test_identical_idle_retries_advance_cursor_and_audit_once(owner, monkeypatch):
    from sqlalchemy import select
    from sovite.db import AuditEvent, MetadataRow, WorkerExecutionOperation
    from test_execution_leases import _pg_race
    _profile_backlog(owner)
    operation = 'cursor-identical-idle'
    def pull():
        return owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                                     operation_id=operation)
    values = _pg_race(owner, monkeypatch, pull, pull)
    assert values == [{'state': 'idle'}, {'state': 'idle'}]
    with owner.platform.db.sessions() as session:
        cursor = session.scalars(select(MetadataRow).where(MetadataRow.key.like('dispatch_fair:%'))).one()
        assert cursor.value['id'] == 'control-099'
        journal = session.scalars(select(WorkerExecutionOperation).where(
            WorkerExecutionOperation.operation_id == operation)).all()
        assert len(journal) == 1 and journal[0].result == {'state': 'idle'}
        audit = session.scalars(select(AuditEvent).where(AuditEvent.action == 'task.execution_idle',
                                                        AuditEvent.target == owner.instance)).all()
        assert len(audit) == 1


@pytest.mark.parametrize('owner', ['direct'], indirect=True)
def test_dispatch_cursor_malformed_raw_json_fails_closed(owner):
    from sqlalchemy import select, text
    from sovite.db import MetadataRow
    from sovite.service import PlatformError
    if owner.platform.db.engine.dialect.name != 'sqlite':
        pytest.skip('PostgreSQL json storage rejects malformed JSON')
    _profile_backlog(owner)
    assert owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                                 operation_id='cursor-malformed-seed')['state'] == 'idle'
    with owner.platform.db.sessions.begin() as session:
        cursor = session.scalars(select(MetadataRow).where(MetadataRow.key.like('dispatch_fair:%'))).one()
        session.execute(text('UPDATE platform_metadata SET value=:raw WHERE key=:key'),
                        {'raw': '{', 'key': cursor.key})
    with pytest.raises(PlatformError) as error:
        owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                              operation_id='cursor-malformed-read')
    assert error.value.code == 'owner_unavailable'


@pytest.mark.parametrize('owner', ['direct'], indirect=True)
@pytest.mark.parametrize('corruption', ['numeric-bool', 'huge-created-at'])
def test_dispatch_cursor_rejects_wrong_types_and_oversized_numbers(owner, corruption):
    import json
    from sqlalchemy import select, text
    from sovite.db import MetadataRow
    from sovite.service import PlatformError
    _profile_backlog(owner)
    assert owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                                 operation_id='cursor-type-seed')['state'] == 'idle'
    with owner.platform.db.sessions.begin() as session:
        cursor = session.scalars(select(MetadataRow).where(MetadataRow.key.like('dispatch_fair:%'))).one()
        value = copy.deepcopy(cursor.value)
        if corruption == 'numeric-bool':
            value['scope']['repository_worker_only'] = 0
        else:
            value['created_at'] = 10 ** 400
        table = f'"{owner.platform.db.schema}".platform_metadata' if owner.platform.db.schema else 'platform_metadata'
        expression = 'CAST(:raw AS JSON)' if owner.platform.db.schema else ':raw'
        session.execute(text(f'UPDATE {table} SET value={expression} WHERE key=:key'),
                        {'raw': json.dumps(value), 'key': cursor.key})
    with pytest.raises(PlatformError) as error:
        owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                              operation_id='cursor-type-read')
    assert error.value.code == 'owner_unavailable'
