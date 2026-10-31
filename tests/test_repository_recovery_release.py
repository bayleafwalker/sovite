"""Current eligible callers copy exact stored recovery bytes, never review them by assertion."""
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
import os
import uuid

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine

from sovite.api import create_app
from sovite.db import Base,Job,MetadataRow
from sovite.flow import contract_revision
from sovite.output_contracts import approve_output,read_output
from sovite.repository_recovery import record_for,payload_for
from sovite.repository_recovery_release import PROCEDURE_REF,PROCEDURE_DIGEST
from sovite.service import Platform,PlatformError
from test_repository_candidate_export import prepared as sqlite_prepared
from test_repository_candidate_export import candidate_destinations
from test_repository_recovery import interrupted,capture
from test_repository_runs import CALLER,OTHER
from test_workflows import revision_ref


@pytest.fixture
def candidate_additional_contracts(request,candidate_destinations):
    return [{'id':'recovery-copy','source_class':'C3','output_class':getattr(request,'param','C3'),'destinations':candidate_destinations,
             'approvers':['cloud-codex'],'procedure_ref':PROCEDURE_REF}]


@pytest.fixture(params=['sqlite','postgres'])
def prepared(sqlite_prepared,request):
    if request.param=='sqlite':yield sqlite_prepared;return
    url=os.environ.get('SOVITE_TEST_POSTGRES_URL')
    if not url:
        if os.environ.get('SOVITE_REPAIR_REQUIRE_PG')=='1':pytest.fail('Disposable PostgreSQL is required')
        pytest.skip('Disposable PostgreSQL is required')
    if url.startswith(('postgresql://','postgres://')):url='postgresql+psycopg://'+url.split('://',1)[1]
    platform,exporter,*_=sqlite_prepared
    admin=create_engine(url);schema='recoverycopy_'+uuid.uuid4().hex
    with admin.begin() as connection:connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    postgres=Platform(replace(platform.settings,database_url=url,database_schema=schema));postgres.db.migrate()
    try:
        with platform.db.engine.connect() as source,postgres.db.engine.begin() as target:
            for table in reversed(Base.metadata.sorted_tables):target.execute(table.delete())
            for table in Base.metadata.sorted_tables:
                rows=[dict(row) for row in source.execute(table.select()).mappings()]
                if rows:target.execute(table.insert(),rows)
        exporter.platform=exporter.execution.platform=postgres
        yield (postgres,exporter,postgres.get_job(sqlite_prepared[2].id),*sqlite_prepared[3:])
    finally:
        postgres.db.engine.dispose()
        with admin.begin() as connection:connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()


def selectors(fixture):
    recovered=capture(fixture)
    return {'workspace_id':'workspace-1','attempt_id':fixture[3],
        'source_digest':recovered['source_digest'],'contract_id':'recovery-copy','destination_id':'private-state'}


def client_for(fixture):
    platform,owner,*_=fixture
    return TestClient(create_app(platform.settings,platform,repository_run_owner=owner))


from contextlib import contextmanager


@contextmanager
def real_http_client(fixture):
    import asyncio
    import socket
    import threading
    import time
    import httpx
    import uvicorn
    platform,owner,*_=fixture
    app=create_app(platform.settings,platform,repository_run_owner=owner)
    sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(128)
    address=sock.getsockname()
    server=uvicorn.Server(uvicorn.Config(app,log_level='critical',lifespan='on'))
    thread=threading.Thread(target=lambda:asyncio.run(server.serve(sockets=[sock])),daemon=True)
    thread.start();deadline=time.monotonic()+5
    try:
        while not server.started:
            assert thread.is_alive() and time.monotonic()<deadline,'HTTP owner failed to start'
            time.sleep(.01)
        with httpx.Client(base_url=f'http://127.0.0.1:{address[1]}',timeout=15) as client:yield client
    finally:
        server.should_exit=True;thread.join(5);sock.close()
        assert not thread.is_alive(),'HTTP owner failed bounded cleanup'


