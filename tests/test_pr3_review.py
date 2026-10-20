"""Regression cases from the PR 3 follow-up review, on both control stores."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import time

import httpx
import pytest
from sqlalchemy import select

from model_support import authority_env, connected, consume, database, infer, v2_bundle
from test_model_authority import request_values, renewal
from sovite.authority import CAPABILITY_SECONDS
from sovite.connector.protocol import FRESHNESS_SECONDS
from sovite.db import BindingState, ExecutionAuthority, HelperEnrollment, HelperSession, MetadataRow, RequestLedger
from sovite.gateway.client import AuthorityClient, GatewayError
from sovite.service import PlatformError


def test_capability_refresh_tied_to_lease_across_60_seconds(authority_env, monkeypatch):
    async def scenario():
        async with connected(authority_env) as env:
            started = time.time()
            import sovite.authority as authority_module
            import sovite.worker as worker_module
            for elapsed in (30, 61, 95):
                now = started + elapsed
                monkeypatch.setattr(authority_module, 'time', SimpleNamespace(time=lambda: now))
                monkeypatch.setattr(worker_module, 'time', SimpleNamespace(time=lambda: now))
                assert env.worker.renew(env.job.id)
                helper_id = next(iter(env.gateway.sessions.values())).session.session_id
                env.authority.touch(env.gateway.id, helper_id)
                values = request_values(env)
                values['request']['deadline'] = int(now) + 20
                permit = env.authority.authorize_request(**values)
                env.authority.finish(**renewal(values, permit['permit']), outcome='cancelled', stopped=True)
                with env.platform.db.sessions() as session:
                    row = session.get(ExecutionAuthority, env.job.id)
                    assert row.expires_at == min(row.deadline, now + CAPABILITY_SECONDS)
            # Refresh remains capped by the execution deadline.
            now = started + 120
            with env.platform.db.sessions.begin() as session:
                session.get(ExecutionAuthority, env.job.id).deadline = now + 5
            assert env.worker.renew(env.job.id)
            with env.platform.db.sessions() as session:
                assert session.get(ExecutionAuthority, env.job.id).expires_at == now + 5
    asyncio.run(scenario())


@pytest.mark.parametrize('withdrawal', ['digest', 'expiry', 'fence'])
def test_capability_refresh_never_revives_revoked_or_replaced_fence(authority_env, withdrawal):
    env = authority_env
    with env.platform.db.sessions.begin() as session:
        row = session.get(ExecutionAuthority, env.job.id)
        row.expires_at = time.time() - 1
        if withdrawal == 'digest': row.capability_digest = ''
        elif withdrawal == 'expiry': row.expires_at = 0
        else: row.fence = 'replacement-fence'
        before = row.expires_at, row.capability_digest, row.fence
    assert env.worker.renew(env.job.id)
    with env.platform.db.sessions() as session:
        row = session.get(ExecutionAuthority, env.job.id)
        assert (row.expires_at, row.capability_digest, row.fence) == before


def test_abandoned_active_ledger_rows_inhibit_instead_of_blocking_forever(authority_env):
    async def scenario():
        async with connected(authority_env) as env:
            values = request_values(env)
            env.authority.authorize_request(**values)
            with env.platform.db.sessions.begin() as session:
                row = session.get(RequestLedger, values['request_id'])
                row.permit_until = time.time() - env.model['cancel_bound_seconds'] + .25
            with pytest.raises(PlatformError) as failure:
                env.authority.authorize_request(**request_values(env))
            assert failure.value.code == 'binding_busy'
            with env.platform.db.sessions.begin() as session:
                session.get(RequestLedger, values['request_id']).permit_until -= 1
            for _ in range(2):
                with pytest.raises(PlatformError) as failure:
                    env.authority.authorize_request(**request_values(env))
                assert failure.value.code == 'binding_inhibited'
                with env.platform.db.sessions() as session:
                    row = session.get(RequestLedger, values['request_id'])
                    assert (row.status, row.outcome) == ('interrupted', 'model_cancel_unconfirmed')
                    assert session.get(BindingState, env.model['id']).inhibited
            assert env.engine.calls == []
    asyncio.run(scenario())


@pytest.mark.parametrize('lost_reply', [False, True])
def test_authorize_transport_failure_finishes_with_certain_stop(authority_env, lost_reply):
    async def scenario():
        async with connected(authority_env) as env:
            original = env.gateway.authority.call
            finishes, requests = [], []
            async def lose_authorize(action, **fields):
                if action == 'authorize':
                    requests.append(fields['request_id'])
                    if lost_reply: await original(action, **fields)
                    raise GatewayError('authority_unavailable')
                if action == 'finish': finishes.append(fields)
                return await original(action, **fields)
            env.gateway.authority.call = lose_authorize
            with pytest.raises(GatewayError, match='authority_unavailable'):
                await infer(env)
            assert len(finishes) == 1
            assert finishes[0]['stopped'] is True
            assert finishes[0]['outcome'] == 'authority_unavailable'
            assert finishes[0]['request_id'] == requests[0]
            assert not next(iter(env.gateway.sessions.values())).busy
            assert env.engine.calls == []
            with env.platform.db.sessions() as session:
                row = session.get(RequestLedger, requests[0])
                if lost_reply:
                    assert (row.status, row.outcome) == ('finished', 'authority_unavailable')
                    assert row.evidence['stop_confirmed']
                else: assert row is None
                assert session.get(BindingState, env.model['id']) is None
            env.gateway.authority.call = original
            assert b'[DONE]' in await consume(await infer(env))
    asyncio.run(scenario())


@pytest.mark.parametrize('stale', ['freshness', 'gateway'])
def test_stale_helper_session_allows_reconnect_after_disconnect_outage(authority_env, stale):
    async def scenario():
        async with connected(authority_env) as env:
            from sovite.connector.helper import PilotHelper
            original = env.gateway.call
            attempts = []
            async def outage(action, **fields):
                if action == 'fence':
                    attempts.append(action)
                    raise GatewayError('authority_unavailable')
                return await original(action, **fields)
            env.gateway.call = outage
            helper = next(iter(env.gateway.sessions.values()))
            await helper.close()
            async with asyncio.timeout(4):
                while env.gateway.sessions or not attempts:
                    await asyncio.sleep(.01)
            # Let all bounded fence attempts fail during the outage.
            await asyncio.sleep(.5)
            assert len(attempts) == 3
            env.gateway.call = original
            with env.platform.db.sessions.begin() as session:
                row = session.get(HelperSession, helper.session.session_id)
                assert row.active
                if stale == 'freshness': row.last_seen = time.time() - FRESHNESS_SECONDS
                else: row.gateway_id = 'old-gateway'
            second = PilotHelper(env.helper_settings)
            task = asyncio.create_task(second.run())
            try:
                async with asyncio.timeout(4):
                    while env.model['id'] not in env.gateway.sessions:
                        if task.done(): task.result()
                        await asyncio.sleep(.01)
                assert b'[DONE]' in await consume(await infer(env))
                with env.platform.db.sessions() as session:
                    assert not session.get(HelperSession, helper.session.session_id).active
                    assert len(session.scalars(select(HelperSession).where(HelperSession.active == True)).all()) == 1
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_stale_helper_session_with_uncertain_request_persists_visible_inhibit(authority_env):
    async def scenario():
        async with connected(authority_env) as env:
            values = request_values(env)
            env.authority.authorize_request(**values)
            with env.platform.db.sessions.begin() as session:
                session.get(HelperSession, values['request']['session_id']).last_seen = time.time() - FRESHNESS_SECONDS
                enrollment = session.get(HelperEnrollment, 'enrollment-a')
                certificate_digest = enrollment.certificate_digest
            with pytest.raises(PlatformError) as failure:
                env.authority.open_session(env.gateway.id, certificate_digest)
            assert failure.value.code == 'binding_inhibited'
            with env.platform.db.sessions() as session:
                helper = session.get(HelperSession, values['request']['session_id'])
                assert not helper.active
                assert helper.last_seen > time.time() - 1
                assert session.get(RequestLedger, values['request_id']).outcome == 'model_cancel_unconfirmed'
                assert session.get(BindingState, env.model['id']).inhibited
    asyncio.run(scenario())


@pytest.mark.parametrize('invalid', ['capability', 'fence', 'outcome', 'stopped'])
def test_authorize_lost_reply_cleanup_requires_current_fence_and_certain_stop(authority_env, invalid):
    async def scenario():
        async with connected(authority_env) as env:
            values = request_values(env)
            env.authority.authorize_request(**values)
            fields = renewal(values, None) | {'outcome': 'authority_unavailable', 'stopped': True}
            if invalid == 'capability': fields['capability'] = 'wrong-capability'
            elif invalid == 'fence':
                with env.platform.db.sessions.begin() as session:
                    session.get(ExecutionAuthority, env.job.id).fence = 'replaced'
            elif invalid == 'outcome': fields['outcome'] = 'completed'
            else: fields['stopped'] = False
            with pytest.raises(PlatformError) as failure:
                env.authority.finish(**fields)
            assert failure.value.code == 'permit_expired'
            with env.platform.db.sessions() as session:
                assert session.get(RequestLedger, values['request_id']).status == 'active'
    asyncio.run(scenario())


def test_disconnect_fence_retries_with_small_bound(authority_env):
    async def scenario():
        async with connected(authority_env) as env:
            original, attempts = env.gateway.call, []
            fenced = asyncio.Event()
            async def transient(action, **fields):
                if action == 'fence':
                    attempts.append(action)
                    if len(attempts) < 3: raise GatewayError('authority_unavailable')
                result = await original(action, **fields)
                if action == 'fence': fenced.set()
                return result
            env.gateway.call = transient
            helper = next(iter(env.gateway.sessions.values()))
            await helper.close()
            await asyncio.wait_for(fenced.wait(), 3)
            with env.platform.db.sessions() as session:
                assert not session.get(HelperSession, helper.session.session_id).active
            assert len(attempts) == 3
    asyncio.run(scenario())


@pytest.mark.parametrize('field,value', [('deadline', 1.5), ('deadline', True), ('deadline', float('inf')),
    ('job_id', ''), ('fence', 'bad/fence'), ('capability', ''), ('workload', 'x' * 161)])
def test_malformed_worker_requests_are_denied_locally(authority_env, field, value):
    async def scenario():
        async with connected(authority_env) as env:
            calls = []
            original = env.gateway.call
            async def observe(action, **fields):
                calls.append(action)
                return await original(action, **fields)
            env.gateway.call = observe
            with pytest.raises(GatewayError) as failure:
                await infer(env, **{field: value})
            assert failure.value.code == 'inference_request_denied'
            assert 'authorize' not in calls
            assert not next(iter(env.gateway.sessions.values())).busy
            assert env.engine.calls == []
    asyncio.run(scenario())


def test_malformed_worker_control_422_is_request_denied():
    async def scenario():
        async def invalid(request):
            return httpx.Response(422, json={'detail': [{'msg': 'private diagnostic'}]})
        client = AuthorityClient('https://control.test', 'x' * 24, transport=httpx.MockTransport(invalid))
        try:
            with pytest.raises(GatewayError, match='^inference_request_denied$'):
                await client.call('authorize')
        finally: await client.close()
    asyncio.run(scenario())


def test_terminal_inference_requests_helper_sessions_retention_sweep(authority_env, monkeypatch):
    from sovite import authority as module
    from sovite.worker import Worker
    env, now = authority_env, time.time()
    env.settings.authority_retention_days = 2
    monkeypatch.setattr(module, 'RETENTION_BATCH', 3, raising=False)
    with env.platform.db.sessions.begin() as session:
        # claim() has already run recovery; permit the first scheduled sweep.
        marker = session.get(MetadataRow, 'authority_retention_sweep')
        if marker: session.delete(marker)
        for index in range(5):
            session.add(HelperSession(id=f'old-{index}', binding_id='binding', enrollment_id='enrollment',
                gateway_id='gateway', generation='generation', challenge='nonce', active=False, last_seen=now - 3 * 86400))
        for name, status, age in [(f'old-request-{i}', 'finished' if i % 2 else 'interrupted', 3) for i in range(5)] + [('recent-request', 'interrupted', 1),
                                  ('active-request', 'active', 3)]:
            session.add(RequestLedger(id=name, job_id=env.job.id, fence='fence', binding_id='binding',
                session_id='retained-session', gateway_id='gateway', workload='worker', request={},
                permit_digest='digest', permit_until=now - 3, status=status, created_at=now - age * 86400,
                updated_at=now - age * 86400))
        for name, active, age in [('retained-session', False, 3), ('active-session', True, 3), ('recent-session', False, 1)]:
            session.add(HelperSession(id=name, binding_id='binding', enrollment_id='enrollment', gateway_id='gateway',
                generation='generation', challenge='nonce', active=active, last_seen=now - age * 86400))
    env.worker.recover_expired()
    with env.platform.db.sessions() as session:
        assert len(session.scalars(select(RequestLedger).where(RequestLedger.id.like('old-request-%'))).all()) == 2
        assert session.get(RequestLedger, 'recent-request') is not None
        assert session.get(RequestLedger, 'active-request') is not None
        assert len(session.scalars(select(HelperSession).where(HelperSession.id.like('old-%'))).all()) == 2
        for name in ['retained-session', 'active-session', 'recent-session']:
            assert session.get(HelperSession, name) is not None
    # A second worker shares the hourly throttle instead of deleting another batch.
    Worker(env.platform).recover_expired()
    with env.platform.db.sessions() as session:
        assert len(session.scalars(select(HelperSession).where(HelperSession.id.like('old-%'))).all()) == 2
        assert len(session.scalars(select(RequestLedger).where(RequestLedger.id.like('old-request-%'))).all()) == 2
    monkeypatch.setattr(module, 'time', SimpleNamespace(time=lambda: now + 3601))
    Worker(env.platform).recover_expired()
    with env.platform.db.sessions() as session:
        assert not session.scalars(select(HelperSession).where(HelperSession.id.like('old-%'))).all()
        assert not session.scalars(select(RequestLedger).where(RequestLedger.id.like('old-request-%'))).all()
        assert session.get(HelperSession, 'retained-session') is not None


def test_terminal_authority_retention_configurable_default_7_days(monkeypatch):
    from sovite.settings import Settings
    assert Settings().authority_retention_days == 7
    monkeypatch.setenv('SOVITE_OPERATOR_TOKEN', 'o' * 24)
    monkeypatch.setenv('SOVITE_AUTHORITY_RETENTION_DAYS', '3')
    assert Settings.from_env().authority_retention_days == 3
    with pytest.raises(ValueError, match='retention'):
        Settings(authority_retention_days=0)


def test_schema_3_rollback_requires_preupgrade_database_restore_documented():
    text = Path('docs/OPERATIONS.md').read_text()
    assert 'rollback requires a database restore from before the upgrade' in text
