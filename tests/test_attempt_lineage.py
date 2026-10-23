"""A new binding cannot discharge uncertain effects or import prior artifacts."""
import json

import pytest

from sovite.db import Job
from sovite.service import PlatformError
from test_result_access import installed, complete
from test_workflow_admission import request
from test_workflow_admission import installed as mixed_installed, complete_source


def make_terminal(platform, status="failed", *, uncertain=False, replay_safe=True):
    job = platform.submit("incident-forensics", "prior private task", "operations", "prior", input_class="C1")
    with platform.db.sessions.begin() as session:
        row = session.get(Job, job.id)
        row.status, row.attempts = status, 1
        row.error_code = "execution_uncertain" if uncertain else "test_failure"
        if not replay_safe:
            row.resolved = {**row.resolved, "harness": {"kind": "agent-http", "idempotent": False}}
    return platform.get_job(job.id)


def test_previous_attempt_is_private_lineage_without_artifact_io(installed, monkeypatch):
    platform, _ = installed
    previous = make_terminal(platform)
    monkeypatch.setattr(platform.store, "get", lambda *_: pytest.fail("lineage must not fetch artifacts"))
    next_job = platform.submit("change-assessment", "new explicitly supplied task", "operations", "next",
        input_class="C1", previous_attempt=previous.receipt, selection_reason="Use the other qualified binding")
    assert next_job.task == "new explicitly supplied task"
    assert next_job.resolved["previous_attempt"]["receipt"] == previous.receipt
    assert next_job.resolved["selection"]["reason"] == "Use the other qualified binding"
    assert next_job.resolved["selection"]["grant_id"] == "change-assessment"
    assert "source_release" not in next_job.resolved
    assert "previous_attempt" not in platform.public_status(next_job.receipt, "operations")


@pytest.mark.parametrize("status", ["queued", "running", "retry_wait"])
def test_previous_attempt_must_have_stopped(installed, status):
    platform, _ = installed
    previous = make_terminal(platform, status)
    with pytest.raises(PlatformError) as exc:
        platform.submit("change-assessment", "retry", "operations", "next", input_class="C1",
                        previous_attempt=previous.receipt)
    assert exc.value.code == "previous_attempt_unfinished"


@pytest.mark.parametrize("status,uncertain,safe", [
    ("blocked", True, True), ("failed", False, False), ("cancelled", False, False),
])
def test_changing_binding_does_not_resolve_uncertain_effects(installed, status, uncertain, safe):
    platform, _ = installed
    previous = make_terminal(platform, status, uncertain=uncertain, replay_safe=safe)
    with pytest.raises(PlatformError) as exc:
        platform.submit("change-assessment", "retry", "operations", "next", input_class="C1",
                        previous_attempt=previous.receipt)
    assert exc.value.code == "previous_effect_uncertain"


def test_lineage_cannot_enumerate_foreign_receipts(installed):
    platform, _ = installed
    previous = make_terminal(platform)
    codes = []
    for receipt in (previous.receipt, "rcpt_missing"):
        with pytest.raises(PlatformError) as exc:
            platform.submit("incident-forensics", "retry", "cloud-codex", "next", previous_attempt=receipt)
        codes.append((exc.value.code, exc.value.status, exc.value.message))
    assert codes[0] == codes[1]


def test_same_idempotency_key_cannot_switch_bindings_or_reason(installed):
    platform, _ = installed
    first = platform.submit("incident-forensics", "task", "operations", "same", input_class="C1",
                            selection_reason="First selection")
    assert platform.submit("incident-forensics", "task", "operations", "same", input_class="C1",
                           selection_reason="First selection").id == first.id
    for grant, reason in (("change-assessment", "First selection"), ("incident-forensics", "Changed selection")):
        with pytest.raises(PlatformError) as exc:
            platform.submit(grant, "task", "operations", "same", input_class="C1", selection_reason=reason)
        assert exc.value.code == "idempotency_conflict"


