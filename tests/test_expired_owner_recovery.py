"""Synthetic expired Work-owner recovery contract."""
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from sovite.collection_transaction import owner_transaction
from sovite.db import Base, Job, WorkerExecutionLease
from sovite.lease_service import LeaseService, _HandBackOwnerUnavailable, execution_owner_key
from test_work_projection_rows import work_rows


def test_expired_started_work_is_held_without_release(work_rows, monkeypatch):
    db, _, _, _ = work_rows
    with db.sessions.begin() as session:
        job = session.get(Job, "job")
        job.resolved = {**job.resolved, "compute": {"id": "fixture"}}
        lease = session.get(WorkerExecutionLease, "job")
        expiry = lease.lease_until
    import sovite.lease_service as lease_module
    monkeypatch.setattr(lease_module.time, "time", lambda: expiry + 1)
    service = LeaseService(SimpleNamespace(db=db, settings=SimpleNamespace(compute_id="fixture", max_attempts=3)),
                           execution_owner_key(workload_id="workload", instance_id="instance"), {})
    with owner_transaction(db) as session:
        assert service._close_owner("job", recovery_session=session) is True
    with db.sessions() as session:
        job = session.get(Job, "job")
        lease = session.get(WorkerExecutionLease, "job")
        tables = Base.metadata.tables
        reservation = session.execute(select(tables["program_reservations"])).mappings().one()
        slot = session.execute(select(tables["program_slots"])).mappings().one()
        gate = session.execute(select(tables["program_gate_state"])).mappings().one()
        assert (job.status, job.error_code, job.attempts) == ("blocked", "execution_uncertain", 1)
        assert lease.phase == "finished" and job.lease_owner is None
        assert reservation.state == "unknown" and slot.state == "unknown"
        assert gate.mode == "inhibited"
        assert job.public_projection is None and job.released_at is None


def test_recovery_rolls_back_after_unknown_write(work_rows, monkeypatch):
    from test_work_projection_rows import _finish_snapshot
    import sovite.lease_service as lease_module
    import sovite.program_ledger as ledger
    db, _, _, _ = work_rows
    with db.sessions.begin() as session:
        job = session.get(Job, "job")
        job.resolved = {**job.resolved, "compute": {"id": "fixture"}}
        expiry = session.get(WorkerExecutionLease, "job").lease_until
    monkeypatch.setattr(lease_module.time, "time", lambda: expiry + 1)
    original = ledger._mark_work_projection_unknown_in_transaction
    def fail_after_write(*args, **kwargs):
        original(*args, **kwargs)
        raise ValueError("synthetic failure")
    monkeypatch.setattr(ledger, "_mark_work_projection_unknown_in_transaction", fail_after_write)
    before = _finish_snapshot(db)
    service = LeaseService(SimpleNamespace(db=db, settings=SimpleNamespace(compute_id="fixture", max_attempts=3)),
                           execution_owner_key(workload_id="workload", instance_id="instance"), {})
    with pytest.raises(ValueError, match="synthetic failure"):
        with owner_transaction(db) as session:
            service._close_owner("job", recovery_session=session)
    assert _finish_snapshot(db) == before


def test_sweep_commits_owner_and_cursor_atomically(work_rows, monkeypatch):
    import sovite.lease_service as lease_module
    from sovite.recovery_cursor import read_cursor
    db, _, _, _ = work_rows
    with db.sessions.begin() as session:
        job = session.get(Job, "job")
        job.resolved = {**job.resolved, "compute": {"id": "fixture"}}
        expiry = session.get(WorkerExecutionLease, "job").lease_until
    monkeypatch.setattr(lease_module.time, "time", lambda: expiry + 1)
    monkeypatch.setattr(lease_module, "sweep_terminal_authority", lambda *_: None)
    service = LeaseService(SimpleNamespace(db=db, settings=SimpleNamespace(compute_id="fixture", max_attempts=3)),
                           execution_owner_key(workload_id="workload", instance_id="instance"), {})
    counts = service.recover_expired()
    assert counts["retired"] == 1
    with owner_transaction(db) as session:
        assert read_cursor(session, "fixture", False).last_job_id is None
        assert session.get(Job, "job").status == "blocked"