@pytest.mark.parametrize('interrupted',[100000],indirect=True)
def test_large_exact_stored_copy_uses_current_caller_without_raw_input(interrupted):
    platform,owner,before,attempt,effects,exports,_=interrupted
    body=selectors(interrupted)
    job=platform.get_job(before.id);record=record_for(job,attempt_id=attempt)
    raw=platform.store.get(record['artifact_key'],record['artifact_sha256'])
    assert len(raw)>65536
    path=f'/api/repository-runs/v1/runs/{job.id}'
    headers={'Authorization':'Bearer '+CALLER}
    with real_http_client(interrupted) as client:
        context=client.post(path+'/recovery-context',json=body,headers=headers)
        assert context.status_code==200,context.text
        answer=context.json();assert answer['procedure_digest']==PROCEDURE_DIGEST
        assert not {'content','bundle_base64','artifact_key'}&answer.keys()
        request={**body,'expected_generation':answer['expected_generation'],
                 'expected_contract_revision':answer['contract_revision']}
        release=client.post(path+'/recovery-release',json=request,headers=headers)
        assert release.status_code==200,release.text
        assert 'content' not in release.json()
        assert client.post(path+'/recovery-release',json=request,headers=headers).json()==release.json()
        received=client.get('/api/output-releases/'+release.json()['release_id'],headers=headers)
        assert received.status_code==200,received.text
        assert received.json()['content'].encode()==raw
        ordinary={'source_job_id':job.id,'source_digest':body['source_digest'],
            'contract_id':'recovery-copy','expected_generation':answer['expected_generation'],
            'expected_contract_revision':answer['contract_revision'],'content':raw.decode(),
            'evidence_refs':[revision_ref('ordinary-review')]}
        assert client.post('/api/output-releases',json=ordinary,headers=headers).status_code==413
    after=platform.get_job(job.id)
    assert (after.status,after.error_code,after.artifact_key)==(before.status,before.error_code,before.artifact_key)
    for key in ('budget_reservations','active_slots','usage_usd','attempts'):
        assert after.resolved['repository_run'][key]==before.resolved['repository_run'][key]
    assert len(effects)==len(exports)==1


@pytest.mark.parametrize('change',['human-procedure','foreign-destination','wrong-digest','foreign-attempt','foreign-caller'])
def test_metadata_context_refuses_substituted_source_or_authority(interrupted,change):
    body=selectors(interrupted);job=interrupted[2]
    if change=='human-procedure':body['contract_id']='candidate-export'
    elif change=='foreign-destination':body['destination_id']='foreign'
    elif change=='wrong-digest':body['source_digest']='sha256:'+'f'*64
    elif change=='foreign-attempt':body['attempt_id']='foreign'
    headers={'Authorization':'Bearer '+(OTHER if change=='foreign-caller' else CALLER)}
    with client_for(interrupted) as client:
        result=client.post(f'/api/repository-runs/v1/runs/{job.id}/recovery-context',json=body,headers=headers)
        assert result.status_code in {403,409},result.text
    with interrupted[0].db.sessions() as session:
        assert not [row for row in session.query(MetadataRow).all() if row.key.startswith('release_')]


def test_ordinary_approval_cannot_alter_fixed_copy_bytes(interrupted):
    platform,_,job,attempt,*_=interrupted;body=selectors(interrupted)
    record=record_for(platform.get_job(job.id),attempt_id=attempt)
    content=platform.store.get(record['artifact_key'],record['artifact_sha256']).decode()
    contract=next(item for item in platform.active()[0]['output_contracts'] if item['id']=='recovery-copy')
    with pytest.raises(PlatformError,match='exact stored recovery copy'):
        approve_output(platform,source_job_id=job.id,source_digest=body['source_digest'],
            contract_id='recovery-copy',expected_contract_revision=contract_revision(contract),
            expected_generation=platform.active()[1],content=content+' ',
            evidence_refs=[revision_ref('caller-cannot-claim-machine-copy')],actor=job.caller)


@pytest.mark.parametrize('candidate_additional_contracts',['C2'],indirect=True)
def test_exact_copy_never_declassifies_the_actual_source(interrupted):
    body=selectors(interrupted);job=interrupted[2]
    with client_for(interrupted) as client:
        response=client.post(f'/api/repository-runs/v1/runs/{job.id}/recovery-context',json=body,
                             headers={'Authorization':'Bearer '+CALLER})
        assert response.status_code==409,response.text


