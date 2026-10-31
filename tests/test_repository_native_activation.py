"""Actual owner/lease/HTTP boundaries; fixture driver never claims native qualification."""
from __future__ import annotations
import asyncio
import copy
import hashlib
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient
from sovite import policy
from sovite.adapters import RuntimeFailure
from sovite.api import create_app
from sovite.db import Job, WorkerExecutionLease
from sovite.repository_native_vm import NativeVMDriver
from sovite.repository_runs import RepositoryRunAttemptExecutor, RepositoryRunError
from sovite.repository_repair_contract.runtime import load_bundle, NATIVE_ACTIVATION_OPERATION
from sovite.service import Platform, PlatformError
from sovite.settings import Settings
from sovite.worker import Worker
from sovite import repository_native_activation as activation
from test_migrations import database
from test_repository_runs import BindingResolver, make_owner, make_repo, packet, submit, ROOT, CALLER


class FixtureDriver:
    """Uses the production bounded wait, with an explicit unqualified file validator."""
    prepare_attempt=NativeVMDriver.prepare_attempt
    def __init__(self,path):
        self.config={'descriptor_path':str(path)}
        self.entered=asyncio.Event();self.effect=asyncio.Event();self.finish=asyncio.Event()
        self.context=None;self.calls=0;self.effects=0;self.profile=None
    async def __call__(self,*args,**kwargs):
        raise AssertionError("per-attempt binding required")
    def activation_profile(self):return {**self.profile,'vm_id':'17'}
    async def prepare_attempt(self,context,authorization_check):
        self.context=context;self.entered.set()
        await NativeVMDriver.prepare_attempt(self,context,authorization_check)
    def for_attempt(self,context):
        self.calls+=1
        if Path(self.config['descriptor_path']).read_text()!='ready':
            raise RuntimeFailure('native_vm_descriptor_unavailable')
        async def execute(*args,**kwargs):
            self.effects+=1;self.effect.set();await self.finish.wait()
        return execute


@pytest.fixture
def native_owner(database,tmp_path,monkeypatch):
    bundle,_=policy.load_bundle(ROOT/'config/platform.local.yaml')
    config=tmp_path/'bundle.yaml';config.write_text(yaml.safe_dump(bundle))
    settings=Settings(database_url=database.engine.url.render_as_string(hide_password=False),
        database_schema=database.schema,bundle_path=str(config),artifact_path=str(tmp_path/'artifacts'),
        operator_token='operator-repo-test-token-0000000000',caller_tokens={'cloud-codex':CALLER},
        require_qualification=False,embedded_worker=False,secure_cookies=False,lease_seconds=30,
        repository_worker_only=True)
    platform=Platform(settings);platform.db.engine.dispose();platform.db=database
    database.migrate();platform.initialize()
    mirror,base=make_repo(tmp_path);resolver=BindingResolver(mirror,base,platform.active()[1])
    driver=FixtureDriver(tmp_path/'descriptor.json')
    lifecycle=RepositoryRunAttemptExecutor(platform,resolver,tmp_path/'state',driver)
    owner=make_owner(platform,resolver,lifecycle);driver.profile=owner.test_profile
    locator=tmp_path/'activation';locator.mkdir(mode=0o750);locator.chmod(0o750)
    monkeypatch.setattr(activation,'LOCATOR',locator/'request.json')
    app=create_app(settings,platform,repository_run_owner=owner)
    yield SimpleNamespace(platform=platform,owner=owner,driver=driver,lifecycle=lifecycle,
        resolver=resolver,base=base,app=app,locator=locator/'request.json')


def headers():return {'Authorization':'Bearer operator-repo-test-token-0000000000'}
def route(job):return f'/api/repository-runs/v1/runs/{job.id}/attempts/{job.id}-{job.attempts}/activation'

