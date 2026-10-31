"""Public-only Go Responses source controls; no real provider calls."""
import asyncio
import copy
from decimal import Decimal
from types import SimpleNamespace
import time

import httpx
import pytest

from sovite.adapters import RuntimeFailure
from sovite.model_gateway_client import ModelGatewayInference
from sovite.policy import GO_ENDPOINT,PUBLIC_RETENTION_SECONDS,PolicyError,validate_bundle,authorize_job
from sovite.repository_native_inference import Accounting,DirectResponses,NativeInferenceBridge
from test_policy_v1alpha2 import bundle
from test_repository_native_inference import fixture as native_fixture
from test_native_responses import HttpPeer,blocks,completed,event


@pytest.fixture
def public_go(bundle):
    model=bundle['models'][0]
    for key in ('connection','identity','digest','max_concurrency','first_token_timeout_seconds','cancel_bound_seconds'):
        model.pop(key,None)
    model.update(adapter='opencode-go',model='gpt-6-luna',endpoint=GO_ENDPOINT,
        api_key_env='SYNTHETIC_GO_KEY',identity_protocol='provider-service',wire_api='responses')
    destination=next(d for d in bundle['destinations'] if d['id']==model['destination'])
    destination.update(kind='cloud',boundary='public',trust_tier='provider',clearance='C0',
        allowed_hosts=['opencode.ai'],retention={'prompt_logging':True,'response_logging':True,
        'persistent_context':False,'max_seconds':PUBLIC_RETENTION_SECONDS,
        'authorization_ref':'owner-published-public-synthetic-fixture',
        'in_memory_cache':{'scope':'provider','max_seconds':PUBLIC_RETENTION_SECONDS,
            'clear_on_cancel':False,'clear_on_session_close':False}})
    for agent in bundle['agents']:agent['tools']=[]
    bundle['harnesses'][0]['kind']='native-codex'
    for grant in bundle['grants']:
        grant.update(input_class='C0',output_class='C0',telemetry_class='C0',receipt_class='C0',caller_clearance='C0')
    return validate_bundle(bundle)


def test_public_go_responses_is_an_enforced_declared_provider_service(public_go):
    model=public_go['models'][0]
    assert model['identity_protocol']=='provider-service' and model['wire_api']=='responses'
    assert validate_bundle(public_go)['apiVersion']=='sovite.platform/v1alpha2'


@pytest.mark.parametrize('data_class',['C1','C2','C3'])
@pytest.mark.parametrize('source',['input','output','tool'])
def test_retention_exception_cannot_receive_sensitive_context_joins(public_go,data_class,source):
    if source=='input':
        public_go['grants'][0].update(input_class=data_class,output_class=data_class)
    elif source=='output':public_go['grants'][0]['output_class']=data_class
    else:
        private=next(d for d in public_go['destinations'] if d.get('boundary','private')=='private' and d['clearance']=='C3')
        tool={'id':'private-source-tool','kind':'http-json','endpoint':'http://127.0.0.1:1234',
            'input_class':'C0','output_class':data_class,'destination':private['id'],'allowed_operations':['read']}
        public_go['tools'].append(tool)
        public_go['agents'][0]['tools']=[tool['id']]
        public_go['grants'][0]['output_class']=data_class
    with pytest.raises(PolicyError):validate_bundle(public_go)


@pytest.mark.parametrize('change',['development','v1','missing-declaration','false-zdr','local-clear',
                                  'private-boundary','rented','foreign-endpoint','chat-path','foreign-adapter'])