def test_recovery_refuses_unexpired_snapshot_without_mutation(work_rows, monkeypatch):
    from test_work_projection_rows import _finish_snapshot
    import sovite.lease_service as lease_module
    db, _, _, _ = work_rows
    with db.sessions.begin() as session:
        job = session.get(Job, "job")
        job.resolved = {**job.resolved, "compute": {"id": "fixture"}}
        expiry = session.get(WorkerExecutionLease, "job").lease_until
    monkeypatch.setattr(lease_module.time, "time", lambda: expiry - 1)
    before = _finish_snapshot(db)
    service = LeaseService(SimpleNamespace(db=db, settings=SimpleNamespace(compute_id="fixture", max_attempts=3)),
                           execution_owner_key(workload_id="workload", instance_id="instance"), {})
    with pytest.raises(_HandBackOwnerUnavailable):
        with owner_transaction(db) as session:
            service._close_owner("job", recovery_session=session)
    assert _finish_snapshot(db) == before


def test_cancelled_expired_work_still_holds_obligation(work_rows, monkeypatch):
    import sovite.lease_service as lease_module
    db, _, _, _ = work_rows
    with db.sessions.begin() as session:
        job = session.get(Job, "job")
        job.resolved = {**job.resolved, "compute": {"id": "fixture"}}
        job.cancel_requested = True
        expiry = session.get(WorkerExecutionLease, "job").lease_until
    monkeypatch.setattr(lease_module.time, "time", lambda: expiry + 1)
    service = LeaseService(SimpleNamespace(db=db, settings=SimpleNamespace(compute_id="fixture", max_attempts=3)),
                           execution_owner_key(workload_id="workload", instance_id="instance"), {})
    with owner_transaction(db) as session:
        assert service._close_owner("job", recovery_session=session)
    with db.sessions() as session:
        job = session.get(Job, "job")
        reservation = session.execute(select(Base.metadata.tables["program_reservations"])).mappings().one()
        assert (job.status, job.error_code) == ("blocked", "execution_uncertain")
        assert reservation.state == "unknown"


@pytest.mark.parametrize("compute_container", [
    None, "malformed-scalar", 7, False, [], {"id": 17}, {},
], ids=["null", "string", "number", "boolean", "array", "object-id-number", "object-empty"])
def test_sweep_malformed_compute_container_does_not_hide_later_valid_work(
    work_rows, monkeypatch, compute_container,
):
    """An unusable stored compute value is candidate-local; the later owner still closes."""
    import sovite.lease_service as lease_module
    db, _, _, _ = work_rows
    with db.sessions.begin() as session:
        job = session.get(Job, "job")
        job.resolved = {**job.resolved, "compute": {"id": "fixture"}}
        expiry = session.get(WorkerExecutionLease, "job").lease_until
        session.add(Job(id="aaa", receipt="other", idempotency_key="other",
                        request_digest="x", caller="fixture", grant_id="fixture",
                        generation=job.generation,
                        resolved={"compute": compute_container, "repository_run": "malformed"},
                        task="synthetic", status="running", attempts=1,
                        lease_owner=job.lease_owner, lease_until=expiry))
        session.flush()
        session.add(WorkerExecutionLease(job_id="aaa", workload_id="workload", instance_id="instance",
                   fence="2" * 32, generation=job.generation, phase="started",
                   lease_until=expiry, execution_deadline=expiry, created_at=1, updated_at=1))
    monkeypatch.setattr(lease_module.time, "time", lambda: expiry + 1)
    monkeypatch.setattr(lease_module, "sweep_terminal_authority", lambda *_: None)
    service = LeaseService(SimpleNamespace(db=db, settings=SimpleNamespace(compute_id="fixture", max_attempts=3)),
                           execution_owner_key(workload_id="workload", instance_id="instance"), {})
    counts = service.recover_expired()
    assert counts == {"examined": 2, "retired": 1, "changed": 0, "busy": 0, "invalid": 1}
    with db.sessions() as session:
        assert session.get(Job, "aaa").status == "running"
        assert session.get(Job, "job").status == "blocked"


@pytest.mark.parametrize(("field", "shape"), [
    ("harness", "missing"), ("harness", "null"), ("harness", "scalar"),
    ("grant", "missing"), ("grant", "null"), ("grant", "scalar"),
], ids=["harness-missing", "harness-null", "harness-scalar",
       "grant-missing", "grant-null", "grant-scalar"])
