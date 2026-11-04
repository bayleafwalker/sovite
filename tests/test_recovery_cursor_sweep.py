"""Public sweep and reservation-barrier behavior for durable recovery cursors."""
from __future__ import annotations

import pytest
from sqlalchemy import select, text, update
from threading import Event, Thread

pytest_plugins = ("test_execution_leases", "test_work_projection_rows")

from sovite.collection_transaction import owner_transaction
from sovite.db import Database, Job, MetadataRow, WorkerExecutionLease, WorkerExecutionOperation
from sovite.lease_service import LeaseService
from sovite.work_projection import _WorkProjectionBusy
from sovite.recovery_cursor import (
    RecoveryCursorUnavailable, advance_cursor, cursor_key, peek_cursor, read_cursor,
)
from sovite.service import PlatformError
from sovite.lease_service import _HandBackOwnerUnavailable
from test_execution_leases import owner, submit  # noqa: F401


def _service(db, *, compute_id="compute"):
    from types import SimpleNamespace
    return LeaseService(SimpleNamespace(
        db=db, settings=SimpleNamespace(
            compute_id=compute_id, execution_workload_identity="workload", max_attempts=3),
    ), "sweep-worker", {})


def _add_jobs(db, count, *, until=0.0):
    with db.sessions.begin() as session:
        for index in range(count):
            job_id = f"job-{index:04d}"
            session.add(Job(
                id=job_id, receipt=f"r-{index:04d}", idempotency_key=f"i-{index:04d}",
                request_digest="d" * 80, caller="caller", grant_id="grant",
                generation="generation", resolved={"compute": {"id": "compute"}}, task="task",
                status="running", lease_owner="old-owner", lease_until=until,
                created_at=1.0, updated_at=1.0,
            ))


def _set_cursor(db, *, phase="idle", last_job_id=None, fair_pointer_job_id=None,
                forward_upper_id=None, scan_started_at=None, covered_through=None):
    with owner_transaction(db) as session:
        cursor = read_cursor(session, "compute", False)
        assert advance_cursor(session, cursor, phase=phase, last_job_id=last_job_id,
                              fair_pointer_job_id=fair_pointer_job_id,
                              forward_upper_id=forward_upper_id,
                              scan_started_at=scan_started_at, covered_through=covered_through)


def _quiet_terminal_sweep(monkeypatch):
    import sovite.lease_service as lease_module
    monkeypatch.setattr(lease_module, "sweep_terminal_authority", lambda *_: None)


def test_sweep_persists_forward_progress_and_completes_after_restart(tmp_path, monkeypatch):
    db = Database(f"sqlite:///{tmp_path / 'cursor-forward-restart.db'}")
    try:
        db.migrate()
        _add_jobs(db, 1001)
        import sovite.lease_service as lease_module
        monkeypatch.setattr(lease_module.time, "time", lambda: 100.0)
        _quiet_terminal_sweep(monkeypatch)
        service = _service(db)
        monkeypatch.setattr(service, "_close_owner", lambda *_args, **_kwargs: False)
        with pytest.raises(RuntimeError, match="recovery sweep incomplete"):
            service.recover_expired()
        with owner_transaction(db) as session:
            cursor = read_cursor(session, "compute", False)
            assert (cursor.phase, cursor.last_job_id, cursor.forward_upper_id) == (
                "forward", "job-0999", "job-1000")
            assert cursor.scan_started_at == 100.0

        restarted = _service(db)
        monkeypatch.setattr(restarted, "_close_owner", lambda *_args, **_kwargs: False)
        counts = restarted.recover_expired()
        assert counts == {"examined": 1, "retired": 0, "changed": 1, "busy": 0, "invalid": 0}
        with owner_transaction(db) as session:
            cursor = read_cursor(session, "compute", False)
            assert cursor.phase == "idle"
            assert cursor.last_job_id is None and cursor.forward_upper_id is None
            assert cursor.fair_pointer_job_id == "job-1000"
            assert cursor.covered_through == 100.0
    finally:
        db.engine.dispose()


def test_completed_cycle_wrap_visits_lower_ids_after_fair_pointer(tmp_path, monkeypatch):
    db = Database(f"sqlite:///{tmp_path / 'cursor-wrap-fairness.db'}")
    try:
        db.migrate()
        _add_jobs(db, 4)
        _set_cursor(db, fair_pointer_job_id="job-0002", covered_through=50.0)
        import sovite.lease_service as lease_module
        monkeypatch.setattr(lease_module.time, "time", lambda: 100.0)
        _quiet_terminal_sweep(monkeypatch)
        service = _service(db)
        visited = []
        monkeypatch.setattr(service, "_close_owner",
                            lambda job_id, **_: visited.append(job_id) or False)
        counts = service.recover_expired()
        assert counts["examined"] == 4
        assert visited == ["job-0003", "job-0000", "job-0001", "job-0002"]
        with owner_transaction(db) as session:
            cursor = read_cursor(session, "compute", False)
            assert cursor.phase == "idle" and cursor.fair_pointer_job_id == "job-0003"
    finally:
        db.engine.dispose()


def test_row_that_expires_after_wrap_position_is_caught_by_completion_guard(tmp_path, monkeypatch):
    db = Database(f"sqlite:///{tmp_path / 'cursor-expiry-during-wrap.db'}")
    try:
        db.migrate()
        _add_jobs(db, 3)
        with db.sessions.begin() as session:
            session.execute(update(Job).where(Job.id == "job-0000").values(lease_until=105.0))
        _set_cursor(db, phase="wrap", fair_pointer_job_id="job-0002",
                    forward_upper_id="job-0002", scan_started_at=100.0)
        import sovite.lease_service as lease_module
        clock = [100.0]
        monkeypatch.setattr(lease_module.time, "time", lambda: clock[0])
        _quiet_terminal_sweep(monkeypatch)
        service = _service(db)
        visited = []

        def let_lower_expire_during_wrap(job_id, **_kwargs):
            visited.append(job_id)
            if job_id == "job-0002":
                clock[0] = 106.0
            return False

        monkeypatch.setattr(service, "_close_owner", let_lower_expire_during_wrap)
        counts = service.recover_expired()
        assert visited == ["job-0001", "job-0002"]
        assert counts["examined"] == 2
        with owner_transaction(db) as session:
            cursor = read_cursor(session, "compute", False)
            assert cursor.phase == "idle" and cursor.covered_through == 100.0
            with pytest.raises(PlatformError) as error:
                service._guard_fresh_reservation(session, "compute")
            assert error.value.code == "owner_unavailable"
        with db.sessions() as session:
            assert session.get(Job, "job-0000").status == "running"
    finally:
        db.engine.dispose()


