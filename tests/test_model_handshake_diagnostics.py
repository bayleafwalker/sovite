"""Test-only setup diagnostics preserve failures and redact private inputs."""
import asyncio

import pytest
from websockets.exceptions import ConnectionClosedOK

from model_support import authority_env, connected, database, v2_bundle
from sovite.gateway.client import GatewayError
from sovite.gateway.runtime import Gateway, HelperConnection


SENTINEL = "private-diagnostic-sentinel-do-not-print"


def note_for(error):
    notes = getattr(error, "__notes__", [])
    matching = [note for note in notes if note.startswith("handshake_diagnostics dropped=")]
    assert len(matching) == 1
    assert SENTINEL not in matching[0]
    header, encoded = matching[0].split(" events=", 1)
    events = []
    for event in filter(None, encoded.split("|")):
        fields = dict(field.split(":", 1) for field in event.split(","))
        fields["elapsed_ms"] = int(fields["elapsed_ms"])
        events.append(fields)
    return {"events": events, "dropped": int(header.rsplit("=", 1)[1])}


@pytest.mark.parametrize("action", ["open", "hello"])
def test_authority_failure_keeps_original_close_and_records_safe_stage(authority_env, monkeypatch, action):
    original = Gateway.call
    async def reject(self, selected, **fields):
        if selected == action:
            error = GatewayError("authority_unavailable")
            error.secret_test_value = SENTINEL
            raise error
        return await original(self, selected, **fields)
    monkeypatch.setattr(Gateway, "call", reject)
    async def scenario():
        entered = False
        with pytest.raises(ConnectionClosedOK) as caught:
            async with connected(authority_env):
                entered = True
        assert not entered
        diagnostic = note_for(caught.value)
        assert any(event.get("action") == action and event.get("outcome") == "error"
                   and event.get("category") == "authority_unavailable" for event in diagnostic["events"])
        connect = next(event for event in diagnostic["events"] if event.get("stage") == "connect")
        assert all(connect[name] == "true" for name in
                   ("tls_present", "tls13", "subprotocol_ok", "no_extensions", "path_ok"))
        assert authority_env.settings.authority_token not in str(getattr(caught.value, "__notes__", []))
    asyncio.run(scenario())


def test_serve_error_preserves_close_and_redacts_exception_message(authority_env, monkeypatch):
    async def reject(self):
        raise RuntimeError(SENTINEL)
    monkeypatch.setattr(HelperConnection, "serve", reject)
    async def scenario():
        with pytest.raises(ConnectionClosedOK) as caught:
            async with connected(authority_env):
                pytest.fail("body must not execute")
        diagnostic = note_for(caught.value)
        assert any(event.get("action") == "open" and event.get("outcome") == "ok"
                   for event in diagnostic["events"])
        assert any(event.get("stage") == "setup" and event.get("outcome") == "unknown"
                   for event in diagnostic["events"])
        assert not any(event.get("stage") == "serve" for event in diagnostic["events"])
    asyncio.run(scenario())


def test_timeout_keeps_original_exception_and_adds_bounded_note(authority_env):
    class NeverReady:
        def __init__(self, settings):
            pass
        async def run(self):
            await asyncio.Event().wait()
    async def scenario():
        with pytest.raises(TimeoutError) as caught:
            async with connected(authority_env, helper_type=NeverReady):
                pytest.fail("body must not execute")
        assert len(note_for(caught.value)["events"]) <= 16
    asyncio.run(scenario())


def test_diagnostics_are_bounded_and_never_echo_unknown_error_codes():
    from model_support import _HandshakeDiagnostics, _error_category
    diagnostics = _HandshakeDiagnostics()
    for _ in range(25):
        diagnostics.add(stage="serve", outcome="enter")
    diagnostics.add(secret=SENTINEL, action=SENTINEL, tls_present=SENTINEL)
    encoded = diagnostics.note()
    class Noted:
        __notes__ = [encoded]
    payload = note_for(Noted())
    assert len(payload["events"]) == 16 and payload["dropped"] == 10
    assert all(0 <= item["elapsed_ms"] <= 60000 for item in payload["events"])
    assert _error_category(GatewayError(SENTINEL)) == "gateway_error"
    assert _error_category(GatewayError([SENTINEL])) == "gateway_error"
    assert SENTINEL not in encoded


def test_setup_reraises_same_exception_and_restores_wrapper(authority_env):
    original_serve = HelperConnection.serve
    original_error = RuntimeError(SENTINEL)
    class FailingHelper:
        def __init__(self, settings):
            pass
        async def run(self):
            raise original_error
    async def scenario():
        with pytest.raises(RuntimeError) as caught:
            async with connected(authority_env, helper_type=FailingHelper):
                pytest.fail("body must not execute")
        assert caught.value is original_error
        note_for(caught.value)  # also verifies that the sentinel is absent
        assert HelperConnection.serve is original_serve
    asyncio.run(scenario())


