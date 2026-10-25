import os

import pytest

from sovite.remote_journal import ExecutionJournal, JournalError


def _record():
    return {
        "schema_version": 1,
        "instance_id": "remote-worker-1",
        "phase": "pending_pull",
        "operation_id": "operation-1",
        "operation_kind": "pull",
        "job_id": None,
        "generation": None,
        "fence": None,
        "expected_result_states": [],
        "lease_until": None,
        "deadline": None,
    }


def test_inspection_keeps_rejecting_fs_group_widened_journal_and_lock(tmp_path):
    path = tmp_path / "execution.json"
    journal = ExecutionJournal(path)
    journal.write(_record())
    journal.close()

    os.chmod(path, 0o660)
    with pytest.raises(JournalError, match="unsafe ownership, permissions"):
        ExecutionJournal.inspect(path)

    os.chmod(path, 0o600)
    os.chmod(str(path) + ".lock", 0o660)
    with pytest.raises(JournalError, match="unsafe ownership or permissions"):
        ExecutionJournal.inspect(path)

    os.chmod(str(path) + ".lock", 0o600)
    assert ExecutionJournal.inspect(path) == _record()
