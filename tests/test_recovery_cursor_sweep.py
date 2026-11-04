"""Cursor fairness and failure-isolation tests for the expired-owner sweep."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from sovite.db import Database, Job
from sovite.lease_service import LeaseService, _HandBackOwnerUnavailable
from sovite.recovery_cursor import read_cursor
from sovite.collection_transaction import owner_transaction
from sovite.work_projection import _WorkProjectionBusy


def _service(db):
    return LeaseService(SimpleNamespace(
        db=db, settings=SimpleNamespace(compute_id="compute", max_attempts=3),
    ), "sweep-worker", {})


def _add_jobs(db, count):
    with db.sessions.begin() as session:
        for index in range(count):
            job_id = f"job-{index:04d}"
            session.add(Job(
                id=job_id, receipt=f"r-{index:04d}", idempotency_key=f"i-{index:04d}",
                request_digest="d" * 80, caller="caller", grant_id="grant",
                generation="generation", resolved={"compute": {"id": "compute"}}, task="task",
                status="running", lease_owner="old-owner", lease_until=0.0,
            ))


def _quiet_terminal_sweep(monkeypatch):
    import sovite.lease_service as lease_module
    monkeypatch.setattr(lease_module, "sweep_terminal_authority", lambda *_: None)


def test_cursor_crosses_more_than_1000_candidates_after_restart(tmp_path, monkeypatch):
    db = Database(f"sqlite:///{tmp_path / 'cursor-fairness.db'}")
    try:
        db.migrate()
        _add_jobs(db, 1001)
        _quiet_terminal_sweep(monkeypatch)
        service = _service(db)
        monkeypatch.setattr(service, "_close_owner", lambda *_args, **_kwargs: False)
        reserve_calls = []
        monkeypatch.setattr(service, "reserve", lambda **_: reserve_calls.append(True))

        with pytest.raises(RuntimeError, match="recovery sweep incomplete"):
            service.claim()
        assert reserve_calls == []
        with owner_transaction(db) as session:
            cursor = read_cursor(session, "compute", False)
            assert cursor.last_job_id == "job-0999"

        # A new service instance proves the cursor is database state, not memory.
        restarted = _service(db)
        monkeypatch.setattr(restarted, "_close_owner", lambda *_args, **_kwargs: False)
        counts = restarted.recover_expired()
        assert counts == {
            "examined": 1, "retired": 0, "changed": 1, "busy": 0, "invalid": 0,
        }
        with owner_transaction(db) as session:
            assert read_cursor(session, "compute", False).last_job_id is None
            assert session.get(Job, "job-1000").status == "running"
    finally:
        db.engine.dispose()


@pytest.mark.parametrize(("refusal", "counter"), [
    (_WorkProjectionBusy, "busy"),
    (_HandBackOwnerUnavailable, "invalid"),
])
def test_candidate_refusal_rolls_back_then_advances_exact_cursor(tmp_path, monkeypatch,
                                                                refusal, counter):
    db = Database(f"sqlite:///{tmp_path / f'cursor-{counter}.db'}")
    try:
        db.migrate()
        _add_jobs(db, 2)
        _quiet_terminal_sweep(monkeypatch)
        service = _service(db)
        calls = []

        def refuse_first(_job_id, **_kwargs):
            calls.append(_job_id)
            if len(calls) == 1:
                raise refusal()
            return False

        monkeypatch.setattr(service, "_close_owner", refuse_first)
        counts = service.recover_expired()
        assert calls == ["job-0000", "job-0001"]
        assert counts[counter] == 1
        assert counts["examined"] == 2
        with owner_transaction(db) as session:
            assert read_cursor(session, "compute", False).last_job_id is None
            assert session.get(Job, "job-0000").status == "running"
    finally:
        db.engine.dispose()


def test_global_sweep_failure_prevents_reserve(tmp_path, monkeypatch):
    db = Database(f"sqlite:///{tmp_path / 'cursor-global-failure.db'}")
    try:
        db.migrate()
        service = _service(db)
        import sovite.lease_service as lease_module
        monkeypatch.setattr(lease_module, "sweep_terminal_authority",
                            lambda *_: (_ for _ in ()).throw(RuntimeError("audit unavailable")))
        reserved = []
        monkeypatch.setattr(service, "reserve", lambda **_: reserved.append(True))
        with pytest.raises(RuntimeError, match="audit unavailable"):
            service.claim()
        assert reserved == []
    finally:
        db.engine.dispose()