def test_sweep_malformed_replay_fields_with_valid_lease_does_not_hide_later_work(
    work_rows, monkeypatch, field, shape,
):
    """Malformed stored harness/grant values are local refusals after lease validation."""
    import sovite.lease_service as lease_module
    db, _, _, _ = work_rows
    with db.sessions.begin() as session:
        later = session.get(Job, "job")
        later.resolved = {**later.resolved, "compute": {"id": "fixture"}}
        expiry = session.get(WorkerExecutionLease, "job").lease_until

        resolved = {
            "compute": {"id": "fixture"},
            "model": {"id": "synthetic-model"},
            "harness": {"kind": "tool-agent", "idempotent": True},
            "grant": {"max_attempts": 3},
            "tools": [],
        }
        if shape == "missing":
            resolved.pop(field)
        elif shape == "null":
            resolved[field] = None
        else:
            resolved[field] = "malformed-scalar"
        session.add(Job(id="aaa", receipt="malformed-replay-fields",
                        idempotency_key=f"malformed-{field}-{shape}",
                        request_digest="x", caller=later.caller, grant_id=later.grant_id,
                        generation=later.generation, resolved=resolved,
                        task="synthetic malformed replay field", status="running", attempts=1,
                        lease_owner=later.lease_owner, lease_until=expiry))
        session.flush()
        session.add(WorkerExecutionLease(
            job_id="aaa", workload_id="workload", instance_id="instance",
            fence="2" * 32, generation=later.generation, phase="started",
            lease_until=expiry, execution_deadline=expiry, created_at=1, updated_at=1,
        ))

    monkeypatch.setattr(lease_module.time, "time", lambda: expiry + 1)
    monkeypatch.setattr(lease_module, "sweep_terminal_authority", lambda *_: None)
    service = LeaseService(SimpleNamespace(db=db, settings=SimpleNamespace(
        compute_id="fixture", max_attempts=3)),
        execution_owner_key(workload_id="workload", instance_id="instance"), {})
    counts = service.recover_expired()
    assert counts == {"examined": 2, "retired": 1, "changed": 0, "busy": 0, "invalid": 1}
    with db.sessions() as session:
        malformed = session.get(Job, "aaa")
        assert malformed.status == "running" and malformed.lease_owner is not None
        assert session.get(WorkerExecutionLease, "aaa").phase == "started"
        assert session.get(Job, "job").status == "blocked"


@pytest.mark.parametrize("phase", ["reserved", "started"])
def test_empty_repository_recovery_respects_lease_phase(work_rows, monkeypatch, phase):
    from test_work_projection_rows import _clear_work_history
    import sovite.lease_service as lease_module
    db, _, _, _ = work_rows
    _clear_work_history(db)
    with db.sessions.begin() as session:
        job = session.get(Job, "job")
        job.resolved = {**job.resolved, "compute": {"id": "fixture"}}
        lease = session.get(WorkerExecutionLease, "job")
        expiry = lease.lease_until
        lease.phase = phase
        if phase == "reserved":
            lease.execution_deadline = None
            job.attempts = 0
    monkeypatch.setattr(lease_module.time, "time", lambda: expiry + 1)
    service = LeaseService(SimpleNamespace(db=db, settings=SimpleNamespace(compute_id="fixture", max_attempts=3)),
                           execution_owner_key(workload_id="workload", instance_id="instance"), {})
    with owner_transaction(db) as session:
        assert service._close_owner("job", recovery_session=session)
    with db.sessions() as session:
        job = session.get(Job, "job")
        expected = ("retry_wait", "lease_expired", 0) if phase == "reserved" else ("blocked", "execution_uncertain", 1)
        assert (job.status, job.error_code, job.attempts) == expected


@pytest.mark.parametrize("attempt_state", ["running", "uncertain"])
def test_recovery_preserves_known_settlement(work_rows, monkeypatch, attempt_state):
    from test_work_projection_rows import _seed_hand_back_known_receipt, _set_hand_back_attempt_state
    import sovite.lease_service as lease_module
    db, _, _, _ = work_rows
    _seed_hand_back_known_receipt(work_rows, actual_usd="0", actual_allowance="0")
    _set_hand_back_attempt_state(db, attempt_state)
    with db.sessions.begin() as session:
        job = session.get(Job, "job")
        job.resolved = {**job.resolved, "compute": {"id": "fixture"}}
        expiry = session.get(WorkerExecutionLease, "job").lease_until
    monkeypatch.setattr(lease_module.time, "time", lambda: expiry + 1)
    service = LeaseService(SimpleNamespace(db=db, settings=SimpleNamespace(compute_id="fixture", max_attempts=3)),
                           execution_owner_key(workload_id="workload", instance_id="instance"), {})
    with owner_transaction(db) as session:
        assert service._close_owner("job", recovery_session=session)
    with db.sessions() as session:
        job = session.get(Job, "job")
        tables = Base.metadata.tables
        reservation = session.execute(select(tables["program_reservations"])).mappings().one()
        gate = session.execute(select(tables["program_gate_state"])).mappings().one()
        assert (job.status, job.error_code) == ("blocked", "execution_uncertain")
        assert reservation.state == "settled"
        assert job.resolved["repository_run"]["program_ledger_projections_v1"][0]["intent_state"] == "known"
        assert gate.mode == ("inhibited" if attempt_state == "uncertain" else "disabled")


