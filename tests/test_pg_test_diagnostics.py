import json
import os
from pathlib import Path
import sys

import pytest

from scripts.ci import pg_test_diagnostics as diagnostics


def test_safe_test_identity_excludes_parameter_text():
    test_name, identity = diagnostics.safe_test_identity(
        'tests/test_work_projection_rows.py::test_case[postgresql-{"token":"synthetic-only"}]'
    )

    assert test_name == "test_case"
    assert "synthetic-only" not in test_name
    assert len(identity) == 64


def test_progress_journal_is_capped_and_marks_truncation(tmp_path):
    path = tmp_path / "progress.jsonl"
    journal = diagnostics.ProgressJournal(path, max_bytes=512)
    try:
        for sequence in range(20):
            journal.write({"event": "test", "sequence": sequence, "value": "x" * 50})
    finally:
        journal.close()

    raw = path.read_bytes()
    records = [json.loads(line) for line in raw.splitlines()]
    assert len(raw) <= 512
    assert records[-1]["event"] == "truncated"
    assert records[-1]["dropped_events"] > 0


def test_bounded_ring_keeps_only_recent_stack_bytes():
    ring = diagnostics.BoundedRing(limit=16)
    ring.append(b"1234567890")
    ring.append(b"abcdefghij")

    assert ring.bytes() == b"[truncated]\nghij"
    assert ring.truncated is True


def _set_owned_ci_url(monkeypatch):
    for key in list(os.environ):
        if key.startswith("PG"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SOVITE_TEST_POSTGRES_CI", "1")
    monkeypatch.setenv("SOVITE_WORK_PROJECTION_REQUIRE_PG", "1")
    monkeypatch.setenv(
        "SOVITE_TEST_POSTGRES_URL",
        "postgresql+psycopg://sovite_resource_test:fixture-only@127.0.0.1:5432/sovite_resource_test",
    )


def test_snapshot_connection_is_limited_to_owned_ci_service(monkeypatch):
    _set_owned_ci_url(monkeypatch)
    kwargs = diagnostics._snapshot_connection_kwargs()

    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 5432
    assert kwargs["dbname"] == "sovite_resource_test"
    assert kwargs["user"] == "sovite_resource_test"
    assert kwargs["connect_timeout"] == 3
    assert kwargs["options"] == "-c statement_timeout=1000"

    monkeypatch.setenv(
        "SOVITE_TEST_POSTGRES_URL",
        "postgresql+psycopg://sovite_resource_test:fixture-only@example.invalid:5432/sovite_resource_test",
    )
    with pytest.raises(ValueError, match="non_owned_service_url_refused"):
        diagnostics._snapshot_connection_kwargs()


def test_supervisor_timeout_discards_raw_child_output_and_reaps(tmp_path, capsys):
    diag_dir = tmp_path / "diagnostics"
    diag_dir.mkdir(mode=0o700)
    status = diagnostics._supervise(
        diag_dir,
        timeout_seconds=0.2,
        command=[sys.executable, "-c", "import time; print('raw-secret-like-fixture'); time.sleep(30)"],
    )

    output = capsys.readouterr().out
    summary = json.loads((diag_dir / "summary.json").read_text())
    assert status == 124
    assert "raw-secret-like-fixture" not in output
    assert summary["timed_out"] is True
    assert summary["test_exit_status"] == 124
    assert summary["process_group"]["group_empty"] is True
    assert summary["process_group"]["leader_reaped"] is True
    assert (diag_dir / "stack.txt").is_file()
    assert (diag_dir / "activity.json").is_file()
    assert "raw-secret-like-fixture" not in "".join(
        path.read_text(errors="replace") for path in diag_dir.iterdir() if path.is_file()
    )


def test_supervisor_preserves_child_failure_status(tmp_path, capsys):
    diag_dir = tmp_path / "diagnostics"
    diag_dir.mkdir(mode=0o700)
    status = diagnostics._supervise(
        diag_dir,
        timeout_seconds=3,
        command=[sys.executable, "-c", "print('raw-error-detail'); raise SystemExit(7)"],
    )

    output = capsys.readouterr().out
    summary = json.loads((diag_dir / "summary.json").read_text())
    assert status == 7
    assert summary["test_exit_status"] == 7
    assert "raw-error-detail" not in output
    assert summary["process_group"]["group_empty"] is True


def test_supervisor_cleans_descendants_after_successful_leader_exit(tmp_path):
    diag_dir = tmp_path / "diagnostics"
    diag_dir.mkdir(mode=0o700)
    child_code = (
        "import subprocess,sys; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])"
    )
    status = diagnostics._supervise(
        diag_dir,
        timeout_seconds=5,
        command=[sys.executable, "-c", child_code],
    )

    summary = json.loads((diag_dir / "summary.json").read_text())
    assert status == 125
    assert summary["process_group"]["term_sent"] is True
    assert summary["process_group"]["group_empty"] is True