def queued(f,key='native-activation-test'):
    value=submit(f.owner,packet(f.base),idempotency_key=key)
    worker=Worker(f.platform,repository_run_executor=f.lifecycle);job=worker.claim()
    assert job.id==value['id'];return worker,job


async def waiting(f,worker,job):
    task=asyncio.create_task(worker.execute(job))
    await asyncio.wait_for(f.driver.entered.wait(),10)
    for _ in range(100):
        if f.locator.exists():break
        await asyncio.sleep(.01)
    assert f.locator.exists()
    return task


def test_current_exact_owner_http_then_running_same_identity(native_owner):
    f=native_owner;worker,job=queued(f)
    async def run():
        task=await waiting(f,worker,job)
        try:
            with TestClient(f.app)as client:
                response=client.get(route(job),headers=headers());assert response.status_code==200,response.text
                value=response.json();load_bundle().validate_response(NATIVE_ACTIVATION_OPERATION,200,value)
                assert value['job_id']==job.id and value['attempt_id']==f'{job.id}-1'
                assert value['fence_sha256']==hashlib.sha256(job.execution_fence.encode()).hexdigest()
                assert value['deadline']==f.driver.context.deadline<=job.execution_deadline
                assert value['principal_id']==job.caller and value['vm_id']=='17'
                assert not {'fence','execution','task','model','capability'}&value.keys()
                assert json.loads(f.locator.read_text())=={'job_id':job.id,'attempt_id':f'{job.id}-1'}
                assert f.locator.stat().st_mode&0o777==0o440
                assert f.driver.effects==0 and not f.lifecycle.effect_started(job.id)
                Path(f.driver.config['descriptor_path']).write_text('ready')
                await asyncio.wait_for(f.driver.effect.wait(),10)
                running=client.get(route(job),headers=headers());assert running.status_code==200,running.text
                assert {k:v for k,v in running.json().items()if k!='lease_until'}=={k:v for k,v in value.items()if k!='lease_until'}
                assert f.lifecycle.effect_started(job.id)
                f.resolver.grant_id='revoked'
                assert client.get(route(job),headers=headers()).status_code==409
        finally:
            worker.stop.set();await asyncio.wait_for(task,10)
    asyncio.run(run())
    assert f.platform.get_job(job.id).error_code=='execution_uncertain'


@pytest.mark.parametrize('fault',['attempt','caller','fence','metadata','cancel','lease','profile','purpose','body','token'])
def test_current_projection_closed_and_current(native_owner,fault):
    f=native_owner;worker,job=queued(f)
    async def run():
        task=await waiting(f,worker,job)
        try:
            url=route(job);auth=headers();method='get';content=None
            with TestClient(f.app)as client:
                assert client.get(url,headers=auth).status_code==200
                if fault=='attempt':url=url.replace(f'{job.id}-1',f'{job.id}-2')
                elif fault=='profile':f.driver.profile={**f.driver.profile,'publication_revision':'foreign'}
                elif fault=='purpose':url+='?purpose=observe'
                elif fault=='body':method='request';content=b'{"purpose":"observe"}'
                elif fault=='token':auth={'Authorization':'Bearer '+CALLER}
                else:
                    with f.platform.db.sessions.begin()as session:
                        row=session.get(Job,job.id)
                        if fault=='caller':row.caller='other-user'
                        elif fault=='cancel':row.cancel_requested=True
                        elif fault=='lease':session.get(WorkerExecutionLease,job.id).lease_until=time.time()-1
                        else:
                            resolved=copy.deepcopy(row.resolved)
                            current=resolved['repository_run']['attempts'][-1]['activation']
                            if fault=='fence':current['fence_sha256']='0'*64
                            if fault=='metadata':current['unknown']=True
                            row.resolved=resolved
                response=(client.request('GET',url,headers=auth,content=content)if method=='request'
                          else client.get(url,headers=auth))
                assert response.status_code in {400,403,409,422},response.text
                assert f.driver.effects==0
        finally:
            worker.stop.set();await asyncio.wait_for(task,10)
    asyncio.run(run())


