"""Public sweep and reservation-barrier behavior for durable recovery cursors."""
from __future__ import annotations

import pytest
from sqlalchemy import select, text, update
from threading import Event, Thread

pytest_plugins = ("test_execution_leases", "test_work_projection_rows")

from sovite.collection_transaction import owner_transaction
from sovite.db import Database, Job, MetadataRow
from sovite.db import WorkerExecutionLease
from sovite.lease_service import LeaseService
from sovite.work_projection import _WorkProjectionBusy
from sovite.recovery_cursor import advance_cursor, cursor_key, read_cursor
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


@pytest.mark.parametrize("cursor_kind", ["incomplete", "malformed", "future"])
def test_reserve_fails_closed_on_incomplete_or_untrusted_cursor(owner, monkeypatch, cursor_kind):
    job = submit(owner, key=f"cursor-{cursor_kind}")
    now = owner.clock[0]
    key = cursor_key(owner.principal.compute_id, False)
    if cursor_kind == "incomplete":
        with owner.platform.db.sessions.begin() as session:
            session.add(MetadataRow(key=key, value={
                "schema_version": "sovite-recovery-cursor/v2",
                "compute_id": owner.principal.compute_id,
                "repository_worker_only": False,
                "phase": "forward", "last_job_id": None,
                "fair_pointer_job_id": None, "forward_upper_id": job.id,
                "scan_started_at": now, "covered_through": None,
            }))
    elif cursor_kind == "malformed":
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


def test_reserve_guard_catches_row_expiring_after_completed_horizon(owner, monkeypatch):
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
    original_authority_lock = owner.platform.db.authority_lock

    def expire_while_waiting_for_owner_lock(session):
        original_authority_lock(session)
        owner.clock[0] = horizon + 6

    monkeypatch.setattr(owner.platform.db, "authority_lock", expire_while_waiting_for_owner_lock)
    from test_execution_leases import snapshot
    before = snapshot(owner.platform)
    with pytest.raises(PlatformError) as error:
        owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                              operation_id="pull-after-horizon-expiry")
    assert error.value.code == "owner_unavailable"
    after = snapshot(owner.platform)
    assert after["jobs"] == before["jobs"]
    assert after["worker_execution_leases"] == before["worker_execution_leases"]
    assert any(row["id"] == queued.id and row["status"] == "queued" for row in after["jobs"])


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
        assert isinstance(result.get("error"), PlatformError)
        assert result["error"].code == "owner_unavailable"
        with owner.platform.db.sessions() as session:
            assert session.get(Job, queued.id).status == "queued"
    finally:
        holder.rollback()
        holder.close()
        worker.join(5)


def test_missing_cursor_still_checks_all_expired_rows_before_reserve(owner):
    expired = submit(owner, key="cursor-missing-expired")
    with owner.platform.db.sessions.begin() as session:
        session.execute(update(Job).where(Job.id == expired.id).values(
            status="running", lease_until=owner.clock[0] - 1, lease_owner="old-owner",
            resolved={"compute": {"id": owner.principal.compute_id},
                      "model": {"id": "older-model"}}))
    queued = submit(owner, key="cursor-missing-queued")
    from test_execution_leases import snapshot
    before = snapshot(owner.platform)
    with pytest.raises(PlatformError) as error:
        owner.service.reserve(principal=owner.principal, instance_id=owner.instance,
                              operation_id="pull-without-checkpoint")
    assert error.value.code == "owner_unavailable"
    after = snapshot(owner.platform)
    assert after["jobs"] == before["jobs"]
    assert any(row["id"] == queued.id and row["status"] == "queued" for row in after["jobs"])


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
    original_reserve = service.reserve

    def reserve_after_recovery(**_kwargs):
        tables = Base.metadata.tables
        with db.sessions() as session:
            job_row = session.get(Job, "job")
            reservation = session.execute(select(tables["program_reservations"])).mappings().one()
            slot = session.execute(select(tables["program_slots"])).mappings().one()
            gate = session.execute(select(tables["program_gate_state"])).mappings().one()
        observed.append((job_row.status, reservation["state"], slot["state"], gate["mode"]))
        return original_reserve(**_kwargs)

    monkeypatch.setattr(service, "reserve", reserve_after_recovery)
    assert service.claim() is None
    assert observed == [("blocked", "unknown", "unknown", "inhibited")]
