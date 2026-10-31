"""Real gateway/helper transport obeys the generic active repository veto."""
import asyncio
import copy
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace

import pytest
from model_support import authority_env,database,v2_bundle,connected,infer,consume
from sovite.db import Job,RequestLedger
from sovite.gateway.client import GatewayError
from sovite.repository_currentness import CurrentnessError,bind_job
from sovite.repository_runs import canonical_packet_digest


def attach(env):
    state=SimpleNamespace(current=True,checks=[])
    def check_job(*,job,binding,authorization_started_at):
        state.checks.append(job.id)
        if not state.current:raise CurrentnessError('repository_currentness_revoked')
    hook=SimpleNamespace(admit=lambda **kwargs:{'scope':'opaque-test-scope'},
                         bind_job=lambda **kwargs:kwargs['admission'],check_job=check_job)
    env.platform.repository_currentness_authorizer=hook
    with env.platform.db.sessions.begin() as session:
        row=session.get(Job,env.job.id)
        packet={'schema_version':'portable-work/v1','workspace_id':'ws-fixture','repository_id':'repo-fixture',
            'executor_id':'fixture','base_sha':'a'*40,'goal':row.task,'max_cost_usd':1.,
            'deadline':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),'concurrency':1}
        resolved=copy.deepcopy(row.resolved)
        resolved['repository_run']={'schema_version':'sovite-repository-run/v1',
            **{name:packet[name] for name in packet if name not in {'schema_version','goal'}},
            'packet_sha256':canonical_packet_digest(packet),'principal_id':row.caller,
            'grant_id':row.grant_id,'generation':row.generation}
        row.resolved=resolved
        # The admitted generic envelope is installed here solely to focus this
        # fixture on an already-started gateway lease; real admission has its
        # separate owner API tests. No external Work authority is simulated.
        bind_job(env.platform,row,{'scope':'opaque-test-scope'})
    return state


def test_currentness_denial_sends_zero_engine_bytes(authority_env):
    state=attach(authority_env);state.current=False
    async def scenario():
        async with connected(authority_env) as env:
            with pytest.raises(GatewayError):await consume(await infer(env))
            assert state.checks and env.engine.calls==[] and env.helper.reservation is None
    asyncio.run(scenario())


def test_currentness_withdrawal_stops_stream_but_allows_stop_report(authority_env):
    state=attach(authority_env)
    async def scenario():
        async with connected(authority_env,stall=True) as env:
            request=await infer(env);consumer=asyncio.create_task(consume(request))
            await asyncio.wait_for(env.engine.started.wait(),3)
            state.current=False
            with pytest.raises(GatewayError):await asyncio.wait_for(consumer,5)
            await asyncio.wait_for(env.engine.stopped.wait(),2)
            with env.platform.db.sessions() as session:
                row=session.get(RequestLedger,request.id)
                assert row.status!='active' and row.outcome!='completed'
                assert row.evidence['stop_confirmed'] is True
    asyncio.run(scenario())
