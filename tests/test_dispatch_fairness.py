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
