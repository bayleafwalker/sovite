import asyncio

import pytest

from sovite.remote_journal import ExecutionJournal, JournalError
from sovite.worker import RemoteWorker


class Client:
    def __init__(self, api_version, journal, pulls=(), lookups=()):
        self.api_version = api_version
        self.journal = journal
        self.pulls = iter(pulls)
        self.lookups = iter(lookups)
        self.pull_calls = 0
        self.lookup_calls = 0

    def pull(self, instance_id, operation_id):
        self.pull_calls += 1
        record = self.journal.load()
        assert record["operation_id"] == operation_id
        assert record.get("pull_protocol", 1) == self.api_version
        return next(self.pulls)

    def lookup(self, instance_id, operation_id):
        self.lookup_calls += 1
        return next(self.lookups)


def _open(tmp_path):
    return ExecutionJournal(tmp_path / "execution.json")


def test_v2_scan_progress_is_journaled_then_cleared_and_next_pull_uses_fresh_key(tmp_path):
    journal = _open(tmp_path)
    client = Client(2, journal, pulls=[{"state": "scan_advanced"}, {"state": "busy"}])
    worker = RemoteWorker(client, "remote-worker-1", journal=journal, pull_protocol=2)

    assert asyncio.run(worker.run_once()) is False
    assert journal.load() is None
    assert asyncio.run(worker.run_once()) is False
    assert journal.load() is None
    assert client.pull_calls == 2
    journal.close()


def test_pending_v2_journal_refuses_v1_process_before_remote_lookup(tmp_path):
    path = tmp_path / "execution.json"
    journal = ExecutionJournal(path)
    v2_client = Client(2, journal)
    v2_worker = RemoteWorker(v2_client, "remote-worker-1", journal=journal, pull_protocol=2)
    v2_worker._journal_write("pending_pull", "pending-v2", "pull",
                             expected={"idle", "reserved", "scan_advanced"})
    record = journal.load()
    assert record["schema_version"] == 2 and record["pull_protocol"] == 2
    journal.close()

    journal = ExecutionJournal(path)
    v1_client = Client(1, journal)
    v1_worker = RemoteWorker(v1_client, "remote-worker-1", journal=journal)
    assert asyncio.run(v1_worker.run_once()) is False
    assert v1_client.lookup_calls == 0
    assert not journal.load() is None
    assert v1_worker.stop.is_set()
    journal.close()


def test_v1_journal_remains_schema_one_and_v2_journal_is_strict(tmp_path):
    journal = _open(tmp_path)
    worker = RemoteWorker(Client(1, journal), "remote-worker-1", journal=journal)
    worker._journal_write("pending_pull", "pending-v1", "pull", expected={"idle", "reserved"})
    assert journal.load()["schema_version"] == 1
    journal.clear()

    worker = RemoteWorker(Client(2, journal), "remote-worker-1", journal=journal, pull_protocol=2)
    worker._journal_write("pending_pull", "pending-v2", "pull",
                          expected={"idle", "reserved", "busy"})
    record = journal.load()
    record["pull_protocol"] = 1
    with pytest.raises(JournalError):
        journal.write(record)
    record["pull_protocol"] = 2.0
    with pytest.raises(JournalError):
        journal.write(record)
    journal.close()


def test_worker_rejects_client_protocol_mismatch_before_journal_use(tmp_path):
    journal = _open(tmp_path)
    with pytest.raises(ValueError, match="client API version"):
        RemoteWorker(Client(1, journal), "remote-worker-1", journal=journal, pull_protocol=2)
    assert journal.load() is None
    journal.close()


def test_v2_unknown_pull_reconciles_confirmed_progress_with_same_key(tmp_path):
    from sovite.execution_client import ExecutionOutcomeUnknown

    class LostClient(Client):
        def pull(self, instance_id, operation_id):
            self.pull_calls += 1
            record = self.journal.load()
            assert record["operation_id"] == operation_id
            assert record["pull_protocol"] == 2
            raise ExecutionOutcomeUnknown(instance_id, operation_id)

        def lookup(self, instance_id, operation_id):
            self.lookup_calls += 1
            return {"state": "found", "operation_id": operation_id,
                    "operation_kind": "pull", "job_id": None, "fence": None,
                    "result_state": "scan_advanced"}

    journal = _open(tmp_path)
    client = LostClient(2, journal)
    worker = RemoteWorker(client, "remote-worker-1", journal=journal, pull_protocol=2)
    assert asyncio.run(worker.run_once()) is False
    assert client.pull_calls == client.lookup_calls == 1
    assert journal.load() is None
    assert not worker.stop.is_set()
    journal.close()


def test_v2_restart_clears_only_confirmed_no_job_outcome(tmp_path):
    path = tmp_path / "execution.json"
    journal = ExecutionJournal(path)
    writer = RemoteWorker(Client(2, journal), "remote-worker-1", journal=journal, pull_protocol=2)
    writer._journal_write("pending_pull", "pending-v2-restart", "pull",
                          expected={"idle", "reserved", "scan_advanced", "profile_unavailable", "busy"})
    journal.close()

    journal = ExecutionJournal(path)
    key = "pending-v2-restart"
    client = Client(2, journal, lookups=[{
        "state": "found", "operation_id": key, "operation_kind": "pull",
        "job_id": None, "fence": None, "result_state": "profile_unavailable",
    }])
    restarted = RemoteWorker(client, "remote-worker-1", journal=journal, pull_protocol=2)
    assert restarted._startup_reconcile() is True
    assert journal.load() is None
    assert not restarted.stop.is_set()
    journal.close()


def test_v2_restart_keeps_recovered_reservation_for_manual_reconciliation(tmp_path):
    path = tmp_path / "execution.json"
    journal = ExecutionJournal(path)
    writer = RemoteWorker(Client(2, journal), "remote-worker-1", journal=journal, pull_protocol=2)
    writer._journal_write("pending_pull", "pending-v2-reserved", "pull",
                          expected={"idle", "reserved", "scan_advanced"})
    journal.close()

    journal = ExecutionJournal(path)
    key = "pending-v2-reserved"
    client = Client(2, journal, lookups=[{
        "state": "found", "operation_id": key, "operation_kind": "pull",
        "job_id": "job-1", "fence": "a" * 32, "result_state": "reserved",
    }])
    restarted = RemoteWorker(client, "remote-worker-1", journal=journal, pull_protocol=2)
    assert restarted._startup_reconcile() is False
    assert restarted.stop.is_set()
    assert journal.load()["phase"] == "manual"
    journal.close()
