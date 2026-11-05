import asyncio
import copy
import time

import httpx
import pytest
from sqlalchemy import select

from connector_support import BODY
from model_support import authority_env, connected, consume, database, infer, v2_bundle
from sovite.db import (BindingState, BundleRevision, ExecutionAuthority, GrantState, HelperEnrollment,
                       Job, MetadataRow, RequestLedger, WorkerExecutionLease)
from sovite.gateway.client import GatewayError
from sovite.gateway.runtime import Gateway
from sovite.gateway.server import create_inference_app
from sovite.connector.helper import PilotHelper
from sovite.connector.identity import sign


async def _wait_for_bounded_engine_start(env, request, consumer, request_deadline, startup_budget):
    engine_started = asyncio.create_task(env.engine.started.wait())
    try:
        done, _ = await asyncio.wait({engine_started, consumer}, timeout=startup_budget,
                                     return_when=asyncio.FIRST_COMPLETED)
        if engine_started not in done or consumer in done:
            error = None
            if consumer.done() and not consumer.cancelled():
                error = consumer.exception()
            safe_codes = {
                'authority_unavailable', 'gateway_fenced', 'model_host_unavailable',
                'model_host_preempted', 'model_identity_mismatch', 'model_protocol_error',
                'permit_expired', 'deadline_exceeded', 'inference_request_denied',
            }
            diagnostic = {
                'engine_started': env.engine.started.is_set(),
                'consumer_done': consumer.done(),
                'startup_budget_seconds': round(startup_budget, 3),
                'consumer_error_class': type(error).__name__ if error is not None else None,
                'consumer_error_code': error.code if isinstance(error, GatewayError)
                    and type(error.code) is str and error.code in safe_codes else None,
                'engine_calls': len(env.engine.calls),
            }
            helper_session = getattr(env.helper, 'session', None)
            helper_phase = getattr(helper_session, 'phase', None)
            helper_task = getattr(env.helper, 'task', None)
            diagnostic.update({
                'helper_closed': helper_phase == 'closed' if helper_session is not None else None,
                'helper_active': getattr(helper_session, 'active', None) is not None
                    if helper_session is not None else None,
                'helper_phase': helper_phase,
                'helper_task_done': helper_task.done() if helper_task is not None else None,
            })
            try:
                with env.platform.db.sessions() as session:
                    ledger = session.get(RequestLedger, request.id)
                    job = session.get(Job, env.job.id)
                    lease = session.get(WorkerExecutionLease, env.job.id)
                now = time.time()
                diagnostic.update({
                    'ledger_status': ledger.status if ledger is not None else None,
                    'ledger_outcome': ledger.outcome if ledger is not None else None,
                    'request_seconds_left': round(request_deadline - now, 3),
                    'job_status': job.status if job is not None else None,
                    'cancel_requested': job.cancel_requested if job is not None else None,
                    'lease_phase': lease.phase if lease is not None else None,
                    'lease_fence_matches': lease.fence == env.job.execution_fence if lease is not None else None,
                    'lease_seconds_left': round(lease.lease_until - now, 3) if lease is not None else None,
                    'execution_seconds_left': round(lease.execution_deadline - now, 3)
                        if lease is not None and lease.execution_deadline is not None else None,
                })
            except Exception as exc:
                diagnostic['snapshot_error_class'] = type(exc).__name__
            raise AssertionError(f'bounded pre-partition start barrier failed: {diagnostic!r}')
    except BaseException:
        if not consumer.done():
            consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        raise
    finally:
        if not engine_started.done():
            engine_started.cancel()
        await asyncio.gather(engine_started, return_exceptions=True)