def test_lower_id_created_during_cycle_is_caught_by_horizon_guard(tmp_path, monkeypatch):
    db = Database(f"sqlite:///{tmp_path / 'cursor-new-lower-id.db'}")
    try:
        db.migrate()
        with db.sessions.begin() as session:
            session.add(Job(
                id="job-0100", receipt="r-0100", idempotency_key="i-0100",
                request_digest="d" * 80, caller="caller", grant_id="grant",
                generation="generation", resolved={"compute": {"id": "compute"}}, task="task",
                status="running", lease_owner="old-owner", lease_until=0.0,
                created_at=1.0, updated_at=1.0,
            ))
        import sovite.lease_service as lease_module
        clock = [100.0]
        monkeypatch.setattr(lease_module.time, "time", lambda: clock[0])
        _quiet_terminal_sweep(monkeypatch)
        service = _service(db)

        def insert_lower_id_during_snapshot(_job_id, *, recovery_session):
            recovery_session.add(Job(
                id="job-0001", receipt="r-new", idempotency_key="i-new",
                request_digest="e" * 80, caller="caller", grant_id="grant",
                generation="generation", resolved={"compute": {"id": "compute"}}, task="new task",
                status="running", lease_owner="old-owner", lease_until=101.0,
                created_at=101.0, updated_at=101.0,
            ))
            recovery_session.flush()
            clock[0] = 102.0
            return False

        monkeypatch.setattr(service, "_close_owner", insert_lower_id_during_snapshot)
        counts = service.recover_expired()
        assert counts["examined"] == 1
        with owner_transaction(db) as session:
            cursor = read_cursor(session, "compute", False)
            assert cursor.phase == "idle" and cursor.covered_through == 100.0
            with pytest.raises(PlatformError) as error:
                service._guard_fresh_reservation(session, "compute")
            assert error.value.code == "owner_unavailable"
        with db.sessions() as session:
            assert session.get(Job, "job-0001").status == "running"
    finally:
        db.engine.dispose()


def test_unchanged_busy_candidate_is_revisited_on_next_completed_cycle(tmp_path, monkeypatch):
    db = Database(f"sqlite:///{tmp_path / 'cursor-busy-revisit.db'}")
    try:
        db.migrate()
        _add_jobs(db, 1)
        import sovite.lease_service as lease_module
        monkeypatch.setattr(lease_module.time, "time", lambda: 100.0)
        _quiet_terminal_sweep(monkeypatch)
        service = _service(db)
        visited = []

        def busy_once(job_id, **_kwargs):
            visited.append(job_id)
            if len(visited) == 1:
                raise _WorkProjectionBusy()
            return False

        monkeypatch.setattr(service, "_close_owner", busy_once)
        first = service.recover_expired()
        assert first["busy"] == 1
        with db.sessions() as session:
            row = session.get(Job, "job-0000")
            assert row.status == "running" and row.lease_until == 0.0
        second = service.recover_expired()
        assert second["examined"] == 1 and second["changed"] == 1
        assert visited == ["job-0000", "job-0000"]
    finally:
        db.engine.dispose()


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_pg_busy_job_is_revisited_unchanged_after_holder_releases(database, monkeypatch):
    db = database
    db.migrate()
    _add_jobs(db, 1)
    with db.sessions.begin() as session:
        session.add(WorkerExecutionLease(
            job_id="job-0000", workload_id="workload", instance_id="instance",
            fence="f" * 32, generation="generation", phase="started",
            lease_until=0.0, execution_deadline=50.0, created_at=1.0, updated_at=1.0,
        ))
    import sovite.lease_service as lease_module
    from sovite.work_projection import lock_work_projection_set
    monkeypatch.setattr(lease_module.time, "time", lambda: 100.0)
    _quiet_terminal_sweep(monkeypatch)
    service = _service(db)
    calls = []

    def contend_then_leave_unchanged(job_id, *, recovery_session):
        calls.append(job_id)
        if len(calls) == 1:
            lock_work_projection_set(recovery_session, (),
                                     nonblocking_owner_ids=(job_id,))
        return False

    monkeypatch.setattr(service, "_close_owner", contend_then_leave_unchanged)
    holder = db.sessions()
    holder.execute(text("BEGIN"))
    holder.execute(select(Job).where(Job.id == "job-0000").with_for_update()).scalar_one()
    try:
        first = service.recover_expired()
        assert first["busy"] == 1
        with db.sessions() as session:
            job = session.get(Job, "job-0000")
            lease = session.get(WorkerExecutionLease, "job-0000")
            assert job.status == "running" and job.lease_until == 0.0
            assert lease.phase == "started" and lease.lease_until == 0.0
    finally:
        holder.rollback()
        holder.close()
    second = service.recover_expired()
    assert second["examined"] == 1 and calls == ["job-0000", "job-0000"]


def test_wrap_cap_restarts_after_refused_rows_and_reaches_later_candidate(tmp_path, monkeypatch):
    db = Database(f"sqlite:///{tmp_path / 'cursor-wrap-cap-restart.db'}")
    try:
        db.migrate()
        _add_jobs(db, 1001)
        _set_cursor(db, phase="wrap", fair_pointer_job_id="job-1000",
                    forward_upper_id="job-1000", scan_started_at=100.0)
        import sovite.lease_service as lease_module
        monkeypatch.setattr(lease_module.time, "time", lambda: 100.0)
        _quiet_terminal_sweep(monkeypatch)
        first = _service(db)
        visited = []

        def refuse_first_thousand(job_id, **_kwargs):
            visited.append(job_id)
            if job_id != "job-1000":
                raise _HandBackOwnerUnavailable()
            return False

        monkeypatch.setattr(first, "_close_owner", refuse_first_thousand)
        with pytest.raises(RuntimeError, match="recovery sweep incomplete"):
            first.recover_expired()
        assert visited == [f"job-{index:04d}" for index in range(1000)]
        with owner_transaction(db) as session:
            cursor = read_cursor(session, "compute", False)
            assert (cursor.phase, cursor.last_job_id) == ("wrap", "job-0999")

        restarted = _service(db)
        monkeypatch.setattr(restarted, "_close_owner", refuse_first_thousand)
        counts = restarted.recover_expired()
        assert visited[-1] == "job-1000"
        assert counts["examined"] == 1 and counts["invalid"] == 0
        with owner_transaction(db) as session:
            cursor = read_cursor(session, "compute", False)
            assert cursor.phase == "idle" and cursor.fair_pointer_job_id == "job-1000"
    finally:
        db.engine.dispose()


