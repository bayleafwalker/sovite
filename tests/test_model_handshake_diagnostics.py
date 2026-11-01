"""Test-only setup diagnostics preserve failures and redact private inputs."""
import asyncio
import json

import pytest
from websockets.exceptions import ConnectionClosedOK

from model_support import authority_env, connected, database, v2_bundle
from sovite.gateway.client import GatewayError
from sovite.gateway.runtime import Gateway, HelperConnection


SENTINEL = "private-diagnostic-sentinel-do-not-print"


def note_for(error):
    notes = getattr(error, "__notes__", [])
    matching = [note for note in notes if note.startswith("handshake_diagnostics=")]
    assert len(matching) == 1
    assert SENTINEL not in matching[0]
    return json.loads(matching[0].split("=", 1)[1])


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
        assert any(event.get("stage") == "serve" and event.get("outcome") == "error"
                   and event.get("category") == "other" for event in diagnostic["events"])
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
    payload = json.loads(encoded.split("=", 1)[1])
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
        assert SENTINEL not in json.dumps(note_for(caught.value))
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
    assert "handshake_diagnostics=" not in captured.out + captured.err
