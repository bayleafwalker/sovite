"""Generic installed veto; the fixture is not a Work-owner implementation."""
import asyncio
import copy
import json

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import select

from sovite import api
from sovite.db import Job
from sovite.repository_currentness import CurrentnessError,FIELD,SCHEMA
from sovite.repository_runs import canonical_packet_digest
from sovite.service import PlatformError
from sovite.worker import Worker
from test_repository_runs import (setup,make_repo,BindingResolver,make_owner,packet,submit,CALLER)
from test_repository_candidate_export import prepared,release,candidate_destinations,candidate_additional_contracts


class Guard:
    def __init__(self):
        self.current=True;self.admissions=[];self.bound=[];self.checks=[];self.parent=None
    def admit(self,**kwargs):
        self.admissions.append(copy.deepcopy({key:value for key,value in kwargs.items() if key!='parent'}))
        self.parent=kwargs['parent']
        if not self.current:raise CurrentnessError('repository_currentness_revoked')
        return {'owner_selector':'opaque-existing-owner-tuple'}
    def bind_job(self,*,job,admission):
        assert job.id and job.caller=='cloud-codex'
        self.bound.append(job.id)
        return admission
    def check_job(self,*,job,binding,authorization_started_at):
        self.checks.append((job.id,authorization_started_at))
        assert binding=={'owner_selector':'opaque-existing-owner-tuple'}
        if not self.current:raise CurrentnessError('repository_currentness_revoked')


@pytest.fixture
def guarded(setup,tmp_path):
    settings,platform=setup
    bundle,_=platform.active()
    bundle=copy.deepcopy(bundle)
    next(row for row in bundle['grants'] if row['id']=='incident-forensics')['repository_currentness_required']=True
    path=tmp_path/'guarded.yaml';path.write_text(yaml.safe_dump(bundle));platform.reload(str(path))
    guard=Guard();platform.repository_currentness_authorizer=guard
    mirror,base=make_repo(tmp_path)
    resolver=BindingResolver(mirror,base,platform.active()[1])
    owner=make_owner(platform,resolver,executor=lambda *_:None)
    return settings,platform,guard,owner,packet(base)


def test_admission_binds_actual_uuid_principal_and_canonical_packet_without_wire_change(guarded):
    _,platform,guard,owner,value=guarded
    result=submit(owner,value)
    job=platform.get_job(result['id']);stored=job.resolved['repository_run'][FIELD]
    assert stored=={'schema_version':SCHEMA,'job_id':job.id,'principal_id':job.caller,
        'packet_sha256':canonical_packet_digest(value),'binding':{'owner_selector':'opaque-existing-owner-tuple'}}
    assert guard.bound==[job.id] and len(guard.admissions)==1 and guard.parent is None
    assert guard.admissions[0]['task']==value['goal']
    assert set(value)=={'schema_version','workspace_id','repository_id','executor_id','base_sha','goal',
                        'max_cost_usd','deadline','concurrency'}
    assert 'authorization_started_at' not in json.dumps(stored)
    with platform.db.sessions() as session:
        platform.authorize_existing_job(session,session.get(Job,job.id))
    assert guard.checks[0][0]==job.id


def test_required_grant_cannot_use_plain_job_when_guard_or_optional_runtime_missing(guarded):
    _,platform,guard,owner,value=guarded
    platform.repository_currentness_authorizer=None
    with pytest.raises(PlatformError) as plain:
        platform.submit('incident-forensics','Plain bypass','cloud-codex','plain-bypass')
    assert plain.value.code=='repository_currentness_required'
    with pytest.raises(PlatformError) as repository:
        submit(owner,value)
    assert repository.value.code=='repository_currentness_unavailable'
    with platform.db.sessions() as session:
        assert session.scalar(select(Job)) is None


def test_stale_active_work_refuses_claim_but_preserves_history_cancel_and_reconciliation(guarded):
    _,platform,guard,owner,value=guarded
    result=submit(owner,value);guard.current=False
    assert submit(owner,copy.deepcopy(value))['id']==result['id']
    assert len(guard.admissions)==1
    assert owner.get(result['id'],principal_id='cloud-codex')['id']==result['id']
    assert owner.list(principal_id='cloud-codex',workspace_id='workspace-1')[0]['id']==result['id']
    assert platform.public_status(platform.get_job(result['id']).receipt,'cloud-codex')['status']=='pending'
    with platform.db.sessions() as session:
        job=session.get(Job,result['id'])
        with pytest.raises(PlatformError) as denied:platform.authorize_existing_job(session,job)
        assert denied.value.code=='repository_currentness_revoked'
        platform.authorize_existing_job(session,job,purpose='observe')
    assert Worker(platform,repository_run_executor=object()).claim() is None
    assert platform.get_job(result['id']).status=='blocked'
    owner.cancel(result['id'],principal_id='cloud-codex',workspace_id='workspace-1')
    assert owner.get(result['id'],principal_id='cloud-codex')['id']==result['id']