@pytest.mark.parametrize('denial,code', [
    ('capability', 'execution_capability_invalid'), ('expired_capability', 'execution_capability_invalid'),
    ('workload', 'workload_denied'), ('fence', 'execution_fenced'), ('lease', 'execution_fenced'),
    ('cancel', 'cancelled'), ('generation', 'revision_superseded'), ('grant', 'grant_inhibited'),
    ('caller', 'caller_denied'), ('qualification', 'qualification_required'),
    ('binding_inhibit', 'binding_inhibited'), ('binding_disabled', 'binding_disabled'),
    ('clearance', 'binding_context_denied'), ('enrollment_revoked', 'model_identity_mismatch'),
    ('pin', 'model_identity_mismatch'), ('wrong_session', 'model_identity_mismatch'),
    ('operation', 'inference_request_denied'), ('body', 'inference_request_denied'),
    ('body_size', 'inference_request_denied'), ('deadline', 'inference_request_denied'),
    ('expired_deadline', 'inference_request_denied'),
])
def test_PIN_BEFORE_PROMPT_CURRENT_AUTHORITY_seven_checks_deny_zero_engine_bytes(authority_env, denial, code):
    async def scenario():
        async with connected(authority_env, relaxed_hello_setup=True) as env:
            changes = {}
            with env.platform.db.sessions.begin() as session:
                execution = session.get(ExecutionAuthority, env.job.id)
                job = session.get(Job, env.job.id)
                revision = session.get(BundleRevision, job.generation)
                bundle = copy.deepcopy(revision.bundle)
                if denial == 'capability': changes['capability'] = 'wrong'
                elif denial == 'expired_capability': execution.expires_at = time.time() - 1
                elif denial == 'workload': changes['workload'] = 'helper'
                elif denial == 'fence': changes['fence'] = 'superseded'
                elif denial == 'lease': job.lease_until = time.time() - 1
                elif denial == 'cancel': job.cancel_requested = True
                elif denial == 'generation': session.get(MetadataRow, 'active_generation').value = {'generation': 'replaced'}
                elif denial == 'grant': session.get(GrantState, job.grant_id).inhibited = True
                elif denial == 'caller': bundle['grants'][0]['callers'] = ['someone-else']
                elif denial == 'qualification': env.settings.require_qualification = True
                elif denial == 'binding_inhibit': session.add(BindingState(id=env.model['id'], inhibited=True))
                elif denial == 'binding_disabled': bundle['models'][0]['enabled'] = False
                elif denial == 'clearance': bundle['destinations'][-1]['clearance'] = 'C0'
                elif denial == 'enrollment_revoked': session.get(HelperEnrollment, 'enrollment-a').revoked = True
                elif denial == 'pin':
                    # Corrupt the recorded session identity, leaving the current qualified recipe unchanged.
                    from sovite.db import HelperSession
                    helper = next(iter(env.gateway.sessions.values()))
                    row = session.get(HelperSession, helper.session.session_id)
                    row.identity = {**row.identity, 'engine_version': 'substituted'}
                elif denial == 'wrong_session':
                    from sovite.db import HelperSession
                    helper = next(iter(env.gateway.sessions.values()))
                    session.get(HelperSession, helper.session.session_id).binding_id = 'other-binding'
                elif denial == 'operation': changes.update(operation='responses')
                elif denial == 'body': changes['body'] = b'{"model":"test-model","messages":[],"stream":true,"url":"http://forbidden"}'
                elif denial == 'body_size': changes['body'] = b'x' * (4 * 1024 * 1024 + 1)
                elif denial == 'deadline': changes['deadline'] = int(time.time()) + 601
                elif denial == 'expired_deadline': changes['deadline'] = int(time.time()) - 1
                revision.bundle = bundle
                if denial == 'generation':
                    session.add(BundleRevision(generation='replaced', bundle=bundle))
            with pytest.raises(GatewayError) as failure:
                request = await infer(env, **changes)
                await consume(request)
            assert failure.value.code == code
            assert env.engine.calls == []  # PIN_BEFORE_PROMPT: no engine request and zero prompt bytes.
            assert env.helper.reservation is None
    asyncio.run(scenario())


def test_EXACT_BINDING_SESSION_IDENTITY_success_records_private_evidence(authority_env):
    async def scenario():
        async with connected(authority_env) as env:
            request = await infer(env)
            result = await consume(request)
            assert b'[DONE]' in result
            assert env.engine.calls[0][1] == BODY
            with env.platform.db.sessions() as session:
                row = session.get(RequestLedger, request.id)
                assert (row.status, row.outcome) == ('finished', 'completed')
                assert row.fence == env.job.execution_fence
                assert row.evidence['preparation']['host_instance_id'] == 'host-1'
                assert row.evidence['pinning'] == 'required'
                assert 'private-canary' not in str(row.request)
                assert env.job.execution_capability not in str(row.__dict__)
    asyncio.run(scenario())