@pytest.mark.parametrize("cursor_kind", ["malformed", "future"])
def test_reserve_fails_closed_on_untrusted_cursor(owner, monkeypatch, cursor_kind):
    job = submit(owner, key=f"cursor-{cursor_kind}")
    now = owner.clock[0]
    key = cursor_key(owner.principal.compute_id, False)
    if cursor_kind == "malformed":
        with owner.platform.db.sessions.begin() as session:
            session.add(MetadataRow(key=key, value={"schema_version": "wrong"}))
    else:
        with owner_transaction(owner.platform.db) as session:
            cursor = read_cursor(session, owner.principal.compute_id, False)
            assert advance_cursor(session, cursor, phase="idle", last_job_id=None,
                                  fair_pointer_job_id=None, forward_upper_id=None,
                                  scan_started_at=None, covered_through=now + 100)

    from test_execution_leases import snapshot
    before = snapshot(owner.platform)
    with pytest.raises(PlatformError) as error:
        owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                              operation_id=f"pull-{cursor_kind}")
    assert error.value.code == "owner_unavailable"
    assert snapshot(owner.platform) == before


def test_direct_reserve_resumes_persisted_incomplete_cycle(owner):
    queued = submit(owner, key="cursor-incomplete-resume")
    now = owner.clock[0]
    with owner.platform.db.sessions.begin() as session:
        session.add(MetadataRow(key=cursor_key(owner.principal.compute_id, False), value={
            "schema_version": "sovite-recovery-cursor/v2",
            "compute_id": owner.principal.compute_id,
            "repository_worker_only": False,
            "phase": "forward", "last_job_id": None,
            "fair_pointer_job_id": None, "forward_upper_id": queued.id,
            "scan_started_at": now, "covered_through": None,
        }))
    result = owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                                   operation_id="pull-incomplete-resume")
    assert result["state"] == "reserved" and result["job"]["job_id"] == queued.id
    with owner.platform.db.sessions() as session:
        cursor = peek_cursor(session, owner.principal.compute_id, False)
        assert cursor.phase == "idle" and cursor.covered_through == now


def test_reserve_recovers_row_expiring_after_completed_horizon_during_lock_wait(owner, monkeypatch):
    earlier = submit(owner, key="cursor-old-running")
    horizon = owner.clock[0]
    with owner.platform.db.sessions.begin() as session:
        session.execute(update(Job).where(Job.id == earlier.id).values(
            status="running", lease_until=horizon + 5, created_at=horizon - 100,
            lease_owner="old-owner", resolved={"compute": {"id": owner.principal.compute_id},
                                                "model": {"id": "older-model"}}))
    queued = submit(owner, key="cursor-new-queued")
    with owner_transaction(owner.platform.db) as session:
        cursor = read_cursor(session, owner.principal.compute_id, False)
        assert advance_cursor(session, cursor, phase="idle", last_job_id=None,
                              fair_pointer_job_id=earlier.id, forward_upper_id=None,
                              scan_started_at=None, covered_through=horizon)
    owner.clock[0] = horizon + 4
    recovery_results = []
    original_recover = owner.service.recover_expired

    def observe_recovery():
        counts = original_recover()
        recovery_results.append(counts)
        return counts

    monkeypatch.setattr(owner.service, "recover_expired", observe_recovery)
    original_authority_lock = owner.platform.db.authority_lock

    def expire_while_waiting_for_owner_lock(session):
        original_authority_lock(session)
        owner.clock[0] = horizon + 6

    monkeypatch.setattr(owner.platform.db, "authority_lock", expire_while_waiting_for_owner_lock)
    from test_execution_leases import snapshot
    before = snapshot(owner.platform)
    result = owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                                   operation_id="pull-after-horizon-expiry")
    assert result["state"] == "reserved" and result["job"]["job_id"] == queued.id
    after = snapshot(owner.platform)
    prior = next(row for row in before["jobs"] if row["id"] == earlier.id)
    recovered = next(row for row in after["jobs"] if row["id"] == earlier.id)
    assert prior["status"] == "running"
    assert recovered["status"] == "running"  # synthetic row deliberately has no lease
    assert recovery_results and recovery_results[0]["invalid"] == 1
    with owner.platform.db.sessions() as session:
        cursor = peek_cursor(session, owner.principal.compute_id, False)
        assert cursor.phase == "idle" and cursor.covered_through >= horizon + 6
    assert any(row["id"] == queued.id and row["status"] == "running" for row in after["jobs"])


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_postgres_reserve_rechecks_expiry_after_blocked_authority_lock(owner, monkeypatch):
    """A real row-lock wait may advance owner time before reservation linearizes."""
    expired = submit(owner, key="cursor-blocked-lock-expiry")
    horizon = owner.clock[0]
    with owner.platform.db.sessions.begin() as session:
        session.execute(update(Job).where(Job.id == expired.id).values(
            status="running", lease_until=horizon + 5, created_at=horizon - 100,
            lease_owner="old-owner", resolved={"compute": {"id": owner.principal.compute_id},
                                                "model": {"id": "older-model"}}))
    queued = submit(owner, key="cursor-blocked-lock-queued")
    with owner_transaction(owner.platform.db) as session:
        cursor = read_cursor(session, owner.principal.compute_id, False)
        assert advance_cursor(session, cursor, phase="idle", last_job_id=None,
                              fair_pointer_job_id=expired.id, forward_upper_id=None,
                              scan_started_at=None, covered_through=horizon)

    holder = owner.platform.db.sessions()
    holder.execute(text("BEGIN"))
    holder.execute(update(MetadataRow).where(MetadataRow.key == "audit_head")
                   .values(value=MetadataRow.value))
    lock_entered, lock_acquired, done = Event(), Event(), Event()
    original_authority_lock = owner.platform.db.authority_lock
    monkeypatch.setattr(owner.platform.db, "authority_lock", lambda session: (
        lock_entered.set(), original_authority_lock(session), lock_acquired.set()))
    recovery_results = []
    original_recover = owner.service.recover_expired

    def observe_recovery():
        counts = original_recover()
        recovery_results.append(counts)
        return counts

    monkeypatch.setattr(owner.service, "recover_expired", observe_recovery)
    result = {}

    def reserve():
        try:
            result["value"] = owner.service.reserve(
                principal=owner.principal, instance_id=owner.instance,
                operation_id="pull-after-blocked-lock")
        except Exception as exc:  # captured for assertion on the test thread
            result["error"] = exc
        finally:
            done.set()

    worker = Thread(target=reserve, daemon=True)
    try:
        worker.start()
        assert lock_entered.wait(5)
        assert not lock_acquired.wait(0.1), "reservation did not wait on the held owner lock"
        owner.clock[0] = horizon + 6
        holder.commit()
        assert done.wait(5), "reserve did not finish after releasing owner lock"
        assert "error" not in result
        assert result["value"]["state"] == "reserved"
        assert result["value"]["job"]["job_id"] == queued.id
        assert recovery_results and recovery_results[0]["invalid"] == 1
        with owner.platform.db.sessions() as session:
            assert session.get(Job, queued.id).status == "running"
            assert session.get(Job, expired.id).status == "running"
            cursor = peek_cursor(session, owner.principal.compute_id, False)
            assert cursor.phase == "idle" and cursor.covered_through >= horizon + 6
    finally:
        holder.rollback()
        holder.close()
        worker.join(5)


