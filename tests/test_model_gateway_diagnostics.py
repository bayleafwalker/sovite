import asyncio
from pathlib import Path
import time

import httpx
import pytest
from sqlalchemy.exc import OperationalError
import sovite
import model_support

from model_support import (
    _DiagnosticASGITransport,
    _DiagnosticGateway,
    _HandshakeDiagnostics,
    _safe_cause_category,
    _safe_exception_category,
    authority_env,
    connected,
    consume,
    database,
    infer,
    v2_bundle,
)
from sovite.gateway.client import AUTHORITY_TIMEOUT, AuthorityClient, GatewayError

_SOURCE_ROOT = Path(__file__).resolve().parents[1]
assert Path(model_support.__file__).resolve() == Path(__file__).with_name("model_support.py")
assert Path(sovite.__file__).resolve() == _SOURCE_ROOT / "sovite" / "__init__.py"


class _ScriptedTransport(httpx.AsyncBaseTransport):
    def __init__(self, *, status=200, content=b'{"permit":"permit-value","permit_ttl_ms":10000}', error=None):
        self.status = status
        self.content = content
        self.error = error
        self.closed = False

    async def handle_async_request(self, request):
        if self.error is not None:
            error = self.error
            if isinstance(error, BaseException):
                raise error
            raise error(request)
        return httpx.Response(self.status, content=self.content, request=request)

    async def aclose(self):
        self.closed = True


class _ReturningTransport(httpx.AsyncBaseTransport):
    def __init__(self, response):
        self.response = response
        self.closed = False

    async def handle_async_request(self, _request):
        return self.response

    async def aclose(self):
        self.closed = True


class _BrokenDiagnostics:
    def add(self, **_fields):
        raise RuntimeError("observer-private-canary-add")

    def note(self):
        raise RuntimeError("observer-private-canary-note")


class _BrokenNoteDiagnostics(_HandshakeDiagnostics):
    def note(self):
        raise RuntimeError("observer-private-canary-note")


class _FixedAuthority:
    def __init__(self, *, result=None, error=None):
        self.result = result
        self.error = error
        self.closed = False

    async def call(self, _action, **_fields):
        if self.error is not None:
            raise self.error
        return self.result

    async def close(self):
        self.closed = True


def _private_request():
    return httpx.Request("POST", "http://control.test/internal/authority/authorize", json={"canary": "private-canary"})


def test_real_authority_success_records_only_safe_diagnostics(authority_env):
    async def scenario():
        async with connected(authority_env) as env:
            request = await infer(env)
            result = await consume(request)
            assert b"[DONE]" in result
            assert AUTHORITY_TIMEOUT == 1
            events = env.authority_diagnostics.events
            assert any(event.get("action") == "authorize" and event.get("outcome") == "ok" for event in events)
            assert any(event.get("action") == "authorize" and event.get("status") == "2xx" for event in events)
            note = env.authority_diagnostics.note()
            assert "private-canary" not in note
            assert "authority-test-credential" not in note

    asyncio.run(scenario())


def test_authority_failure_note_marks_owner_state_unobserved(authority_env):
    async def scenario():
        async with connected(authority_env) as env:
            async def unavailable(_action, **_fields):
                raise GatewayError("authority_unavailable") from TimeoutError("private-canary-timeout")

            env.gateway.authority.call = unavailable
            with pytest.raises(GatewayError) as failure:
                await env.gateway.call("authorize", request={"canary": "private-canary"})
            notes = "\n".join(getattr(failure.value, "__notes__", ()))
            assert "cause_category:timeout" in notes
            assert "owner_state:not_observed" in notes
            assert "job_status" not in notes
            assert "lease_" not in notes
            assert "private-canary" not in notes

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("error_factory", "category"),
    [
        (lambda request: httpx.ReadTimeout("private-canary", request=request), "http_timeout"),
        (lambda request: httpx.ConnectError("private-canary", request=request), "transport_error"),
        (lambda _request: OperationalError("select", {}, RuntimeError("private-canary")), "database_operational_error"),
    ],
)
def test_transport_exception_diagnostics_are_finite_and_redacted(error_factory, category):
    async def scenario():
        diagnostics = _HandshakeDiagnostics()
        inner = _ScriptedTransport(error=error_factory)
        observed = _DiagnosticASGITransport(inner, diagnostics)
        client = AuthorityClient("http://control.test", "never-record-this-token", transport=observed)
        gateway = _DiagnosticGateway(client, diagnostics)
        try:
            with pytest.raises((GatewayError, OperationalError)):
                await gateway.call("authorize", request={"canary": "private-canary"})
            assert any(event.get("category") == category for event in diagnostics.events)
            note = diagnostics.note()
            assert "private-canary" not in note
            assert "never-record-this-token" not in note
        finally:
            await client.close()
        assert inner.closed

    asyncio.run(scenario())


