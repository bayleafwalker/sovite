"""Actual resource owner API: typed targets, exact leases and output scopes."""
import base64
import copy
from dataclasses import replace
import hashlib
import time
import sys

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select,func

from sovite.api import create_app
from sovite.db import Job,ResourceOwnerRequest,ResourceWorkloadScope
from sovite.resource_gateway_store import canonical
from test_execution_leases import owner
from test_migrations import database
from test_policy_v1alpha2 import bundle as gateway_bundle
from test_resource_owner_api import OPERATOR,TENANT,WORKSPACE,DOMAIN,POLICY_REVISION
from test_worker_remote_boundary import TOKEN,admit_agent_http_owner,pull_and_start


pytestmark=pytest.mark.skipif(sys.version_info<(3,12),reason='Released owner contract runtime requires Python3.12')

PREFIX='/internal/resource-owner/v2/'


def catalog(data_class='C0'):
    declared={'source_class':data_class,'output_class':data_class,'destination':'private-state'}
    return {'schema_version':'sovite-resource-catalog/v2','entries':[
        {'kind':'model-peer-health','target_id':'synthetic-daemon','target_revision':'sha256:'+'e'*64,
         'read_profile':declared},
        {'kind':'kvm-lab','target_id':'synthetic-lab','target_revision':'sha256:'+'f'*64,
         **{key:declared for key in ('create_profile','observe_profile','stop_profile')}}]}


def setup(owner,*,data_class='C0'):
    admit_agent_http_owner(owner)
    owner.platform.submit('native-code-review','synthetic closed resources','operations','resource-v2-job',input_class='C0')
    settings=replace(owner.platform.settings,operator_token=OPERATOR,execution_tokens={'actual-workload':TOKEN})
    client=TestClient(create_app(settings=settings,platform=owner.platform))
    admin={'Authorization':'Bearer '+OPERATOR};worker={'Authorization':'Bearer '+TOKEN}
    job,_,_=pull_and_start(client,owner)
    scope={'workload_id':'actual-workload','gateway_id':'resource-gateway-a','tenant_id':TENANT,
           'workspace_id':WORKSPACE,'domain_id':DOMAIN,'catalog':catalog(data_class)}
    assert client.put(PREFIX+'workload-scopes',headers=admin,json=scope).status_code==200
    response=client.post('/internal/resource-owner/v1/enrollments',headers=admin,json={
        **{key:scope[key] for key in ('gateway_id','tenant_id','workspace_id','domain_id')},
        'expires_at':time.time()+3600})
    assert response.status_code==200
    gateway={'Authorization':'Bearer '+response.json()['bearer_token']}
    execution={'job_id':job['job_id'],'instance_id':owner.instance,'generation':job['generation'],
               'fence':job['fence'],'operation_id':'resource-v2-operation'}
    return locals()


def submit(f,resource,operation_id=None):
    execution={**f['execution'],'operation_id':operation_id or f['execution']['operation_id']}
    return f['client'].post(PREFIX+'requests',headers=f['worker'],json={'execution':execution,'resource':resource})


def grant(f,operation,target):
    response=f['client'].post(PREFIX+'output-grants',headers=f['admin'],json={
        'workload_id':'actual-workload','principal_id':'operations','tenant_id':TENANT,'workspace_id':WORKSPACE,
        'domain_id':DOMAIN,'target_id':target,'operation':operation,'policy_revision':POLICY_REVISION})
    assert response.status_code==200,response.json()
    return response.json()


def binding(f,submitted):
    return {'request_id':submitted['request_id'],'fence_fingerprint':submitted['fence_fingerprint'],
        'policy_revision':POLICY_REVISION,'destination':'private-state','actor':'operations'}


def deliver(f,submitted,value):
    content=canonical(value)
    return f['client'].post(PREFIX+'result',headers=f['gateway'],json={**binding(f,submitted),
        'observation_digest':'sha256:'+hashlib.sha256(content).hexdigest(),
        'observation_b64':base64.b64encode(content).decode(),'release':'allowed'})


def health():
    return {'schema_version':'sovite-resource-peer-health/v1','workspace_id':WORKSPACE,
        'resource_domain_id':DOMAIN,'target_id':'synthetic-daemon','operation':'model-peer.health-read',
        'health_scope':'peer-http-daemon','daemon_responsive':True}