def test_missing_cursor_reserve_recovers_expired_row_before_fresh_reservation(owner, monkeypatch):
    expired = submit(owner, key="cursor-missing-expired")
    with owner.platform.db.sessions.begin() as session:
        session.execute(update(Job).where(Job.id == expired.id).values(
            status="running", lease_until=owner.clock[0] - 1, lease_owner="old-owner",
            resolved={"compute": {"id": owner.principal.compute_id},
                      "model": {"id": "older-model"}}))
    queued = submit(owner, key="cursor-missing-queued")
    recovery_results = []
    original_recover = owner.service.recover_expired

    def observe_recovery():
        counts = original_recover()
        recovery_results.append(counts)
        return counts

    monkeypatch.setattr(owner.service, "recover_expired", observe_recovery)
    result = owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                                   operation_id="pull-without-checkpoint")
    assert result["state"] == "reserved" and result["job"]["job_id"] == queued.id
    assert recovery_results[0]["examined"] == 1
    assert recovery_results[0]["invalid"] == 1
    with owner.platform.db.sessions() as session:
        assert session.get(Job, expired.id).status == "running"
        cursor = peek_cursor(session, owner.principal.compute_id, False)
        assert cursor.phase == "idle"


def test_no_expired_owner_allows_bootstrap_reservation_without_cursor(owner):
    queued = submit(owner, key="cursor-bootstrap-queued")
    result = owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                                   operation_id="pull-bootstrap")
    assert result["state"] == "reserved"
    assert result["job"]["job_id"] == queued.id
    with owner.platform.db.sessions() as session:
        assert session.get(MetadataRow, cursor_key(owner.principal.compute_id, False)) is None


def test_existing_idempotent_reservation_replay_bypasses_fresh_cursor_guard(owner):
    first_job = submit(owner, key="cursor-replay-first")
    first = owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                                  operation_id="pull-replay-key")
    assert first["state"] == "reserved" and first["job"]["job_id"] == first_job.id
    expired = submit(owner, key="cursor-replay-expired")
    with owner.platform.db.sessions.begin() as session:
        session.execute(update(Job).where(Job.id == expired.id).values(
            status="running", lease_until=owner.clock[0] - 1, lease_owner="old-owner",
            resolved={"compute": {"id": owner.principal.compute_id},
                      "model": {"id": "older-model"}}))
    replay = owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                                   operation_id="pull-replay-key")
    assert replay == first


def test_execution_api_pull_uses_real_reserve_for_empty_bootstrap(owner, monkeypatch):
    from fastapi import FastAPI
    from types import SimpleNamespace
    from sovite.execution_api import Pull, install_routes
    from sovite.lease_service import ExecutionPrincipal

    class FakeRemoteExecution:
        def __init__(self, _platform):
            self.leases = owner.service

    import sovite.execution_api as api_module
    calls = []
    original_reserve = owner.service.reserve

    def witnessed_reserve(**kwargs):
        calls.append(kwargs)
        return original_reserve(**kwargs)

    monkeypatch.setattr(owner.service, "reserve", witnessed_reserve)
    monkeypatch.setattr(api_module, "RemoteExecution", FakeRemoteExecution)
    app = FastAPI()
    settings = SimpleNamespace(execution_tokens={"workload": "token"}, compute_id=owner.principal.compute_id)
    install_routes(app, object(), settings)
    pull_route = next(route for route in app.routes
                      if getattr(route, "path", None) == "/internal/execution/v1/pull")
    result = pull_route.endpoint(Pull(instance_id="worker-1", operation_id="pull-1"),
                                 owner.principal)
    assert result == {"state": "idle"}
    assert calls == [{"principal": ExecutionPrincipal(owner.principal.workload_id, owner.principal.compute_id),
                      "instance_id": "worker-1", "operation_id": "pull-1",
                      "external_profile": "agent-http-v1"}]
    with owner.platform.db.sessions() as session:
        assert session.get(MetadataRow, cursor_key(owner.principal.compute_id, False)) is None


def test_claim_closes_expired_work_unknown_before_reserve(work_rows, monkeypatch):
    from types import SimpleNamespace
    from sqlalchemy import select
    from sovite.db import Base, Job, WorkerExecutionLease
    from sovite.lease_service import LeaseService, execution_owner_key

    db, _, _, _ = work_rows
    with db.sessions.begin() as session:
        job = session.get(Job, "job")
        job.resolved = {**job.resolved, "compute": {"id": "fixture"}}
        expiry = session.get(WorkerExecutionLease, "job").lease_until
    import sovite.lease_service as lease_module
    monkeypatch.setattr(lease_module.time, "time", lambda: expiry + 1)
    _quiet_terminal_sweep(monkeypatch)
    service = LeaseService(SimpleNamespace(db=db, active=lambda _session: None, settings=SimpleNamespace(
        compute_id="fixture", execution_workload_identity="workload", max_attempts=3,
        lease_seconds=30)),
        execution_owner_key(workload_id="workload", instance_id="instance"), {})
    monkeypatch.setattr(service, "bind_trusted_worker", lambda **_: None)
    observed = []
    original_reserve_once = service._reserve_once

    def reserve_after_recovery(**kwargs):
        if kwargs.get("recovery_done"):
            tables = Base.metadata.tables
            with db.sessions() as session:
                job_row = session.get(Job, "job")
                reservation = session.execute(select(tables["program_reservations"])).mappings().one()
                slot = session.execute(select(tables["program_slots"])).mappings().one()
                gate = session.execute(select(tables["program_gate_state"])).mappings().one()
            observed.append((job_row.status, reservation["state"], slot["state"], gate["mode"]))
        return original_reserve_once(**kwargs)

    monkeypatch.setattr(service, "_reserve_once", reserve_after_recovery)
    assert service.claim() is None
    assert observed == [("blocked", "unknown", "unknown", "inhibited")]


