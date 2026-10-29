from sovite.remote_worker_cli import _CONTROL_OR_STORE_ENV, main

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


def test_remote_worker_cli_refuses_ambient_database_and_artifact_credentials(monkeypatch, capsys):
    for name in _CONTROL_OR_STORE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SOVITE_DATABASE_URL", "postgresql+psycopg://secret-value")
    assert main([]) == 2
    output = capsys.readouterr().err
    assert "SOVITE_DATABASE_URL" in output
    assert "secret-value" not in output

    monkeypatch.delenv("SOVITE_DATABASE_URL")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "storage-secret-value")
    assert main([]) == 2
    output = capsys.readouterr().err
    assert "AWS_SECRET_ACCESS_KEY" in output
    assert "storage-secret-value" not in output


def test_remote_worker_cli_refuses_control_execution_token_map(monkeypatch, capsys):
    for name in _CONTROL_OR_STORE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SOVITE_EXECUTION_TOKENS", '{"workload":"server-token"}')
    assert main([]) == 2
    output = capsys.readouterr().err
    assert "SOVITE_EXECUTION_TOKENS" in output
    assert "server-token" not in output


@pytest.mark.parametrize("heartbeat", [None, "relative-heartbeat"])
def test_missing_or_relative_heartbeat_is_a_configuration_error_before_journal_io(tmp_path, heartbeat):
    journal = tmp_path / "execution.json"
    env = {key: value for key, value in os.environ.items() if not key.startswith(("SOVITE_", "AWS_"))}
    env.update(SOVITE_EXECUTION_INSTANCE_ID="startup-test", SOVITE_EXECUTION_JOURNAL_PATH=str(journal),
               SOVITE_CONTROL_URL="https://127.0.0.1:9", SOVITE_EXECUTION_TOKEN="synthetic-startup-execution-token")
    if heartbeat is not None:
        env["SOVITE_REMOTE_WORKER_HEALTH_PATH"] = heartbeat
    result = subprocess.run(
        [sys.executable, "-m", "sovite.remote_worker_cli"], env=env,
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 2
    assert "SOVITE_REMOTE_WORKER_HEALTH_PATH must be an absolute path" in result.stderr
    assert not result.stdout
    assert not list(tmp_path.iterdir()), "configuration refusal must precede journal creation"


def test_offline_journal_inspection_does_not_require_runtime_heartbeat(monkeypatch, tmp_path, capsys):
    from sovite.remote_journal import ExecutionJournal

    journal = ExecutionJournal(tmp_path / "execution.json")
    journal.close()
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    monkeypatch.delenv("SOVITE_REMOTE_WORKER_HEALTH_PATH", raising=False)
    monkeypatch.setenv("SOVITE_EXECUTION_JOURNAL_PATH", str(tmp_path / "execution.json"))
    assert main(["--inspect-journal"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "empty"
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}
