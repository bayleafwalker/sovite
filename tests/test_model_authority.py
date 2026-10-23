import asyncio
import copy
import hashlib
import threading
import time
from types import SimpleNamespace
import uuid

import httpx
import pytest
from sqlalchemy import select

from connector_support import BODY
from model_support import authority_env, connected, consume, database, infer, v2_bundle
from sovite.authority import CAPABILITY_SECONDS, checksum
from sovite.connector.identity import digest
from sovite.db import BindingState, ExecutionAuthority, HelperSession, Job, MetadataRow, RequestLedger
from sovite.gateway.client import AuthorityClient, GatewayError
from sovite.service import PlatformError, job_dict


def request_values(env):
    helper = next(iter(env.gateway.sessions.values()))
    return dict(gateway_id=env.gateway.id, request_id=uuid.uuid4().hex, capability=env.job.execution_capability, workload='worker',
                request={'job_id': env.job.id, 'fence': env.job.execution_fence, 'session_id': helper.session.session_id,
                         'operation': 'chat', 'body_digest': digest(BODY), 'body_size': len(BODY), 'deadline': int(time.time()) + 20})


def renewal(values, permit):
    return {k: v for k, v in values.items() if k != 'request'} | {'permit': permit}


def test_LEASE_FENCED_claim_digest_only_hand_back_and_reclaim_replace_capability(authority_env):
    env = authority_env
    with env.platform.db.sessions() as session:
        execution = session.get(ExecutionAuthority, env.job.id)
        assert execution.capability_digest == checksum(env.job.execution_capability)
        assert 0 < execution.expires_at - time.time() <= CAPABILITY_SECONDS
        assert execution.context['lease_owner'] == env.worker.id
        assert execution.context['caller'] == env.job.caller
        assert env.job.execution_capability not in str(job_dict(env.job, detail=True))
    env.worker.hand_back(env.job.id)
    with env.platform.db.sessions() as session:
        assert session.get(ExecutionAuthority, env.job.id).capability_digest == ''
    claimed = env.worker.claim()
    assert claimed.execution_fence != env.job.execution_fence
    assert claimed.execution_capability != env.job.execution_capability


def test_LEASE_FENCED_lease_recovery_replaces_execution_capability(authority_env):
    env = authority_env
    with env.platform.db.sessions.begin() as session:
        session.get(Job, env.job.id).lease_until = time.time() - 1
    env.worker.recover_expired()
    with env.platform.db.sessions() as session:
        assert session.get(ExecutionAuthority, env.job.id).capability_digest == ''
    claimed = env.worker.claim()
    assert claimed.execution_fence != env.job.execution_fence


def test_LEASE_FENCED_hand_back_reclaim_while_streaming_cancels_old_fence(authority_env):
    async def scenario():
        async with connected(authority_env, stall=True) as env:
            request = await infer(env)
            consumer = asyncio.create_task(consume(request))
            await asyncio.wait_for(env.engine.started.wait(), 3)
            env.worker.hand_back(env.job.id)
            replacement = env.worker.claim()
            assert replacement.execution_fence != env.job.execution_fence
            with pytest.raises(GatewayError, match='execution_capability_invalid'):
                await asyncio.wait_for(consumer, 4)
            await asyncio.wait_for(env.engine.stopped.wait(), 2)
            with env.platform.db.sessions() as session:
                assert session.get(RequestLedger, request.id).outcome != 'completed'
    asyncio.run(scenario())


@pytest.mark.parametrize('first', ['authorize', 'cancel'])
def test_CURRENT_AUTHORITY_cancellation_authorization_transaction_race(authority_env, monkeypatch, first):
    async def scenario():
        async with connected(authority_env) as env:
            values = request_values(env)
            entered, release, contender = threading.Event(), threading.Event(), threading.Event()
            if first == 'authorize':
                original = env.authority._checks
                def pause(*args, **kwargs):
                    result = original(*args, **kwargs)
                    entered.set()
                    assert release.wait(5)
                    return result
                monkeypatch.setattr(env.authority, '_checks', pause)
                winner = asyncio.create_task(asyncio.to_thread(env.authority.authorize_request, **values))
                def second():
                    contender.set()
                    return env.platform.cancel(env.job.id)
            else:
                original = env.platform.get_job
                def pause(*args, **kwargs):
                    result = original(*args, **kwargs)
                    entered.set()
                    assert release.wait(5)
                    return result
                monkeypatch.setattr(env.platform, 'get_job', pause)
                winner = asyncio.create_task(asyncio.to_thread(env.platform.cancel, env.job.id))
                def second():
                    contender.set()
                    return env.authority.authorize_request(**values)
            assert await asyncio.to_thread(entered.wait, 3)
            loser = asyncio.create_task(asyncio.to_thread(second))
            assert await asyncio.to_thread(contender.wait, 3)
            try:
                await asyncio.sleep(.05)
                assert not loser.done()  # Both databases serialize at the same authority lock.
            finally:
                release.set()
            if first == 'authorize':
                permit = await winner
                await loser
                monkeypatch.setattr(env.authority, '_checks', original)
                with pytest.raises(PlatformError, match='Inference authority denied') as failure:
                    env.authority.renew_permit(**renewal(values, permit['permit']))
                assert failure.value.code == 'cancelled'
            else:
                await winner
                with pytest.raises(PlatformError) as failure: await loser
                assert failure.value.code == 'cancelled'
            assert env.engine.calls == []
    asyncio.run(scenario())


