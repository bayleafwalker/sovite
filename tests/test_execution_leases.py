"""O2 root-owned internal owner oracles; no HTTP or replacement state machine.

The backend fixture is the actual existing SQLite/disposable-PG Database.
Controlled stored-row negatives are explicitly labelled; they are not field
observations. Root owns every PostgreSQL execution and semantic mutant.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from sqlalchemy import select, text

from test_migrations import database  # noqa: F401
from test_policy_v1alpha2 import bundle as gateway_bundle  # noqa: F401
from sovite import lease_service as leases
from sovite import policy
from sovite.authority import checksum, execution_context
from sovite.db import (
    Base,
    ExecutionAuthority,
    GrantState,
    Job,
    MetadataRow,
    WorkerExecutionLease,
    WorkerExecutionOperation,
)
from sovite.service import Platform, PlatformError, job_dict
from sovite.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
LIMIT = 4 * 1024 * 1024


class AuditFault(RuntimeError):
    pass


DIGEST_VERSION = "sovite-worker-execution-operation/v1"
_UNSET = object()


def reference_bytes(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def reference_digest(value):
    return hashlib.sha256(reference_bytes(value)).hexdigest()


def snapshot(platform):
    with platform.db.engine.connect() as connection:
        return {
            table.name: [
                dict(row)
                for row in connection.execute(
                    select(table).order_by(*table.primary_key.columns)
                ).mappings()
            ]
            for table in Base.metadata.sorted_tables
        }


def refused(code, call, *args, status=None, **kwargs):
    with pytest.raises(PlatformError) as caught:
        call(*args, **kwargs)
    assert caught.value.code == code
    if status is not None:
        assert caught.value.status == status
    assert "PRIVATE-O2-SENTINEL" not in str(caught.value)
    return caught.value


@pytest.fixture(params=["direct", "gateway"])
def owner(request, database, gateway_bundle, tmp_path, monkeypatch):  # noqa: F811
    # Detect editable-install contamination before any owner/database fixture work.
    assert Path(leases.__file__).resolve() == ROOT / "sovite/lease_service.py"
    bundle = (
        copy.deepcopy(gateway_bundle)
        if request.param == "gateway"
        else policy.load_bundle(ROOT / "config/platform.local.yaml")[0]
    )
    bundle["mode"] = "development"
    path = tmp_path / "owner-bundle.yaml"
    path.write_text(yaml.safe_dump(bundle))
    settings = Settings(
        database_url=database.engine.url.render_as_string(hide_password=False),
        database_schema=database.schema,
        bundle_path=str(path),
        artifact_path=str(tmp_path / "artifacts"),
        require_qualification=False,
        execution_workload_identity="legacy-global",
        lease_seconds=30,
        job_timeout_seconds=120,
    )
    platform = Platform(settings)
    platform.db.engine.dispose()
    platform.db = database
    platform.db.migrate()
    platform.initialize()
    principal = leases.ExecutionPrincipal("actual-workload", "local")
    service = leases.LeaseService(platform, "unbound-owner-service", {})
    clock = [time.time() + 1]
    monkeypatch.setattr(leases, "time", SimpleNamespace(time=lambda: clock[0]))
    return SimpleNamespace(
        platform=platform,
        service=service,
        principal=principal,
        clock=clock,
        model=request.param,
        bundle=bundle,
        instance="instance-1",
    )


def submit(owner, *, key="submit-1", task="PRIVATE-O2-SENTINEL naïve — β"):
    return owner.platform.submit("incident-forensics", task, "operations", key)


def pull(owner, *, operation="pull-1", instance=_UNSET, service=None, principal=None):
    owner.clock[0] = max(owner.clock[0], time.time() + 1)
    return (service or owner.service).reserve(
        principal=principal or owner.principal,
        instance_id=owner.instance if instance is _UNSET else instance,
        operation_id=operation,
    )


def context(owner, reservation, *, operation="start-1", instance=None):
    job = reservation["job"]
    return leases.ExecutionContext(
        instance or owner.instance,
        operation,
        job["job_id"],
        job["generation"],
        job["fence"],
    )


def start(owner, reservation, *, operation="start-1", instance=None):
    return owner.service.start(
        principal=owner.principal,
        context=context(owner, reservation, operation=operation, instance=instance),
    )


def reopen(owner):
    # Reuse the same actual Database after a new Platform initialization. No
    # create_app/TestClient/service is constructed here.
    fresh = Platform(owner.platform.settings)
    fresh.db.engine.dispose()
    fresh.db = owner.platform.db
    fresh.initialize()
    return leases.LeaseService(fresh, "reopened-unbound-service", {})


def test_owner_fixture_actual_schema6_submit_and_authorize_control(owner):
    job = submit(owner)
    with owner.platform.db.sessions() as session:
        assert session.get(MetadataRow, "schema_version").value == {"version": 6}
        assert (
            owner.platform.authorize(
                session, job.grant_id, job.caller, generation=job.generation
            )["generation"]
            == job.generation
        )
        assert session.get(WorkerExecutionLease, job.id) is None
        assert session.get(ExecutionAuthority, job.id) is None
    assert owner.platform.db.verify_audit()["valid"]


@pytest.mark.parametrize(
    "kind,request_value,expected",
    [
        (
            "pull",
            {"instance_id": "instance-1", "operation_id": "pull-1"},
            "6def1742f65e2c9554e465cf74a65a1fa49946291a2448b9e5f917276167e90b",
        ),
        (
            "start",
            {
                "instance_id": "instance-1",
                "operation_id": "start-1",
                "job_id": "job-1",
                "generation": "sha256:" + "a" * 64,
                "fence": "0123456789abcdef0123456789abcdef",
            },
            "2b495125d31748ff9903c6d95a098a170f4b3cde7bab5688ee87a435a4f9b663",
        ),
    ],
)
def test_operation_digest_independent_fixed_vectors(kind, request_value, expected):
    principal = leases.ExecutionPrincipal("worker", "local")
    reference = {
        "schema_version": DIGEST_VERSION,
        "operation_kind": kind,
        "workload_id": "worker",
        "compute_id": "local",
        "request": request_value,
    }
    assert reference_digest(reference) == expected
    assert (
        leases.operation_request_digest(
            operation_kind=kind, principal=principal, request=request_value
        )
        == expected
    )
    assert (
        leases.operation_request_digest(
            operation_kind=kind,
            principal=principal,
            request=dict(reversed(list(request_value.items()))),
        )
        == expected
    )


def test_reserve_then_first_start_changes_only_owned_state(owner):
    job = submit(owner)
    prior = owner.platform.get_job(job.id)
    reservation = pull(owner)
    assert set(reservation) == {"state", "job"} and reservation["state"] == "reserved"
    view = reservation["job"]
    assert set(view) == {
        "job_id",
        "generation",
        "fence",
        "lease_until",
        "task",
        "resolved",
    }
    assert (view["job_id"], view["generation"], view["task"], view["resolved"]) == (
        job.id,
        job.generation,
        job.task,
        job.resolved,
    )
    assert len(reference_bytes(reservation)) <= LIMIT
    assert len(view["fence"]) == 32 and all(
        ch in "0123456789abcdef" for ch in view["fence"]
    )
    with owner.platform.db.sessions() as session:
        row = session.get(Job, job.id)
        lease = session.get(WorkerExecutionLease, job.id)
        assert (row.status, row.attempts, row.started_at) == ("running", 0, None)
        assert row.lease_until == lease.lease_until == view["lease_until"]
        assert lease.phase == "reserved" and lease.execution_deadline is None
        assert (lease.workload_id, lease.instance_id, lease.fence) == (
            owner.principal.workload_id,
            owner.instance,
            view["fence"],
        )
        expected_owner = "execution:" + reference_digest(
            {
                "schema_version": "sovite-worker-execution-owner/v1",
                "workload_id": owner.principal.workload_id,
                "instance_id": owner.instance,
            }
        )
        assert row.lease_owner == expected_owner
        assert session.get(ExecutionAuthority, job.id) is None
        journal = session.get(
            WorkerExecutionOperation,
            (owner.principal.workload_id, owner.instance, "pull-1"),
        )
        assert journal.operation_kind == "pull"
        assert (journal.job_id, journal.fence) == (job.id, view["fence"])
        assert journal.request_digest == reference_digest(
            {
                "schema_version": DIGEST_VERSION,
                "operation_kind": "pull",
                "workload_id": owner.principal.workload_id,
                "compute_id": owner.principal.compute_id,
                "request": {"instance_id": owner.instance, "operation_id": "pull-1"},
            }
        )
        assert journal.result == {
            "state": "reserved",
            "job_id": job.id,
            "generation": job.generation,
            "fence": view["fence"],
            "lease_until": view["lease_until"],
            "task_sha256": hashlib.sha256(job.task.encode()).hexdigest(),
            "resolved_sha256": reference_digest(job.resolved),
        }
    result = start(owner, reservation)
    assert result["state"] == "started"
    assert (result["job_id"], result["generation"], result["fence"]) == (
        job.id,
        job.generation,
        view["fence"],
    )
    expected_keys = {
        "state",
        "job_id",
        "generation",
        "fence",
        "lease_until",
        "deadline",
    }
    assert set(result) == expected_keys | (
        {"execution"} if owner.model == "gateway" else set()
    )
    assert result["deadline"] == owner.clock[0] + min(
        job.resolved["grant"].get("timeout_seconds", 120), 120
    )
    with owner.platform.db.sessions() as session:
        row = session.get(Job, job.id)
        lease = session.get(WorkerExecutionLease, job.id)
        assert (row.attempts, row.started_at, lease.phase) == (
            1,
            owner.clock[0],
            "started",
        )
        assert lease.execution_deadline == result["deadline"]
        assert (row.task, row.resolved, row.public_projection, row.artifact_key) == (
            prior.task,
            prior.resolved,
            None,
            None,
        )
        journal = session.get(
            WorkerExecutionOperation,
            (owner.principal.workload_id, owner.instance, "start-1"),
        )
        assert journal.operation_kind == "start"
        assert journal.request_digest == reference_digest(
            {
                "schema_version": DIGEST_VERSION,
                "operation_kind": "start",
                "workload_id": owner.principal.workload_id,
                "compute_id": owner.principal.compute_id,
                "request": {
                    "instance_id": owner.instance,
                    "operation_id": "start-1",
                    "job_id": job.id,
                    "generation": job.generation,
                    "fence": view["fence"],
                },
            }
        )
        assert journal.result == {
            key: value for key, value in result.items() if key != "execution"
        }
        authority = session.get(ExecutionAuthority, job.id)
        if owner.model == "gateway":
            assert set(result["execution"]) == {"capability", "context"}
            assert authority.fence == lease.fence == result["fence"]
            assert (
                authority.context
                == result["execution"]["context"]
                == execution_context(row, owner.principal.workload_id)
            )
            assert authority.capability_digest == checksum(
                result["execution"]["capability"]
            )
        else:
            assert authority is None
    assert owner.platform.db.verify_audit()["valid"]


@pytest.mark.parametrize("private_field", ["task", "resolved"])
def test_reopened_pull_reconstructs_same_reservation_and_never_claims_second(
    owner, private_field
):
    first, second = submit(owner), submit(owner, key="submit-2")
    reservation = pull(owner)
    before = snapshot(owner.platform)
    assert pull(owner, service=reopen(owner)) == reservation
    assert snapshot(owner.platform) == before
    assert owner.platform.get_job(second.id).status == "queued"
    assert reservation["job"]["job_id"] == first.id
    # Guarded reconstruction binds private bytes without storing them in journal.
    with owner.platform.db.sessions.begin() as session:
        row = session.get(Job, first.id)
        if private_field == "task":
            row.task = "changed private bytes"
        else:
            changed = copy.deepcopy(row.resolved)
            changed["agent"]["instructions"] = "changed private recipe bytes"
            row.resolved = changed
    before = snapshot(owner.platform)
    refused("execution_fenced", pull, owner, service=reopen(owner), status=409)
    assert snapshot(owner.platform) == before


def test_first_start_only_bearer_and_no_execution_on_any_current_replay(owner):
    submit(owner)
    reserved = pull(owner)
    first = start(owner, reserved)
    before = snapshot(owner.platform)
    expected = {
        "state": "already_started",
        **{k: first[k] for k in ("job_id", "generation", "fence")},
    }
    assert start(owner, reserved) == expected
    assert snapshot(owner.platform) == before
    replay = owner.service.start(
        principal=owner.principal,
        context=context(owner, reserved, operation="start-other-key"),
    )
    assert replay == expected and "execution" not in replay
    with owner.platform.db.sessions() as session:
        assert session.get(Job, first["job_id"]).attempts == 1
    if owner.model == "gateway":
        secret = first["execution"]["capability"]
        if secret in json.dumps(snapshot(owner.platform), ensure_ascii=False):
            pytest.fail("raw bearer persisted in owner state")
        if secret in str(
            job_dict(owner.platform.get_job(first["job_id"]), detail=True)
        ):
            pytest.fail("raw bearer exposed in job projection")
    # Deliberately lose the first response and reopen. Reconciliation cannot remint.
    assert (
        reopen(owner).start(principal=owner.principal, context=context(owner, reserved))
        == expected
    )
    before = snapshot(owner.platform)
    refused("execution_fenced", pull, owner, status=409)
    assert snapshot(owner.platform) == before


def test_same_operation_key_changed_kind_or_server_compute_conflicts(owner):
    submit(owner)
    reserved = pull(owner)
    before = snapshot(owner.platform)
    refused(
        "idempotency_conflict",
        pull,
        owner,
        principal=replace(owner.principal, compute_id="foreign-compute"),
        status=409,
    )
    refused(
        "idempotency_conflict",
        owner.service.start,
        principal=owner.principal,
        context=context(owner, reserved, operation="pull-1"),
        status=409,
    )
    assert snapshot(owner.platform) == before


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("job_id", "unknown-job"),
        ("generation", "sha256:" + "0" * 64),
        ("fence", "0" * 32),
        ("instance_id", "foreign-instance"),
    ],
)
def test_start_foreign_or_stale_context_has_no_effect(owner, field, replacement):
    submit(owner)
    reserved = pull(owner)
    ctx = replace(context(owner, reserved), **{field: replacement})
    before = snapshot(owner.platform)
    expected = (
        "execution_scope_denied"
        if field in {"job_id", "instance_id"}
        else "execution_fenced"
    )
    refused(expected, owner.service.start, principal=owner.principal, context=ctx)
    assert snapshot(owner.platform) == before


def test_foreign_workload_is_not_permitted_by_same_instance_or_job(owner):
    submit(owner)
    reserved = pull(owner)
    before = snapshot(owner.platform)
    refused(
        "execution_scope_denied",
        owner.service.start,
        principal=replace(owner.principal, workload_id="foreign-workload"),
        context=context(owner, reserved),
        status=403,
    )
    assert snapshot(owner.platform) == before


@pytest.mark.parametrize(
    "mutation,code,status",
    [
        ("grant-inhibited", "grant_inhibited", 403),
        ("qualification-required", "qualification_required", 403),
        ("cancelled", "cancelled", 409),
        ("owner-outage", "owner_unavailable", 503),
    ],
)
@pytest.mark.parametrize("phase", ["reserved", "started"])
def test_current_authority_is_checked_at_first_start_and_replay(
    owner, monkeypatch, mutation, code, status, phase
):
    job = submit(owner)
    reserved = pull(owner)
    with owner.platform.db.sessions() as session:
        assert (
            owner.platform.authorize(session, job.grant_id, job.caller)["generation"]
            == job.generation
        )
    if phase == "started":
        assert start(owner, reserved)["state"] == "started"
    if mutation == "grant-inhibited":
        # Controlled owner-row negative isolates grant currentness: unlike the
        # public inhibit operation, it does not also set cancel_requested and
        # let that unrelated predicate mask a missing authority recheck.
        with owner.platform.db.sessions.begin() as session:
            session.get(GrantState, job.grant_id).inhibited = True
    elif mutation == "qualification-required":
        owner.platform.settings.require_qualification = True
    elif mutation == "cancelled":
        owner.platform.cancel(job.id)
    else:

        def unavailable(*args, **kwargs):
            raise PlatformError("owner_unavailable", "Owner unavailable", 503)

        monkeypatch.setattr(owner.platform, "active", unavailable)
    before = snapshot(owner.platform)
    refused(code, start, owner, reserved, status=status)
    assert snapshot(owner.platform) == before


@pytest.mark.parametrize("replay_operation", ["pull-1", "new-pull"])
def test_idle_is_observed_empty_not_unavailable_owner(
    owner, monkeypatch, replay_operation
):
    assert pull(owner) == {"state": "idle"}
    before = snapshot(owner.platform)
    assert pull(owner, service=reopen(owner)) == {"state": "idle"}
    assert snapshot(owner.platform) == before

    def unavailable(*args, **kwargs):
        raise PlatformError("owner_unavailable", "Owner unavailable", 503)

    monkeypatch.setattr(owner.platform, "active", unavailable)
    before = snapshot(owner.platform)
    refused("owner_unavailable", pull, owner, operation=replay_operation, status=503)
    assert snapshot(owner.platform) == before


@pytest.mark.parametrize("owner", ["gateway"], indirect=True)
@pytest.mark.parametrize("phase", ["reserved", "started"])
def test_gateway_binding_check_requires_no_online_helper(owner, phase):
    job = submit(owner)
    reserved = pull(owner)
    if phase == "started":
        first = start(owner, reserved)
        assert first["state"] == "started"  # No helper session was ever created.
    owner.platform.inhibit_binding(job.resolved["model"]["id"], "Fixture owner stop")
    assert owner.platform.get_job(job.id).cancel_requested is False
    before = snapshot(owner.platform)
    refused("binding_inhibited", start, owner, reserved, status=403)
    assert snapshot(owner.platform) == before


def test_payload_utf8_whole_response_cap_precedes_all_reservation_effects(owner):
    # Genuine accepted programmatic policy: each name/description satisfies the
    # existing16000-character bound. The expanded selected recipe exceeds4MiB
    # UTF8 even though every component is valid. No corrupt-row shortcut masks
    # the response-byte guard.
    bundle = copy.deepcopy(owner.bundle)
    agent_id = next(
        g["agent"] for g in bundle["grants"] if g["id"] == "incident-forensics"
    )
    agent = next(a for a in bundle["agents"] if a["id"] == agent_id)
    if owner.model == "direct":
        bundle["models"][0].update(
            adapter="ollama", model="fixture-model", endpoint="http://localhost:11434"
        )
        bundle["harnesses"][0]["kind"] = "tool-agent"
    tool_ids = []
    for index in range(100):
        tool = {
            "id": f"oversize-tool-{index}",
            "name": "β" * 16000,
            "description": "β" * 16000,
            "kind": "kubernetes-read",
            "input_class": "C3",
            "output_class": "C3",
            "destination": "private-runtime",
            "allowed_operations": ["list-pods"],
            "read_only": True,
        }
        tool_ids.append(tool["id"])
        bundle["tools"].append(tool)
    agent["tools"] = tool_ids
    generation = owner.platform.compile(bundle)
    owner.platform.activate(generation)
    job = submit(owner)
    reference = {
        "state": "reserved",
        "job": {
            "job_id": job.id,
            "generation": job.generation,
            "fence": "a" * 32,
            "lease_until": time.time(),
            "task": job.task,
            "resolved": job.resolved,
        },
    }
    assert len(reference_bytes(reference)) > LIMIT
    before = snapshot(owner.platform)
    refused("execution_payload_too_large", pull, owner, status=413)
    assert snapshot(owner.platform) == before
    assert owner.platform.get_job(job.id).status == "queued"


def test_audit_failure_rolls_back_reserved_and_started_owner_state(owner, monkeypatch):
    submit(owner)
    original = owner.platform.db.audit

    def broken(session, *args, **kwargs):
        session.flush()  # Prove the effect exists in this very transaction.
        if session.scalar(select(WorkerExecutionLease)) is None:
            pytest.fail("audit fault occurred before a lease effect")
        raise AuditFault("injected-audit-failure")

    before = snapshot(owner.platform)
    monkeypatch.setattr(owner.platform.db, "audit", broken)
    with pytest.raises(AuditFault):
        pull(owner)
    assert snapshot(owner.platform) == before
    monkeypatch.setattr(owner.platform.db, "audit", original)
    reserved = pull(owner)
    before = snapshot(owner.platform)

    def broken_start(session, *args, **kwargs):
        session.flush()
        lease = session.get(WorkerExecutionLease, reserved["job"]["job_id"])
        assert lease.phase == "started"
        assert session.get(Job, lease.job_id).attempts == 1
        raise AuditFault("injected-start-audit-failure")

    monkeypatch.setattr(owner.platform.db, "audit", broken_start)
    with pytest.raises(AuditFault):
        start(owner, reserved)
    assert snapshot(owner.platform) == before


def test_reserved_expiry_reclaims_without_attempt_and_changes_fence(owner):
    job = submit(owner)
    reserved = pull(owner)
    owner.clock[0] = reserved["job"]["lease_until"] + 1
    owner.service.recover_expired()  # Explicit maintenance baseline.
    row = owner.platform.get_job(job.id)
    assert row.attempts == 0 and row.started_at is None
    assert row.status in {"queued", "retry_wait"}
    replacement = pull(owner, operation="pull-2")
    assert replacement["job"]["fence"] != reserved["job"]["fence"]
    before = snapshot(owner.platform)
    refused("execution_fenced", start, owner, reserved, status=409)
    assert snapshot(owner.platform) == before
    assert start(owner, replacement, operation="start-2")["state"] == "started"
    assert owner.platform.get_job(job.id).attempts == 1


@pytest.mark.parametrize("value", [True, "has:colon", None, "", "a" * 65])
def test_invalid_instance_refuses_before_database_effect(owner, value):
    before = snapshot(owner.platform)
    refused("invalid_request", pull, owner, instance=value, status=422)
    assert snapshot(owner.platform) == before


def test_trusted_binding_requires_hash_of_complete_service_worker_id(owner):
    worker_id = "compute:host:12345:opaque-worker-instance"
    service = leases.LeaseService(owner.platform, worker_id, {})
    instance = hashlib.sha256(worker_id.encode()).hexdigest()
    service.bind_trusted_worker(principal=owner.principal, instance_id=instance)
    job = submit(owner)
    reserved = pull(owner, instance=instance, service=service)
    assert owner.platform.get_job(job.id).lease_owner == worker_id
    assert reserved["job"]["job_id"] == job.id
    foreign = leases.LeaseService(owner.platform, worker_id, {})
    before = snapshot(owner.platform)
    refused(
        "execution_scope_denied",
        foreign.bind_trusted_worker,
        principal=owner.principal,
        instance_id="0" * 64,
        status=403,
    )
    assert snapshot(owner.platform) == before


def _pg_race(owner, monkeypatch, first, second, *, allow_refusal=False):
    """Witness actual DB contention before releasing the winning transaction."""
    assert owner.platform.db.engine.dialect.name == "postgresql"
    original = owner.platform.db.authority_lock
    held, entered, release = threading.Event(), threading.Event(), threading.Event()
    pids, results = {}, []

    def locked(session):
        name = threading.current_thread().name
        if name in {"o2-winner", "o2-loser"}:
            pids[name] = session.execute(text("SELECT pg_backend_pid()")).scalar_one()
        if name == "o2-loser":
            entered.set()
        original(session)
        if name == "o2-winner":
            held.set()
            assert release.wait(10)

    monkeypatch.setattr(owner.platform.db, "authority_lock", locked)

    def contender(call):
        try:
            results.append(call())
        except Exception as exc:
            results.append(exc)

    winner = threading.Thread(
        name="o2-winner", target=contender, args=(first,), daemon=True
    )
    loser = threading.Thread(
        name="o2-loser", target=contender, args=(second,), daemon=True
    )
    try:
        winner.start()
        assert held.wait(5)
        loser.start()
        assert entered.wait(5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with owner.platform.db.engine.connect() as conn:
                waiting = conn.execute(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=:pid AND NOT granted)"
                    ),
                    {"pid": pids["o2-loser"]},
                ).scalar_one()
            if waiting:
                break
            assert loser.is_alive(), "contender finished before a real lock wait"
        else:
            pytest.fail("contender did not reach actual PostgreSQL lock wait")
    finally:
        release.set()
        for thread in (winner, loser):
            if thread.ident is not None:
                thread.join(10)
    assert not winner.is_alive() and not loser.is_alive()
    if not allow_refusal:
        assert all(isinstance(value, dict) for value in results), (
            "owner contender failed"
        )
    return results


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
@pytest.mark.parametrize("population", ["one-job", "model-limit"])
def test_pg_real_lock_wait_two_pullers_preserve_concurrency(
    owner, monkeypatch, population
):
    if population == "model-limit":
        bundle = copy.deepcopy(owner.bundle)
        bundle["models"][0]["max_concurrency"] = 1
        owner.platform.activate(owner.platform.compile(bundle))
    submit(owner)
    if population == "model-limit":
        submit(owner, key="second-job")
    results = _pg_race(
        owner,
        monkeypatch,
        lambda: pull(owner, instance="first", operation="same-key"),
        lambda: pull(owner, instance="second", operation="same-key"),
    )
    assert sorted(value["state"] for value in results) == ["idle", "reserved"]
    with owner.platform.db.sessions() as session:
        assert len(session.scalars(select(WorkerExecutionLease)).all()) == 1
        assert all(row.attempts == 0 for row in session.scalars(select(Job)))


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
@pytest.mark.parametrize("keys", ["same-key", "different-key"])
def test_pg_real_lock_wait_concurrent_starts_issue_once(owner, monkeypatch, keys):
    job = submit(owner)
    reservation = pull(owner)
    results = _pg_race(
        owner,
        monkeypatch,
        lambda: start(owner, reservation, operation="first-start"),
        lambda: start(
            owner,
            reservation,
            operation="first-start" if keys == "same-key" else "second-start",
        ),
    )
    assert sorted(value["state"] for value in results) == ["already_started", "started"]
    assert sum("execution" in value for value in results) == (
        1 if owner.model == "gateway" else 0
    )
    with owner.platform.db.sessions() as session:
        assert session.get(Job, job.id).attempts == 1
        assert session.get(WorkerExecutionLease, job.id).phase == "started"


def test_minimum_valid_ascii_instance_is_not_coerced_or_overrestricted(owner):
    assert pull(owner, instance="1") == {"state": "idle"}


def test_private_recipe_mutation_of_response_does_not_mutate_durable_job(owner):
    job = submit(owner)
    reservation = pull(owner)
    reservation["job"]["resolved"]["agent"]["instructions"] = "client-only mutation"
    assert owner.platform.get_job(job.id).resolved == job.resolved
    assert pull(owner)["job"]["resolved"] == job.resolved


@pytest.mark.parametrize(
    "field,value",
    [
        ("instance_id", True),
        ("operation_id", None),
        ("job_id", False),
        ("generation", "not-a-generation"),
        ("fence", "not-a-uuid"),
    ],
)
def test_invalid_context_types_refuse_before_effect(owner, field, value):
    submit(owner)
    reservation = pull(owner)
    bad = replace(context(owner, reservation), **{field: value})
    before = snapshot(owner.platform)
    refused(
        "invalid_request",
        owner.service.start,
        principal=owner.principal,
        context=bad,
        status=422,
    )
    assert snapshot(owner.platform) == before


def test_reserve_has_no_implicit_retention_or_recovery_side_effect(owner, monkeypatch):
    submit(owner)

    def forbidden(*args, **kwargs):
        pytest.fail(
            "reserve invoked implicit maintenance outside its transaction baseline"
        )

    monkeypatch.setattr(leases, "sweep_terminal_authority", forbidden)
    monkeypatch.setattr(owner.service, "recover_expired", forbidden)
    assert pull(owner)["state"] == "reserved"


def test_actual_missing_active_generation_is_not_observed_idle(owner):
    assert pull(owner) == {"state": "idle"}
    with owner.platform.db.sessions.begin() as session:
        session.delete(session.get(MetadataRow, "active_generation"))
    before = snapshot(owner.platform)
    refused("not_initialized", pull, owner, operation="after-corruption", status=503)
    assert snapshot(owner.platform) == before


def test_precommit_fault_rolls_back_owner_and_no_response_escapes(owner):
    from sqlalchemy import event

    submit(owner)
    before = snapshot(owner.platform)

    def fail_commit(session):
        session.flush()
        assert session.scalar(select(WorkerExecutionLease)) is not None
        assert session.scalar(select(WorkerExecutionOperation)) is not None
        raise AuditFault("injected-before-commit")

    event.listen(owner.platform.db.sessions, "before_commit", fail_commit)
    try:
        with pytest.raises(AuditFault):
            pull(owner)
    finally:
        event.remove(owner.platform.db.sessions, "before_commit", fail_commit)
    assert snapshot(owner.platform) == before


def test_postcommit_response_fault_reopens_without_capability_recovery(owner):
    submit(owner)
    reserved = pull(owner)
    try:
        result = start(owner, reserved)
        assert result["state"] == "started"
        raise AuditFault("injected-after-owner-commit-before-delivery")
    except AuditFault:
        pass
    with owner.platform.db.sessions() as session:
        assert session.get(Job, reserved["job"]["job_id"]).attempts == 1
        assert (
            session.get(WorkerExecutionLease, reserved["job"]["job_id"]).phase
            == "started"
        )
    current = snapshot(owner.platform)
    replay = reopen(owner).start(
        principal=owner.principal, context=context(owner, reserved)
    )
    assert replay == {
        "state": "already_started",
        **{key: reserved["job"][key] for key in ("job_id", "generation", "fence")},
    }
    assert snapshot(owner.platform) == current


def test_execution_owner_key_hashes_complete_scope_without_truncation():
    workload = "w" * 160
    instance = "i" * 64
    expected = "execution:" + reference_digest(
        {
            "schema_version": "sovite-worker-execution-owner/v1",
            "workload_id": workload,
            "instance_id": instance,
        }
    )
    assert (
        leases.execution_owner_key(workload_id=workload, instance_id=instance)
        == expected
    )
    assert len(expected) == 74
    assert (
        leases.execution_owner_key(workload_id="x" + workload[1:], instance_id=instance)
        != expected
    )
    assert (
        leases.execution_owner_key(workload_id=workload, instance_id="x" + instance[1:])
        != expected
    )


def test_started_retry_preserves_first_started_at_and_new_attempt_fence(owner):
    job = submit(owner)
    first = pull(owner)
    assert start(owner, first)["state"] == "started"
    original = owner.platform.get_job(job.id)
    owner.clock[0] = first["job"]["lease_until"] + 1
    owner.service.recover_expired()
    assert owner.platform.get_job(job.id).status == "retry_wait"
    second = pull(owner, operation="second-pull")
    assert second["job"]["fence"] != first["job"]["fence"]
    assert owner.platform.get_job(job.id).attempts == 1
    assert start(owner, second, operation="second-start")["state"] == "started"
    row = owner.platform.get_job(job.id)
    assert row.attempts == 2 and row.started_at == original.started_at


def test_known_403_dispatch_denial_blocks_candidate_but_is_not_outage(owner):
    job = submit(owner)
    following = owner.platform.submit(
        "change-assessment",
        "following eligible fixture",
        "operations",
        "following-eligible",
        # Current-main flow admission requires an explicit class within this
        # second grant's ceiling, rather than defaulting to the bundle maximum.
        input_class=next(g["input_class"] for g in owner.bundle["grants"]
                         if g["id"] == "change-assessment"),
    )
    assert following.resolved["input_class"] == next(
        g["input_class"] for g in owner.bundle["grants"] if g["id"] == "change-assessment"
    )
    assert job.created_at < following.created_at
    with owner.platform.db.sessions.begin() as session:
        assert (
            owner.platform.authorize(session, job.grant_id, job.caller)["generation"]
            == job.generation
        )
        # Isolate dispatch's current grant check from Platform.inhibit's separate
        # eager queued-job transition. This is a controlled owner-row fixture.
        session.get(GrantState, job.grant_id).inhibited = True
    assert owner.platform.get_job(job.id).cancel_requested is False
    selected = pull(owner)
    assert selected["state"] == "reserved"
    assert selected["job"]["job_id"] == following.id
    row = owner.platform.get_job(job.id)
    assert (row.status, row.error_code, row.attempts) == (
        "blocked",
        "grant_inhibited",
        0,
    )
    with owner.platform.db.sessions() as session:
        assert session.get(WorkerExecutionLease, job.id) is None
        assert session.get(ExecutionAuthority, job.id) is None
    assert owner.platform.db.verify_audit()["valid"]


def test_owner_outage_with_queued_candidate_cannot_block_it_or_record_idle(
    owner, monkeypatch
):
    submit(owner)

    def unavailable(*args, **kwargs):
        raise PlatformError("owner_unavailable", "Owner unavailable", 503)

    monkeypatch.setattr(owner.platform, "active", unavailable)
    before = snapshot(owner.platform)
    refused("owner_unavailable", pull, owner, status=503)
    assert snapshot(owner.platform) == before


def test_first_start_cannot_choose_another_compute_from_same_workload(owner):
    submit(owner)
    reservation = pull(owner)
    before = snapshot(owner.platform)
    refused(
        "execution_scope_denied",
        owner.service.start,
        principal=replace(owner.principal, compute_id="foreign-compute"),
        context=context(owner, reservation),
        status=403,
    )
    assert snapshot(owner.platform) == before


@pytest.mark.parametrize("change", ["unknown", "null", "bool", "nonfinite"])
def test_operation_digest_rejects_invalid_original_request_without_coercion(change):
    request = {"instance_id": "instance-1", "operation_id": "pull-1"}
    if change == "unknown":
        request["compute_id"] = "caller-chosen"
    elif change == "null":
        request["instance_id"] = None
    elif change == "bool":
        request["operation_id"] = True
    else:
        request["instance_id"] = float("nan")
    refused(
        "invalid_request",
        leases.operation_request_digest,
        operation_kind="pull",
        principal=leases.ExecutionPrincipal("worker", "local"),
        request=request,
        status=422,
    )


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
@pytest.mark.parametrize("control", ["cancel", "inhibit"])
@pytest.mark.parametrize("first", ["start", "control"])
def test_pg_real_lock_wait_control_and_start_have_one_serial_order(
    owner, monkeypatch, control, first
):
    job = submit(owner)
    reserved = pull(owner)

    def change_control():
        if control == "cancel":
            owner.platform.cancel(job.id)
        else:
            owner.platform.inhibit(job.grant_id, "Owner race fixture inhibit")
        return {"state": "control-changed"}

    def launch():
        return start(owner, reserved)

    calls = (launch, change_control) if first == "start" else (change_control, launch)
    results = _pg_race(owner, monkeypatch, *calls, allow_refusal=True)
    row = owner.platform.get_job(job.id)
    assert row.cancel_requested and row.public_projection is None
    if first == "start":
        assert all(isinstance(value, dict) for value in results)
        assert sorted(value["state"] for value in results) == [
            "control-changed",
            "started",
        ]
        assert row.attempts == 1
    else:
        refusals = [value for value in results if isinstance(value, PlatformError)]
        assert len(refusals) == 1
        assert refusals[0].code in (
            {"cancelled"} if control == "cancel" else {"cancelled", "grant_inhibited"}
        )
        assert sum(isinstance(value, dict) for value in results) == 1
        assert row.attempts == 0 and row.started_at is None
        with owner.platform.db.sessions() as session:
            assert session.get(ExecutionAuthority, job.id) is None


@pytest.mark.parametrize("corruption", ["phase", "infinite-lease"])
def test_corrupt_owner_rows_refuse_without_fabricating_phase_or_deadline(
    owner, corruption
):
    job = submit(owner)
    reserved = pull(owner)
    # Controlled stored-row negatives, never a production repair or producer
    # history. Both canonical scheduling copies agree for the infinite case,
    # so a mere mismatch predicate cannot mask missing finite-time validation.
    with owner.platform.db.sessions.begin() as session:
        lease = session.get(WorkerExecutionLease, job.id)
        if corruption == "phase":
            lease.phase = "unknown"
        else:
            lease.lease_until = float("inf")
            session.get(Job, job.id).lease_until = float("inf")
    before = snapshot(owner.platform)
    refused("owner_unavailable", start, owner, reserved, status=503)
    assert snapshot(owner.platform) == before


def test_trusted_renew_keeps_job_and_universal_lease_coherent_with_fixed_deadline(
    owner,
):
    from sovite.worker import Worker

    submitted = submit(owner)
    worker = Worker(owner.platform)
    job = worker.claim()
    assert job.id == submitted.id and owner.platform.get_job(job.id).status == "running"
    with owner.platform.db.sessions() as session:
        lease = session.get(WorkerExecutionLease, job.id)
        assert lease is not None, "renewal needs the universal lease capability"
        previous_until = lease.lease_until
        deadline = lease.execution_deadline
        identity = (lease.workload_id, lease.instance_id, lease.fence, lease.phase)
        assert session.get(Job, job.id).lease_until == previous_until
        if owner.model == "gateway":
            assert session.get(ExecutionAuthority, job.id).deadline == deadline
    owner.clock[0] += 5
    assert worker.renew(job.id) is True
    with owner.platform.db.sessions() as session:
        lease = session.get(WorkerExecutionLease, job.id)
        assert (
            session.get(Job, job.id).lease_until == lease.lease_until > previous_until
        )
        assert lease.execution_deadline == deadline
        assert (
            lease.workload_id,
            lease.instance_id,
            lease.fence,
            lease.phase,
        ) == identity
        if owner.model == "gateway":
            assert session.get(ExecutionAuthority, job.id).deadline == deadline


@pytest.mark.parametrize("owner", ["gateway"], indirect=True)
@pytest.mark.parametrize("withdrawal", ["digest", "expiry", "fence"])
def test_withdrawn_gateway_refresh_is_false_and_extends_neither_scheduling_copy(
    owner, withdrawal
):
    from sovite.worker import Worker

    submit(owner)
    worker = Worker(owner.platform)
    job = worker.claim()
    with owner.platform.db.sessions() as session:
        assert session.get(WorkerExecutionLease, job.id) is not None, (
            "withdrawal oracle needs a universal lease"
        )
    owner.clock[0] += 1
    assert worker.renew(job.id) is True  # Independent successful refresh control.
    with owner.platform.db.sessions.begin() as session:
        row = session.get(ExecutionAuthority, job.id)
        assert row.capability_digest and row.expires_at > owner.clock[0]
        if withdrawal == "digest":
            row.capability_digest = ""
        elif withdrawal == "expiry":
            row.expires_at = 0
        else:
            row.fence = "0" * 32
    before = snapshot(owner.platform)
    owner.clock[0] += 1
    assert worker.renew(job.id) is False
    assert snapshot(owner.platform) == before


@pytest.mark.parametrize("action", ["renew", "shutdown"])
def test_same_instance_old_execute_cannot_renew_or_hand_back_replacement(owner, action):
    import asyncio
    from sovite.adapters import RuntimeFailure
    from sovite.worker import Worker

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        checks = []

        class HeldRunner:
            authorization_check = None

            async def run(self, recipe, task, job_id):
                entered.set()
                await release.wait()
                if action == "renew":
                    try:
                        self.authorization_check()
                    except RuntimeFailure as exc:
                        checks.append(exc.code)
                        raise
                    checks.append("incorrectly-authorized-old-task")
                return {"synthetic": True, "answer": "old task"}

        submit(owner)
        worker = Worker(owner.platform, HeldRunner())
        old = worker.claim()
        with owner.platform.db.sessions() as session:
            lease = session.get(WorkerExecutionLease, old.id)
            assert lease is not None, "captured-fence race needs the universal lease"
            old_fence = lease.fence
        running = asyncio.create_task(worker.execute(old))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            with owner.platform.db.sessions.begin() as session:
                session.get(Job, old.id).lease_until = owner.clock[0] - 1
                session.get(WorkerExecutionLease, old.id).lease_until = (
                    owner.clock[0] - 1
                )
            worker.recover_expired()
            replacement = worker.claim()
            assert replacement.id == old.id and replacement.execution_fence != old_fence
            before = snapshot(owner.platform)
            if action == "shutdown":
                worker.stop.set()  # Actual execute shutdown path must use old fence.
            else:
                release.set()  # Actual authorization_check must use old fence.
            await asyncio.wait_for(running, 3)
            after = snapshot(owner.platform)
            for table in ("jobs", "worker_execution_leases", "execution_authority"):
                assert after[table] == before[table]
            if action == "renew":
                assert checks == ["authorization_revoked"]
        finally:
            release.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("owner", ["gateway"], indirect=True)
@pytest.mark.parametrize(
    "restriction,code",
    [("inhibited", "binding_inhibited"), ("disabled", "binding_disabled")],
)
def test_reserve_binding_denial_blocks_only_denied_candidate_and_continues(
    owner, restriction, code, monkeypatch
):
    bundle = copy.deepcopy(owner.bundle)
    first_agent = next(
        agent
        for agent in bundle["agents"]
        if agent["id"] == bundle["grants"][0]["agent"]
    )
    first_model = next(
        model for model in bundle["models"] if model["id"] == first_agent["model"]
    )
    following_model = copy.deepcopy(first_model)
    following_model["id"] = "following-model"
    bundle["models"].append(following_model)
    following_agent = copy.deepcopy(first_agent)
    following_agent.update(id="following-agent", model="following-model")
    bundle["agents"].append(following_agent)
    following_grant = copy.deepcopy(
        next(g for g in bundle["grants"] if g["id"] == "incident-forensics")
    )
    following_grant.update(id="following-grant", agent="following-agent")
    bundle["grants"].append(following_grant)
    if restriction == "disabled":
        first_model["enabled"] = False  # Genuine valid authored configuration.
    generation = owner.platform.compile(bundle)
    owner.platform.activate(generation)
    if restriction == "disabled":
        # Current-main admission rejects disabled models before enqueue. Prove
        # that rule, then reproduce only the prior owner's enqueue behavior
        # for a legacy queued row; all later owner checks remain real.
        before = snapshot(owner.platform)
        refused("binding_disabled", submit, owner, status=403)
        assert snapshot(owner.platform) == before
        actual_check = policy.check_role_input

        def legacy_admission_check(authored, grant_id, selected):
            legacy = copy.deepcopy(authored)
            next(m for m in legacy["models"] if m["id"] == first_model["id"])["enabled"] = True
            return actual_check(legacy, grant_id, selected)

        with monkeypatch.context() as legacy_setup:
            legacy_setup.setattr(policy, "check_role_input", legacy_admission_check)
            denied = submit(owner)
    else:
        denied = submit(owner)
    following = owner.platform.submit(
        "following-grant", "following eligible task", "operations", "binding-following"
    )
    assert denied.created_at < following.created_at
    with owner.platform.db.sessions() as session:
        if restriction == "disabled":
            refused("binding_disabled", owner.platform.authorize, session,
                    denied.grant_id, denied.caller, status=403)
        else:
            assert owner.platform.authorize(session, denied.grant_id, denied.caller)["generation"] == generation
        assert owner.platform.authorize_existing_job(session, following)["generation"] == generation
    if restriction == "inhibited":
        owner.platform.inhibit_binding(first_model["id"], "Reserve binding fixture")
    assert owner.platform.get_job(denied.id).cancel_requested is False
    selected = pull(owner)
    assert selected["state"] == "reserved" and selected["job"]["job_id"] == following.id
    row = owner.platform.get_job(denied.id)
    assert (row.status, row.error_code, row.attempts, row.cancel_requested) == (
        "blocked",
        code,
        0,
        False,
    )
    with owner.platform.db.sessions() as session:
        assert session.get(WorkerExecutionLease, denied.id) is None
        assert session.get(ExecutionAuthority, denied.id) is None


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
@pytest.mark.parametrize("first", ["start", "grant-only-writer"])
def test_pg_grant_only_writer_isolates_authorization_from_cancellation(
    owner, monkeypatch, first
):
    job = submit(owner)
    reserved = pull(owner)

    def grant_only_writer():
        # Clearly labelled controlled owner-row fault writer: deliberately no
        # public inhibit/cancel side effect, to discriminate the grant predicate.
        with owner.platform.db.sessions.begin() as session:
            owner.platform.db.authority_lock(session)
            session.get(GrantState, job.grant_id).inhibited = True
            owner.platform.db.audit(
                session,
                "controlled-fixture",
                "fixture.grant_only_inhibited",
                job.grant_id,
            )
        return {"state": "grant-only-inhibited"}

    def launch():
        return start(owner, reserved)

    calls = (
        (launch, grant_only_writer) if first == "start" else (grant_only_writer, launch)
    )
    results = _pg_race(owner, monkeypatch, *calls, allow_refusal=True)
    row = owner.platform.get_job(job.id)
    assert row.cancel_requested is False
    if first == "start":
        assert all(isinstance(value, dict) for value in results)
        assert sorted(value["state"] for value in results) == [
            "grant-only-inhibited",
            "started",
        ]
        assert row.attempts == 1
    else:
        refusals = [value for value in results if isinstance(value, PlatformError)]
        assert len(refusals) == 1 and refusals[0].code == "grant_inhibited"
        assert row.attempts == 0 and row.started_at is None
        with owner.platform.db.sessions() as session:
            assert session.get(ExecutionAuthority, job.id) is None


def _whole_response_boundary_fixture(owner, *, complete_bytes):
    """Genuine compiled private inputs, with independent exact UTF8 accounting.

    The actual accepted baseline Job supplies every metadata/recipe field. Only
    valid policy display strings supply padding; source generation and UUID IDs
    retain fixed widths. Neither task nor recipe individually exceeds its cap.
    """
    owner.clock[0] = (
        2_000_000_000.125  # Fixed finite Unix time makes metadata byte width explicit.
    )
    task = "t" * 60_000
    assert len(task.encode()) < owner.platform.settings.max_request_bytes
    bundle = copy.deepcopy(owner.bundle)
    agent_id = next(
        g["agent"] for g in bundle["grants"] if g["id"] == "incident-forensics"
    )
    agent = next(a for a in bundle["agents"] if a["id"] == agent_id)
    if owner.model == "direct":
        bundle["models"][0].update(
            adapter="ollama", model="fixture-model", endpoint="http://localhost:11434"
        )
        bundle["harnesses"][0]["kind"] = "tool-agent"
    tools = []
    for index in range(100):
        tool = {
            "id": f"boundary-tool-{index}",
            "name": "",
            "description": "",
            "kind": "kubernetes-read",
            "input_class": "C3",
            "output_class": "C3",
            "destination": "private-runtime",
            "allowed_operations": ["list-pods"],
            "read_only": True,
        }
        tools.append(tool)
        bundle["tools"].append(tool)
    agent["tools"] = [tool["id"] for tool in tools]
    owner.platform.activate(owner.platform.compile(bundle))
    baseline = submit(owner, key=f"boundary-baseline-{complete_bytes}", task=task)

    def response_for(job):
        return {
            "state": "reserved",
            "job": {
                "job_id": job.id,
                "generation": job.generation,
                "fence": "a" * 32,
                "lease_until": owner.clock[0] + owner.platform.settings.lease_seconds,
                "task": job.task,
                "resolved": job.resolved,
            },
        }

    remaining = complete_bytes - len(reference_bytes(response_for(baseline)))
    assert remaining > 0
    for tool in tools:
        for field in ("name", "description"):
            count = min(16000, remaining // 2)
            value = "β" * count
            if remaining % 2 and count < 16000:
                value += "x"
            tool[field] = value
            remaining -= len(value.encode())
    assert remaining == 0, "declared valid policy padding capacity was insufficient"
    owner.platform.activate(owner.platform.compile(bundle))
    job = submit(owner, key=f"boundary-final-{complete_bytes}", task=task)
    reference = response_for(job)
    assert len(reference_bytes(job.resolved)) < LIMIT
    assert len(job.task.encode()) < owner.platform.settings.max_request_bytes
    assert len(reference_bytes(reference)) == complete_bytes
    assert (
        owner.platform.get_job(baseline.id).status == "blocked"
    )  # Prior generation, not a candidate.
    return job, reference


@pytest.mark.parametrize("extra_byte", [0, 1])
def test_complete_response_exact_cap_and_plus_one_with_individually_valid_private_inputs(
    owner, extra_byte
):
    job, expected = _whole_response_boundary_fixture(
        owner, complete_bytes=LIMIT + extra_byte
    )
    before = snapshot(owner.platform)
    if extra_byte:
        refused("execution_payload_too_large", pull, owner, status=413)
        assert snapshot(owner.platform) == before
        assert owner.platform.get_job(job.id).status == "queued"
    else:
        response = pull(owner)
        assert response["state"] == "reserved" and response["job"]["job_id"] == job.id
        assert response["job"]["task"] == expected["job"]["task"]
        assert response["job"]["resolved"] == expected["job"]["resolved"]
        assert len(reference_bytes(response)) == LIMIT


def test_boundary_fixture_compiles_individually_valid_inputs_with_exact_full_size_control(
    owner,
):
    # This setup control runs even while reserve/start are stubs. It cannot
    # substitute for the missing positive whole-response owner transition above.
    for target in (LIMIT, LIMIT + 1):
        job, reference = _whole_response_boundary_fixture(owner, complete_bytes=target)
        assert len(reference_bytes(reference)) == target
        assert len(reference_bytes(job.resolved)) < LIMIT
        assert len(job.task.encode()) < owner.platform.settings.max_request_bytes


def _private_hash_reference_job(owner):
    task = "naïve — β"
    assert task.encode().hex() == "6e61c3af766520e2809420ceb2"
    assert (
        hashlib.sha256(task.encode()).hexdigest()
        == "a19aad37b3b79391fd7818f8eb049d7c586721448e5c0bdd37938aa35c89975a"
    )
    job = submit(owner, key="private-hash-vector", task=task)
    # Main's class-aware policy adds exactly these declared selection fields.
    # Independently qualified genuine old/current submissions establish both
    # full preimages; removing only the two additions must retain the old hash.
    assert job.resolved["input_class"] == "C3"
    assert job.resolved["selection"] == {
        "generation": job.generation, "grant_id": "incident-forensics",
        "harness_id": "assessment-harness", "model_id": "assessment-model",
        "reason": None, "reason_source": "not_supplied",
    }
    legacy = {k: v for k, v in job.resolved.items() if k not in {"input_class", "selection"}}
    previous = {
        "direct": "3b7ea048eba615fd8b0832d7d2d828054ee9f53565010796226f91c76e06734c",
        "gateway": "60f69c3279fe7d248cd3234248d272bc733d30aca5465b789de81f1e5e0cb9d0",
    }
    assert reference_digest(legacy) == previous[owner.model]
    expected = {
        "direct": "e43a6295b1320a8c3ee9f1f959af3ffd601f6ca8e1ca5f91e28be6ccd950c61b",
        "gateway": "870e50b9c7a3b98d0ccd98a8c786ba8bb46972e5e7b6adf6089bc9d25a1e8f67",
    }
    assert reference_digest(job.resolved) == expected[owner.model]
    return job, expected[owner.model]


def test_private_hash_reference_controls_on_actual_accepted_owner_inputs(owner):
    _private_hash_reference_job(owner)


def test_private_hash_vectors_bind_journal_and_equal_recipe_key_orders(owner):
    job, recipe_sha = _private_hash_reference_job(owner)
    reserved = pull(owner)
    with owner.platform.db.sessions() as session:
        journal = session.get(
            WorkerExecutionOperation,
            (owner.principal.workload_id, owner.instance, "pull-1"),
        )
        assert (
            journal.result["task_sha256"]
            == "a19aad37b3b79391fd7818f8eb049d7c586721448e5c0bdd37938aa35c89975a"
        )
        assert journal.result["resolved_sha256"] == recipe_sha

    def reverse_keys(value):
        if isinstance(value, dict):
            return {
                key: reverse_keys(item) for key, item in reversed(list(value.items()))
            }
        if isinstance(value, list):
            return [reverse_keys(item) for item in value]
        return value

    reordered = reverse_keys(job.resolved)
    assert reference_digest(reordered) == recipe_sha
    with owner.platform.db.sessions.begin() as session:
        session.get(Job, job.id).resolved = reordered
    assert pull(owner, service=reopen(owner)) == reserved
    with owner.platform.db.sessions.begin() as session:
        session.get(
            Job, job.id
        ).task = "nai\u0308ve — β"  # Same visible text, different original bytes.
    before = snapshot(owner.platform)
    refused("execution_fenced", pull, owner, service=reopen(owner), status=409)
    assert snapshot(owner.platform) == before
