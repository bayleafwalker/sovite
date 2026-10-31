"""Historical candidate data uses current data-owner authority, not a Work lease."""
import asyncio
from pathlib import Path
import shutil

import pytest

from test_repository_candidate_export import prepared,release,candidate_additional_contracts
from test_repository_candidate_retention import served_workspace,candidate_destinations
from test_repository_currentness import Guard
from sovite.db import Job
from sovite.repository_currentness import bind_job
from sovite.repository_candidate_retention import CandidateRetention,WorkspaceTarget
from sovite.service import PlatformError


def test_ready_workspace_recovery_and_owner_history_survive_work_revocation(prepared,served_workspace,tmp_path):
    platform,exporter,job,attempt,*_=prepared
    guard=Guard();platform.repository_currentness_authorizer=guard
    # The existing completed-owner fixture isolates historical data paths. Bind
    # the same generic envelope that real admission installs, without claiming
    # any real Work-owner authority for this synthetic authorizer.
    with platform.db.sessions.begin() as session:
        bind_job(platform,session.get(Job,job.id),{'owner_selector':'opaque-existing-owner-tuple'})
    content=exporter._prepare_for_owner_review(job.id,attempt,principal_id=job.caller,workspace_id='workspace-1')
    approved=release(platform,job,content)
    client,loss,ws,*_=served_workspace
    target=WorkspaceTarget('https://workspace.invalid',ws,'workspace-'+ws,frozenset({'C3'}))
    state=tmp_path/'publication'
    async def scenario():
        async with client('producer') as current:
            retention=CandidateRetention(state,target,current)
            key=await retention.prepare(exporter,job.id,attempt,release_id=approved,
                principal_id=job.caller,workspace_id='workspace-1')
            receipt=await retention.retain(exporter,key)
            guard.current=False
            with platform.db.sessions() as session:
                with pytest.raises(PlatformError):platform.authorize_existing_job(session,session.get(Job,job.id))
            # Exact already-ready acknowledgement recovery must not restart
            # producer authorization or require the active Work hook.
            platform.repository_currentness_authorizer=None
            assert await retention.retain(exporter,key)==receipt
        exporter.resolver.grant_id='expired-producer'
        shutil.rmtree(exporter.execution.state_root)
        async with client('successor') as reader:
            raw=await CandidateRetention(tmp_path/'reader',target,reader).recover(receipt)
        assert raw==content.encode()
    asyncio.run(scenario())
    assert platform.public_status(job.receipt,job.caller)['status']=='available'