def test_public_retention_is_narrow_and_cannot_claim_local_provider_control(public_go,change):
    model=public_go['models'][0]
    destination=next(d for d in public_go['destinations'] if d['id']==model['destination'])
    if change=='development':public_go['mode']='development'
    elif change=='v1':public_go['apiVersion']='sovite.platform/v1alpha1'
    elif change=='missing-declaration':destination['retention'].pop('authorization_ref')
    elif change=='false-zdr':destination['retention']['prompt_logging']=False
    elif change=='local-clear':destination['retention']['in_memory_cache']['clear_on_cancel']=True
    elif change=='private-boundary':destination['boundary']='private'
    elif change=='rented':destination['trust_tier']='rented'
    elif change=='foreign-endpoint':model['endpoint']='https://foreign.invalid/responses'
    elif change=='chat-path':model['endpoint']=GO_ENDPOINT+'/chat/completions'
    else:model['adapter']='openai-compatible';model.pop('identity_protocol')
    with pytest.raises(PolicyError):validate_bundle(public_go)


@pytest.fixture
def go_fixture(native_fixture):
    context,budget,record,accounting,payload,transport=native_fixture
    context.model.update(adapter='opencode-go',model='gpt-6-luna',endpoint=GO_ENDPOINT,
        identity_protocol='provider-service',api_key_env='SYNTHETIC_GO_KEY')
    payload['model']='gpt-6-luna'
    accounting['opencode_go']={'billing_source':'go','reservation_allowance':1.,
        'maximum_root_allowance':2.,'maximum_allowance_per_list_usd':4.}
    # This fixture checks bridge persistence order; owner/lineage enforcement
    # uses the real database transaction in test_repository_go_quota.py.
    budget.quota_persist=lambda mutate,quota:mutate(record)
    transport.usage={'input_tokens':19,'output_tokens':17,'total_tokens':36,'cost':0,
        'opencode':{'billing_source':'go','list_cost':.0000104,'allowance':.0000416,'estimated':None,
            'tokens':{'input':19,'output':17,'reasoning':0,'cache_read':0,'cache_write_5m':0,'cache_write_1h':0}}}
    return context,budget,record,accounting,payload,transport


def test_known_zero_charge_is_distinct_from_list_price_and_allowance(go_fixture):
    context,budget,record,accounting,payload,transport=go_fixture
    async def scenario():
        bridge=NativeInferenceBridge(context,budget,lambda:True,accounting,transport=transport)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge.app),base_url='http://bridge') as client:
            result=await client.post('/v1/responses',json=payload,headers={'Authorization':'Bearer '+bridge.token})
        assert result.status_code==200
        reservation=record['budget_reservations'][0]
        assert reservation['state']=='settled' and record['usage_usd']==0
        observation=reservation['provider_observation']
        assert Decimal(observation['charge_usd'])==0
        assert Decimal(observation['list_cost_usd'])==Decimal('.0000104')
        assert Decimal(observation['allowance'])==Decimal('.0000416')
        assert Decimal(reservation['quota']['actual'])==Decimal('.0000416') and not record['active_slots']
    asyncio.run(scenario())


@pytest.mark.parametrize('unknown',['missing-usage','missing-charge','missing-list','missing-allowance',
    'balance','byok','estimated','nonfinite','positive-charge','quota-overrun','stream-loss',
    'missing-billable-tokens','billable-input-overrun','reasoning-exceeds-output'])
