import asyncio
import json
import sys

import pytest

from sovite import resource_gateway_cli as cli
from sovite.resource_gateway_store import ResourceOperationStore


def test_poll_failure_reports_fixed_counts_without_private_content(capsys):
    secret = 'PRIVATE-CANARY-token-target-url-principal'
    class Gateway:
        async def poll_once(self):
            raise RuntimeError(secret)
    async def stop(seconds):
        assert seconds == 5
        raise StopAsyncIteration
    with pytest.raises(StopAsyncIteration):
        asyncio.run(cli.poll_loop(Gateway(), sleep=stop, clock=lambda: 1))
    output = capsys.readouterr().err
    assert secret not in output
    assert json.loads(output) == {'component': 'resource_gateway', 'code': 'poll_degraded',
                                  'poll_errors': 1, 'unavailable': 0, 'unknown': 0, 'refused': 0}


def test_unknown_and_refusal_are_visible_without_result_content(capsys):
    class Gateway:
        async def poll_once(self):
            return [{'state': 'unknown', 'secret': 'PRIVATE-CANARY'}, {'state': 'unavailable'}, {'accepted': False}]
    async def stop(_): raise StopAsyncIteration
    with pytest.raises(StopAsyncIteration):
        asyncio.run(cli.poll_loop(Gateway(), sleep=stop, clock=lambda: 1))
    output = capsys.readouterr().err
    assert 'PRIVATE-CANARY' not in output
    assert json.loads(output)['unknown'] == 1
    assert json.loads(output)['unavailable'] == 1
    assert json.loads(output)['refused'] == 1


def test_unconfigured_policy_fails_with_actionable_fixed_code(tmp_path, monkeypatch, capsys):
    store = ResourceOperationStore(tmp_path / 'db')
    store.install()
    monkeypatch.setattr(sys, 'argv', ['gateway', '--database', str(tmp_path / 'db'), 'poll'])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().err)['code'] == 'resource_policy_missing'