@pytest.mark.parametrize('stop',['shutdown','revoke','cancel','wrong_descriptor'])
def test_wait_cancellation_known_nonexecuted_actual_worker(native_owner,stop):
    f=native_owner;worker,job=queued(f)
    async def run():
        task=await waiting(f,worker,job)
        assert not f.lifecycle.effect_started(job.id)
        if stop=='shutdown':worker.stop.set()
        elif stop=='revoke':f.resolver.grant_id='revoked'
        elif stop=='wrong_descriptor':Path(f.driver.config['descriptor_path']).write_text('foreign')
        else:
            with f.platform.db.sessions.begin()as session:session.get(Job,job.id).cancel_requested=True
        await asyncio.wait_for(task,12)
    asyncio.run(run())
    observed=f.platform.get_job(job.id)
    assert f.driver.effects==0
    assert observed.error_code not in {'execution_uncertain','external_execution_uncertain'}
    assert observed.status in {'failed','cancelled','blocked'}
    assert not f.lifecycle.effect_started(job.id)
    assert observed.attempts==1 # no automatic single-use replay


def test_locator_single_use_collision_and_symlink(native_owner):
    f=native_owner;worker,job=queued(f)
    async def run():
        task=await waiting(f,worker,job)
        try:
            context=f.driver.context;activation.publish_locator(context)
            original=f.locator.read_bytes()
            with pytest.raises(RuntimeFailure):activation.publish_locator(replace(context,attempt_id='foreign'))
            assert f.locator.read_bytes()==original
            f.locator.unlink();f.locator.symlink_to(f.driver.config['descriptor_path'])
            with pytest.raises(RuntimeFailure):activation.publish_locator(context)
        finally:worker.stop.set();await asyncio.wait_for(task,10)
    asyncio.run(run())


def test_repository_only_skips_legacy_queue_before_limit_and_recovery(native_owner):
    f=native_owner
    # More than the owner's bounded claim scan; a post-query Python filter starves.
    ordinary=[f.platform.submit('incident-forensics',f'legacy-{n}','operations',f'legacy-{n}')for n in range(101)]
    value=submit(f.owner,packet(f.base),idempotency_key='repository-after-legacy')
    worker=Worker(f.platform,repository_run_executor=f.lifecycle)
    selected=worker.claim();assert selected.id==value['id']
    assert all(f.platform.get_job(j.id).status=='queued'for j in ordinary)
    # Ordinary local/remote eligibility remains available on the same API compute.
    normal=Worker(f.platform,repository_run_executor=f.lifecycle)
    normal.leases.repository_worker_only=False
    claimed=normal.claim();assert claimed.id in {j.id for j in ordinary}
    with f.platform.db.sessions.begin()as session:
        row=session.get(Job,claimed.id);lease=session.get(WorkerExecutionLease,claimed.id)
        row.lease_until=lease.lease_until=time.time()-1
        before=(row.status,row.error_code,row.lease_owner,row.lease_until,row.attempts)
    worker.recover_expired()
    with f.platform.db.sessions()as session:
        row=session.get(Job,claimed.id)
        assert (row.status,row.error_code,row.lease_owner,row.lease_until,row.attempts)==before
    normal.recover_expired()
    assert f.platform.get_job(claimed.id).status!='running'


def test_activation_failure_before_effect_does_not_clear_prior_unknown_budget(native_owner):
    f=native_owner;worker,job=queued(f)
    async def run():
        task=await waiting(f,worker,job)
        unknown=[{'id':'prior-unknown','reserved_usd':.25,'uncertain':True}]
        with f.platform.db.sessions.begin()as session:
            row=session.get(Job,job.id);resolved=copy.deepcopy(row.resolved)
            resolved['repository_run']['budget_reservations']=unknown
            resolved['repository_run']['active_slots']=['prior-unknown']
            row.resolved=resolved
        worker.stop.set();await asyncio.wait_for(task,10)
        observed=f.platform.get_job(job.id).resolved['repository_run']
        assert observed['budget_reservations']==unknown
        assert observed['active_slots']==['prior-unknown']
        assert f.driver.effects==0
    asyncio.run(run())