def test_stale_discovery_generation_is_refused(installed):
    platform, _ = installed
    with pytest.raises(PlatformError) as exc:
        platform.submit("incident-forensics", "task", "cloud-codex", "new", expected_generation="sha256:" + "0" * 64)
    assert exc.value.code == "revision_superseded"


def test_mcp_records_rationale_without_changing_reference_return(installed):
    platform, bundle = installed
    from test_result_access import configure
    configure(platform, bundle, return_contract="reference")
    response = request(platform, "POST", "/mcp", token=platform.settings.caller_tokens["operations"],
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "begin_task", "arguments": {
            "grant_id": "incident-forensics", "task": "task", "idempotency_key": "mcp-selection",
            "selection_reason": "PRIVATE-PREFERENCE-CANARY", "expected_generation": platform.active()[1]}}})
    result = response.json()["result"]
    assert result["isError"] is False
    assert set(result["structuredContent"]) == {"receipt", "status"}
    assert "PRIVATE-PREFERENCE-CANARY" not in response.text


@pytest.mark.parametrize("value", ["", " ", "x" * 501, "\ud800", {}, 123])
def test_selection_reason_is_bounded_private_text(installed, value):
    platform, _ = installed
    with pytest.raises(PlatformError) as exc:
        platform.submit("incident-forensics", "task", "cloud-codex", "new", selection_reason=value)
    assert exc.value.code == "invalid_selection_reason"


def test_lineage_never_releases_private_source_to_a_public_role(mixed_installed):
    platform, binding = mixed_installed
    source = complete_source(platform, binding)
    with pytest.raises(PlatformError) as exc:
        platform.submit_workflow_role(binding, "inline_reviewer", "guessing prior content",
            "workflow-consumer", "public-retry", previous_attempt=source.receipt)
    assert exc.value.code == "output_release_required"


def test_lineage_uncertainty_is_rechecked_at_dispatch(installed):
    platform, _ = installed
    previous = make_terminal(platform)
    child = platform.submit("change-assessment", "task", "operations", "next", input_class="C1",
                            previous_attempt=previous.receipt)
    with platform.db.sessions.begin() as session:
        session.get(Job, previous.id).error_code = "execution_uncertain"
    from sovite.worker import Worker
    assert Worker(platform).claim() is None
    assert platform.get_job(child.id).error_code == "previous_effect_uncertain"


def test_exact_replay_recovers_owned_receipt_after_inhibit(installed):
    platform, _ = installed
    first = platform.submit("incident-forensics", "task", "cloud-codex", "replay", selection_reason="reason")
    platform.inhibit("incident-forensics", "paused")
    replay = platform.submit("incident-forensics", "task", "cloud-codex", "replay", selection_reason="reason")
    assert replay.id == first.id
    assert platform.public_status(first.receipt, "cloud-codex")["status"] == "unavailable"


def test_completed_prior_with_removed_grant_is_lineage_only(installed):
    platform, bundle = installed
    previous = make_terminal(platform, "completed")
    bundle["grants"] = [grant for grant in bundle["grants"] if grant["id"] != "incident-forensics"]
    import yaml
    from pathlib import Path
    Path(platform.settings.bundle_path).write_text(yaml.safe_dump(bundle))
    platform.reload(platform.settings.bundle_path)
    child = platform.submit("change-assessment", "new task", "operations", "next", input_class="C1",
                            previous_attempt=previous.receipt)
    assert child.resolved["previous_attempt"]["receipt"] == previous.receipt
    from sovite.worker import Worker
    assert Worker(platform).claim().id == child.id


def test_replay_cannot_cross_operator_caller_role_boundary(installed):
    platform, _ = installed
    platform.submit("incident-forensics", "operator task", "operations", "role-key", is_operator=True)
    with pytest.raises(PlatformError) as exc:
        platform.submit("incident-forensics", "operator task", "operations", "role-key")
    assert exc.value.code == "idempotency_conflict"
    assert exc.value.receipt is None