def test_invalid_response_and_non_success_status_record_status_without_body():
    async def scenario():
        diagnostics = _HandshakeDiagnostics()
        invalid = _ScriptedTransport(content=b"not-json-private-canary")
        observed = _DiagnosticASGITransport(invalid, diagnostics)
        client = AuthorityClient("http://control.test", "never-record-this-token", transport=observed)
        gateway = _DiagnosticGateway(client, diagnostics)
        try:
            with pytest.raises(GatewayError) as failure:
                await gateway.call("authorize", request={"canary": "private-canary"})
            assert failure.value.code == "authority_unavailable"
            assert any(event.get("cause_category") == "invalid_response" for event in diagnostics.events)
            assert any(event.get("status") == "2xx" for event in diagnostics.events)
            assert "private-canary" not in diagnostics.note()
        finally:
            await client.close()
        assert invalid.closed

        diagnostics = _HandshakeDiagnostics()
        rejected = _ScriptedTransport(status=503, content=b'{"detail":{"code":"private-canary"}}')
        observed = _DiagnosticASGITransport(rejected, diagnostics)
        client = AuthorityClient("http://control.test", "never-record-this-token", transport=observed)
        gateway = _DiagnosticGateway(client, diagnostics)
        try:
            with pytest.raises(GatewayError) as failure:
                await gateway.call("authorize", request={"canary": "private-canary"})
            assert failure.value.code == "authority_unavailable"
            assert any(event.get("status") == "5xx" for event in diagnostics.events)
            assert "private-canary" not in diagnostics.note()
        finally:
            await client.close()
        assert rejected.closed

    asyncio.run(scenario())


def test_transport_diagnostics_do_not_consume_response_body():
    async def scenario():
        diagnostics = _HandshakeDiagnostics()
        body = b"private-canary-response-body"
        inner = _ScriptedTransport(status=503, content=body)
        observed = _DiagnosticASGITransport(inner, diagnostics)
        try:
            response = await observed.handle_async_request(_private_request())
            assert response.status_code == 503
            assert await response.aread() == body
            assert "private-canary" not in diagnostics.note()
            assert any(event.get("status") == "5xx" for event in diagnostics.events)
        finally:
            await observed.aclose()
        assert inner.closed

    asyncio.run(scenario())


def test_gateway_error_observer_never_attempts_blocking_db_snapshot(authority_env, monkeypatch):
    async def scenario():
        database_reads = []

        def blocking_database_read(*_args, **_kwargs):
            database_reads.append(True)
            time.sleep(3.0)
            raise AssertionError("diagnostic observer attempted database access")

        async with connected(authority_env) as env:
            monkeypatch.setattr(env.platform.db, "sessions", blocking_database_read)
            original = GatewayError("authority_unavailable")

            async def fail_authority(_action, **_fields):
                await asyncio.sleep(0.005)
                raise original

            monkeypatch.setattr(env.gateway.authority, "call", fail_authority)
            heartbeat = {"ticks": 0}
            stopped = asyncio.Event()

            async def beat():
                while not stopped.is_set():
                    heartbeat["ticks"] += 1
                    await asyncio.sleep(0.001)

            heartbeat_task = asyncio.create_task(beat())
            started = time.monotonic()
            try:
                with pytest.raises(GatewayError) as failure:
                    await asyncio.wait_for(env.gateway.call("authorize"), timeout=1.0)
                elapsed = time.monotonic() - started
                assert failure.value is original
                assert elapsed < 1.0
                assert database_reads == []
                assert heartbeat["ticks"] > 0
                assert any(event.get("owner_state") == "not_observed"
                           for event in env.authority_diagnostics.events)
            finally:
                stopped.set()
                await asyncio.gather(heartbeat_task, return_exceptions=True)

        assert all(task.done() for task in env.engine.handlers)

    asyncio.run(scenario())