def test_activation_not_started_malformed_is_unknown(native_owner):
    f=native_owner;worker,job=queued(f)
    for fence in (None,False,{},''):
        assert f.lifecycle.activation_not_started(job.id,fence)is False
    with f.platform.db.sessions.begin()as session:
        row=session.get(Job,job.id);row.resolved={**row.resolved,'repository_run':None}
    assert f.lifecycle.activation_not_started(job.id,job.execution_fence)is False


def test_running_persist_failure_stays_known_nonexecuted(native_owner,monkeypatch):
    f=native_owner;worker,job=queued(f)
    original=f.lifecycle._persist
    def persist(*args,**kwargs):
        if f.driver.calls>=2:
            raise RuntimeFailure('fixture_persist_refused')
        return original(*args,**kwargs)
    monkeypatch.setattr(f.lifecycle,'_persist',persist)
    async def run():
        task=await waiting(f,worker,job)
        Path(f.driver.config['descriptor_path']).write_text('ready')
        await asyncio.wait_for(task,10)
    asyncio.run(run())
    observed=f.platform.get_job(job.id)
    assert f.driver.effects==0 and not f.lifecycle.effect_started(job.id)
    assert observed.error_code=='fixture_persist_refused'


def test_sdk1_4_preserves_all_previous1_3_named_schemas():
    from importlib import resources
    from sovite.repository_repair_contract import runtime
    definitions=json.loads(resources.files(runtime.PACKAGE).joinpath('data/schema/contract.schema.json').read_text())['$defs']
    previous={name:value for name,value in definitions.items()if name!='NativeActivation'}
    assert len(previous)==27
    assert hashlib.sha256(json.dumps(previous,sort_keys=True,separators=(',',':')).encode()).hexdigest()=='8d146f69e161a67bfde174cb7c60936166412037cc30529cecfb6ce00e771a0b'


def test_stale_work_veto_stops_wait_but_preserves_history(native_owner):
    from test_repository_currentness import Guard
    f=native_owner
    bundle=copy.deepcopy(f.platform.active()[0])
    next(g for g in bundle['grants']if g['id']=='incident-forensics')['repository_currentness_required']=True
    path=Path(f.platform.settings.bundle_path);path.write_text(yaml.safe_dump(bundle));f.platform.reload(str(path))
    f.resolver.generation=f.platform.active()[1]
    guard=Guard();f.platform.repository_currentness_authorizer=guard
    worker,job=queued(f)
    async def run():
        task=await waiting(f,worker,job)
        guard.current=False
        with TestClient(f.app)as client:
            assert client.get(route(job),headers=headers()).status_code==409
        await asyncio.wait_for(task,12)
    asyncio.run(run())
    assert f.driver.effects==0 and not f.lifecycle.effect_started(job.id)
    assert f.platform.get_job(job.id).error_code!='execution_uncertain'
    assert f.owner.get(job.id,principal_id='cloud-codex')['id']==job.id
    f.owner.cancel(job.id,principal_id='cloud-codex',workspace_id='workspace-1')


def test_wait_never_extends_started_execution_deadline(native_owner):
    f=native_owner;f.platform.settings=replace(f.platform.settings,job_timeout_seconds=2)
    worker,job=queued(f)
    async def run():
        task=await waiting(f,worker,job)
        original=f.driver.context.deadline
        await asyncio.wait_for(task,6)
        with f.platform.db.sessions()as session:
            assert session.get(WorkerExecutionLease,job.id).execution_deadline==original
    asyncio.run(run())
    assert f.driver.effects==0
    assert f.platform.get_job(job.id).error_code not in {'execution_uncertain','external_execution_uncertain'}