@pytest.mark.parametrize('field,value',[('job_id','foreign-job'),('principal_id','other-user'),
                                      ('packet_sha256','0'*64),('schema_version','unknown')])
def test_foreign_or_changed_generic_binding_refuses_before_guard(guarded,field,value):
    _,platform,guard,owner,request=guarded
    result=submit(owner,request)
    with platform.db.sessions.begin() as session:
        row=session.get(Job,result['id']);resolved=copy.deepcopy(row.resolved)
        resolved['repository_run'][FIELD][field]=value;row.resolved=resolved
    with platform.db.sessions() as session:
        with pytest.raises(PlatformError) as refused:
            platform.authorize_existing_job(session,session.get(Job,result['id']))
    assert refused.value.code=='repository_currentness_binding_invalid' and guard.checks==[]


def test_bound_job_missing_hook_fails_active_but_not_data_owner_observation(guarded):
    _,platform,guard,owner,value=guarded
    result=submit(owner,value);platform.repository_currentness_authorizer=None
    with platform.db.sessions() as session:
        row=session.get(Job,result['id'])
        with pytest.raises(PlatformError) as refused:platform.authorize_existing_job(session,row)
        assert refused.value.code=='repository_currentness_unavailable'
        platform.authorize_existing_job(session,row,purpose='observe')
    assert submit(owner,copy.deepcopy(value))['id']==result['id']
    assert owner.cancel(result['id'],principal_id='cloud-codex',workspace_id='workspace-1')['id']==result['id']


@pytest.mark.parametrize('bad',[{'not_finite':float('inf')},{'oversized':'x'*(16*1024)}, {'not_json':b'bytes'}])
def test_invalid_opaque_guard_binding_cannot_create_a_job(guarded,bad):
    _,platform,guard,owner,value=guarded
    guard.bind_job=lambda **kwargs:bad
    with pytest.raises(PlatformError) as refused:submit(owner,value)
    assert refused.value.code=='repository_currentness_binding_invalid'
    with platform.db.sessions() as session:assert session.scalar(select(Job)) is None


def test_unknown_guard_error_is_static_and_contains_no_owner_details(guarded):
    _,platform,guard,owner,value=guarded
    def unavailable(**kwargs):raise RuntimeError('private-owner-endpoint bearer=sensitive-fixture')
    guard.admit=unavailable
    with pytest.raises(PlatformError) as refused:submit(owner,value)
    assert refused.value.status==503 and refused.value.code=='repository_currentness_unavailable'
    assert 'private-owner' not in str(refused.value) and 'sensitive' not in str(refused.value)


def test_outer_authorization_timer_is_shared_through_recursive_checks_and_then_reset(guarded):
    _,platform,guard,owner,value=guarded
    first=submit(owner,value)['id'];second=submit(owner,{**value,'goal':'Second authorized fixture'},
                                               idempotency_key='test-key-00000001')['id']
    original=guard.check_job
    def recursive(**kwargs):
        original(**kwargs)
        if kwargs['job'].id==first:
            with platform.db.sessions() as session:
                platform.authorize_existing_job(session,session.get(Job,second))
    guard.check_job=recursive
    with platform.db.sessions() as session:platform.authorize_existing_job(session,session.get(Job,first))
    assert len(guard.checks)==2 and guard.checks[0][1]==guard.checks[1][1]
    with platform.db.sessions() as session:platform.authorize_existing_job(session,session.get(Job,second))
    assert guard.checks[2][1]>guard.checks[0][1]


def test_public_purpose_and_resolved_binding_fields_cannot_bypass_active_guard(guarded):
    settings,platform,guard,owner,value=guarded
    guard.current=False
    app=api.create_app(settings,platform,repository_run_owner=owner,
                       repository_currentness_authorizer=guard)
    with TestClient(app) as client:
        headers={'Authorization':'Bearer '+CALLER,'Idempotency-Key':'public-bypass-0001'}
        from sovite.repository_runs import expected_preview_id
        body={'schema_version':'sovite-repository-run/v1','packet':value,
              'preview_id':expected_preview_id(value,'cloud-codex')}
        for extra in ({'purpose':'observe'},{'repository_currentness_required':False},
                      {'project_currentness':{'purpose':'observe'}},
                      {'resolved':{'repository_run':{'purpose':'observe'}}}):
            assert client.post('/api/repository-runs/v1/runs',json={**body,**extra},headers=headers).status_code==422
        assert client.post('/api/repository-runs/v1/runs',json=body,headers=headers).status_code==409
        assert app.state.platform.repository_currentness_authorizer is guard
    with pytest.raises(ValueError):api.create_app(settings,platform,repository_currentness_authorizer=Guard())


