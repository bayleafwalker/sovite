"""Protected bridge uses real durable budget semantics with synthetic transport."""
import asyncio
import copy
from types import SimpleNamespace
import time

import httpx
import pytest

from sovite.adapters import RuntimeFailure
from sovite.model_gateway_client import ResponsesResult
from sovite.repository_native_inference import Accounting, NativeInferenceBridge
from sovite.repository_native_runtime import NATIVE_TOOLS
from sovite.repository_runs import RepositoryRunBudget


@pytest.fixture
def fixture():
    model={'id':'synthetic-model','model':'synthetic-fixed','adapter':'openai-compatible','wire_api':'responses'}
    context=SimpleNamespace(model=model,deadline=time.time()+10)
    record={'usage_usd':0.,'budget_reservations':[],'active_slots':[],'peak_concurrency':0}
    budget=RepositoryRunBudget(2.,1,used_usd=0.,reservations=[],active_slots=[],peak_concurrency=0,
        authorization_check=lambda:True,persist=lambda operation:operation(record))
    accounting={'model_id':'synthetic-model','valid_until':time.time()+20,'maximum_request_bytes':100000,
                'maximum_input_tokens':100000,'maximum_output_tokens':128,'reservation_usd':1.,'input_usd_per_million':1.,'output_usd_per_million':2.}
    payload={'model':'synthetic-fixed','stream':True,'store':False,'input':'synthetic',
             'tools':[{'type':'function','name':name,'parameters':{'type':'object'}} for name in sorted(NATIVE_TOOLS)]}
    class Synthetic:
        def __init__(self):self.calls=[];self.usage={'input_tokens':3,'output_tokens':2,'total_tokens':5};self.failure=None
        async def responses(self,model,payload,authorization):
            self.calls.append(copy.deepcopy(payload));authorization()
            if self.failure:raise RuntimeFailure(self.failure)
            return ResponsesResult(b'event: response.completed\ndata: {}\n\n',{'output':[]},self.usage)
    return context,budget,record,accounting,payload,Synthetic()


def test_known_completed_usage_settles_exact_positive_cost(fixture):
    context,budget,record,accounting,payload,transport=fixture
    async def scenario():
        bridge=NativeInferenceBridge(context,budget,lambda:True,accounting,transport=transport)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge.app),base_url='http://bridge') as client:
            response=await client.post('/v1/responses',json=payload,headers={'Authorization':'Bearer '+bridge.token})
            assert response.status_code==200
            assert transport.calls[0]['max_output_tokens']==128
            assert record['usage_usd']==pytest.approx(.000007)
            assert record['budget_reservations'][0]['state']=='settled' and not record['active_slots']
    asyncio.run(scenario())


@pytest.mark.parametrize('unknown',['missing-usage','stream-loss'])
def test_unknown_cost_or_stream_keeps_reservation_and_forbids_new_effects(fixture,unknown):
    context,budget,record,accounting,payload,transport=fixture
    if unknown=='missing-usage':transport.usage=None
    else:transport.failure='external_execution_uncertain'
    async def scenario():
        bridge=NativeInferenceBridge(context,budget,lambda:True,accounting,transport=transport)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge.app),base_url='http://bridge') as client:
            header={'Authorization':'Bearer '+bridge.token}
            assert (await client.post('/v1/responses',json=payload,headers=header)).status_code==409
            assert bridge.failed and bridge.failure.is_set()
            assert record['budget_reservations'][0]['state']=='reserved'
            assert len(record['active_slots'])==1 and record['usage_usd']==0
            assert (await client.post('/v1/responses',json=payload,headers=header)).status_code==409
            assert len(transport.calls)==1
    asyncio.run(scenario())


def test_owner_revocation_refuses_before_reserving_or_inference(fixture):
    context,budget,record,accounting,payload,transport=fixture
    def revoked():raise RuntimeFailure('authorization_revoked')
    async def scenario():
        bridge=NativeInferenceBridge(context,budget,revoked,accounting,transport=transport)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge.app),base_url='http://bridge') as client:
            response=await client.post('/v1/responses',json=payload,headers={'Authorization':'Bearer '+bridge.token})
            assert response.status_code==409 and transport.calls==[]
            assert record['budget_reservations']==[]
    asyncio.run(scenario())


@pytest.mark.parametrize('change',[{'reservation_usd':0},{'input_usd_per_million':'unknown'},{'valid_until':0},
                                   {'model_id':'foreign-model'},{'maximum_input_tokens':None},{'reservation_usd':.01}])
def test_unknown_or_expired_fee_facts_cannot_enable_a_bridge(fixture,change):
    context,budget,record,accounting,payload,transport=fixture
    with pytest.raises(RuntimeFailure,match='native_model_accounting_unavailable'):
        NativeInferenceBridge(context,budget,lambda:True,{**accounting,**change},transport=transport)


def test_actual_loopback_listener_authentication_and_lifecycle(fixture):
    context,budget,record,accounting,payload,transport=fixture
    async def scenario():
        bridge=NativeInferenceBridge(context,budget,lambda:True,accounting,transport=transport)
        async with bridge.listen():
            async with httpx.AsyncClient(base_url=bridge.origin,trust_env=False) as client:
                assert (await client.post('/v1/responses',json=payload)).status_code==403
                assert transport.calls==[]
                response=await client.post('/v1/responses',json=payload,headers={'Authorization':'Bearer '+bridge.token})
                assert response.status_code==200 and len(transport.calls)==1
    asyncio.run(scenario())


@pytest.mark.parametrize('tool',[{'type':'function_call','name':'apply_patch'},
    {'type':'custom_tool_call','name':'apply_patch','input':'*** Begin Patch'},
    {'type':'function_call','name':'fs_write'}, {'type':'mcp_call','name':'host'}])
def test_unadvertised_provider_tool_never_reaches_native_and_keeps_unknown_reservation(fixture,tool):
    context,budget,record,accounting,payload,transport=fixture
    original=transport.responses
    async def response(*args):
        result=await original(*args)
        return ResponsesResult(result.sse,{'output':[tool]},result.usage)
    transport.responses=response
    async def scenario():
        bridge=NativeInferenceBridge(context,budget,lambda:True,accounting,transport=transport)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge.app),base_url='http://bridge') as client:
            result=await client.post('/v1/responses',json=payload,headers={'Authorization':'Bearer '+bridge.token})
            assert result.status_code==409 and 'Begin Patch' not in result.text
            assert record['budget_reservations'][0]['state']=='reserved' and record['active_slots']
            assert bridge.failed and len(transport.calls)==1
    asyncio.run(scenario())


def test_hidden_tool_in_intermediate_sse_cannot_be_hidden_by_empty_completed_output(fixture):
    context,budget,record,accounting,payload,transport=fixture
    original=transport.responses
    async def response(*args):
        result=await original(*args)
        return ResponsesResult(result.sse,{'output':[]},result.usage,(
            {'type':'response.output_item.added','item':{'type':'custom_tool_call','name':'apply_patch'}},))
    transport.responses=response
    async def scenario():
        bridge=NativeInferenceBridge(context,budget,lambda:True,accounting,transport=transport)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge.app),base_url='http://bridge') as client:
            result=await client.post('/v1/responses',json=payload,headers={'Authorization':'Bearer '+bridge.token})
            assert result.status_code==409 and bridge.failed
            assert record['budget_reservations'][0]['state']=='reserved'
    asyncio.run(scenario())
