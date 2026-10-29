import pytest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import installation
import local_platform


def test_compose_up_migrates_before_runtime_services(monkeypatch):
    calls = []
    monkeypatch.setattr(installation, "writer_state", lambda: ([], []))
    monkeypatch.setattr(installation, "compose", lambda *args, **kwargs: calls.append(args))
    installation.up(None)
    assert calls == [
        ("up", "-d", "--wait", "postgres", "garage"),
        ("run", "--rm", "--no-deps", "control", "db", "migrate"),
        ("up", "-d", "--wait"),
    ]


def test_compose_up_refuses_migration_while_writers_are_running(monkeypatch):
    calls = []
    monkeypatch.setattr(installation, "writer_state", lambda: (["control", "worker"], ["worker"]))
    monkeypatch.setattr(installation, "compose", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(RuntimeError, match="Stop existing writers.*worker"):
        installation.up(None)
    assert calls == []


def test_local_init_refuses_migration_while_managed_writer_is_live(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(local_platform, "LOCAL", tmp_path)
    monkeypatch.setattr(local_platform, "read_processes",
        lambda: {"worker": {"pid": 123, "identity": "live-worker"}})
    monkeypatch.setattr(local_platform, "process_identity", lambda _pid: "live-worker")
    monkeypatch.setattr(local_platform, "command", lambda _info, *args: calls.append(args))
    info = {"environment": {
        "SOVITE_QUALIFICATION_PRIVATE_KEY": str(tmp_path / "private.pem"),
        "SOVITE_QUALIFICATION_PUBLIC_KEYS": '["' + str(tmp_path / "public.pem") + '"]'}}
    with pytest.raises(RuntimeError, match="Stop local platform writers.*worker"):
        local_platform.finish_init(info)
    assert calls == []


def test_local_init_runs_offline_migration_before_platform_init(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(local_platform, "LOCAL", tmp_path)
    monkeypatch.setattr(local_platform, "read_processes", lambda: {})
    monkeypatch.setattr(local_platform, "command", lambda _info, *args: calls.append(args))
    private, public = tmp_path / "private.pem", tmp_path / "public.pem"
    private.write_text("private")
    public.write_text("public")
    info = {"environment": {
        "SOVITE_QUALIFICATION_PRIVATE_KEY": str(private),
        "SOVITE_QUALIFICATION_PUBLIC_KEYS": '["' + str(public) + '"]'}}
    local_platform.finish_init(info)
    assert calls[:2] == [("db", "migrate"), ("init",)]
    assert info["initialization_status"] == "complete"