def test_explicit_c0_health_scope_has_no_pod_fields_and_exact_current_output_reader(owner):
    f=setup(owner);client=f['client']
    result=submit(f,{'operation':'model-peer.health-read','target_id':'synthetic-daemon'})
    assert result.status_code==200,result.json()
    selected=result.json()
    assert submit(f,{'operation':'model-peer.health-read','target_id':'synthetic-daemon'}).json()==selected
    with owner.platform.db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ResourceOwnerRequest))==1
        scope=session.get(ResourceWorkloadScope,'actual-workload')
        assert scope.namespace is scope.pod_name is scope.observation_id is None
    assert client.post('/internal/resource-owner/v1/poll',headers=f['gateway'],json={}).json()['requests']==[]
    packet=client.post(PREFIX+'poll',headers=f['gateway'],json={}).json()['requests'][0]
    assert packet['target']==catalog()['entries'][0] and packet.get('create') is None
    assert 'namespace' not in packet and 'pod_name' not in packet
    assert client.post(PREFIX+'current',headers=f['gateway'],json=binding(f,selected)).json()=={'current':False}
    installed=grant(f,'model-peer.health-read','synthetic-daemon')
    assert installed['schema_version']=='sovite-resource-peer-health/v1'
    assert client.post(PREFIX+'current',headers=f['gateway'],json=binding(f,selected)).json()=={'current':True}
    assert deliver(f,selected,health()).json()=={'accepted':True,'state':'stored'}
    assert deliver(f,selected,health()).json()=={'accepted':True,'state':'stored'}
    response=client.post(PREFIX+'result/read',headers=f['worker'],json={
        'execution':f['execution'],'request_id':selected['request_id']})
    assert response.status_code==200,response.json()
    assert response.json()['observation']==health() and response.json()['output_class']=='C0'
    client.delete('/internal/resource-owner/v1/output-grants/'+installed['grant_id'],headers=f['admin'])
    assert client.post(PREFIX+'result/read',headers=f['worker'],json={
        'execution':f['execution'],'request_id':selected['request_id']}).status_code==404


def test_c0_job_cannot_silently_receive_c1_declared_source(owner):
    f=setup(owner,data_class='C1')
    response=submit(f,{'operation':'model-peer.health-read','target_id':'synthetic-daemon'})
    assert response.status_code==403 and response.json()['detail']['code']=='resource_clearance_denied'
    with owner.platform.db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ResourceOwnerRequest))==0


@pytest.mark.parametrize('fault',['endpoint','command','fake-pod','wrong-kind','unknown-target','wrong-fence'])
def test_closed_selector_and_real_lease_never_gain_target_authority(owner,fault):
    f=setup(owner)
    resource={'operation':'model-peer.health-read','target_id':'synthetic-daemon'}
    expected=422
    if fault=='endpoint':resource['url']='https://another.invalid/anything'
    elif fault=='command':resource['command']=['sh','-c','anything']
    elif fault=='fake-pod':resource['namespace']='192.168.10.55'
    elif fault=='wrong-kind':resource['operation']='kvm-lab.create';expected=403
    elif fault=='unknown-target':resource['target_id']='unconfigured';expected=403
    else:f['execution']['fence']='0'*32;expected=403
    assert submit(f,resource).status_code==expected
    with owner.platform.db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ResourceOwnerRequest))==0


def test_resource_admission_reuses_actual_active_platform_authorization(owner,monkeypatch):
    from sovite.service import PlatformError
    f=setup(owner);calls=[];original=owner.platform.authorize_existing_job
    def stale(session,job,*args,**kwargs):
        calls.append((job.id,kwargs.get('purpose','active')))
        raise PlatformError('repository_currentness_revoked','stale fixture',409)
    monkeypatch.setattr(owner.platform,'authorize_existing_job',stale)
    assert submit(f,{'operation':'model-peer.health-read','target_id':'synthetic-daemon'}).status_code==403
    assert calls and all(purpose=='active' for _,purpose in calls)


