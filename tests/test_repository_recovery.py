"""Interrupted native-callback recovery keeps the real owner outcome unknown.

Git, lifecycle, artifact, release and quarantine paths are real. Cloud transport
is an explicit unqualified fixture; the separate KVM probe supplies its proof.
"""
import asyncio
from dataclasses import dataclass,asdict
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
from types import SimpleNamespace

import pytest

from sovite.adapters import RuntimeFailure
from sovite.db import Job,WorkerExecutionLease
from sovite.flow import contract_revision
from sovite.output_contracts import approve_output,read_output
from sovite.repository_candidate_export import CandidateExportOwner,CandidateExportError
from sovite.repository_native_vm import BoundNativeVM
import sovite.repository_native_vm as native_vm
import sovite.repository_native_git as native_git
from sovite.repository_native_recovery import recover_attempt
from sovite.repository_recovery import RepositoryRecoveryService,record_for,payload_for
from sovite.repository_runs import RepositoryRunError
from sovite.repository_sessions import _canonical_json
from sovite.worker import Worker
from test_repository_candidate_export import prepared,candidate_destinations,candidate_additional_contracts
from test_repository_runs import make_owner,packet,submit,git
from test_workflows import revision_ref


@dataclass(frozen=True)
class Identity:
    job_id:str
    attempt_id:str
    workspace_id:str
    repository_id:str
    generation:str
    fence_sha256:str
    deadline:float


@dataclass(frozen=True)
class Descriptor:
    identity:Identity
    observed:dict
    recovery_until:float