@pytest.mark.parametrize('withdrawal', ['lease', 'fence', 'cancel', 'binding', 'generation', 'enrollment', 'control_partition'])
def test_LEASE_FENCED_BOUNDED_STOP_mid_stream_withdrawal_cancels_no_completion(authority_env, withdrawal):
    async def scenario():
        async with connected(authority_env, stall=True) as env:
            request_deadline = int(time.time()) + 25
            request = await infer(env, deadline=request_deadline)
            consumer = asyncio.create_task(consume(request))
            try:
                startup_budget = min(10.0, request_deadline - time.time(),
                                     env.job.lease_until - time.time(),
                                     env.job.execution_deadline - time.time())
                assert startup_budget > 5.0
                await _wait_for_bounded_engine_start(env, request, consumer, request_deadline, startup_budget)
                remaining = min(request_deadline - time.time(),
                                env.job.lease_until - time.time(),
                                env.job.execution_deadline - time.time())
                assert remaining > 5.0
                start = time.monotonic()
                with env.platform.db.sessions.begin() as session:
                    if withdrawal == 'lease': session.get(Job, env.job.id).lease_until = time.time() - 1
                    elif withdrawal == 'fence': session.get(ExecutionAuthority, env.job.id).fence = 'replacement-fence'
                    elif withdrawal == 'cancel': session.get(Job, env.job.id).cancel_requested = True
                    elif withdrawal == 'binding': session.add(BindingState(id=env.model['id'], inhibited=True))
                    elif withdrawal == 'generation': session.get(MetadataRow, 'active_generation').value = {'generation': 'superseded'}
                    elif withdrawal == 'enrollment': session.get(HelperEnrollment, 'enrollment-a').revoked = True
                if withdrawal == 'control_partition':
                    with env.platform.db.sessions() as session:
                        job = session.get(Job, env.job.id)
                        lease = session.get(WorkerExecutionLease, env.job.id)
                        assert job is not None and lease is not None
                        now = time.time()
                        remaining = min(request_deadline - now, lease.lease_until - now,
                                        lease.execution_deadline - now)
                        assert lease.fence == env.job.execution_fence
                        assert job.lease_until == lease.lease_until
                        assert job.status == 'running' and not job.cancel_requested
                        assert remaining > 5.0
                    original = env.gateway.authority.call
                    async def partition(action, **kwargs):
                        if action in {'renew', 'touch'}: raise GatewayError('authority_unavailable')
                        return await original(action, **kwargs)
                    env.gateway.authority.call = partition
                with pytest.raises(GatewayError):
                    await asyncio.wait_for(consumer, 5)
                await asyncio.wait_for(env.engine.stopped.wait(), 2)
                assert time.monotonic() - start < 5
                with env.platform.db.sessions() as session:
                    ledger = session.get(RequestLedger, request.id)
                    assert ledger.outcome != 'completed'
                    assert ledger.status != 'active'
            finally:
                if not consumer.done():
                    consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)
    asyncio.run(scenario())


def test_CURRENT_AUTHORITY_two_concurrent_requests_one_binding_no_queue(authority_env):
    async def scenario():
        async with connected(authority_env, stall=True) as env:
            results = await asyncio.gather(infer(env), infer(env), return_exceptions=True)
            assert sum(isinstance(r, GatewayError) and r.code == 'binding_busy' for r in results) == 1
            request = next(r for r in results if not isinstance(r, Exception))
            await asyncio.wait_for(env.engine.started.wait(), 3)
            env.platform.cancel(env.job.id)
            with pytest.raises(GatewayError): await consume(request)
            assert len(env.engine.calls) == 1
    asyncio.run(scenario())


def test_NO_TRANSPARENT_REPLAY_gateway_restart_visible_interruption(authority_env):
    async def scenario():
        async with connected(authority_env, stall=True) as env:
            request = await infer(env)
            consumer = asyncio.create_task(consume(request))
            await asyncio.wait_for(env.engine.started.wait(), 3)
            # Simulate loss of the gateway, without graceful cancellation or transport retry.
            await next(iter(env.gateway.sessions.values())).socket.close()
            with pytest.raises(GatewayError): await asyncio.wait_for(consumer, 4)
            await asyncio.wait_for(env.engine.stopped.wait(), 3)
            replacement = Gateway(env.gateway.authority)
            await replacement.start()
            with pytest.raises(GatewayError, match='model_host_unavailable'):
                await replacement.infer(env.model['id'], job_id=env.job.id, fence=env.job.execution_fence,
                    capability=env.job.execution_capability, workload='worker', operation='chat', body=BODY, deadline=int(time.time()) + 20)
            assert len(env.engine.calls) == 1
    asyncio.run(scenario())


