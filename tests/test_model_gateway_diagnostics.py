import asyncio
from pathlib import Path

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


def test_authority_failure_note_correlates_safe_live_owner_state(authority_env):
    async def scenario():
        async with connected(authority_env) as env:
            async def unavailable(_action, **_fields):
                raise GatewayError("authority_unavailable") from TimeoutError("private-canary-timeout")

            env.gateway.authority.call = unavailable
            with pytest.raises(GatewayError) as failure:
                await env.gateway.call("authorize", request={"canary": "private-canary"})
            notes = "\n".join(getattr(failure.value, "__notes__", ()))
            assert "cause_category:timeout" in notes
            assert "job_status:running" in notes
            assert "lease_phase:started" in notes
            assert "lease_fence_matches_claim:true" in notes
            assert "lease_live:true" in notes
            assert "deadline_live:true" in notes
            assert "helper_phase:idle" in notes
            assert "helper_active:false" in notes
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
        snapshot_calls = []

        class _CountingBrokenDiagnostics(_BrokenDiagnostics):
            def __init__(self):
                self.add_calls = 0

            def add(self, **fields):
                self.add_calls += 1
                return super().add(**fields)

        def fail_snapshot():
            snapshot_calls.append(True)
            raise RuntimeError("snapshot-canary")

        broken_diagnostics = _CountingBrokenDiagnostics()
        original = GatewayError("authority_unavailable")
        gateway = _DiagnosticGateway(_FixedAuthority(error=original), broken_diagnostics,
                                     state_snapshot=fail_snapshot)
        with pytest.raises(GatewayError) as failure:
            await gateway.call("authorize")
        assert failure.value is original
        assert broken_diagnostics.add_calls == 1
        assert snapshot_calls
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
        gateway = _DiagnosticGateway(_FixedAuthority(error=authority_error), note_diagnostics,
                                     state_snapshot=fail_snapshot)
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

    asyncio.run(scenario())


def test_broken_diagnostics_do_not_mask_real_authority_success_or_cleanup(authority_env):
    async def scenario():
        async with connected(authority_env, diagnostics_factory=_BrokenDiagnostics) as env:
            request = await infer(env)
            result = await consume(request)
            assert b"[DONE]" in result
            assert env.engine.calls

    asyncio.run(scenario())