@pytest.mark.parametrize("owner", ["direct"], indirect=True)
def test_direct_reserve_then_real_pull_resumes_partial_cycle_and_replays_nonce(owner, monkeypatch):
    """Direct reserve and HTTP pull resume persisted progress without a local claim."""
    from dataclasses import replace
    from test_worker_remote_boundary import admit_agent_http_owner, TOKEN
    from sovite.lease_service import LeaseService
    from sovite.api import create_app
    from sovite.execution_api import Pull

    admit_agent_http_owner(owner)
    queued = owner.platform.submit(
        "native-code-review", "synthetic bounded recovery pull", "operations",
        "submit-expired-sweep-pull", input_class="C2",
    )
    now = owner.clock[0]
    expired_id = "zzzz-expired-before-pull"
    with owner.platform.db.sessions.begin() as session:
        session.add(Job(
            id=expired_id, receipt="expired-receipt-before-pull",
            idempotency_key="expired-key-before-pull",
            request_digest="d" * 80, caller=queued.caller, grant_id=queued.grant_id,
            generation=queued.generation,
            resolved={"compute": {"id": owner.principal.compute_id},
                      "model": {"id": "prior-model"}},
            task="synthetic expired job without a current lease", status="running",
            lease_owner="expired-owner", lease_until=now - 1,
            created_at=now - 100, updated_at=now - 1, attempts=1,
        ))
    # Model a process restart after a bounded wrap scan already persisted its
    # cursor. The final candidate is still ahead of the cursor.
    with owner_transaction(owner.platform.db) as session:
        cursor = read_cursor(session, owner.principal.compute_id, False)
        assert advance_cursor(
            session, cursor, phase="wrap", last_job_id="zzzz",
            fair_pointer_job_id=expired_id, forward_upper_id=expired_id,
            scan_started_at=now, covered_through=now - 10,
        )

    # Prove that callers do not need to pass through the local Worker.claim path.
    monkeypatch.setattr(LeaseService, "claim",
                        lambda *_args, **_kwargs: pytest.fail("local claim must not be used"))
    events = []
    original_authority_lock = owner.platform.db.authority_lock

    def record_authority_lock(session):
        events.append("authority")
        return original_authority_lock(session)

    monkeypatch.setattr(owner.platform.db, "authority_lock", record_authority_lock)
    if owner.platform.db.engine.dialect.name == "postgresql":
        from sqlalchemy.orm import Session
        original_execute = Session.execute

        def record_advisory_lock(session, statement, *args, **kwargs):
            if "pg_advisory_xact_lock" in str(statement).lower():
                events.append("claim_advisory")
            return original_execute(session, statement, *args, **kwargs)

        monkeypatch.setattr(Session, "execute", record_advisory_lock)

    operation_id = "pull-expired-progress"
    first_reservation = owner.service.reserve(
        principal=owner.principal, instance_id=owner.instance,
        operation_id=operation_id, external_profile="agent-http-v1",
    )
    assert first_reservation["state"] == "reserved"
    assert first_reservation["job"]["job_id"] == queued.id
    assert "authority" in events
    if owner.platform.db.engine.dialect.name == "postgresql":
        assert events[:2] == ["authority", "claim_advisory"]
    with owner.platform.db.sessions() as session:
        cursor = peek_cursor(session, owner.principal.compute_id, False)
        assert cursor.phase == "idle"
        assert cursor.last_job_id is None
        assert session.get(Job, queued.id).status == "running"
        assert session.get(Job, expired_id).status == "running"
        assert session.scalar(select(WorkerExecutionOperation).where(
            WorkerExecutionOperation.operation_id == operation_id)) is not None

    # Replace the service object to model a remote-only process restart. The
    # HTTP route constructs its own LeaseService and resumes from stored state.
    owner.service = LeaseService(owner.platform, "restarted-remote-test", {})
    settings = replace(
        owner.platform.settings,
        execution_tokens={"actual-workload": TOKEN},
    )
    app = create_app(settings=settings, platform=owner.platform)
    pull = next(route.endpoint for route in app.routes
                if getattr(route, "path", None) == "/internal/execution/v1/pull")
    body = {"instance_id": owner.instance, "operation_id": operation_id}
    # Call the installed endpoint itself to exercise its request validation,
    # reservation service and response projection without TestClient's known
    # async-loop wait in this environment.
    reservation = pull(Pull(**body), principal=owner.principal)
    assert reservation == first_reservation
    with owner.platform.db.sessions() as session:
        cursor = peek_cursor(session, owner.principal.compute_id, False)
        assert cursor.phase == "idle"
        assert cursor.last_job_id is None
        assert cursor.covered_through == now

    replay = pull(Pull(**body), principal=owner.principal)
    assert replay == reservation


@pytest.mark.parametrize("owner", ["direct"], indirect=True)
@pytest.mark.parametrize("compute_case", [
    "local", "number", "boolean", "object", "array", "missing", "foreign",
])
def test_direct_reserve_classifies_unknown_compute_as_local_and_foreign_string_as_other(
    owner, compute_case, monkeypatch,
):
    """Only an explicit foreign JSON string is outside recovery scope."""
    queued = submit(owner, key=f"compute-classification-queue-{compute_case}")
    now = owner.clock[0]
    if compute_case == "local":
        compute = {"id": owner.principal.compute_id}
    elif compute_case == "number":
        compute = {"id": 123}
    elif compute_case == "boolean":
        compute = {"id": False}
    elif compute_case == "object":
        compute = {"id": {"untrusted": "value"}}
    elif compute_case == "array":
        compute = {"id": ["untrusted"]}
    elif compute_case == "missing":
        compute = None
    else:
        compute = {"id": "another-valid-compute"}
    resolved = {"model": {"id": "prior-model"}}
    if compute is not None:
        resolved["compute"] = compute
    expired_id = f"a-expired-compute-{compute_case}"
    with owner.platform.db.sessions.begin() as session:
        session.add(Job(
            id=expired_id, receipt=f"receipt-{compute_case}",
            idempotency_key=f"key-{compute_case}", request_digest="e" * 80,
            caller=queued.caller, grant_id=queued.grant_id, generation=queued.generation,
            resolved=resolved, task="synthetic malformed compute owner", status="running",
            lease_owner="expired-owner", lease_until=now - 1,
            created_at=now - 100, updated_at=now - 1, attempts=1,
        ))

    recovery_results = []
    original_recover = owner.service.recover_expired

    def observe_real_recovery():
        counts = original_recover()
        recovery_results.append(counts)
        return counts

    monkeypatch.setattr(owner.service, "recover_expired", observe_real_recovery)
    result = owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                                   operation_id=f"pull-compute-{compute_case}")
    assert result["state"] == "reserved" and result["job"]["job_id"] == queued.id
    if compute_case == "foreign":
        assert recovery_results == []
        with owner.platform.db.sessions() as session:
            assert peek_cursor(session, owner.principal.compute_id, False) is None
    else:
        assert len(recovery_results) == 1
        assert recovery_results[0]["examined"] == 1
        assert recovery_results[0]["invalid"] == 1
        with owner.platform.db.sessions() as session:
            assert session.get(Job, expired_id).status == "running"
            cursor = peek_cursor(session, owner.principal.compute_id, False)
            assert cursor is not None and cursor.phase == "idle"