def test_SESSION_IDENTITY_second_live_helper_rejected_no_takeover(authority_env):
    async def scenario():
        async with connected(authority_env) as env:
            original = next(iter(env.gateway.sessions.values()))
            second = PilotHelper(env.helper_settings)
            with pytest.raises(Exception): await second.run()
            assert env.gateway.sessions[env.model['id']] is original
            await consume(await infer(env))
            assert len(env.engine.calls) == 1
    asyncio.run(scenario())


class ChangedPreparation(PilotHelper):
    def assertion(self, challenge, session_id, **extra):
        value = super().assertion(challenge, session_id, **extra)
        if extra:
            value['engine_version'] = 'swapped-version'
        return value


def test_PIN_BEFORE_PROMPT_preparation_pin_swap_sends_zero_prompt_bytes(authority_env):
    async def scenario():
        async with connected(authority_env, helper_type=ChangedPreparation, relaxed_hello_setup=True) as env:
            request = await infer(env)
            with pytest.raises(GatewayError): await consume(request)
            assert env.engine.calls == []
            with env.platform.db.sessions() as session:
                assert session.get(RequestLedger, request.id).outcome != 'completed'
    asyncio.run(scenario())


def test_INFERENCE_ONLY_helper_credentials_rejected_as_workload_identity(authority_env):
    async def scenario():
        async with connected(authority_env) as env:
            app = create_inference_app(env.gateway, {'worker': 'workload-test-' + 'w' * 24})
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://inference.test') as client:
                for token in [(env.path / 'helper.pem').read_text().strip(), env.settings.operator_token, env.settings.authority_token, env.job.execution_capability]:
                    response = await client.post(f'/inference/{env.model["id"]}/chat', content=BODY,
                        headers={'Authorization': 'Bearer ' + token})
                    assert response.status_code == 403 and response.json()['code'] == 'workload_denied'
                response = await client.post(f'/inference/{env.model["id"]}/chat', content=BODY,
                    headers={'Authorization': 'Bearer ' + 'workload-test-' + 'w' * 24, 'X-Sovite-Capability': env.job.execution_capability,
                             'X-Sovite-Job': env.job.id, 'X-Sovite-Fence': env.job.execution_fence, 'X-Sovite-Deadline': str(int(time.time()) + 20)})
                assert response.status_code == 200 and b'[DONE]' in response.content
            assert len(env.engine.calls) == 1
    asyncio.run(scenario())


def test_BOUNDED_STOP_permits_and_cancel_progress_with_full_downstream_queue(authority_env):
    async def scenario():
        async with connected(authority_env, stall=True) as env:
            frames = []
            original = env.helper.session.receive_envelope
            def observe(frame):
                frames.append(frame.message['type'])
                return original(frame)
            env.helper.session.receive_envelope = observe
            original_stream = env.helper.engine.stream
            async def two_chunks(*args, **kwargs):
                async for block in original_stream(*args, **kwargs):
                    yield block
                    yield block
            env.helper.engine.stream = two_chunks
            request = await infer(env)
            request.output = asyncio.Queue(maxsize=1)
            await asyncio.wait_for(env.engine.started.wait(), 3)
            async with asyncio.timeout(3):
                while 'permit' not in frames:
                    await asyncio.sleep(.02)
            assert request.output.full()  # Consumer has read nothing; control messages still progress.
            env.platform.cancel(env.job.id)
            await asyncio.wait_for(request.done.wait(), 4)
            await asyncio.wait_for(env.engine.stopped.wait(), 2)
            assert 'cancel' in frames and request.error.code == 'cancelled'
            with env.platform.db.sessions() as session:
                assert session.get(RequestLedger, request.id).evidence['stop_confirmed']
    asyncio.run(scenario())