def test_lab_observe_stop_bind_actual_create_and_never_settle_execution(owner):
    f=setup(owner);client=f['client']
    before=copy.deepcopy(owner.platform.get_job(f['job']['job_id']).resolved)
    create=submit(f,{'operation':'kvm-lab.create','target_id':'synthetic-lab'},'lab-create')
    assert create.status_code==200,create.json()
    create_id=create.json()['request_id']
    selector={'operation':'kvm-lab.observe','target_id':'synthetic-lab','create_request_id':create_id}
    observed=submit(f,selector,'lab-observe')
    assert observed.status_code==200,observed.json()
    packets=client.post(PREFIX+'poll',headers=f['gateway'],json={}).json()['requests']
    packet=next(row for row in packets if row['request_id']==observed.json()['request_id'])
    assert packet['create']['create_request_id']==create_id and packet['create']['principal_id']=='operations'
    grant(f,'kvm-lab.observe','synthetic-lab')
    value={'schema_version':'sovite-resource-lab-observed/v1','workspace_id':WORKSPACE,
        'resource_domain_id':DOMAIN,'target_id':'synthetic-lab','operation':'kvm-lab.observe',
        'lab_id':create_id,'state':'unknown','effect_outcome':'unknown',
        'deadline':min(packet['create']['created_at']+30,packet['create']['execution_deadline'])}
    assert deliver(f,observed.json(),{**value,'unit':'private-unit.service'}).status_code==422
    assert deliver(f,observed.json(),{**value,'lab_id':'0'*64}).status_code==422
    assert deliver(f,observed.json(),{**value,'deadline':packet['create']['created_at']+301}).status_code==422
    assert deliver(f,observed.json(),value).json()=={'accepted':True,'state':'stored'}
    row=owner.platform.get_job(f['job']['job_id'])
    assert row.status=='running' and row.resolved==before
    invalid=submit(f,{**selector,'create_request_id':'0'*64},'foreign-create')
    assert invalid.status_code==404
    with owner.platform.db.sessions.begin() as session:
        session.get(ResourceOwnerRequest,create_id).principal_id='foreign-caller'
    assert submit(f,{**selector,'operation':'kvm-lab.stop'},'foreign-stop').status_code==404


def test_v1_submit_cannot_reinterpret_catalog_as_fake_pod(owner):
    f=setup(owner)
    result=f['client'].post('/internal/resource-owner/v1/requests',headers=f['worker'],json=f['execution'])
    assert result.status_code==403 and result.json()['detail']=='resource_scope_denied'
    with owner.platform.db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ResourceOwnerRequest))==0


def test_poll_advances_bounded_invalid_storage_page_without_starving_valid_request(owner):
    f=setup(owner)
    selected=submit(f,{'operation':'model-peer.health-read','target_id':'synthetic-daemon'}).json()
    # Controlled stored-row fence faults, not owner-admitted requests or live facts.
    with owner.platform.db.sessions.begin() as session:
        original=session.get(ResourceOwnerRequest,selected['request_id'])
        fields={column.name:copy.deepcopy(getattr(original,column.name)) for column in ResourceOwnerRequest.__table__.columns}
        for number in range(100):
            row=copy.deepcopy(fields);row['id']='0'*60+f'{number:04x}';row['fence']='0'*32
            assert row['id']<selected['request_id']
            session.add(ResourceOwnerRequest(**row))
    first=f['client'].post(PREFIX+'poll',headers=f['gateway'],json={})
    assert first.status_code==200 and first.json()['requests']==[]
    cursor=first.json()['next_cursor'];assert cursor=='0'*60+'0063'
    second=f['client'].post(PREFIX+'poll',headers=f['gateway'],json={'after':cursor})
    assert [row['request_id'] for row in second.json()['requests']]==[selected['request_id']]
    assert second.json()['next_cursor'] is None


@pytest.mark.parametrize('state,outcome',[('unknown','known'),('running','unknown'),('stopped','unknown'),('absent','unknown')])
def test_lab_observation_cannot_mislabel_unknown_or_known_effect(owner,state,outcome):
    f=setup(owner)
    selected=submit(f,{'operation':'kvm-lab.create','target_id':'synthetic-lab'}).json()
    grant(f,'kvm-lab.create','synthetic-lab')
    value={'schema_version':'sovite-resource-lab-created/v1','workspace_id':WORKSPACE,
        'resource_domain_id':DOMAIN,'target_id':'synthetic-lab','operation':'kvm-lab.create',
        'lab_id':selected['request_id'],'state':state,'effect_outcome':outcome,'deadline':time.time()+10}
    response=deliver(f,selected,value)
    assert response.status_code==422,response.json()
    with owner.platform.db.sessions() as session:
        assert session.get(ResourceOwnerRequest,selected['request_id']).observation is None