@pytest.mark.parametrize("owner", ["direct"], indirect=True)
@pytest.mark.parametrize("duplicate_field", ["phase", "covered_through"])
def test_duplicate_raw_cursor_keys_fail_closed_in_peek_cas_reaper_and_reserve(
    owner, duplicate_field,
):
    """Duplicate JSON keys remain visible to strict cursor checks on each backend."""
    queued = submit(owner, key=f"duplicate-cursor-{duplicate_field}")
    key = cursor_key(owner.principal.compute_id, False)
    with owner_transaction(owner.platform.db) as session:
        valid_cursor = read_cursor(session, owner.principal.compute_id, False)

    common = (
        '{"schema_version":"sovite-recovery-cursor/v2",'
        f'"compute_id":"{owner.principal.compute_id}",'
        '"repository_worker_only":false,'
    )
    tail = (
        ',"last_job_id":null,"fair_pointer_job_id":null,'
        '"forward_upper_id":null,"scan_started_at":null'
    )
    if duplicate_field == "phase":
        raw = common + '"phase":"forward","phase":"idle"' + tail + ',"covered_through":null}'
        duplicate_key = '"phase"'
    else:
        raw = common + '"phase":"idle"' + tail + ',"covered_through":null,"covered_through":0}'
        duplicate_key = '"covered_through"'

    db = owner.platform.db
    metadata_table = ("\"platform_metadata\"" if db.schema is None
                      else f'"{db.schema}"."platform_metadata"')
    with db.sessions.begin() as session:
        if db.engine.dialect.name == "postgresql":
            session.execute(text(
                f"UPDATE {metadata_table} SET value = CAST(:raw AS JSON) WHERE key = :key"
            ), {"raw": raw, "key": key})
        else:
            session.execute(text(
                f"UPDATE {metadata_table} SET value = :raw WHERE key = :key"
            ), {"raw": raw, "key": key})

    with db.sessions() as session:
        if db.engine.dialect.name == "postgresql":
            storage_type = session.execute(text(
                f"SELECT pg_typeof(value)::text FROM {metadata_table} WHERE key = :key"
            ), {"key": key}).scalar_one()
            assert storage_type == "json"
        else:
            columns = session.execute(text("PRAGMA table_info(platform_metadata)")).mappings().all()
            storage_type = next(row["type"] for row in columns if row["name"] == "value")
            assert storage_type.upper() == "JSON"
        raw_stored = session.execute(text(
            f"SELECT CAST(value AS TEXT) FROM {metadata_table} WHERE key = :key"
        ), {"key": key}).scalar_one()
        assert raw_stored.count(duplicate_key) == 2
        with pytest.raises(RecoveryCursorUnavailable):
            peek_cursor(session, owner.principal.compute_id, False)

    with pytest.raises(RecoveryCursorUnavailable):
        with owner_transaction(db) as session:
            assert advance_cursor(
                session, valid_cursor, phase="idle", last_job_id=None,
                fair_pointer_job_id=None, forward_upper_id=None,
                scan_started_at=None, covered_through=1.0,
            )

    with pytest.raises(PlatformError) as sweep_error:
        owner.service.recover_expired()
    assert sweep_error.value.code == "owner_unavailable"
    assert raw_stored not in str(sweep_error.value)

    from test_execution_leases import snapshot
    before = snapshot(owner.platform)
    with pytest.raises(PlatformError) as reserve_error:
        owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                              operation_id=f"pull-{duplicate_field}")
    assert reserve_error.value.code == "owner_unavailable"
    assert raw_stored not in str(reserve_error.value)
    after = snapshot(owner.platform)
    assert after["jobs"] == before["jobs"]
    assert after["worker_execution_operations"] == before["worker_execution_operations"]
    with db.sessions() as session:
        still_raw = session.execute(text(
            f"SELECT CAST(value AS TEXT) FROM {metadata_table} WHERE key = :key"
        ), {"key": key}).scalar_one()
        assert still_raw.count(duplicate_key) == 2
    assert queued.status == "queued"


@pytest.mark.parametrize("owner", ["direct"], indirect=True)
def test_duplicate_compute_keys_are_scanned_but_explicit_foreign_compute_is_not(owner):
    """Duplicate or producer-invalid selector keys are examined, valid foreign IDs are excluded."""
    import json

    local_id = owner.principal.compute_id
    duplicate_rows = [
        ("root-foreign-local", f'{{"compute":{{"id":"foreign"}},"compute":{{"id":"{local_id}"}},"model":{{"id":"m"}}}}'),
        ("root-local-foreign", f'{{"compute":{{"id":"{local_id}"}},"compute":{{"id":"foreign"}},"model":{{"id":"m"}}}}'),
        ("nested-foreign-local", f'{{"compute":{{"id":"foreign","id":"{local_id}"}},"model":{{"id":"m"}}}}'),
        ("nested-local-foreign", f'{{"compute":{{"id":"{local_id}","id":"foreign"}},"model":{{"id":"m"}}}}'),
    ]
    now = owner.clock[0]
    stored = {}
    for label, raw in duplicate_rows:
        job = submit(owner, key=f"duplicate-compute-{label}")
        with owner.platform.db.sessions.begin() as session:
            session.execute(update(Job).where(Job.id == job.id).values(
                status="running", lease_owner="expired-owner", lease_until=now - 1,
                created_at=now - 100, updated_at=now - 1,
                resolved={"compute": {"id": local_id}, "model": {"id": "m"}},
            ))
        stored[job.id] = raw

    invalid_compute_ids = ["", "9-leading", "a" * 97, "éclair", "line\nbreak"]
    for index, invalid_compute_id in enumerate(invalid_compute_ids):
        job = submit(owner, key=f"invalid-compute-selector-{index}")
        raw = json.dumps(
            {"compute": {"id": invalid_compute_id}, "model": {"id": "m"}},
            separators=(",", ":"), ensure_ascii=False,
        )
        with owner.platform.db.sessions.begin() as session:
            session.execute(update(Job).where(Job.id == job.id).values(
                status="running", lease_owner="expired-owner", lease_until=now - 1,
                created_at=now - 100, updated_at=now - 1,
                resolved={"compute": {"id": local_id}, "model": {"id": "m"}},
            ))
        stored[job.id] = raw

    foreign = submit(owner, key="duplicate-compute-foreign-valid")
    foreign_compute_id = "foreign-valid-Compute_1"
    foreign_raw = json.dumps(
        {"compute": {"id": foreign_compute_id}, "model": {"id": "foreign-model"}},
        separators=(",", ":"),
    )
    with owner.platform.db.sessions.begin() as session:
        session.execute(update(Job).where(Job.id == foreign.id).values(
            status="running", lease_owner="foreign-owner", lease_until=now - 1,
            created_at=now - 100, updated_at=now - 1,
            resolved={"compute": {"id": foreign_compute_id}, "model": {"id": "foreign-model"}},
        ))

    db = owner.platform.db
    jobs_table = '"jobs"' if db.schema is None else f'"{db.schema}"."jobs"'
    for job_id, raw in stored.items():
        with db.sessions.begin() as session:
            if db.engine.dialect.name == "postgresql":
                session.execute(text(
                    f"UPDATE {jobs_table} SET resolved = CAST(:raw AS JSON) WHERE id = :job_id"
                ), {"raw": raw, "job_id": job_id})
            else:
                session.execute(text(
                    f"UPDATE {jobs_table} SET resolved = :raw WHERE id = :job_id"
                ), {"raw": raw, "job_id": job_id})
    with db.sessions.begin() as session:
        if db.engine.dialect.name == "postgresql":
            session.execute(text(
                f"UPDATE {jobs_table} SET resolved = CAST(:raw AS JSON) WHERE id = :job_id"
            ), {"raw": foreign_raw, "job_id": foreign.id})
        else:
            session.execute(text(
                f"UPDATE {jobs_table} SET resolved = :raw WHERE id = :job_id"
            ), {"raw": foreign_raw, "job_id": foreign.id})

    counts = owner.service.recover_expired()
    assert counts == {"examined": 9, "retired": 0, "changed": 0, "busy": 0, "invalid": 9}
    with db.sessions() as session:
        cursor = peek_cursor(session, local_id, False)
        assert cursor.phase == "idle"
        for job_id, expected_raw in stored.items():
            assert session.get(Job, job_id).status == "running"
            raw_stored = session.execute(text(
                f"SELECT CAST(resolved AS TEXT) FROM {jobs_table} WHERE id = :job_id"
            ), {"job_id": job_id}).scalar_one()
            assert raw_stored == expected_raw
        assert session.get(Job, foreign.id).status == "running"
        foreign_stored = session.execute(text(
            f"SELECT CAST(resolved AS TEXT) FROM {jobs_table} WHERE id = :job_id"
        ), {"job_id": foreign.id}).scalar_one()
        assert foreign_stored == foreign_raw