def test_CURRENT_AUTHORITY_concurrent_control_transactions_one_binding(authority_env):
    async def scenario():
        async with connected(authority_env) as env:
            barrier = threading.Barrier(2)
            def authorize(values):
                barrier.wait(timeout=3)
                try: return env.authority.authorize_request(**values)
                except PlatformError as error: return error.code
            results = await asyncio.gather(*(asyncio.to_thread(authorize, request_values(env)) for _ in range(2)))
            assert sum(isinstance(r, dict) for r in results) == 1
            assert results.count('binding_busy') == 1
            assert env.engine.calls == []  # Denied-before-prompt for the concurrent request.
    asyncio.run(scenario())


def test_BOUNDED_STOP_permit_ttl_caps_lease_expiry_no_resurrection(authority_env, monkeypatch):
    async def scenario():
        async with connected(authority_env) as env:
            now = time.time()
            with env.platform.db.sessions.begin() as session:
                session.get(Job, env.job.id).lease_until = now + .25
            import sovite.authority as module
            monkeypatch.setattr(module, 'time', SimpleNamespace(time=lambda: now))
            values = request_values(env)
            permit = env.authority.authorize_request(**values)
            assert 1 <= permit['permit_ttl_ms'] <= 250
            with env.platform.db.sessions() as session:
                assert session.get(RequestLedger, values['request_id']).permit_until <= now + .25
            monkeypatch.setattr(module, 'time', SimpleNamespace(time=lambda: now + .3))
            with pytest.raises(PlatformError) as failure:
                env.authority.renew_permit(**renewal(values, permit['permit']))
            assert failure.value.code == 'permit_expired'
            assert env.engine.calls == []
    asyncio.run(scenario())


def test_BOUNDED_STOP_permit_maximum_replay_and_session_fence(authority_env, monkeypatch):
    async def scenario():
        async with connected(authority_env) as env:
            values = request_values(env)
            permit = env.authority.authorize_request(**values)
            assert 1 <= permit['permit_ttl_ms'] <= 3000
            with pytest.raises(PlatformError) as failure: env.authority.authorize_request(**values)
            assert failure.value.code == 'request_replayed'
            env.authority.fence_session(env.gateway.id, values['request']['session_id'])
            with pytest.raises(PlatformError): env.authority.renew_permit(**renewal(values, permit['permit']))
            with env.platform.db.sessions() as session:
                assert session.get(BindingState, env.model['id']).inhibited
                assert session.get(RequestLedger, values['request_id']).outcome == 'model_cancel_unconfirmed'
    asyncio.run(scenario())


def test_NO_TRANSPARENT_REPLAY_restart_fences_gateway_and_unconfirmed_generation(authority_env):
    async def scenario():
        async with connected(authority_env) as env:
            values = request_values(env)
            permit = env.authority.authorize_request(**values)
            env.authority.start('new-gateway')
            with pytest.raises(PlatformError): env.authority.renew_permit(**renewal(values, permit['permit']))
            with env.platform.db.sessions() as session:
                assert session.get(RequestLedger, values['request_id']).status == 'interrupted'
                assert session.get(BindingState, env.model['id']).inhibited
                assert not session.get(HelperSession, values['request']['session_id']).active
            assert env.engine.calls == []
    asyncio.run(scenario())


def test_CURRENT_AUTHORITY_late_completion_after_lease_expiry_is_discarded(authority_env):
    async def scenario():
        async with connected(authority_env) as env:
            original = env.gateway.authority.call
            async def withdraw_on_complete(action, **values):
                if action == 'finish' and values['outcome'] == 'completed':
                    with env.platform.db.sessions.begin() as session:
                        session.get(Job, env.job.id).lease_until = time.time() - 1
                return await original(action, **values)
            env.gateway.authority.call = withdraw_on_complete
            request = await infer(env)
            delivered = []
            with pytest.raises(GatewayError, match='execution_fenced'):
                async for block in request.stream(): delivered.append(block)
            assert b'[DONE]' not in b''.join(delivered)
            with env.platform.db.sessions() as session:
                assert session.get(RequestLedger, request.id).outcome != 'completed'
    asyncio.run(scenario())


def test_INFERENCE_ONLY_authority_rejects_operator_caller_helper_credentials(authority_env):
    async def scenario():
        env = authority_env
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url='http://control.test') as client:
            for token in ['', env.settings.operator_token, *env.settings.caller_tokens.values(), 'helper-certificate-identity']:
                response = await client.post('/internal/authority/start', json={'gateway_id': 'test'}, headers={'Authorization': 'Bearer ' + token})
                assert response.status_code == 403
                assert response.json()['detail']['code'] == 'workload_denied'
            response = await client.post('/internal/authority/start', json={'gateway_id': 'test'}, headers={'Authorization': 'Bearer ' + env.settings.authority_token})
            assert response.status_code == 200
    asyncio.run(scenario())