@pytest.fixture
def interrupted(prepared,tmp_path,monkeypatch):
    platform,exporter,_,_,mirror,base=prepared
    lifecycle=exporter.execution
    lifecycle.timeout_seconds=1
    monkeypatch.setattr(native_git,'_PROCESS_LIMIT',4096)
    monkeypatch.setattr(native_git,'_tool',lambda name:Path(shutil.which(name)).resolve(strict=True))
    monkeypatch.setattr(native_git,'_git_exec_path',lambda binary:Path(subprocess.run(
        [str(binary),'--exec-path'],check=True,capture_output=True,text=True).stdout.strip()))
    state=tmp_path/'native';state.mkdir(mode=0o700)
    driver=SimpleNamespace(state_root=state,platform=platform,observer=lambda _:None,
        git_facts=native_git.tool_facts(),config={'descriptor_path':str(tmp_path/'descriptor'),
            'descriptor_owner_uid':__import__('os').getuid(),'routine_credentials_root':str(tmp_path)})
    effects=[];exports=[];quiesces=[]
    values={}
    async def exchange(verb,*_args,**kwargs):
        if verb=='quiesce-v1':
            quiesces.append(verb)
            return _canonical_json(values['ack'])
        assert verb=='export-v1'
        exports.append(verb)
        return values['frame']
    async def verify_key():pass
    routines=SimpleNamespace(exchange=exchange,verify_key=verify_key)
    monkeypatch.setattr(native_vm,'ForcedSSH',lambda *_args:routines)
    def load(_path,*,expected,observe,owner_uid,now):
        assert expected==values['descriptor'].identity
        assert now<values['descriptor'].recovery_until
        return values['descriptor']
    def frame(stream,*,exported):
        header=json.loads(stream.readline());bundle=stream.read()
        assert len(bundle)==header['bundle_bytes']
        assert hashlib.sha256(bundle).hexdigest()==header['bundle_sha256']
        return header,bundle
    owner_module=SimpleNamespace(AttemptIdentity=Identity,load_recovery_descriptor=load,
        validate_quiescence=lambda raw,descriptor:json.loads(raw),read_bundle_frame=frame)
    monkeypatch.setattr(native_vm,'_owner_module',lambda:owner_module)
    class Driver:
        async def __call__(self,*_args):raise AssertionError('Per-attempt binding required')
        async def recover_attempt(self,**kwargs):return await recover_attempt(driver,**kwargs)
        def for_attempt(self,context):
            identity=Identity(context.job_id,context.attempt_id,context.workspace_id,context.repository_id,
                context.generation,hashlib.sha256(context.fence.encode()).hexdigest(),context.deadline)
            descriptor=Descriptor(identity,{'boot_id':'fixture-original-boot'},context.deadline+60)
            values['descriptor']=descriptor
            owner_module.load_descriptor=lambda *_args,**_kwargs:descriptor
            bound=BoundNativeVM(driver,context,descriptor,owner_module,routines=routines)
            async def execute(root,intent,worktree,goal,budget,authorization_check,repair_context,transport):
                run=platform.get_job(context.job_id).resolved['repository_run']
                bound._persist_recovery(root,base,base,run)
                budget.acquire_slot('ambiguous-provider-call')
                budget.reserve_cost('ambiguous-provider-call',.4)
                guest=tmp_path/'guest'
                git('clone','--no-hardlinks',str(worktree),str(guest))
                (guest/'src'/'main.txt').write_text('partial guest edit before interruption\n')
                git('add','-A',cwd=guest)
                git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','partial',cwd=guest)
                tip=git('rev-parse','HEAD',cwd=guest)
                git('update-ref','refs/heads/candidate',tip,cwd=guest)
                bundle=tmp_path/'partial.bundle'
                git('bundle','create',str(bundle),'refs/heads/candidate','^'+base,cwd=guest)
                raw=bundle.read_bytes()
                values['ack']={'original_base_sha':base,'boot_id':'fixture-original-boot',
                               'recovery_until':descriptor.recovery_until}
                header={'schema_version':'cloud.native-vm-export.v1','original_base_sha':base,
                    'tip_sha':tip,'bundle_bytes':len(raw),'bundle_sha256':hashlib.sha256(raw).hexdigest(),
                    'quiescence':values['ack']}
                values['frame']=_canonical_json(header)+b'\n'+raw
                effects.append('edit-and-ambiguous-provider-call')
                raise RuntimeFailure('external_execution_uncertain')
            bound._execute_native=execute
            return bound
    lifecycle.executor=Driver()
    owner=make_owner(platform,exporter.resolver,executor=lifecycle)
    created=submit(owner,packet(base),idempotency_key='interruption-recovery-fixture')
    assert asyncio.run(Worker(platform,repository_run_executor=lifecycle).run_once())
    job=platform.get_job(created['id'])
    assert job.status=='blocked' and job.error_code=='execution_uncertain'
    assert len(effects)==1 and exports==[] and quiesces==['quiesce-v1']
    attempt=job.resolved['repository_run']['attempts'][-1]['id']
    return platform,owner,job,attempt,effects,exports,values


def capture(fixture):
    platform,owner,job,attempt,*_=fixture
    return asyncio.run(RepositoryRecoveryService(owner).capture(job.id,attempt,
        principal_id=job.caller,workspace_id='workspace-1'))


def test_fresh_successor_recovers_exact_partial_bytes_after_native_deadline(interrupted):
    platform,owner,before,attempt,effects,exports,values=interrupted
    time.sleep(max(0,values['descriptor'].identity.deadline-time.time())+.02)
    recovered=capture(interrupted)
    assert recovered['kind']=='repository-recovery-artifact'
    assert recovered['publication_eligible'] is False and recovered['repair_eligible'] is False
    after=platform.get_job(before.id)
    assert (after.status,after.error_code,after.artifact_key,after.artifact_sha256,after.public_projection)==(
        before.status,before.error_code,before.artifact_key,before.artifact_sha256,before.public_projection)
    for key in ('budget_reservations','active_slots','usage_usd','attempts'):
        assert after.resolved['repository_run'][key]==before.resolved['repository_run'][key]
    assert after.resolved['repository_run']['budget_reservations'][0]['state']=='reserved'
    artifact=record_for(after,attempt_id=attempt);data=payload_for(platform,after,artifact)
    assert data['receipt_projection']['within_deadline'] is False
    assert data['bundle_base64'] and data['receipt_projection']['changed_paths']==['src/main.txt']
    # A new service object reconciles exact immutable evidence. No native,
    # model, seed or second guest capture occurs.
    assert capture(interrupted)==recovered
    assert effects==['edit-and-ambiguous-provider-call'] and exports==['export-v1']
    content=_canonical_json(data).decode()
    approved=approve_output(platform,source_job_id=before.id,source_digest=recovered['source_digest'],
        contract_id='candidate-export',expected_contract_revision=contract_revision(platform.active()[0]['output_contracts'][0]),
        expected_generation=platform.active()[1],content=content,evidence_refs=[revision_ref('partial-review')],
        actor=before.caller)
    exporter=CandidateExportOwner(platform,owner.resolver,owner.executor)
    assert exporter.export(before.id,attempt,release_id=approved['release_id'],destination='private-state',
        principal_id=before.caller,workspace_id='workspace-1')==content
    assert platform.public_status(before.receipt,before.caller)['status']!='available'