@pytest.mark.parametrize("owner", ["direct"], indirect=True)
def test_fresh_pull_retries_after_capped_expired_owner_cycle_and_ignores_bad_running_model(
    owner, monkeypatch,
):
    """A new pull resumes cap progress and reserves after ignoring an unknown model."""
    import time as wall_time
    from dataclasses import replace
    from test_worker_remote_boundary import admit_agent_http_owner, TOKEN
    from sovite.api import create_app
    from sovite.execution_api import Pull

    admit_agent_http_owner(owner)
    owner.clock[0] = max(owner.clock[0], wall_time.time() + 1)
    queued = owner.platform.submit(
        "native-code-review", "fresh pull after bounded recovery", "operations",
        "submit-fresh-pull-after-recovery", input_class="C2",
    )
    now = owner.clock[0]
    expired_ids = [f"a-expired-pull-{index:04d}" for index in range(1001)]
    with owner.platform.db.sessions.begin() as session:
        for index, job_id in enumerate(expired_ids):
            session.add(Job(
                id=job_id, receipt=f"pull-expired-receipt-{index:04d}",
                idempotency_key=f"pull-expired-key-{index:04d}",
                request_digest="d" * 80, caller=queued.caller, grant_id=queued.grant_id,
                generation=queued.generation,
                resolved={"compute": {"id": owner.principal.compute_id},
                          "model": None if index == 0 else {"id": "old-model"}},
                task="synthetic expired row for fresh pull", status="running",
                lease_owner="expired-owner", lease_until=now - 1,
                created_at=now - 100, updated_at=now - 1, attempts=1,
            ))

    # The cap and persisted cursor are exercised through the real reserve and
    # pull paths; closure is held at candidate scope so this case focuses on
    # public denial/retry, fair cursor progress, and malformed-model capacity.
    closed = []
    monkeypatch.setattr(LeaseService, "claim",
                        lambda *_args, **_kwargs: pytest.fail("local claim must not be used"))
    monkeypatch.setattr(LeaseService, "_close_owner",
                        lambda _service, job_id, *, recovery_session: closed.append(job_id) or False)
    settings = replace(owner.platform.settings, execution_tokens={"actual-workload": TOKEN})
    app = create_app(settings=settings, platform=owner.platform)
    pull = next(route.endpoint for route in app.routes
                if getattr(route, "path", None) == "/internal/execution/v1/pull")
    first_body = {"instance_id": owner.instance, "operation_id": "fresh-pull-first-pass"}
    with pytest.raises(PlatformError) as first_error:
        pull(Pull(**first_body), principal=owner.principal)
    assert first_error.value.code == "owner_unavailable"
    assert len(closed) == 1000
    with owner_transaction(owner.platform.db) as session:
        cursor = read_cursor(session, owner.principal.compute_id, False)
        assert cursor.phase == "forward" and cursor.last_job_id == expired_ids[999]
        assert session.get(Job, queued.id).status == "queued"
        assert session.scalar(select(WorkerExecutionOperation).where(
            WorkerExecutionOperation.operation_id == first_body["operation_id"])) is None

    retry_body = {"instance_id": owner.instance, "operation_id": "fresh-pull-retry-pass"}
    reservation = pull(Pull(**retry_body), principal=owner.principal)
    assert reservation["state"] == "reserved"
    assert reservation["job"]["job_id"] == queued.id
    assert len(closed) == 1001
    with owner.platform.db.sessions() as session:
        cursor = peek_cursor(session, owner.principal.compute_id, False)
        assert cursor.phase == "idle" and cursor.last_job_id is None
        assert session.get(Job, queued.id).status == "running"
        assert session.get(Job, expired_ids[0]).status == "running"
        operation = session.scalar(select(WorkerExecutionOperation).where(
            WorkerExecutionOperation.operation_id == retry_body["operation_id"]))
        assert operation is not None and operation.result["state"] == "reserved"


@pytest.mark.parametrize("owner", ["direct"], indirect=True)
@pytest.mark.parametrize("model_value", [None, "malformed-model", 7, [], {}],
                         ids=["null", "string", "number", "array", "object-empty"])
def test_direct_reserve_counts_malformed_running_model_as_unknown_and_reserves_later_job(
    owner, model_value,
):
    import time as wall_time
    owner.clock[0] = max(owner.clock[0], wall_time.time() + 1)
    expired = submit(owner, key=f"bad-running-model-{type(model_value).__name__}-{model_value!r}")
    now = owner.clock[0]
    with owner.platform.db.sessions.begin() as session:
        session.execute(update(Job).where(Job.id == expired.id).values(
            status="running", lease_owner="expired-owner", lease_until=now - 1,
            created_at=now - 100, updated_at=now - 1,
            resolved={"compute": {"id": owner.principal.compute_id}, "model": model_value},
        ))
    queued = submit(owner, key=f"queued-after-bad-model-{type(model_value).__name__}")
    counts = []
    original_recover = owner.service.recover_expired

    def observe_recovery():
        result = original_recover()
        counts.append(result)
        return result

    owner.service.recover_expired = observe_recovery
    result = owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                                   operation_id=f"direct-bad-model-{type(model_value).__name__}")
    assert result["state"] == "reserved" and result["job"]["job_id"] == queued.id
    assert counts and counts[0]["examined"] == 1 and counts[0]["invalid"] == 1
    with owner.platform.db.sessions() as session:
        assert session.get(Job, expired.id).status == "running"
        assert session.get(Job, queued.id).status == "running"


