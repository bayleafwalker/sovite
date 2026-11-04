"""Producer-to-consumer shape checks for expired-owner recovery retries."""
from __future__ import annotations

import copy
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sovite.api import create_app
from sovite.db import Job, WorkerExecutionLease
from sovite.execution_api import ExecutionMutation, Pull
from sovite.lease_service import (
    ExecutionPrincipal,
    LeaseService,
    _HandBackOwnerUnavailable,
    _validate_recovery_retry_recipe,
)
from sovite.policy import bundle_generation
from sovite.workflows import compile_workflow
from test_execution_leases import owner, submit  # noqa: F401
from test_migrations import database  # noqa: F401
from test_policy_v1alpha2 import bundle as gateway_bundle  # noqa: F401
from test_workflows import mixed_graph, revision_ref


def _recipe(owner, *, key="retry-viability-source"):
    source = submit(owner, key=key)
    return copy.deepcopy(source.resolved)


def _binding(owner):
    document, bundle = mixed_graph()
    generation = bundle_generation(bundle)
    return compile_workflow(
        document,
        bundle,
        requirements_ref=revision_ref(),
        session_policy_ref=revision_ref("session-policy"),
        installation_id="retry-viability-test",
        generation=generation,
        input_class="C3",
    )


def _valid_repair():
    return {
        "parent_run_id": "parent-run",
        "root_run_id": "root-run",
        "repair_number": 1,
        "max_repairs": 3,
        "candidate_sha256": "a" * 64,
        "candidate_tip_sha": "b" * 40,
        "candidate_release_id": "candidate-release",
        "feedback_release_id": "feedback-release",
        "feedback_sha256": "c" * 64,
        "feedback_contract_ids": ["contract-one"],
        "input_key": "repair-input",
        "input_sha256": "d" * 64,
    }