@pytest.mark.parametrize('change',['foreign-principal','foreign-attempt','live-lease','revoke-binding'])
def test_recovery_requires_exact_owner_attempt_and_no_live_execution(interrupted,change):
    platform,owner,job,attempt,_,exports,_=interrupted
    principal=job.caller
    if change=='foreign-principal':principal='other-user'
    if change=='foreign-attempt':attempt+='-foreign'
    if change=='live-lease':
        with platform.db.sessions.begin() as session:
            lease=session.get(WorkerExecutionLease,job.id);lease.phase='started';lease.lease_until=time.time()+60
    if change=='revoke-binding':owner.resolver.grant_id='revoked-grant'
    with pytest.raises(RepositoryRunError):
        asyncio.run(RepositoryRecoveryService(owner).capture(job.id,attempt,
            principal_id=principal,workspace_id='workspace-1'))
    assert exports==[]


@pytest.mark.parametrize('change',['missing-context','wrong-packet','changed-boot'])
def test_missing_or_substituted_recovery_context_never_recaptures(interrupted,change):
    platform,owner,job,attempt,_,exports,values=interrupted
    root=next((Path(owner.executor.state_root).parent/'native').iterdir())
    if change=='missing-context':(root/'recovery-context.json').unlink()
    elif change=='wrong-packet':
        path=root/'recovery-context.json';value=json.loads(path.read_bytes())
        value['packet_sha256']='0'*64;path.write_bytes(_canonical_json(value))
    else:
        from dataclasses import replace
        values['descriptor']=replace(values['descriptor'],observed={'boot_id':'foreign-reboot'})
    with pytest.raises(RepositoryRunError):capture(interrupted)
    assert exports==[] and not platform.get_job(job.id).resolved['repository_run'].get('recoveries')


def test_current_work_revocation_does_not_erase_partial_data_authority(interrupted):
    from test_repository_currentness import Guard
    from sovite.repository_currentness import bind_job
    from sovite.service import PlatformError
    platform,_,job,_,_,_,_=interrupted
    guard=Guard();platform.repository_currentness_authorizer=guard
    with platform.db.sessions.begin() as session:
        bind_job(platform,session.get(Job,job.id),{'owner_selector':'fixture-owner-work'})
    guard.current=False
    with platform.db.sessions() as session:
        with pytest.raises(PlatformError):platform.authorize_existing_job(session,session.get(Job,job.id))
    assert capture(interrupted)['repair_eligible'] is False


def test_recovery_kind_cannot_become_a_repair_predecessor(interrupted):
    from sovite.repository_repairs import admit
    platform,_,job,_,_,_,_=interrupted
    capture(interrupted)
    # A specific negative control for the explicit recovery-kind barrier, even
    # if another defect were to mislabel the uncertain Job as completed.
    with platform.db.sessions.begin() as session:
        parent=session.get(Job,job.id);parent.status='completed'
        repair={key:None for key in ('parent_run_id','root_run_id','repair_number','max_repairs',
            'candidate_sha256','candidate_tip_sha','candidate_release_id','feedback_release_id','feedback_sha256',
            'feedback_contract_ids','input_key','input_sha256')}
        repair['parent_run_id']=job.id
        with pytest.raises(Exception) as refused:
            admit(platform,session,{'repair':repair},job.caller,{},job.task)
        assert getattr(refused.value,'code',None)=='repository_repair_unavailable'


