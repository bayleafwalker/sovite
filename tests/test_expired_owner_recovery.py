"""Synthetic expired Work-owner recovery contract."""
import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from sovite.collection_transaction import owner_transaction
from sovite.db import Base, BundleRevision, Job, WorkerExecutionLease
from sovite.lease_service import LeaseService, _HandBackOwnerUnavailable, execution_owner_key
from sovite.policy import bundle_generation, resolve_recipe, validate_bundle
from test_policy_v1alpha2 import bundle as gateway_bundle  # noqa: F401
from test_work_projection_rows import work_rows


def _producer_retry_state(base_bundle, recipe_variant):
    bundle = copy.deepcopy(base_bundle)
    old_compute = bundle["grants"][0]["compute"]
    bundle["compute"][0]["id"] = "fixture"
    for grant in bundle["grants"]:
        if grant["compute"] == old_compute:
            grant["compute"] = "fixture"
    grant = bundle["grants"][0]
    agent = next(item for item in bundle["agents"] if item["id"] == grant["agent"])
    harness = next(item for item in bundle["harnesses"] if item["id"] == agent["harness"])
    if recipe_variant == "idempotent-harness":
        harness["idempotent"] = True
    elif recipe_variant == "read-only-tool":
        tool_id = "recovery-retry-read-only"
        bundle["tools"].append({
            "id": tool_id, "name": "Recovery retry read-only probe",
            "description": "A producer-backed read-only replay tool.",
            "kind": "kubernetes-read", "input_class": grant["input_class"],
            "output_class": grant["output_class"],
            "destination": bundle["compute"][0]["destination"],
            "allowed_operations": ["list-pods"], "read_only": True,
        })
        agent["tools"].append(tool_id)
    elif recipe_variant == "default-max-attempts-and-flags":
        for name in ("max_attempts", "timeout_seconds", "on_outage"):
            grant.pop(name, None)
        harness.pop("idempotent", None)
    else:
        raise AssertionError(recipe_variant)
    bundle = validate_bundle(bundle)
    generation = bundle_generation(bundle)
    return bundle, generation, grant["id"], resolve_recipe(bundle, grant["id"])


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
    ("harness.idempotent", "truthy-string"),
    ("tool.read_only", "truthy-string"),
    ("attempts", "zero"), ("attempts", "negative"),
    ("grant.max_attempts", "string"), ("grant.max_attempts", "boolean"),
    ("grant.max_attempts", "zero"), ("grant.max_attempts", "negative"),
    ("grant.max_attempts", "too-large"), ("grant.max_attempts", "float"),
], ids=["harness-missing", "harness-null", "harness-scalar",
       "grant-missing", "grant-null", "grant-scalar", "idempotent-truthy-string",
       "read-only-truthy-string", "attempts-zero", "attempts-negative",
       "max-attempts-string", "max-attempts-boolean",
       "max-attempts-zero", "max-attempts-negative", "max-attempts-too-large",
       "max-attempts-float"])
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
                "agent": {"id": "synthetic-agent"},
            "model": {"id": "synthetic-model"},
            "harness": {"kind": "tool-agent", "idempotent": False},
            "grant": {"max_attempts": 3},
            "tools": [],
        }
        if field in {"attempts", "harness.idempotent", "tool.read_only", "grant.max_attempts"}:
            if field == "harness.idempotent":
                resolved["harness"] = {"kind": "native-codex", "idempotent": "yes"}
            elif field == "tool.read_only":
                resolved["harness"] = {"kind": "tool-agent", "idempotent": False}
                resolved["tools"] = [{"kind": "http-json", "read_only": "yes"}]
            elif field == "grant.max_attempts":
                bad_values = {
                    "string": "3", "boolean": True, "zero": 0,
                    "negative": -1, "too-large": 11, "float": 3.0,
                }
                resolved["grant"]["max_attempts"] = bad_values[shape]
        elif shape == "missing":
            resolved.pop(field)
        elif shape == "null":
            resolved[field] = None
        else:
            resolved[field] = "malformed-scalar"
        session.add(Job(id="aaa", receipt="malformed-replay-fields",
                        idempotency_key=f"malformed-{field}-{shape}",
                        request_digest="x", caller=later.caller, grant_id=later.grant_id,
                        generation=later.generation, resolved=resolved,
                        task="synthetic malformed replay field", status="running",
                        attempts=(0 if field == "attempts" and shape == "zero" else
                                  -1 if field == "attempts" else 1),
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


@pytest.mark.parametrize("replay_recipe", [
    "idempotent-harness", "read-only-tool", "default-max-attempts-and-flags",
])
def test_sweep_preserves_valid_idempotent_and_read_only_replay_recipes(
    work_rows, monkeypatch, gateway_bundle, replay_recipe,
):
    """Producer-backed replay-safe recipe forms still allow a bounded retry."""
    import sovite.lease_service as lease_module
    db, _, _, _ = work_rows
    bundle, generation, grant_id, resolved = _producer_retry_state(gateway_bundle, replay_recipe)
    with db.sessions.begin() as session:
        later = session.get(Job, "job")
        later.resolved = {**later.resolved, "compute": {"id": "fixture"}}
        expiry = session.get(WorkerExecutionLease, "job").lease_until
        session.add(Job(id="aaa", receipt="valid-replay-recipe",
                        idempotency_key="valid-replay-recipe", request_digest="x",
                        caller=later.caller, grant_id=grant_id,
                        generation=generation, resolved=resolved,
                        task="synthetic replay safe", status="running", attempts=1,
                        lease_owner=later.lease_owner, lease_until=expiry))
        session.add(BundleRevision(generation=generation, bundle=bundle))
        session.flush()
        session.add(WorkerExecutionLease(
            job_id="aaa", workload_id="workload", instance_id="instance",
            fence="3" * 32, generation=generation, phase="started",
            lease_until=expiry, execution_deadline=expiry, created_at=1, updated_at=1,
        ))
    monkeypatch.setattr(lease_module.time, "time", lambda: expiry + 1)
    monkeypatch.setattr(lease_module, "sweep_terminal_authority", lambda *_: None)
    service = LeaseService(SimpleNamespace(db=db, settings=SimpleNamespace(
        compute_id="fixture", max_attempts=3)),
        execution_owner_key(workload_id="workload", instance_id="instance"), {})
    counts = service.recover_expired()
    assert counts["examined"] == 2 and counts["retired"] == 2
    with db.sessions() as session:
        replayed = session.get(Job, "aaa")
        assert (replayed.status, replayed.error_code, replayed.attempts) == (
            "retry_wait", "lease_expired", 1)
        assert session.get(WorkerExecutionLease, "aaa").phase == "finished"
        assert session.get(Job, "job").status == "blocked"


@pytest.mark.parametrize("phase", ["reserved", "started"])
def test_empty_repository_recovery_respects_lease_phase(work_rows, monkeypatch, gateway_bundle, phase):
    from test_work_projection_rows import _clear_work_history
    import sovite.lease_service as lease_module
    db, _, _, _ = work_rows
    _clear_work_history(db)
    bundle, generation, grant_id, producer_recipe = _producer_retry_state(
        gateway_bundle, "default-max-attempts-and-flags"
    )
    with db.sessions.begin() as session:
        job = session.get(Job, "job")
        run = dict(job.resolved["repository_run"])
        run["deadline"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        job.resolved = {**producer_recipe, "repository_run": run}
        job.generation = generation
        job.grant_id = grant_id
        lease = session.get(WorkerExecutionLease, "job")
        expiry = lease.lease_until
        lease.generation = generation
        lease.phase = phase
        if phase == "reserved":
            lease.execution_deadline = None
            job.attempts = 0
        session.add(BundleRevision(generation=generation, bundle=bundle))
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
