"""Exact caller copy releases feed existing Workspace current-reader recovery."""
import asyncio
import hashlib
import shutil

import pytest
from sovite.repository_candidate_export import CandidateExportOwner
from sovite.repository_candidate_retention import CandidateRetention,CandidateRetentionError,WorkspaceTarget
from test_repository_recovery_release import (prepared,sqlite_prepared,candidate_additional_contracts,
    interrupted,client_for)
from test_repository_candidate_retention import served_workspace
from test_repository_runs import CALLER


@pytest.fixture
def candidate_destinations(served_workspace):
    return ['private-state','workspace-'+served_workspace[2]]


@pytest.mark.parametrize('lost',['knowledge.artifact.begin-v1','knowledge.artifact.upload-v1',
                                 'knowledge.artifact.finalize-v1'])
def test_exact_api_copy_release_survives_retention_lost_reply_and_current_reader(
        interrupted,served_workspace,tmp_path,lost):
    platform,owner,job,attempt,effects,exports,_=interrupted
    headers={'Authorization':'Bearer '+CALLER};path=f'/api/repository-runs/v1/runs/{job.id}'
    with client_for(interrupted) as api:
        captured=api.post(path+'/recovery',json={'workspace_id':'workspace-1','attempt_id':attempt},headers=headers)
        assert captured.status_code==200,captured.text
        body={'workspace_id':'workspace-1','attempt_id':attempt,'source_digest':captured.json()['source_digest'],
              'contract_id':'recovery-copy','destination_id':'workspace-'+served_workspace[2]}
        context=api.post(path+'/recovery-context',json=body,headers=headers)
        assert context.status_code==200,context.text
        request={**body,'expected_generation':context.json()['expected_generation'],
                 'expected_contract_revision':context.json()['contract_revision']}
        release=api.post(path+'/recovery-release',json=request,headers=headers)
        assert release.status_code==200,release.text
    exporter=CandidateExportOwner(platform,owner.resolver,owner.executor)
    client,loss,ws,*_=served_workspace
    target=WorkspaceTarget('https://workspace.invalid',ws,'workspace-'+ws,frozenset({'C3'}))
    loss.operation=lost
    async def scenario():
        async with client('producer') as producer:
            first=CandidateRetention(tmp_path/'publication',target,producer)
            key=await first.prepare(exporter,job.id,attempt,release_id=release.json()['release_id'],
                principal_id=job.caller,workspace_id='workspace-1')
            with pytest.raises(CandidateRetentionError):await first.retain(exporter,key)
            receipt=await CandidateRetention(tmp_path/'publication',target,producer).retain(exporter,key)
        owner.resolver.grant_id='producer-no-longer-current';shutil.rmtree(owner.executor.state_root)
        async with client('successor') as successor:
            raw=await CandidateRetention(tmp_path/'reader',target,successor).recover(receipt)
        assert hashlib.sha256(raw).hexdigest()==captured.json()['source_digest'].split(':')[1]
        async with client('outsider') as outsider:
            with pytest.raises(CandidateRetentionError):
                await CandidateRetention(tmp_path/'outsider',target,outsider).recover(receipt)
        async with client('admin') as admin:
            await admin.invoke('knowledge.member.set-v1',{'workspace_id':ws,
                'principal_id':'test:successor:0','role':None},idempotency_key='revoke-recovery-reader')
        async with client('successor') as revoked:
            with pytest.raises(CandidateRetentionError):
                await CandidateRetention(tmp_path/'reader',target,revoked).recover(receipt)
        observed=platform.get_job(job.id)
        assert observed.status==job.status and observed.error_code==job.error_code
        for name in ('budget_reservations','active_slots','usage_usd'):
            assert observed.resolved['repository_run'][name]==job.resolved['repository_run'][name]
        assert len(effects)==len(exports)==1
    asyncio.run(scenario())