def _valid_run(**overrides):
    value = {"deadline": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
    value.update(overrides)
    return value


def test_retry_recipe_producer_defaults_and_admitted_optional_shapes(owner):
    """Current producer output and valid optional metadata remain consumable."""
    resolved = _recipe(owner)
    _validate_recovery_retry_recipe(resolved)

    # Producer defaults and omitted optionals are part of the stored contract.
    for field in ("previous_attempt", "workflow_binding", "repository_run", "source_release", "transfer_id"):
        assert field not in resolved or resolved[field] is None
    without_optional_recipe_values = copy.deepcopy(resolved)
    without_optional_recipe_values["grant"].pop("timeout_seconds", None)
    without_optional_recipe_values["model"].pop("connection", None)
    _validate_recovery_retry_recipe(without_optional_recipe_values)

    for timeout in (1, 3600):
        value = copy.deepcopy(resolved)
        value["grant"]["timeout_seconds"] = timeout
        _validate_recovery_retry_recipe(value)

    for connection in ({}, {"kind": "model-gateway"}):
        value = copy.deepcopy(resolved)
        value["model"]["connection"] = connection
        _validate_recovery_retry_recipe(value)

    value = copy.deepcopy(resolved)
    value["previous_attempt"] = {
        "receipt": "rcpt_previous",
        "job_id": "previous-job",
        "submission_role": "caller",
    }
    _validate_recovery_retry_recipe(value)

    binding = _binding(owner)
    value = copy.deepcopy(resolved)
    value.update(workflow_binding=binding, workflow_role="inline_reviewer")
    _validate_recovery_retry_recipe(value)

    # The producer-compiled transfer selector has a target role and contract.
    value.update(source_release="release_01", transfer_id="inline")
    _validate_recovery_retry_recipe(value)

    value = copy.deepcopy(resolved)
    value["repository_run"] = _valid_run(repair=_valid_repair())
    _validate_recovery_retry_recipe(value)


@pytest.mark.parametrize("owner", ["direct"], indirect=True)
def test_malformed_retry_recipe_is_held_and_does_not_hide_later_reservation(owner):
    """A malformed producer-era record stays owned while later valid work reserves."""
    source = submit(owner, key="retry-viability-held")
    owner.clock[0] = max(owner.clock[0], time.time() + 1)
    first = owner.service.reserve(
        principal=owner.principal, instance_id=owner.instance, operation_id="retry-viability-pull-1"
    )
    assert first["state"] == "reserved" and first["job"]["job_id"] == source.id

    later = submit(owner, key="retry-viability-later")
    malformed = copy.deepcopy(source.resolved)
    # These fields are all read by reserve/start consumers after the row has
    # already passed initial submission. Each mutation must fail locally.
    malformed["agent"] = None
    malformed["grant"]["timeout_seconds"] = True
    malformed["model"]["connection"] = "malformed-connection"
    malformed["previous_attempt"] = {"receipt": "", "job_id": "previous-job", "submission_role": "caller"}
    now = owner.clock[0] + 10
    with owner.platform.db.sessions.begin() as session:
        row = session.get(Job, source.id)
        lease = session.get(WorkerExecutionLease, source.id)
        assert row is not None and lease is not None
        row.resolved = malformed
        row.lease_until = now - 1
        row.updated_at = now - 1
        lease.lease_until = now - 1
        lease.updated_at = now - 1
    owner.clock[0] = now

    next_reservation = owner.service.reserve(
        principal=owner.principal, instance_id=owner.instance, operation_id="retry-viability-pull-2"
    )
    assert next_reservation["state"] == "reserved"
    assert next_reservation["job"]["job_id"] == later.id

    with owner.platform.db.sessions() as session:
        held = session.get(Job, source.id)
        held_lease = session.get(WorkerExecutionLease, source.id)
        assert held.status == "running"
        assert held.lease_owner is not None and held.lease_until == now - 1
        assert held.resolved == malformed
        assert held_lease.phase == "reserved" and held_lease.lease_until == now - 1
        assert session.get(Job, later.id).status == "running"


def test_stale_workflow_generation_is_left_untouched_while_later_job_reserves(owner):
    stale = submit(owner, key="retry-stale-workflow")
    later = submit(owner, key="retry-stale-workflow-later")
    binding = _binding(owner)
    with owner.platform.db.sessions.begin() as session:
        row = session.get(Job, stale.id)
        stale_resolved = copy.deepcopy(row.resolved)
        stale_resolved.update(workflow_binding=binding, workflow_role="inline_reviewer")
        row.resolved = stale_resolved
        original_updated_at = row.updated_at
        # The synthetic binding is producer-compiled and structurally sound, but
        # it belongs to another installed generation.
    owner.clock[0] = max(owner.clock[0], time.time() + 1)
    reservation = owner.service.reserve(
        principal=owner.principal, instance_id=owner.instance,
        operation_id="retry-stale-workflow-pull",
    )
    assert reservation["state"] == "reserved" and reservation["job"]["job_id"] == later.id
    with owner.platform.db.sessions() as session:
        unchanged = session.get(Job, stale.id)
        assert unchanged.status == "queued"
        assert unchanged.resolved == stale_resolved
        assert unchanged.updated_at == original_updated_at


@pytest.mark.parametrize("owner", ["gateway"], indirect=True)
def test_refused_expired_owner_still_counts_against_same_model_limit(owner):
    """A refused owner cannot make same-model capacity appear free."""
    first_job = submit(owner, key="retry-same-model-held")
    owner.clock[0] = max(owner.clock[0], time.time() + 1)
    first = owner.service.reserve(
        principal=owner.principal, instance_id=owner.instance, operation_id="same-model-pull-1"
    )
    assert first["state"] == "reserved" and first["job"]["job_id"] == first_job.id
    later = submit(owner, key="retry-same-model-later")
    malformed = copy.deepcopy(first_job.resolved)
    malformed["agent"] = None
    now = owner.clock[0] + 10
    with owner.platform.db.sessions.begin() as session:
        row = session.get(Job, first_job.id)
        lease = session.get(WorkerExecutionLease, first_job.id)
        row.resolved = malformed
        row.lease_until = lease.lease_until = now - 1
        row.updated_at = lease.updated_at = now - 1
    owner.clock[0] = now

    idle = owner.service.reserve(
        principal=owner.principal, instance_id=owner.instance, operation_id="same-model-pull-2"
    )
    assert idle == {"state": "idle"}
    with owner.platform.db.sessions() as session:
        assert session.get(Job, first_job.id).status == "running"
        assert session.get(Job, later.id).status == "queued"


def test_installed_pull_recovers_malformed_owner_then_starts_later_job(owner, monkeypatch):
    """The real agent-http pull/start routes use recovery without a local claim."""
    from test_worker_remote_boundary import TOKEN, admit_agent_http_owner

    admit_agent_http_owner(owner)
    expired = owner.platform.submit(
        "native-code-review", "malformed expired remote recipe", "operations",
        "remote-retry-expired", input_class="C2",
    )
    owner.clock[0] = max(owner.clock[0], time.time() + 1)
    settings = replace(owner.platform.settings, execution_tokens={"actual-workload": TOKEN})
    app = create_app(settings=settings, platform=owner.platform)
    pull_route = next(route.endpoint for route in app.routes
                      if route.path == "/internal/execution/v1/pull")
    start_route = next(route.endpoint for route in app.routes
                       if route.path == "/internal/execution/v1/start")
    principal = ExecutionPrincipal("actual-workload", "local")
    monkeypatch.setattr(LeaseService, "claim", lambda *_args, **_kwargs: pytest.fail("local claim used"))

    first = pull_route(Pull(instance_id=owner.instance, operation_id="remote-retry-pull-1"),
                       principal=principal)
    assert first["state"] == "reserved" and first["job"]["job_id"] == expired.id
    later = owner.platform.submit(
        "native-code-review", "later valid remote recipe", "operations",
        "remote-retry-later", input_class="C2",
    )
    malformed = copy.deepcopy(expired.resolved)
    malformed["grant"]["timeout_seconds"] = True
    now = owner.clock[0] + 10
    with owner.platform.db.sessions.begin() as session:
        row = session.get(Job, expired.id)
        lease = session.get(WorkerExecutionLease, expired.id)
        row.resolved = malformed
        row.lease_until = lease.lease_until = now - 1
        row.updated_at = lease.updated_at = now - 1
    owner.clock[0] = now

    next_pull = pull_route(Pull(instance_id=owner.instance, operation_id="remote-retry-pull-2"),
                           principal=principal)
    assert next_pull["state"] == "reserved"
    assert next_pull["job"]["job_id"] == later.id
    started = start_route(ExecutionMutation(
        instance_id=owner.instance, operation_id="remote-retry-start-1",
        job_id=later.id, generation=next_pull["job"]["generation"],
        fence=next_pull["job"]["fence"],
    ), principal=principal)
    assert started["state"] == "started" and started["job_id"] == later.id
    with owner.platform.db.sessions() as session:
        held = session.get(Job, expired.id)
        assert held.status == "running" and held.resolved == malformed
        assert session.get(WorkerExecutionLease, expired.id).phase == "reserved"
        assert session.get(Job, later.id).attempts == 1


@pytest.mark.parametrize("mutation", [
    "agent-missing", "agent-null", "agent-scalar", "agent-id-null", "agent-id-empty",
    "timeout-bool", "timeout-string", "timeout-zero", "timeout-negative",
    "timeout-too-large", "timeout-float", "connection-null", "connection-scalar",
    "deadline-missing", "deadline-naive", "deadline-invalid", "repair-missing-field",
    "repair-bad-count", "previous-missing-job-id", "workflow-role-missing",
    "workflow-transfer-missing", "source-release-without-binding",
], ids=[
    "agent-missing", "agent-null", "agent-scalar", "agent-id-null", "agent-id-empty",
    "timeout-bool", "timeout-string", "timeout-zero", "timeout-negative",
    "timeout-too-large", "timeout-float", "connection-null", "connection-scalar",
    "deadline-missing", "deadline-naive", "deadline-invalid", "repair-missing-field",
    "repair-bad-count", "previous-missing-job-id", "workflow-role-missing",
    "workflow-transfer-missing", "source-release-without-binding",
])
def test_retry_recipe_shape_matrix_rejects_nonproducer_values(owner, mutation):
    resolved = _recipe(owner, key=f"retry-matrix-{mutation}")
    if mutation == "agent-missing":
        resolved.pop("agent")
    elif mutation == "agent-null":
        resolved["agent"] = None
    elif mutation == "agent-scalar":
        resolved["agent"] = "code-review"
    elif mutation == "agent-id-null":
        resolved["agent"]["id"] = None
    elif mutation == "agent-id-empty":
        resolved["agent"]["id"] = ""
    elif mutation.startswith("timeout-"):
        resolved["grant"]["timeout_seconds"] = {
            "bool": True, "string": "300", "zero": 0, "negative": -1,
            "too-large": 3601, "float": 1.5,
        }[mutation.removeprefix("timeout-")]
    elif mutation == "connection-null":
        resolved["model"]["connection"] = None
    elif mutation == "connection-scalar":
        resolved["model"]["connection"] = "model-gateway"
    elif mutation.startswith("deadline-"):
        run = _valid_run()
        if mutation == "deadline-missing":
            run.pop("deadline")
        elif mutation == "deadline-naive":
            run["deadline"] = "2030-01-01T00:00:00"
        else:
            run["deadline"] = "not-a-time"
        resolved["repository_run"] = run
    elif mutation.startswith("repair-"):
        repair = _valid_repair()
        if mutation == "repair-missing-field":
            repair.pop("input_sha256")
        else:
            repair["repair_number"] = True
        resolved["repository_run"] = _valid_run(repair=repair)
    elif mutation == "previous-missing-job-id":
        resolved["previous_attempt"] = {"receipt": "rcpt_previous", "submission_role": "caller"}
    elif mutation == "workflow-role-missing":
        resolved["workflow_binding"] = _binding(owner)
    elif mutation == "workflow-transfer-missing":
        resolved.update(workflow_binding=_binding(owner), workflow_role="inline_reviewer",
                         source_release="release_01", transfer_id="absent-transfer")
    elif mutation == "source-release-without-binding":
        resolved["source_release"] = "release_01"
    else:  # pragma: no cover - keeps additions to the matrix explicit
        raise AssertionError(mutation)

    with pytest.raises(_HandBackOwnerUnavailable):
        _validate_recovery_retry_recipe(resolved)