def test_generated_recovery_http_route_uses_current_exact_owner(interrupted):
    from fastapi.testclient import TestClient
    from sovite.api import create_app
    from test_repository_runs import CALLER
    platform,owner,job,attempt,_,exports,_=interrupted
    with TestClient(create_app(platform.settings,platform,repository_run_owner=owner)) as client:
        body={'workspace_id':'workspace-1','attempt_id':attempt}
        url=f'/api/repository-runs/v1/runs/{job.id}/recovery'
        headers={'Authorization':'Bearer '+CALLER}
        for extra in ({'purpose':'observe'},{'deadline':time.time()+3600},
                      {'resolved':{'project_currentness':{}}},{'owner':'foreign'}):
            assert client.post(url,json={**body,**extra},headers=headers).status_code==422
        assert exports==[]
        response=client.post(url,json=body,headers=headers)
        assert response.status_code==200,response.text
        assert response.json()['repair_eligible'] is False
        assert client.post(url,json=body,headers=headers).json()==response.json()
        assert exports==['export-v1']


def test_lost_host_ack_reuses_validated_local_bytes_without_vm_recapture(interrupted,monkeypatch):
    platform,owner,job,attempt,effects,exports,values=interrupted
    original=platform.store.put;lost=True
    def lose_once(job_id,value):
        nonlocal lost
        result=original(job_id,value)
        if lost and job_id==job.id and value.get('kind')=='repository-recovery-artifact':
            lost=False
            raise RuntimeError('public fixture lost host acknowledgement')
        return result
    monkeypatch.setattr(platform.store,'put',lose_once)
    with pytest.raises(RepositoryRunError):capture(interrupted)
    assert not platform.get_job(job.id).resolved['repository_run'].get('recoveries')
    assert exports==['export-v1']
    recovered=capture(interrupted)
    assert recovered['source_digest'] and exports==['export-v1'] and len(effects)==1


def test_source_grant_revocation_after_capture_still_withholds_new_disclosure(interrupted,monkeypatch):
    from sovite.service import PlatformError
    platform,owner,job,attempt,_,_,_=interrupted
    captured=capture(interrupted)
    current=platform.get_job(job.id)
    content=_canonical_json(payload_for(platform,current,record_for(current,attempt_id=attempt))).decode()
    platform.settings.caller_tokens.pop(job.caller)
    with pytest.raises(RepositoryRunError):capture(interrupted)
    with pytest.raises(PlatformError):
        approve_output(platform,source_job_id=job.id,source_digest=captured['source_digest'],
            contract_id='candidate-export',expected_contract_revision=contract_revision(platform.active()[0]['output_contracts'][0]),
            expected_generation=platform.active()[1],content=content,evidence_refs=[revision_ref('partial-review')],
            actor=job.caller)


def test_existing_lease_reaper_recovers_partitioned_owner_without_reexecution(interrupted):
    import copy
    platform,owner,job,_,effects,exports,_=interrupted
    # Model the durable records left when the old controller disappears before
    # its normal error/handback write. The existing lease owner, not recovery,
    # must first classify that execution as uncertain.
    with platform.db.sessions.begin() as session:
        current=session.get(Job,job.id);lease=session.get(WorkerExecutionLease,job.id)
        current.status='running';current.error_code=None;current.lease_until=time.time()-1
        lease.phase='started';lease.lease_until=current.lease_until
        resolved=copy.deepcopy(current.resolved)
        resolved['repository_run']['attempts'][-1]['state']='running'
        current.resolved=resolved
    with pytest.raises(RepositoryRunError):capture(interrupted)
    Worker(platform,repository_run_executor=owner.executor).recover_expired()
    assert platform.get_job(job.id).status=='blocked'
    assert capture(interrupted)['outcome']=='uncertain'
    assert len(effects)==1 and exports==['export-v1']