def test_mcp_conflict_exposes_only_owned_receipt_and_bounded_code(installed):
    platform, _ = installed
    first = platform.submit("incident-forensics", "task", "operations", "conflict", input_class="C1")
    response = request(platform, "POST", "/mcp", token=platform.settings.caller_tokens["operations"],
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "begin_task", "arguments": {
            "grant_id": "change-assessment", "task": "task", "idempotency_key": "conflict", "input_class": "C1"}}})
    result = response.json()["result"]
    assert result["isError"]
    assert result["structuredContent"] == {"error": {"code": "idempotency_conflict", "receipt": first.receipt}}


@pytest.mark.parametrize("ledger_state,confirmed,allowed", [
    (None, None, False), ("active", False, False), ("finished", False, False), ("finished", True, True),
])
def test_gateway_lineage_requires_retained_stop_evidence(installed, ledger_state, confirmed, allowed):
    platform, _ = installed
    prior = make_terminal(platform, "completed")
    from sovite.db import RequestLedger
    with platform.db.sessions.begin() as session:
        row = session.get(Job, prior.id)
        row.resolved = {**row.resolved, "model": {**row.resolved["model"], "connection": {"kind": "model-gateway"}}}
        if ledger_state is not None:
            session.add(RequestLedger(id="request", job_id=prior.id, fence="fence", binding_id="binding",
                session_id="session", gateway_id="gateway", workload="worker", request={},
                permit_digest="0" * 64, permit_until=1, status=ledger_state,
                outcome="completed" if confirmed else "model_cancel_unconfirmed", created_at=0, updated_at=0,
                evidence={"stop_confirmed": confirmed}))
    if allowed:
        child = platform.submit("change-assessment", "new task", "operations", "next", input_class="C1",
                                previous_attempt=prior.receipt)
        assert child.resolved["previous_attempt"]["receipt"] == prior.receipt
        # claim() sweeps the old ledger row. Positive evidence captured at
        # admission must survive ordinary retention through execution/release.
        from sovite.worker import Worker
        worker = Worker(platform)
        assert worker.claim().id == child.id
        worker.finish(child.id, artifact=platform.store.put(child.id, {"answer": "new result"}))
        assert platform.public_status(child.receipt, "operations")["status"] == "available"
    else:
        with pytest.raises(PlatformError) as exc:
            platform.submit("change-assessment", "new task", "operations", "next", input_class="C1",
                            previous_attempt=prior.receipt)
        assert exc.value.code == "previous_effect_uncertain"


@pytest.mark.parametrize("name", ["operator", "author", "activator"])
def test_caller_cannot_shadow_administrative_identity(name):
    from sovite.settings import Settings
    with pytest.raises(ValueError, match="administrative role names"):
        Settings(caller_tokens={name: "test-caller-token"})


def test_lineage_refuses_operator_receipt_for_same_named_caller(installed):
    platform, _ = installed
    prior = platform.submit("incident-forensics", "operator task", "operations", "op-key", is_operator=True)
    with platform.db.sessions.begin() as session:
        session.get(Job, prior.id).status = "completed"
    with pytest.raises(PlatformError) as exc:
        platform.submit("change-assessment", "new task", "operations", "new-key", input_class="C1",
                        previous_attempt=prior.receipt)
    assert exc.value.code == "previous_attempt_unavailable"


def test_workflow_http_conflict_recovers_owned_receipt(mixed_installed):
    platform, binding = mixed_installed
    body = {"binding": binding, "role": "implementer", "task": "task",
            "idempotency_key": "workflow-key", "selection_reason": "first choice"}
    token = platform.settings.caller_tokens["workflow-consumer"]
    first = request(platform, "POST", "/api/workflows/jobs", token=token, json=body)
    assert first.status_code == 201
    body["selection_reason"] = "changed choice"
    second = request(platform, "POST", "/api/workflows/jobs", token=token, json=body)
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "idempotency_conflict"
    assert second.json()["detail"]["receipt"] == first.json()["receipt"]