def test_unknown_or_unqualified_billing_holds_both_reservations(go_fixture,unknown):
    context,budget,record,accounting,payload,transport=go_fixture
    if unknown=='missing-usage':transport.usage=None
    elif unknown=='missing-charge':transport.usage.pop('cost')
    elif unknown=='missing-list':transport.usage['opencode'].pop('list_cost')
    elif unknown=='missing-allowance':transport.usage['opencode'].pop('allowance')
    elif unknown in {'balance','byok'}:transport.usage['opencode']['billing_source']=unknown
    elif unknown=='estimated':transport.usage['opencode']['estimated']=True
    elif unknown=='nonfinite':transport.usage['cost']=float('nan')
    elif unknown=='positive-charge':transport.usage['cost']=.01
    elif unknown=='quota-overrun':transport.usage['opencode']['allowance']=1.1
    elif unknown=='missing-billable-tokens':transport.usage['opencode'].pop('tokens')
    elif unknown=='billable-input-overrun':transport.usage['opencode']['tokens']['cache_read']=1000000
    elif unknown=='reasoning-exceeds-output':transport.usage['opencode']['tokens']['reasoning']=18
    else:transport.failure='external_execution_uncertain'
    async def scenario():
        bridge=NativeInferenceBridge(context,budget,lambda:True,accounting,transport=transport)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge.app),base_url='http://bridge') as client:
            headers={'Authorization':'Bearer '+bridge.token}
            result=await client.post('/v1/responses',json=payload,headers=headers)
            assert result.status_code==409
            reservation=record['budget_reservations'][0]
            assert reservation['state']=='reserved' and reservation['actual_usd'] is None
            assert reservation['quota']['actual'] is None and reservation['quota']['reserved']==1.
            assert record['active_slots'] and not reservation.get('provider_observation')
            assert (await client.post('/v1/responses',json=payload,headers=headers)).status_code==409
            assert len(transport.calls)==1
    asyncio.run(scenario())


def test_go_direct_transport_uses_only_fixed_service_and_stable_actual_job_header(go_fixture,monkeypatch):
    context,_,_,_,payload,transport=go_fixture
    context.generation='sha256:'+'a'*64;context.job_id='synthetic-go-job';context.fence='b'*32
    monkeypatch.setenv('SYNTHETIC_GO_KEY','public-fixture-key-with-no-provider-authority')
    context.model['timeout_seconds']=5
    async def scenario():
        peer=HttpPeer(blocks(completed(model='gpt-6-luna',usage=transport.usage)))
        async with peer.serve():
            direct=DirectResponses(context)
            fixed_url,headers=direct._responses_destination(time.time()+5)
            assert fixed_url==GO_ENDPOINT+'/responses'
            assert headers['x-opencode-session']==context.job_id
            assert headers['User-Agent']=='sovite-platform/opencode-go'
            assert not any(name.lower().startswith('x-sovite') for name in headers)
            # Explicit unqualified loopback endpoint fixture, not a production
            # model publication or alternate provider configuration surface.
            direct._responses_destination=lambda deadline:(peer.origin+'/responses',headers)
            result=await direct.responses(context.model,payload,lambda:True)
            assert result.usage['cost']==0
        assert len(peer.requests)==1 and peer.requests[0][1]['x-opencode-session']==context.job_id
    asyncio.run(scenario())


def test_generic_gateway_cannot_qualify_go_provider_as_local_weights(go_fixture):
    context,_,_,_,payload,_=go_fixture
    gateway=ModelGatewayInference(origin='https://gateway.invalid',token='fixture',verify=True,
        binding_id=context.model['id'],model_id=context.model['model'],generation='sha256:'+'a'*64,
        job_id='fixture',fence='b'*32,deadline=time.time()+1,capability='fixture',workload='fixture',binding_timeout=1)
    with pytest.raises(RuntimeFailure,match='unsupported_model_adapter'):
        asyncio.run(gateway.responses(context.model,payload))


def test_legacy_direct_codex_cannot_bypass_repository_call_budget(public_go):
    from sovite.adapters import AdapterRunner
    with pytest.raises(RuntimeFailure,match='native_go_requires_repository_runtime'):
        asyncio.run(AdapterRunner().native_codex({'model':public_go['models'][0]},'synthetic','fixture',isolated=True))


def test_wire_positive_charge_cannot_underflow_into_known_zero(go_fixture,monkeypatch):
    from sovite.model_gateway_client import _responses_result
    context,_,_,accounting,_,transport=go_fixture
    stream=completed(model='gpt-6-luna',usage=transport.usage).replace(b'"cost": 0',b'"cost": 1e-400')
    result=_responses_result(stream,'gpt-6-luna',exact_numbers=True)
    assert result.usage['cost']==Decimal('1e-400')
    with pytest.raises(RuntimeFailure):
        Accounting(accounting,context.model['id'],adapter='opencode-go').go_observation(result.usage)