@pytest.mark.parametrize("attempt_state", ["preparing", "awaiting_activation", "running", "interrupted", "uncertain", "completed", "ineligible", "failed", "cancelled", "activation_cancelled"])
def test_expired_current_attempt_states_hold_unresolved_work(work_rows, monkeypatch, attempt_state):
    from test_work_projection_rows import _set_hand_back_attempt_state
    import sovite.lease_service as lease_module
    db, _, _, _ = work_rows
    _set_hand_back_attempt_state(db, attempt_state)
    with db.sessions.begin() as session:
        job = session.get(Job, "job")
        job.resolved = {**job.resolved, "compute": {"id": "fixture"}}
        expiry = session.get(WorkerExecutionLease, "job").lease_until
    monkeypatch.setattr(lease_module.time, "time", lambda: expiry + 1)
    service = LeaseService(SimpleNamespace(db=db, settings=SimpleNamespace(compute_id="fixture", max_attempts=3)),
                           execution_owner_key(workload_id="workload", instance_id="instance"), {})
    with owner_transaction(db) as session:
        assert service._close_owner("job", recovery_session=session)
    with db.sessions() as session:
        job = session.get(Job, "job")
        assert (job.status, job.error_code, job.attempts) == ("blocked", "execution_uncertain", 1)
        assert job.resolved["repository_run"]["program_ledger_projections_v1"][0]["intent_state"] == "unknown"


def test_reserved_work_association_refuses_without_mutation(work_rows, monkeypatch):
    from test_work_projection_rows import _finish_snapshot
    import sovite.lease_service as lease_module
    db, _, _, _ = work_rows
    with db.sessions.begin() as session:
        job = session.get(Job, "job")
        job.resolved = {**job.resolved, "compute": {"id": "fixture"}}
        lease = session.get(WorkerExecutionLease, "job")
        expiry = lease.lease_until
        lease.phase = "reserved"
        lease.execution_deadline = None
    monkeypatch.setattr(lease_module.time, "time", lambda: expiry + 1)
    before = _finish_snapshot(db)
    service = LeaseService(SimpleNamespace(db=db, settings=SimpleNamespace(compute_id="fixture", max_attempts=3)),
                           execution_owner_key(workload_id="workload", instance_id="instance"), {})
    with pytest.raises(_HandBackOwnerUnavailable):
        with owner_transaction(db) as session:
            service._close_owner("job", recovery_session=session)
    assert _finish_snapshot(db) == before


def test_recovery_preserves_retained_legacy_history(work_rows, monkeypatch):
    from test_work_projection_rows import _seed_retained_legacy_history
    import sovite.lease_service as lease_module
    db, _, _, _ = work_rows
    _seed_retained_legacy_history(db)
    with db.sessions.begin() as session:
        job = session.get(Job, "job")
        job.resolved = {**job.resolved, "compute": {"id": "fixture"}}
        expiry = session.get(WorkerExecutionLease, "job").lease_until
        links = [dict(row) for row in session.execute(select(Base.metadata.tables["program_legacy_links"])).mappings()]
    monkeypatch.setattr(lease_module.time, "time", lambda: expiry + 1)
    service = LeaseService(SimpleNamespace(db=db, settings=SimpleNamespace(compute_id="fixture", max_attempts=3)),
                           execution_owner_key(workload_id="workload", instance_id="instance"), {})
    with owner_transaction(db) as session:
        assert service._close_owner("job", recovery_session=session)
    with db.sessions() as session:
        job = session.get(Job, "job")
        assert [dict(row) for row in session.execute(select(Base.metadata.tables["program_legacy_links"])).mappings()] == links
        assert job.attempts == 1 and job.status == "blocked"
        assert job.resolved["repository_run"]["budget_reservations"][0]["call_id"] == "source-call"
