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


def test_concurrent_fixtures_serialize_and_restore_after_out_of_order_exit(authority_env, tmp_path, v2_bundle):
    from sovite.db import Database
    from model_support import authority_env as fixture_factory
    second_path = tmp_path / "second-fixture"
    second_path.mkdir()
    second_db = Database(f"sqlite:///{second_path}/second.db")
    second_env = fixture_factory.__wrapped__(second_db, second_path, v2_bundle)
    original_serve = HelperConnection.serve
    async def scenario():
        first_entered, first_release = asyncio.Event(), asyncio.Event()
        second_started, second_entered, second_release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def first():
            async with connected(authority_env):
                first_entered.set()
                await first_release.wait()
        async def second():
            second_started.set()
            async with connected(second_env):
                second_entered.set()
                await second_release.wait()
        one = asyncio.create_task(first())
        two = None
        try:
            await asyncio.wait_for(first_entered.wait(), 5)
            two = asyncio.create_task(second())
            await asyncio.wait_for(second_started.wait(), 5)
            # The second scope requests exit first, but cannot enter until the
            # first patch has been restored.
            second_release.set()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(second_entered.wait(), 1)
            first_release.set()
            await asyncio.wait_for(asyncio.gather(one, two), 5)
            assert second_entered.is_set()
            assert HelperConnection.serve is original_serve
        finally:
            first_release.set()
            second_release.set()
            tasks = [task for task in (one, two) if task is not None]
            await asyncio.gather(*tasks, return_exceptions=True)
    try:
        asyncio.run(scenario())
    finally:
        second_db.engine.dispose()


def test_waiting_lock_is_cancellable_nonblocking_and_reusable_across_loops():
    from model_support import _handshake_fixture_lock
    async def scenario():
        ticked = asyncio.Event()
        async with _handshake_fixture_lock():
            async def wait_for_lock():
                async with _handshake_fixture_lock():
                    pytest.fail("cancelled waiter must never enter")
            waiter = asyncio.create_task(wait_for_lock())
            async def tick():
                await asyncio.sleep(.05)
                ticked.set()
            ticker = asyncio.create_task(tick())
            await asyncio.wait_for(ticked.wait(), 1)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            await ticker
        async with _handshake_fixture_lock():
            pass
    asyncio.run(scenario())
    asyncio.run(scenario())


def test_same_task_nested_fixture_lock_fails_explicitly():
    from model_support import _handshake_fixture_lock
    async def nested():
        async with _handshake_fixture_lock():
            async with _handshake_fixture_lock():
                pytest.fail("nested fixture must not enter")
    async def scenario():
        with pytest.raises(RuntimeError, match="handshake fixture is not reentrant"):
            await asyncio.wait_for(nested(), .5)
    asyncio.run(scenario())
