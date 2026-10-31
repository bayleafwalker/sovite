"""Repository-only eligibility never changes ordinary owner jobs or protocol."""
from dataclasses import replace
import pytest
from sovite import lease_service as leases
from sovite.adapters import RuntimeFailure
from sovite.worker import Worker
from test_execution_leases import owner,submit,pull,context,snapshot,refused
from test_migrations import database
from test_policy_v1alpha2 import bundle as gateway_bundle


def test_repository_only_rejects_ordinary_reservation_replay_and_start(owner):
    submit(owner);reserved=pull(owner)
    restricted=leases.LeaseService(owner.platform,owner.service.id,{},repository_worker_only=True)
    before=snapshot(owner.platform)
    refused('execution_scope_denied',restricted.reserve,principal=owner.principal,
        instance_id=owner.instance,operation_id='pull-1')
    refused('execution_scope_denied',restricted.start,principal=owner.principal,
        context=context(owner,reserved))
    assert snapshot(owner.platform)==before
    # Default-false service continues the actual existing protocol unchanged.
    assert owner.service.start(principal=owner.principal,context=context(owner,reserved))['state']=='started'


def test_repository_only_cannot_renew_finish_or_hand_back_ordinary_job(owner):
    submit(owner)
    worker=Worker(owner.platform);job=worker.claim();assert job is not None
    worker.leases.repository_worker_only=True
    before=snapshot(owner.platform)
    assert worker.renew(job.id,fence=job.execution_fence)is False
    worker.hand_back(job.id,fence=job.execution_fence)
    worker.finish(job.id,error=RuntimeFailure('fixture_failure'),fence=job.execution_fence)
    assert snapshot(owner.platform)==before