def test_authority_settings_reject_credential_reuse():
    from sovite.settings import Settings
    for tokens in [{'operator_token': 'x' * 24}, {'caller_tokens': {'worker': 'x' * 24}}]:
        with pytest.raises(ValueError, match='dedicated'):
            Settings(authority_token='x' * 24, **tokens)


def test_BOUNDED_STOP_authority_timeout_invalid_response_and_roundtrip_budget():
    async def scenario():
        async def unavailable(request):
            raise httpx.ConnectError('sensitive upstream diagnostic')
        client = AuthorityClient('https://control.test', 'x' * 24, transport=httpx.MockTransport(unavailable))
        with pytest.raises(GatewayError, match='authority_unavailable'):
            await client.call('renew')
        await client.close()
        async def stale(request):
            await asyncio.sleep(.06)
            return httpx.Response(200, json={'permit': 'test', 'permit_ttl_ms': 50})
        client = AuthorityClient('https://control.test', 'x' * 24, transport=httpx.MockTransport(stale))
        with pytest.raises(GatewayError, match='permit_expired'): await client.call('renew')
        await client.close()
        async def excessive(request):
            return httpx.Response(200, json={'permit': 'test', 'permit_ttl_ms': 3001})
        client = AuthorityClient('https://control.test', 'x' * 24, transport=httpx.MockTransport(excessive))
        with pytest.raises(GatewayError, match='authority_unavailable'): await client.call('renew')
        await client.close()
        async def slow(request):
            await asyncio.sleep(5)
        client = AuthorityClient('https://control.test', 'x' * 24, transport=httpx.MockTransport(slow))
        start = time.monotonic()
        with pytest.raises(GatewayError, match='authority_unavailable'): await client.call('renew')
        assert time.monotonic() - start < 1.5
        await client.close()
    asyncio.run(scenario())


def test_gateway_entrypoint_reads_only_reduced_settings(tmp_path, monkeypatch):
    import json
    from sovite import cli
    from sovite.gateway import server
    path = tmp_path / 'gateway.json'
    fields = {'authority_url': 'https://control.test', 'authority_token': 'a' * 32, 'authority_ca_file': 'control-ca.pem',
              'workload_tokens': {'worker': 'w' * 32}, 'cert_file': 'gateway.pem', 'key_file': 'gateway.key', 'helper_ca_file': 'helper-ca.pem'}
    path.write_text(json.dumps(fields))
    monkeypatch.setenv('SOVITE_GATEWAY_SETTINGS', str(path))
    monkeypatch.setenv('SOVITE_DATABASE_URL', 'forbidden-ambient-database')
    monkeypatch.setenv('SOVITE_S3_ENDPOINT', 'forbidden-ambient-store')
    monkeypatch.setenv('SOVITE_OPERATOR_TOKEN', 'forbidden-ambient-token')
    settings = server.GatewaySettings.from_env()
    settings.validate()
    assert not {'database_url', 's3_endpoint', 'operator_token'}.intersection(vars(settings))
    monkeypatch.setattr('sovite.settings.Settings.from_env', lambda: pytest.fail('shared runtime settings loaded'))
    called = []
    monkeypatch.setattr(server, 'main', lambda: called.append(True))
    monkeypatch.setattr('sys.argv', ['sovite', 'gateway'])
    cli.main()
    assert called == [True]
    fields['database_url'] = 'not-permitted'
    path.write_text(json.dumps(fields))
    with pytest.raises(TypeError): server.GatewaySettings.from_env()


def test_CURRENT_AUTHORITY_inprocess_invalid_request_fails_closed(authority_env):
    async def scenario():
        async with connected(authority_env) as env:
            values = request_values(env)
            values['request']['body_digest'] = 'not-a-digest'
            with pytest.raises(PlatformError) as failure: env.authority.authorize_request(**values)
            assert failure.value.code == 'inference_request_denied'
            assert env.engine.calls == []
    asyncio.run(scenario())


def test_o2_actual_legacy_gateway_fence_matches_universal_and_captured_workload(authority_env):
    from sovite.db import WorkerExecutionLease

    env = authority_env
    with env.platform.db.sessions() as session:
        lease = session.get(WorkerExecutionLease, env.job.id)
        execution = session.get(ExecutionAuthority, env.job.id)
        assert lease is not None, "gateway must share the universal execution owner"
        assert lease.phase == "started"
        assert execution.fence == lease.fence == env.job.execution_fence
        assert execution.context["workload"] == lease.workload_id == env.settings.execution_workload_identity
        assert execution.deadline == lease.execution_deadline
    env.worker.hand_back(env.job.id)
    with env.platform.db.sessions() as session:
        assert session.get(WorkerExecutionLease, env.job.id).phase == "finished"
        assert session.get(ExecutionAuthority, env.job.id).capability_digest == ""