@pytest.mark.parametrize("owner", ["direct"], indirect=True)
@pytest.mark.parametrize("entrypoint", ["reserve", "pull"], ids=["direct-reserve", "installed-pull"])
@pytest.mark.parametrize("malformed_recipe", [
    "model", "connection", "grant", "input_class", "harness", "tools-object",
    "tool-item", "idempotent", "read_only",
], ids=["model", "connection", "grant", "input-class", "harness",
       "tools-object", "tool-item", "idempotent", "read-only"])
def test_expired_reserved_malformed_recipe_stays_held_and_later_reservation_progresses(
    owner, monkeypatch, entrypoint, malformed_recipe,
):
    """Malformed pre-start recipes remain unchanged without wedging fresh reserve or pull."""
    import copy
    import time as wall_time
    from dataclasses import replace

    if entrypoint == "pull":
        from test_worker_remote_boundary import admit_agent_http_owner, TOKEN
        from sovite.api import create_app
        from sovite.execution_api import Pull

        admit_agent_http_owner(owner)

        def submit_case(key):
            return owner.platform.submit(
                "native-code-review", f"reserved recovery {key}", "operations", key,
                input_class="C2",
            )
    else:
        from test_execution_leases import submit

        def submit_case(key):
            return submit(owner, key=key, task=f"reserved recovery {key}")

    candidate = submit_case(f"malformed-reserved-{entrypoint}-{malformed_recipe}")
    first = owner.service.reserve(
        principal=owner.principal, instance_id=owner.instance,
        operation_id=f"reserve-before-malformation-{entrypoint}-{malformed_recipe}",
    )
    assert first["state"] == "reserved" and first["job"]["job_id"] == candidate.id
    now = max(owner.clock[0], wall_time.time() + 1)
    owner.clock[0] = now + 2
    expiry = now - 1
    with owner.platform.db.sessions.begin() as session:
        row = session.get(Job, candidate.id)
        assert type(row.resolved) is dict
        resolved = copy.deepcopy(row.resolved)
        if malformed_recipe == "model":
            resolved["model"] = None
        elif malformed_recipe == "connection":
            resolved["model"]["connection"] = "malformed-connection"
        elif malformed_recipe == "grant":
            resolved["grant"] = None
        elif malformed_recipe == "input_class":
            resolved["grant"]["input_class"] = 17
        elif malformed_recipe == "harness":
            resolved["harness"] = "malformed-harness"
        elif malformed_recipe == "tools-object":
            resolved["tools"] = {"kind": "malformed"}
        elif malformed_recipe == "tool-item":
            resolved["tools"] = ["malformed-tool"]
        elif malformed_recipe == "idempotent":
            resolved["harness"]["idempotent"] = "yes"
        else:
            resolved["tools"] = [{"kind": "http-json", "read_only": "yes"}]
        row.resolved = resolved
        row.lease_until = expiry
        row.updated_at = expiry
        lease = session.get(WorkerExecutionLease, candidate.id)
        assert lease is not None and lease.phase == "reserved"
        lease.lease_until = expiry
        lease.updated_at = expiry
        assert lease.execution_deadline is None

    queued = submit_case(f"valid-after-malformed-reserved-{entrypoint}-{malformed_recipe}")
    monkeypatch.setattr(LeaseService, "claim",
                        lambda *_args, **_kwargs: pytest.fail("recovery must not use local claim"))
    if entrypoint == "pull":
        settings = replace(owner.platform.settings, execution_tokens={"actual-workload": TOKEN})
        app = create_app(settings=settings, platform=owner.platform)
        pull = next(route.endpoint for route in app.routes
                    if getattr(route, "path", None) == "/internal/execution/v1/pull")
        result = pull(Pull(instance_id=owner.instance,
                           operation_id=f"pull-after-malformed-{malformed_recipe}"),
                      principal=owner.principal)
    else:
        result = owner.service.reserve(
            principal=owner.principal, instance_id=owner.instance,
            operation_id=f"reserve-after-malformed-{malformed_recipe}",
        )

    assert result["state"] == "reserved" and result["job"]["job_id"] == queued.id
    with owner.platform.db.sessions() as session:
        held = session.get(Job, candidate.id)
        held_lease = session.get(WorkerExecutionLease, candidate.id)
        assert held.status == "running" and held.lease_owner is not None
        assert held.lease_until == expiry
        assert held.resolved == resolved
        assert held_lease.phase == "reserved" and held_lease.lease_until == expiry
        assert held_lease.execution_deadline is None
        assert session.get(Job, queued.id).status == "running"


@pytest.mark.parametrize("owner", ["direct"], indirect=True)
@pytest.mark.parametrize("database", ["sqlite"], indirect=True)
def test_invalid_raw_running_job_json_is_candidate_local_and_does_not_block_reserve(
    owner, database, monkeypatch,
):
    """Malformed raw SQLite JSON is isolated while a later queued job reserves."""
    import time as wall_time
    assert owner.platform.db is database
    owner.clock[0] = max(owner.clock[0], wall_time.time() + 1)
    now = owner.clock[0]
    expired = submit(owner, key="invalid-raw-running-json")
    queued = submit(owner, key="queue-after-invalid-raw-json")
    db = owner.platform.db
    jobs_table = '"jobs"' if db.schema is None else f'"{db.schema}"."jobs"'
    with db.sessions.begin() as session:
        session.execute(update(Job).where(Job.id == expired.id).values(
            status="running", lease_owner="expired-owner", lease_until=now - 1,
            created_at=now - 100, updated_at=now - 1,
            resolved={"compute": {"id": owner.principal.compute_id}, "model": None},
        ))
    raw_invalid = '{"compute":{"id":"local"},"model":'
    with db.sessions.begin() as session:
        session.execute(text(
            f"UPDATE {jobs_table} SET resolved = :raw WHERE id = :job_id"
        ), {"raw": raw_invalid, "job_id": expired.id})

    recovery_results = []
    original_recover = owner.service.recover_expired

    def observe_recovery():
        result = original_recover()
        recovery_results.append(result)
        return result

    monkeypatch.setattr(owner.service, "recover_expired", observe_recovery)
    result = owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                                   operation_id="pull-after-invalid-raw-json")
    assert result["state"] == "reserved" and result["job"]["job_id"] == queued.id
    assert recovery_results == [{"examined": 1, "retired": 0, "changed": 0,
                                 "busy": 0, "invalid": 1}]
    with db.sessions() as session:
        cursor = peek_cursor(session, owner.principal.compute_id, False)
        assert cursor.phase == "idle"
        assert session.get(Job, queued.id).status == "running"
        raw_stored, status = session.execute(text(
            f"SELECT CAST(resolved AS TEXT), status FROM {jobs_table} WHERE id = :job_id"
        ), {"job_id": expired.id}).one()
        assert raw_stored == raw_invalid and status == "running"
