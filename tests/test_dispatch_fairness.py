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