def test_metadata_stream_is_refused_before_reading_the_whole_body(prepared):
    import asyncio
    platform,exporter,job,*_=prepared
    from test_repository_runs import make_owner
    owner=make_owner(platform,exporter.resolver,executor=exporter.execution)
    app=create_app(platform.settings,platform,repository_run_owner=owner)
    async def scenario():
        reads=0;sent=[]
        async def receive():
            nonlocal reads
            reads+=1
            assert reads<=2,'Metadata route read beyond its closed byte bound'
            return {'type':'http.request','body':b'x'*3000,'more_body':True}
        async def send(message):sent.append(message)
        scope={'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':'POST',
            'scheme':'http','path':f'/api/repository-runs/v1/runs/{job.id}/recovery',
            'raw_path':b'/','query_string':b'','headers':[(b'authorization',('Bearer '+CALLER).encode())],
            'client':('127.0.0.1',1234),'server':('testserver',80),'root_path':''}
        await asyncio.wait_for(app(scope,receive,send),2)
        assert reads==2 and next(item for item in sent if item['type']=='http.response.start')['status']==413
    asyncio.run(scenario())


@pytest.mark.parametrize('fault',['grant','approver-after-store','source-after-store','wrong-generation','wrong-revision'])
def test_release_rechecks_current_authority_and_exact_revision(interrupted,monkeypatch,fault):
    from sovite.db import GrantState
    platform,_,job,*_=interrupted;body=selectors(interrupted)
    path=f'/api/repository-runs/v1/runs/{job.id}';headers={'Authorization':'Bearer '+CALLER}
    with client_for(interrupted) as client:
        context=client.post(path+'/recovery-context',json=body,headers=headers).json()
        request={**body,'expected_generation':context['expected_generation'],
                 'expected_contract_revision':context['contract_revision']}
        if fault=='grant':
            with platform.db.sessions.begin() as session:
                state=session.get(GrantState,job.grant_id)
                if state is None:session.add(GrantState(id=job.grant_id,inhibited=True))
                else:state.inhibited=True
        elif fault=='wrong-generation':request['expected_generation']='sha256:'+'f'*64
        elif fault=='wrong-revision':request['expected_contract_revision']='sha256:'+'f'*64
        else:
            original=platform.store.put
            def revoke_after_persistence(key,value):
                result=original(key,value)
                if key.startswith('release_'):
                    if fault=='source-after-store':
                        record=record_for(platform.get_job(job.id),attempt_id=body['attempt_id'])
                        (platform.store.root/record['artifact_key']).unlink()
                    else:platform.settings.caller_tokens.pop(job.caller,None)
                return result
            monkeypatch.setattr(platform.store,'put',revoke_after_persistence)
        response=client.post(path+'/recovery-release',json=request,headers=headers)
        assert response.status_code in {403,409},response.text
    with platform.db.sessions() as session:
        assert not [row for row in session.query(MetadataRow).all() if row.key.startswith('release_')]


def test_actual_http_lost_release_reply_reconciles_one_existing_approval(interrupted,monkeypatch):
    import httpx
    platform,_,job,*_=interrupted;body=selectors(interrupted)
    path=f'/api/repository-runs/v1/runs/{job.id}';headers={'Authorization':'Bearer '+CALLER}
    with real_http_client(interrupted) as client:
        context=client.post(path+'/recovery-context',json=body,headers=headers).json()
        request={**body,'expected_generation':context['expected_generation'],
                 'expected_contract_revision':context['contract_revision']}
        original=client._transport.handle_request;lost=False
        def drop_after_commit(request):
            nonlocal lost
            response=original(request)
            if request.url.path.endswith('/recovery-release') and not lost:
                assert response.status_code==200
                response.read();response.close();lost=True
                raise httpx.ReadError('Explicit fixture: committed acknowledgement lost',request=request)
            return response
        monkeypatch.setattr(client._transport,'handle_request',drop_after_commit)
        with pytest.raises(httpx.ReadError):client.post(path+'/recovery-release',json=request,headers=headers)
        assert client.post(path+'/recovery-context',json=body,headers=headers).json()==context
        reconciled=client.post(path+'/recovery-release',json=request,headers=headers)
        assert reconciled.status_code==200,reconciled.text
    with platform.db.sessions() as session:
        records=[row for row in session.query(MetadataRow).all() if row.key.startswith('release_')]
        assert len(records)==1 and records[0].key==reconciled.json()['release_id']
    after=platform.get_job(job.id)
    assert after.status==job.status and after.resolved['repository_run']['budget_reservations']==job.resolved['repository_run']['budget_reservations']