def test_outer_timeout_cancellation_is_not_classified_as_confirmed_timeout():
    class _NeverResponds(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            await asyncio.Event().wait()

        async def aclose(self):
            return None

    async def scenario():
        diagnostics = _HandshakeDiagnostics()
        observed = _DiagnosticASGITransport(_NeverResponds(), diagnostics)
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(observed.handle_async_request(_private_request()), timeout=0.01)
            assert diagnostics.events[-1]["category"] == "cancelled"
            assert diagnostics.events[-1]["outcome"] == "cancelled"
            assert not any(event.get("category") in {"timeout", "http_timeout"} for event in diagnostics.events)
            assert "private-canary" not in diagnostics.note()
        finally:
            await observed.aclose()

    asyncio.run(scenario())


def test_context_diagnostic_is_cycle_bounded_and_never_emits_exception_text():
    first = RuntimeError("private-canary-first")
    second = RuntimeError("private-canary-second")
    first.__context__ = second
    second.__context__ = first
    assert _safe_cause_category(first) == "unknown"
    assert _safe_exception_category(ValueError("private-canary")) == "invalid_response"
    diagnostics = _HandshakeDiagnostics()
    diagnostics.add(action="authorize", stage="authority", outcome="error", category="other",
                    cause_category=_safe_cause_category(first))
    assert diagnostics.events[-1]["cause_category"] == "unknown"
    assert "private-canary" not in diagnostics.note()


def test_early_helper_failure_attaches_only_sanitized_setup_note(authority_env):
    class _EarlyFailureHelper:
        instances = []

        def __init__(self, _settings):
            self.session = None
            self.task = None
            self.instances.append(self)

        async def run(self):
            self.task = asyncio.current_task()
            await asyncio.sleep(0)
            raise RuntimeError("private-canary-helper-error")

    async def scenario():
        with pytest.raises(RuntimeError) as failure:
            async with connected(authority_env, helper_type=_EarlyFailureHelper):
                raise AssertionError("early failing helper unexpectedly connected")
        notes = "\n".join(getattr(failure.value, "__notes__", ()))
        assert "stage:setup" in notes
        assert "outcome:unknown" in notes
        assert "private-canary-helper-error" not in notes
        assert _EarlyFailureHelper.instances[-1].task.done()

    asyncio.run(scenario())


def test_transport_observer_failure_preserves_original_error_and_cancellation(monkeypatch):
    async def scenario():
        original_error = httpx.ConnectError("private-canary-transport")
        inner = _ScriptedTransport(error=original_error)
        observed = _DiagnosticASGITransport(inner, _BrokenDiagnostics())
        with pytest.raises(httpx.ConnectError) as failure:
            await observed.handle_async_request(_private_request())
        assert failure.value is original_error
        await observed.aclose()
        assert inner.closed

        original_cancel = asyncio.CancelledError("private-canary-cancel")
        inner = _ScriptedTransport(error=original_cancel)
        observed = _DiagnosticASGITransport(inner, _BrokenDiagnostics())
        with pytest.raises(asyncio.CancelledError) as failure:
            await observed.handle_async_request(_private_request())
        assert failure.value is original_cancel
        await observed.aclose()
        assert inner.closed

        def fail_category(_error):
            raise RuntimeError("observer-private-canary-category")

        monkeypatch.setattr("model_support._safe_exception_category", fail_category)
        original_error = httpx.ConnectError("private-canary-classifier")
        inner = _ScriptedTransport(error=original_error)
        observed = _DiagnosticASGITransport(inner, _HandshakeDiagnostics())
        with pytest.raises(httpx.ConnectError) as failure:
            await observed.handle_async_request(_private_request())
        assert failure.value is original_error
        await observed.aclose()
        assert inner.closed

    asyncio.run(scenario())


def test_transport_observer_failure_preserves_success_response_and_close():
    async def scenario():
        request = _private_request()
        body = b"private-canary-response-body"
        response = httpx.Response(200, content=body, request=request)
        inner = _ReturningTransport(response)
        observed = _DiagnosticASGITransport(inner, _BrokenDiagnostics())
        try:
            result = await observed.handle_async_request(request)
            assert result is response
            assert await result.aread() == body
        finally:
            await observed.aclose()
        assert inner.closed

    asyncio.run(scenario())


def test_gateway_observer_failures_preserve_errors_cancellation_success_and_close(monkeypatch):
    async def scenario():
        class _CountingBrokenDiagnostics(_BrokenDiagnostics):
            def __init__(self):
                self.add_calls = 0

            def add(self, **fields):
                self.add_calls += 1
                return super().add(**fields)

        broken_diagnostics = _CountingBrokenDiagnostics()
        original = GatewayError("authority_unavailable")
        gateway = _DiagnosticGateway(_FixedAuthority(error=original), broken_diagnostics)
        with pytest.raises(GatewayError) as failure:
            await gateway.call("authorize")
        assert failure.value is original
        assert broken_diagnostics.add_calls == 1
        await gateway.close()
        assert gateway.authority.closed

        category_calls = []

        def fail_error_category(_error):
            category_calls.append(True)
            raise RuntimeError("private-canary-category")

        original = GatewayError("authority_unavailable")
        gateway = _DiagnosticGateway(_FixedAuthority(error=original), _HandshakeDiagnostics())
        with monkeypatch.context() as patch:
            patch.setattr("model_support._error_category", fail_error_category)
            with pytest.raises(GatewayError) as failure:
                await gateway.call("authorize")
        assert failure.value is original
        assert category_calls
        await gateway.close()
        assert gateway.authority.closed

        original_cancel = asyncio.CancelledError("private-canary-gateway-cancel")
        gateway = _DiagnosticGateway(_FixedAuthority(error=original_cancel), _BrokenDiagnostics())
        with pytest.raises(asyncio.CancelledError) as failure:
            await gateway.call("authorize")
        assert failure.value is original_cancel
        await gateway.close()
        assert gateway.authority.closed

        result = object()
        gateway = _DiagnosticGateway(_FixedAuthority(result=result), _BrokenDiagnostics())
        assert await gateway.call("authorize") is result
        await gateway.close()
        assert gateway.authority.closed

        class _CountingBrokenNoteDiagnostics(_BrokenNoteDiagnostics):
            def __init__(self):
                super().__init__()
                self.notes = 0

            def note(self):
                self.notes += 1
                return super().note()

        note_diagnostics = _CountingBrokenNoteDiagnostics()
        authority_error = GatewayError("authority_unavailable")
        gateway = _DiagnosticGateway(_FixedAuthority(error=authority_error), note_diagnostics)
        with pytest.raises(GatewayError) as failure:
            await gateway.call("authorize")
        assert failure.value is authority_error
        assert note_diagnostics.notes == 1
        await gateway.close()
        assert gateway.authority.closed

        cause_calls = []

        def fail_cause_category(_error):
            cause_calls.append(True)
            raise RuntimeError("private-canary-category")

        original = GatewayError("authority_unavailable")
        gateway = _DiagnosticGateway(_FixedAuthority(error=original), _HandshakeDiagnostics())
        with monkeypatch.context() as patch:
            patch.setattr("model_support._safe_cause_category", fail_cause_category)
            with pytest.raises(GatewayError) as failure:
                await gateway.call("authorize")
        assert failure.value is original
        assert cause_calls
        await gateway.close()
        assert gateway.authority.closed

    asyncio.run(scenario())


def test_broken_diagnostics_do_not_mask_connected_setup_failure(authority_env):
    class _EarlyFailureHelper:
        instances = []

        def __init__(self, _settings):
            self.session = None
            self.task = None
            self.error = RuntimeError("private-canary-setup")
            self.instances.append(self)

        async def run(self):
            self.task = asyncio.current_task()
            await asyncio.sleep(0)
            raise self.error

    async def scenario():
        with pytest.raises(RuntimeError) as failure:
            async with connected(authority_env, helper_type=_EarlyFailureHelper,
                                 diagnostics_factory=_BrokenDiagnostics):
                raise AssertionError("early failing helper unexpectedly connected")
        instance = _EarlyFailureHelper.instances[-1]
        assert failure.value is instance.error
        assert instance.task.done()
        notes = "\n".join(getattr(failure.value, "__notes__", ()))
        assert "setup_sidecar" in notes
        assert "stage:helper" in notes and "outcome:exception" in notes
        assert "private-canary-setup" not in notes

    asyncio.run(scenario())


def test_broken_diagnostics_do_not_mask_real_authority_success_or_cleanup(authority_env):
    async def scenario():
        async with connected(authority_env, diagnostics_factory=_BrokenDiagnostics) as env:
            events = env.setup_sidecar.events
            assert any(event["stage"] == "owner" and event["action"] == "hello"
                       and event["outcome"] == "entered" for event in events)
            assert any(event["stage"] == "owner" and event["action"] == "hello"
                       and event["outcome"] == "ok" for event in events), env.setup_sidecar.note()
            request = await infer(env)
            result = await consume(request)
            assert b"[DONE]" in result
            assert env.engine.calls

    asyncio.run(scenario())


def test_independent_sidecar_observes_owner_timeout_when_primary_diagnostics_break(authority_env):
    async def scenario():
        async with connected(authority_env, diagnostics_factory=_BrokenDiagnostics) as env:
            env.observed_authority_app.delay_action = "renew"
            env.observed_authority_app.delay_seconds = 2
            with pytest.raises(GatewayError, match="authority_unavailable"):
                await env.gateway.call("renew", request_id="safe-test-id", capability="safe-test-capability",
                                       workload="worker", permit="safe-test-permit")
            events = env.setup_sidecar.events
            assert any(event["stage"] == "owner_asgi" and event["action"] == "renew"
                       and event["outcome"] == "entered" for event in events)
            assert any(event["stage"] == "owner_asgi" and event["action"] == "renew"
                       and event["outcome"] == "cancelled" for event in events), env.setup_sidecar.note()
            assert not any(event["stage"] == "owner" and event["action"] == "renew"
                           for event in events), env.setup_sidecar.note()
            assert any(event["stage"] == "client" and event["action"] == "renew"
                       and event["outcome"] == "cancelled" for event in events)
            rendered = env.setup_sidecar.note()
            assert "safe-test" not in rendered
            assert "authority-test-credential" not in rendered

    asyncio.run(scenario())


def test_bounded_start_failure_includes_sanitized_authority_trace():
    from types import SimpleNamespace
    from test_model_gateway import _wait_for_bounded_engine_start

    async def scenario():
        diagnostics = _HandshakeDiagnostics()
        original = GatewayError('authority_unavailable')
        original.__cause__ = TimeoutError('private-canary')
        gateway = _DiagnosticGateway(_FixedAuthority(error=original), diagnostics)
        env = SimpleNamespace(
            engine=SimpleNamespace(started=asyncio.Event(), calls=[]),
            helper=SimpleNamespace(session=None, task=None),
            authority_diagnostics=diagnostics,
            platform=SimpleNamespace(db=SimpleNamespace(sessions=lambda: None)),
        )
        consumer = asyncio.create_task(gateway.call('authorize', secret='private-canary'))
        with pytest.raises(AssertionError, match='bounded pre-partition start barrier failed') as caught:
            await _wait_for_bounded_engine_start(env, SimpleNamespace(id='private-canary'), consumer, time.time() + 25, 1)
        note = caught.value.__notes__[0]
        assert 'action:authorize' in note
        assert 'category:authority_unavailable' in note
        assert 'cause_category:timeout' in note
        assert 'owner_state:not_observed' in note
        assert 'private-canary' not in note
        assert len(note) < 5000

    asyncio.run(scenario())


def test_bounded_start_success_does_not_read_diagnostics():
    from types import SimpleNamespace
    from test_model_gateway import _wait_for_bounded_engine_start

    async def scenario():
        started = asyncio.Event()
        started.set()
        env = SimpleNamespace(engine=SimpleNamespace(started=started), authority_diagnostics=_BrokenDiagnostics())
        consumer = asyncio.create_task(asyncio.Event().wait())
        try:
            await _wait_for_bounded_engine_start(env, None, consumer, time.time() + 25, 1)
            assert not consumer.done()
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

    asyncio.run(scenario())


def test_bounded_start_observer_failure_preserves_barrier_failure():
    from types import SimpleNamespace
    from test_model_gateway import _wait_for_bounded_engine_start

    async def scenario():
        env = SimpleNamespace(
            engine=SimpleNamespace(started=asyncio.Event(), calls=[]),
            helper=SimpleNamespace(session=None, task=None),
            authority_diagnostics=_BrokenDiagnostics(),
            platform=SimpleNamespace(db=SimpleNamespace(sessions=lambda: None)),
        )
        consumer = asyncio.create_task(asyncio.sleep(0))
        with pytest.raises(AssertionError, match='bounded pre-partition start barrier failed') as caught:
            await _wait_for_bounded_engine_start(env, SimpleNamespace(id='x'), consumer, time.time() + 25, 1)
        assert not getattr(caught.value, '__notes__', [])

    asyncio.run(scenario())


@pytest.mark.parametrize('failed_action', ['finish', 'renew'])
def test_inference_failure_reports_sanitized_authority_action(authority_env, failed_action):
    from test_model_gateway import _assert_inference_completed
    from sovite.gateway.server import create_inference_app
    from connector_support import BODY

    async def scenario():
        async with connected(authority_env, relaxed_open_setup=True) as env:
            transport = env.gateway.authority.client._transport.transport
            original = transport.handle_async_request

            async def fail_action(request):
                if request.url.path.endswith('/' + failed_action):
                    return httpx.Response(503, json={'detail': {'code': 'private-canary'}}, request=request)
                return await original(request)

            transport.handle_async_request = fail_action
            app = create_inference_app(env.gateway, {'worker': 'workload-test-' + 'w' * 24})
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://inference.test') as client:
                response = await client.post(f'/inference/{env.model["id"]}/chat', content=BODY,
                    headers={'Authorization': 'Bearer ' + 'workload-test-' + 'w' * 24,
                             'X-Sovite-Capability': env.job.execution_capability,
                             'X-Sovite-Job': env.job.id, 'X-Sovite-Fence': env.job.execution_fence,
                             'X-Sovite-Deadline': str(int(time.time()) + 20)})
            assert b'authority_unavailable' in response.content
            with pytest.raises(AssertionError) as raised:
                _assert_inference_completed(response, env.authority_diagnostics)
            note = ' '.join(raised.value.__notes__)
            assert f'action:{failed_action}' in note
            assert 'status:5xx' in note
            for secret in ('private-canary', env.job.execution_capability, env.settings.authority_token,
                           'workload-test-' + 'w' * 24):
                assert secret not in note
            if failed_action == 'finish':
                assert env.engine.calls and b'[DONE]' not in response.content

    asyncio.run(scenario())


def test_inference_failure_diagnostic_error_does_not_mask_assertion():
    from test_model_gateway import _assert_inference_completed
    response = httpx.Response(200, content=b'failure')
    with pytest.raises(AssertionError):
        _assert_inference_completed(response, _BrokenNoteDiagnostics())


def test_denial_failure_notes_are_bounded_and_redacted():
    from types import SimpleNamespace
    from test_model_gateway import _note_denial_failure

    diagnostics = _HandshakeDiagnostics()
    for _ in range(30):
        diagnostics.add(action='authorize', stage='authority', outcome='error',
                        category='authority_unavailable', cause_category='timeout',
                        owner_state='not_observed', secret='private-canary')
    env = SimpleNamespace(authority_diagnostics=diagnostics,
                          engine=SimpleNamespace(calls=['private-canary']),
                          helper=SimpleNamespace(reservation='private-canary'))
    original = AssertionError('original failure')
    _note_denial_failure(original, env)
    notes = ' '.join(original.__notes__)
    assert 'action:authorize' in notes and 'cause_category:timeout' in notes
    assert 'engine_calls_present=True' in notes and 'reservation_absent=False' in notes
    assert 'private-canary' not in notes and len(notes) < 5000


def test_denial_failure_broken_observers_preserve_original_assertion():
    from types import SimpleNamespace
    from test_model_gateway import _note_denial_failure

    class BrokenEngine:
        @property
        def calls(self):
            raise RuntimeError('private-canary')

    env = SimpleNamespace(authority_diagnostics=_BrokenDiagnostics(),
                          engine=BrokenEngine(), helper=SimpleNamespace(reservation=None))
    original = AssertionError('original failure')
    try:
        raise original
    except AssertionError as error:
        _note_denial_failure(error, env)
        assert error is original
    assert str(original) == 'original failure'
    assert not getattr(original, '__notes__', [])


def test_denial_assertion_failure_keeps_assertion_and_observations(authority_env, monkeypatch):
    import test_model_gateway as target

    async def unavailable(env, **changes):
        env.authority_diagnostics.add(action='authorize', stage='authority', outcome='error',
                                      category='authority_unavailable', cause_category='timeout',
                                      owner_state='not_observed', secret='private-canary')
        raise GatewayError('authority_unavailable')

    monkeypatch.setattr(target, 'infer', unavailable)
    with pytest.raises(AssertionError) as caught:
        target.test_PIN_BEFORE_PROMPT_CURRENT_AUTHORITY_seven_checks_deny_zero_engine_bytes(
            authority_env, 'cancel', 'cancelled')
    notes = ' '.join(caught.value.__notes__)
    assert 'action:authorize' in notes and 'cause_category:timeout' in notes
    assert 'engine_calls_present=False' in notes and 'reservation_absent=True' in notes
    assert 'private-canary' not in notes

@pytest.mark.parametrize("mode", ["timeout", "transport", "malformed", "status"])
def test_connected_start_failure_has_bounded_sanitized_diagnostics(authority_env, monkeypatch, mode):
    class Injected(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            assert request.url.path.endswith('/start')
            if mode == 'timeout':
                await asyncio.Event().wait()
            if mode == 'transport':
                raise httpx.ConnectError('private-canary', request=request)
            if mode == 'malformed':
                return httpx.Response(200, content=b'private-canary', request=request)
            return httpx.Response(503, content=b'{"detail":{"code":"private-canary"}}', request=request)

    original = model_support._DiagnosticASGITransport
    monkeypatch.setattr(model_support, '_DiagnosticASGITransport',
                        lambda transport, diagnostics: original(Injected(), diagnostics))

    async def scenario():
        started = time.monotonic()
        with pytest.raises(GatewayError) as failure:
            async with connected(authority_env):
                pytest.fail('start must fail')
        elapsed = time.monotonic() - started
        notes = '\n'.join(getattr(failure.value, '__notes__', ()))
        assert failure.value.code == 'authority_unavailable'
        assert 'action:start' in notes and 'owner_state:not_observed' in notes
        assert 'duration_ms:' in notes
        assert 'private-canary' not in notes and 'authority-test-credential' not in notes
        if mode == 'timeout':
            assert AUTHORITY_TIMEOUT == 1 and 0.9 <= elapsed < 5
            assert 'cause_category:timeout' in notes and 'category:cancelled' in notes
        elif mode == 'transport':
            assert 'category:transport_error' in notes and 'cause_category:transport_error' in notes
        elif mode == 'malformed':
            assert 'status:2xx' in notes and 'cause_category:invalid_response' in notes
        else:
            assert 'status:5xx' in notes

    asyncio.run(scenario())


def test_connected_start_broken_observer_preserves_failure(authority_env, monkeypatch):
    class Injected(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError('private-canary', request=request)

    original = model_support._DiagnosticASGITransport
    monkeypatch.setattr(model_support, '_DiagnosticASGITransport',
                        lambda transport, diagnostics: original(Injected(), diagnostics))

    async def scenario():
        with pytest.raises(GatewayError) as failure:
            async with connected(authority_env, diagnostics_factory=_BrokenDiagnostics):
                pytest.fail('start must fail')
        assert failure.value.code == 'authority_unavailable'

    asyncio.run(scenario())


@pytest.mark.parametrize("failure_stage", ["transport_constructor", "websocket_server_entry"])
def test_connected_restores_owner_routes_after_setup_failure(authority_env, monkeypatch, failure_stage):
    owner_routes = [route.dependant for route in authority_env.app.routes
                    if getattr(route, "path", "").startswith("/internal/authority/")
                    and getattr(route, "dependant", None) is not None]
    original_calls = [route.call for route in owner_routes]

    if failure_stage == "transport_constructor":
        def fail_transport(*_args, **_kwargs):
            raise RuntimeError("private-canary-constructor")

        monkeypatch.setattr(model_support, "_DiagnosticASGITransport", fail_transport)
    else:
        class FailedServe:
            async def __aenter__(self):
                raise RuntimeError("private-canary-server-entry")

            async def __aexit__(self, *_args):
                return False

        monkeypatch.setattr(model_support, "serve", lambda *_args, **_kwargs: FailedServe())

    async def scenario():
        with pytest.raises(RuntimeError, match="private-canary"):
            async with connected(authority_env):
                pytest.fail("setup failure was not raised")
        assert [route.call for route in owner_routes] == original_calls

    asyncio.run(scenario())


def test_connected_start_cleanup_failures_preserve_original_and_attempt_all(authority_env, monkeypatch):
    owner_routes = [route.dependant for route in authority_env.app.routes
                    if getattr(route, "path", "").startswith("/internal/authority/")
                    and getattr(route, "dependant", None) is not None]
    original_calls = [route.call for route in owner_routes]
    calls = []
    class Injected(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError('private-canary', request=request)

    class Server:
        def close(self):
            calls.append('server_close')
            raise RuntimeError('private-canary-close')

        async def wait_closed(self):
            calls.append('server_wait_closed')
            raise asyncio.CancelledError()

    async def start_server(*_args, **_kwargs):
        return Server()

    async def fail_gateway_close(_self):
        calls.append('gateway_close')
        raise asyncio.CancelledError()

    original = model_support._DiagnosticASGITransport
    monkeypatch.setattr(model_support, '_DiagnosticASGITransport',
                        lambda transport, diagnostics: original(Injected(), diagnostics))
    monkeypatch.setattr(model_support.asyncio, 'start_server', start_server)
    monkeypatch.setattr(model_support._DiagnosticGateway, 'close', fail_gateway_close)

    async def scenario():
        with pytest.raises(GatewayError) as failure:
            async with connected(authority_env):
                pytest.fail('start must fail')
        assert failure.value.code == 'authority_unavailable'
        notes = '\n'.join(getattr(failure.value, '__notes__', ()))
        assert 'action:start' in notes and 'cause_category:transport_error' in notes
        assert 'private-canary' not in notes
        assert calls == ['gateway_close', 'server_close', 'server_wait_closed']
        assert [route.call for route in owner_routes] == original_calls

    asyncio.run(scenario())


@pytest.mark.parametrize("failure_kind", ["error", "cancellation"])
def test_connected_success_propagates_first_close_failure_after_all_cleanup(authority_env, monkeypatch, failure_kind):
    owner_routes = [route.dependant for route in authority_env.app.routes
                    if getattr(route, "path", "").startswith("/internal/authority/")
                    and getattr(route, "dependant", None) is not None]
    original_calls = [route.call for route in owner_routes]
    real_start_server = model_support.asyncio.start_server
    servers = []
    calls = []
    close_error = (asyncio.CancelledError("private-canary-close") if failure_kind == "cancellation"
                   else RuntimeError("private-canary-close"))

    async def capture_server(*args, **kwargs):
        server = await real_start_server(*args, **kwargs)
        servers.append(server)
        return server

    async def fail_gateway_close(_self):
        calls.append("gateway_close")
        raise close_error

    monkeypatch.setattr(model_support.asyncio, "start_server", capture_server)
    monkeypatch.setattr(model_support._DiagnosticGateway, "close", fail_gateway_close)

    async def scenario():
        with pytest.raises(type(close_error)) as failure:
            async with connected(authority_env) as env:
                assert any(event["stage"] == "owner" and event["action"] == "hello"
                           and event["outcome"] == "ok" for event in env.setup_sidecar.events)
        assert failure.value is close_error
        assert calls == ["gateway_close"]
        assert servers and not servers[0].is_serving()
        assert [route.call for route in owner_routes] == original_calls

    asyncio.run(scenario())


def test_connected_start_cancellation_is_recorded_and_propagated(authority_env, monkeypatch):
    original = asyncio.CancelledError('private-canary')
    async def cancel_start(_self):
        raise original

    monkeypatch.setattr(model_support._DiagnosticGateway, 'start', cancel_start)

    async def scenario():
        with pytest.raises(asyncio.CancelledError) as failure:
            async with connected(authority_env):
                pytest.fail('start must cancel')
        assert failure.value is original
        notes = '\n'.join(getattr(original, '__notes__', ()))
        assert 'outcome:cancelled,stage:setup' in notes
        assert 'private-canary' not in notes

    asyncio.run(scenario())