def test_missing_required_guard_is_not_advertised_and_discovery_makes_no_work_reads(guarded):
    _,platform,guard,owner,_=guarded
    platform.repository_currentness_authorizer=None
    capabilities=owner.capabilities('workspace-1','cloud-codex')
    assert capabilities['availability']=='unavailable'
    executor=capabilities['executors'][0]
    assert not executor['launch_supported']
    assert executor['blockers'][0]['code']=='repository_currentness_unavailable'
    assert not guard.admissions and not guard.checks
    platform.repository_currentness_authorizer=guard
    assert owner.capabilities('workspace-1','cloud-codex')['availability']=='available'
    assert not guard.admissions and not guard.checks


def test_parent_currentness_is_inherited_from_actual_job_even_without_new_requirement(guarded):
    from sovite.repository_currentness import admit
    _,platform,guard,owner,value=guarded
    result=submit(owner,value)
    with platform.db.sessions() as session:
        parent=session.get(Job,result['id'])
        recipe=copy.deepcopy(parent.resolved);recipe['grant']['repository_currentness_required']=False
        child=copy.deepcopy(parent.resolved['repository_run']);child.pop(FIELD)
        child['repair']={'parent_run_id':parent.id}
        assert admit(platform,session,repository_run=child,task=parent.task,
            caller=parent.caller,recipe=recipe)=={'owner_selector':'opaque-existing-owner-tuple'}
        assert guard.parent is parent
        platform.repository_currentness_authorizer=None
        with pytest.raises(CurrentnessError) as refused:
            admit(platform,session,repository_run=child,task=parent.task,caller=parent.caller,recipe=recipe)
        assert refused.value.code=='repository_currentness_unavailable'


def test_cancelled_started_worker_can_record_cleanup_after_work_revocation(guarded):
    from sovite.db import WorkerExecutionLease
    _,platform,guard,owner,value=guarded
    result=submit(owner,value)
    worker=Worker(platform,repository_run_executor=object())
    claimed=worker.claim();assert claimed is not None and claimed.id==result['id']
    guard.current=False
    owner.cancel(result['id'],principal_id='cloud-codex',workspace_id='workspace-1')
    worker.finish(claimed.id,cancelled=True,fence=claimed.execution_fence)
    row=platform.get_job(claimed.id)
    assert row.status=='cancelled' and row.lease_owner is None and row.lease_until is None
    with platform.db.sessions() as session:
        assert session.get(WorkerExecutionLease,claimed.id).phase=='finished'


def test_historical_candidate_data_uses_current_data_grant_without_work_activity(prepared):
    from sovite.repository_currentness import bind_job
    from sovite.repository_candidate_export import CandidateExportError
    platform,exporter,job,attempt,*_=prepared
    guard=Guard();platform.repository_currentness_authorizer=guard
    with platform.db.sessions.begin() as session:
        bind_job(platform,session.get(Job,job.id),{'owner_selector':'opaque-existing-owner-tuple'})
    guard.current=False
    with platform.db.sessions() as session:
        with pytest.raises(PlatformError):platform.authorize_existing_job(session,session.get(Job,job.id))
    content=exporter._prepare_for_owner_review(job.id,attempt,principal_id=job.caller,workspace_id='workspace-1')
    approved=release(platform,job,content)
    assert exporter.export(job.id,attempt,release_id=approved,destination='private-state',
        principal_id=job.caller,workspace_id='workspace-1')==content
    assert platform.public_status(job.receipt,job.caller)['status']=='available'
    platform.inhibit(job.grant_id,'Current data-owner authority withdrawn')
    with pytest.raises(CandidateExportError):
        exporter.export(job.id,attempt,release_id=approved,destination='private-state',
            principal_id=job.caller,workspace_id='workspace-1')


@pytest.mark.parametrize('response',[False,True,{}])
def test_check_hook_requires_raise_or_none_not_a_boolean_authority(guarded,response):
    _,platform,guard,owner,value=guarded
    result=submit(owner,value)
    guard.check_job=lambda **kwargs:response
    with platform.db.sessions() as session:
        with pytest.raises(PlatformError) as refused:
            platform.authorize_existing_job(session,session.get(Job,result['id']))
        assert refused.value.code=='repository_currentness_unavailable'


def test_unavailable_guard_never_reports_idle_or_changes_existing_queue(guarded):
    _,platform,guard,owner,value=guarded
    result=submit(owner,value);platform.repository_currentness_authorizer=None
    before=platform.get_job(result['id'])
    with pytest.raises(PlatformError) as refused:
        Worker(platform,repository_run_executor=object()).claim()
    assert refused.value.status==503
    after=platform.get_job(result['id'])
    assert (after.status,after.attempts,after.lease_owner,after.lease_until)==(
        before.status,before.attempts,before.lease_owner,before.lease_until)
