"""Actual Workspace retention of uncertain execution evidence under data authority."""
import asyncio
import hashlib
import shutil

import pytest

from sovite.flow import contract_revision
from sovite.output_contracts import approve_output
from sovite.repository_candidate_export import CandidateExportOwner
from sovite.repository_candidate_retention import CandidateRetention,CandidateRetentionError,WorkspaceTarget
from sovite.repository_recovery import record_for,payload_for
from sovite.repository_sessions import _canonical_json
from test_repository_recovery import interrupted,capture
from test_repository_candidate_export import prepared,candidate_additional_contracts
from test_repository_candidate_retention import served_workspace
from test_workflows import revision_ref


@pytest.fixture
def candidate_destinations(served_workspace):
    return ['private-state','workspace-'+served_workspace[2]]


@pytest.mark.parametrize('lost',['knowledge.artifact.begin-v1','knowledge.artifact.upload-v1',
                                 'knowledge.artifact.finalize-v1'])
def test_partial_candidate_fresh_successor_ready_recovery_without_work_success(
        interrupted,served_workspace,tmp_path,lost):
    platform,owner,job,attempt,effects,exports,_=interrupted
    recovered=capture(interrupted)
    current=platform.get_job(job.id)
    value=payload_for(platform,current,record_for(current,attempt_id=attempt))
    content=_canonical_json(value).decode()
    approved=approve_output(platform,source_job_id=job.id,source_digest=recovered['source_digest'],
        contract_id='candidate-export',expected_contract_revision=contract_revision(platform.active()[0]['output_contracts'][0]),
        expected_generation=platform.active()[1],content=content,evidence_refs=[revision_ref('partial-owner-review')],
        actor=job.caller)['release_id']
    exporter=CandidateExportOwner(platform,owner.resolver,owner.executor)
    client,loss,ws,*_=served_workspace
    target=WorkspaceTarget('https://workspace.invalid',ws,'workspace-'+ws,frozenset({'C3'}))
    loss.operation=lost
    async def scenario():
        async with client('producer') as producer:
            initial=CandidateRetention(tmp_path/'publication',target,producer)
            key=await initial.prepare(exporter,job.id,attempt,release_id=approved,
                principal_id=job.caller,workspace_id='workspace-1')
            with pytest.raises(CandidateRetentionError):await initial.retain(exporter,key)
            # Reconstruct the publisher from durable exact transport intent;
            # no new capture/model/lease operation is available here.
            receipt=await CandidateRetention(tmp_path/'publication',target,producer).retain(exporter,key)
        observed=platform.get_job(job.id)
        assert observed.status=='blocked' and observed.error_code=='execution_uncertain'
        assert observed.resolved['repository_run']['budget_reservations']==job.resolved['repository_run']['budget_reservations']
        evidence=[e for e in observed.resolved['repository_run']['evidence'] if e['label']=='retained-recovery-candidate']
        assert len(evidence)==1 and evidence[0]['attempt_id']==attempt
        owner.resolver.grant_id='producer-no-longer-current'
        shutil.rmtree(owner.executor.state_root)
        async with client('successor') as reader:
            raw=await CandidateRetention(tmp_path/'successor',target,reader).recover(receipt)
        assert raw==content.encode() and hashlib.sha256(raw).hexdigest()==receipt['sha256']
        assert value['kind']=='repository-recovery-artifact' and value['repair_eligible'] is False
    asyncio.run(scenario())
    assert effects==['edit-and-ambiguous-provider-call'] and exports==['export-v1']