def test_successful_setup_is_silent_and_restores_wrapper(authority_env, capsys):
    original_serve = HelperConnection.serve
    async def scenario():
        async with connected(authority_env) as env:
            assert env.model['id'] in env.gateway.sessions
        assert HelperConnection.serve is original_serve
    asyncio.run(scenario())
    captured = capsys.readouterr()
    assert "handshake_diagnostics dropped=" not in captured.out + captured.err


def test_nested_out_of_order_fixtures_work_across_event_loops(tmp_path, v2_bundle):
    from model_support import authority_env as fixture_factory
    from sovite.db import Database
    original_serve = HelperConnection.serve
    async def scenario(first, second):
        outer, inner = connected(first), connected(second)
        outer_open = inner_open = False
        try:
            await outer.__aenter__()
            outer_open = True
            await inner.__aenter__()
            inner_open = True
            assert HelperConnection.serve is original_serve
            await outer.__aexit__(None, None, None)
            outer_open = False
            assert second.model['id'] in second.gateway.sessions
            await inner.__aexit__(None, None, None)
            inner_open = False
            assert HelperConnection.serve is original_serve
        finally:
            if inner_open:
                await inner.__aexit__(None, None, None)
            if outer_open:
                await outer.__aexit__(None, None, None)
    for index in range(2):
        envs, databases = [], []
        try:
            for side in range(2):
                directory = tmp_path / f"fixture-{index}-{side}"
                directory.mkdir()
                db = Database(f"sqlite:///{directory}/test.db")
                databases.append(db)
                envs.append(fixture_factory.__wrapped__(db, directory, v2_bundle))
            asyncio.run(scenario(*envs))
        finally:
            for db in databases:
                db.engine.dispose()
    assert HelperConnection.serve is original_serve


def test_explicit_setup_budget_is_scoped_to_initial_open(authority_env, monkeypatch):
    import model_support
    original = model_support._DiagnosticASGITransport.handle_async_request
    calls = []
    async def slow_open(self, request):
        if request.url.path.endswith('/open'):
            calls.append('open')
            await asyncio.sleep(1.2)
        return await original(self, request)
    monkeypatch.setattr(model_support._DiagnosticASGITransport, 'handle_async_request', slow_open)
    async def scenario():
        async with connected(authority_env, relaxed_open_setup=True) as env:
            assert calls == ['open']
            assert env.gateway.authority.preparing is False
            with pytest.raises(GatewayError, match='authority_unavailable'):
                await env.gateway.authority.call('open', gateway_id=env.gateway.id,
                                                 certificate_digest='0' * 64)
            assert calls == ['open', 'open']
    asyncio.run(scenario())


@pytest.mark.parametrize('action', ['open', 'hello'])
def test_production_authority_setup_actions_keep_one_second_timeout(monkeypatch, action):
    import httpx
    from sovite.gateway.client import AuthorityClient
    observed, cancelled = [], []
    wait_for = asyncio.wait_for
    def observe_wait(awaitable, timeout):
        observed.append(timeout)
        return wait_for(awaitable, timeout)
    monkeypatch.setattr(asyncio, 'wait_for', observe_wait)
    async def slow(_request):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)
    async def scenario():
        client = AuthorityClient('https://control.test', 'x' * 24, transport=httpx.MockTransport(slow))
        try:
            with pytest.raises(GatewayError, match='authority_unavailable'):
                await client.call(action)
            assert observed == [1] and cancelled == [True]
        finally:
            await client.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('action', ['open', 'hello'])
def test_test_only_initial_setup_budget_remains_bounded(monkeypatch, action):
    import httpx
    from model_support import _SetupOpenAuthorityClient
    observed, cancelled = [], []
    wait_for = asyncio.wait_for
    def observe_wait(awaitable, timeout):
        observed.append(timeout)
        return wait_for(awaitable, timeout)
    monkeypatch.setattr(asyncio, 'wait_for', observe_wait)
    async def slow(_request):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)
    async def scenario():
        client = _SetupOpenAuthorityClient('https://control.test', 'x' * 24,
                                           transport=httpx.MockTransport(slow), setup_actions=frozenset({action}))
        try:
            with pytest.raises(GatewayError, match='authority_unavailable'):
                await client.call(action)
            assert observed == [4] and cancelled == [True]
        finally:
            await client.close()
    asyncio.run(scenario())


def test_explicit_hello_budget_is_scoped_to_initial_setup(authority_env, monkeypatch):
    import model_support
    original = model_support._DiagnosticASGITransport.handle_async_request
    calls = []
    async def slow_hello(self, request):
        if request.url.path.endswith('/hello'):
            calls.append('hello')
            await asyncio.sleep(1.2)
        return await original(self, request)
    monkeypatch.setattr(model_support._DiagnosticASGITransport, 'handle_async_request', slow_hello)
    async def scenario():
        async with connected(authority_env, relaxed_hello_setup=True) as env:
            assert calls == ['hello'] and env.gateway.authority.preparing is False
            with pytest.raises(GatewayError, match='authority_unavailable'):
                await env.gateway.authority.call('hello', gateway_id=env.gateway.id)
            assert calls == ['hello', 'hello']
    asyncio.run(scenario())