def test_BOUNDED_STOP_binding_operator_inhibit_fences_and_closes_helper(authority_env):
    async def scenario():
        async with connected(authority_env, stall=True) as env:
            request = await infer(env)
            consumer = asyncio.create_task(consume(request))
            await asyncio.wait_for(env.engine.started.wait(), 3)
            env.platform.inhibit_binding(env.model['id'], 'Qualified operator stop')
            with pytest.raises(GatewayError): await asyncio.wait_for(consumer, 5)
            await asyncio.wait_for(env.engine.stopped.wait(), 2)
            with env.platform.db.sessions() as session:
                assert session.get(BindingState, env.model['id']).inhibited
                assert session.get(RequestLedger, request.id).outcome != 'completed'
    asyncio.run(scenario())


def test_PIN_BEFORE_PROMPT_expired_signed_qualification_denies_zero_engine_bytes(authority_env):
    async def scenario():
        async with connected(authority_env) as env:
            import base64
            from sovite.db import Qualification, canonical
            from sovite.qualification import initialize_keys, issue_qualification
            from cryptography.hazmat.primitives import serialization
            env.settings.require_qualification = True
            env.settings.qualification_private_key = str(env.path / 'qualification.key')
            env.settings.qualification_public_keys = [str(env.path / 'qualification.pem')]
            initialize_keys(env.settings)
            evidence = {'generation': env.job.generation, 'profile': 'local', 'scope': 'Synthetic gateway authorization only',
                        'checks': [{'name': 'synthetic', 'status': 'passed'}]}
            qualification = issue_qualification(env.platform.db, env.settings, env.job.generation, 'local', evidence)
            key = serialization.load_pem_private_key((env.path / 'qualification.key').read_bytes(), None)
            with env.platform.db.sessions.begin() as session:
                row = session.get(Qualification, qualification.id)
                row.expires_at = time.time() - 1
                row.payload = {**row.payload, 'expires_at': row.expires_at}
                row.signature = base64.b64encode(key.sign(canonical(row.payload))).decode()
            with pytest.raises(GatewayError, match='qualification_required'): await infer(env)
            assert env.engine.calls == []
    asyncio.run(scenario())


def test_CURRENT_AUTHORITY_real_https_control_and_tls_helper(authority_env):
    async def scenario():
        async with connected(authority_env) as env:
            import socket
            import ssl
            import uvicorn
            from sovite.gateway.client import AuthorityClient
            server_socket = socket.socket()
            server_socket.bind(('127.0.0.1', 0))
            port = server_socket.getsockname()[1]
            config = uvicorn.Config(env.app, access_log=False, log_level='critical',
                ssl_certfile=str(env.path / 'server.pem'), ssl_keyfile=str(env.path / 'server.key'), lifespan='off')
            config.load()
            config.ssl.minimum_version = config.ssl.maximum_version = ssl.TLSVersion.TLSv1_3
            server = uvicorn.Server(config)
            serving = asyncio.create_task(server.serve(sockets=[server_socket]))
            trust = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            trust.load_verify_locations(env.path / 'ca.pem')
            trust.minimum_version = trust.maximum_version = ssl.TLSVersion.TLSv1_3
            client = AuthorityClient(f'https://127.0.0.1:{port}', env.settings.authority_token, verify=trust)
            original = env.gateway.authority
            try:
                async with asyncio.timeout(3):
                    while not server.started: await asyncio.sleep(.01)
                env.gateway.authority = client
                request = await infer(env)
                assert b'[DONE]' in await consume(request)
                assert len(env.engine.calls) == 1
            finally:
                env.gateway.authority = original
                await client.close()
                server.should_exit = True
                await asyncio.wait_for(serving, 3)
                server_socket.close()
    asyncio.run(scenario())


class DelayedInvocation(PilotHelper):
    async def generate(self, frame):
        await asyncio.sleep(6.1)
        await super().generate(frame)


def test_BOUNDED_STOP_renewal_preserves_qualified_first_token_deadline(authority_env):
    async def scenario():
        async with connected(authority_env, helper_type=DelayedInvocation) as env:
            request = await infer(env)
            assert b'[DONE]' in await asyncio.wait_for(consume(request), 9)
            assert len(env.engine.calls) == 1
    asyncio.run(scenario())
