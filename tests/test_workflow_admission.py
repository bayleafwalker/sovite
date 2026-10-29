"""Admission, explicit declassification, and class-aware HTTP/MCP delivery."""
import asyncio
import copy
import json

import httpx
import pytest
import yaml
from sqlalchemy import select

from sovite.api import create_app
from sovite.db import AuditEvent, Job
from sovite.flow import contract_revision
from sovite.output_contracts import approve_output, read_output
from sovite.service import Platform, PlatformError
from sovite.settings import Settings
from sovite.worker import Worker
from sovite.workflows import compile_workflow
from test_workflows import coordinated, direct, installed_bundle, revision_ref


OP = "operator-token-for-workflow-tests-000"
CALLER = "caller-token-for-workflow-tests-00000"
APPROVER = "approver-token-for-workflow-tests-00"


def mixed_bundle():
    bundle = installed_bundle()
    bundle["restricted_class"] = "C3"
    bundle["harnesses"][0]["kind"] = "demo"
    bundle["models"][0].update(adapter="demo", model="synthetic")
    bundle["models"][0].pop("endpoint")
    bundle["destinations"].append({"id": "public-model", "kind": "cloud",
                                   "boundary": "public", "clearance": "C0"})
    bundle["models"].append({"id": "public-model", "adapter": "demo",
                             "model": "synthetic-public", "destination": "public-model"})
    for agent in bundle["agents"]:
        if agent["id"] in {"inline_reviewer", "holistic_reviewer"}:
            agent["model"] = "public-model"
    for grant in bundle["grants"]:
        sensitive = grant["agent"] not in {"inline_reviewer", "holistic_reviewer"}
        grant.update(input_class="C3" if sensitive else "C0",
                     output_class="C3" if sensitive else "C0",
                     return_contract="class-aware", caller_clearance="C0",
                     result_readers=["approver"], workflow_only=not sensitive)
    public = copy.deepcopy(next(g for g in bundle["grants"] if g["id"] == "inline_reviewer-grant"))
    public.update(id="public-direct", workflow_only=False)
    bundle["grants"].append(public)
    bundle["output_contracts"] = [{
        "id": "public-review", "source_class": "C3", "output_class": "C0",
        "destinations": ["public-model"], "approvers": ["approver"],
        "procedure_ref": revision_ref("sanitization-procedure"),
    }]
    return bundle


def mixed_definition():
    document = coordinated()
    for role in ("inline_reviewer", "holistic_reviewer"):
        document["roles"][role]["input_class"] = "C0"
    for transfer in document["transfers"]:
        if transfer["id"] in {"inline", "holistic"}:
            transfer["output_contract"] = "public-review"
    return document


@pytest.fixture
def installed(tmp_path):
    path = tmp_path / "bundle.yaml"
    path.write_text(yaml.safe_dump(mixed_bundle()))
    settings = Settings(database_url=f"sqlite:///{tmp_path}/state.db",
        artifact_path=str(tmp_path / "artifacts"), bundle_path=str(path),
        operator_token=OP, caller_tokens={"workflow-consumer": CALLER, "approver": APPROVER},
        require_qualification=False, installation_id="workflow-test", compute_id="workers")
    platform = Platform(settings)
    platform.db.migrate()
    platform.initialize()
    binding = platform.bind_workflow(mixed_definition(), revision_ref(), revision_ref("session-policy"),
        platform.active()[1], input_class="C3")
    yield platform, binding
    platform.db.engine.dispose()


def complete_source(platform, binding, key="source"):
    job = platform.submit_workflow_role(binding, "implementer", "SENSITIVE-RAW-CANDIDATE",
        "workflow-consumer", key)
    assert asyncio.run(Worker(platform).run_once())
    return platform.get_job(job.id)


def approval(platform, source, **changes):
    args = dict(source_job_id=source.id, source_digest="sha256:" + source.artifact_sha256,
        contract_id="public-review",
        expected_contract_revision=contract_revision(platform.active()[0]["output_contracts"][0]),
        expected_generation=platform.active()[1],
        content="Approved public candidate and acceptance criteria.",
        evidence_refs=[revision_ref("independent-sanitization-check")], actor="approver")
    args.update(changes)
    return args


def request(platform, method, path, token=OP, **kwargs):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(platform.settings, platform)),
                                     base_url="http://localhost:8080") as client:
            return await client.request(method, path, headers={"Authorization": "Bearer " + token}, **kwargs)
    return asyncio.run(run())


def mcp(platform, name, arguments, token=CALLER):
    response = request(platform, "POST", "/mcp", token=token,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": arguments}})
    assert response.status_code == 200
    return response.json()["result"]


def test_missing_input_uses_bundle_maximum_not_grant_ceiling(installed):
    platform, _ = installed
    with pytest.raises(PlatformError) as denied:
        platform.submit("public-direct", "unclassified", "workflow-consumer", "unclassified")
    assert denied.value.code == "class_denied"
    private = platform.submit("implementer-grant", "private", "workflow-consumer", "private")
    assert private.resolved["input_class"] == "C3"
    public = platform.submit("public-direct", "public", "workflow-consumer", "public", "C0")
    assert public.resolved["input_class"] == "C0"


def test_exact_approved_content_is_the_entire_public_input(installed):
    platform, binding = installed
    source = complete_source(platform, binding)
    args = approval(platform, source)
    release = approve_output(platform, **args)
    assert approve_output(platform, **args) == release
    job = platform.submit_workflow_role(binding, "inline_reviewer", "", "workflow-consumer",
        "review", source_release=release["release_id"], transfer_id="inline")
    assert job.task == args["content"]
    assert "SENSITIVE-RAW-CANDIDATE" not in job.task
    assert "requirements_ref" not in job.task
    replay = platform.submit_workflow_role(binding, "inline_reviewer", "", "workflow-consumer",
        "review", source_release=release["release_id"], transfer_id="inline")
    assert replay.id == job.id
    assert asyncio.run(Worker(platform).run_once())
    assert platform.get_job(job.id).status == "completed"
    with platform.db.sessions() as session:
        events = session.scalars(select(AuditEvent).where(AuditEvent.action == "output.declassified")).all()
        assert len(events) == 1
        assert args["content"] not in json.dumps([event.detail for event in events])


@pytest.mark.parametrize("case", ["missing-release", "additional-content", "wrong-transfer",
                                  "wrong-source-role", "different-requirements"])
def test_public_review_rejects_unapproved_or_mismatched_transfers(installed, case):
    platform, binding = installed
    source = complete_source(platform, binding)
    release = approve_output(platform, **approval(platform, source))
    role, task, transfer, selected = "inline_reviewer", "", "inline", binding
    release_id = release["release_id"]
    if case == "missing-release":
        release_id = None
    elif case == "additional-content":
        task = "unapproved private addition"
    elif case == "wrong-transfer":
        transfer = "rework"
    elif case == "wrong-source-role":
        role, transfer = "holistic_reviewer", "holistic"
    else:
        selected = platform.bind_workflow(mixed_definition(), revision_ref(revision="r2"),
            revision_ref("session-policy"), platform.active()[1], input_class="C3")
    with pytest.raises(PlatformError):
        platform.submit_workflow_role(selected, role, task, "workflow-consumer", "invalid-review",
            source_release=release_id, transfer_id=transfer)
    with platform.db.sessions() as session:
        assert len(session.scalars(select(Job)).all()) == 1


@pytest.mark.parametrize("change", [
    {"actor": "workflow-consumer"},
    {"source_digest": "sha256:" + "0" * 64},
    {"expected_generation": "sha256:" + "0" * 64},
    {"expected_contract_revision": "sha256:" + "0" * 64},
    {"evidence_refs": []},
])
def test_approval_requires_exact_source_policy_and_authority(installed, change):
    platform, binding = installed
    source = complete_source(platform, binding)
    with pytest.raises(PlatformError):
        approve_output(platform, **approval(platform, source, **change))


def test_corrupt_source_cannot_be_declassified(installed):
    platform, binding = installed
    source = complete_source(platform, binding)
    (platform.store.root / source.artifact_key).write_text("tampered")
    with pytest.raises(PlatformError) as denied:
        approve_output(platform, **approval(platform, source))
    assert denied.value.code == "artifact_integrity_failed"


def test_output_reader_requires_contract_scope_and_current_source(installed):
    platform, binding = installed
    source = complete_source(platform, binding)
    args = approval(platform, source)
    release = approve_output(platform, **args)
    assert read_output(platform, release["release_id"], "approver")["content"] == args["content"]
    with pytest.raises(PlatformError):
        read_output(platform, release["release_id"], "workflow-consumer")
    platform.inhibit("implementer-grant", "Source authority revoked")
    with pytest.raises(PlatformError):
        read_output(platform, release["release_id"], "approver")
    with pytest.raises(PlatformError):
        platform.submit_workflow_role(binding, "inline_reviewer", "", "workflow-consumer", "blocked",
            source_release=release["release_id"], transfer_id="inline")


@pytest.mark.parametrize("role", ["product_planner", "researcher"])
def test_external_authoring_roles_cannot_run_in_delivery_loop(installed, role):
    platform, binding = installed
    with pytest.raises(PlatformError) as denied:
        platform.submit_workflow_role(binding, role, "scope", "workflow-consumer", role)
    assert denied.value.code == "workflow_binding_mismatch"


@pytest.mark.parametrize("field,value", [
    ("installation_id", "another-installation"), ("input_class", "C0"),
    ("dispatch_authorized", True),
])
def test_forged_binding_is_rejected(installed, field, value):
    platform, binding = installed
    forged = copy.deepcopy(binding)
    forged[field] = value
    with pytest.raises(PlatformError):
        platform.submit_workflow_role(forged, "implementer", "task", "workflow-consumer", "forged")


def test_superseded_generation_cannot_bind_or_submit(installed):
    platform, binding = installed
    bundle = copy.deepcopy(platform.active()[0])
    bundle["metadata"]["display_name"] = "Revised"
    platform.activate(platform.compile(bundle))
    with pytest.raises(PlatformError) as denied:
        platform.bind_workflow(mixed_definition(), revision_ref(), revision_ref("session-policy"),
            binding["bundle_generation"], input_class="C3")
    assert denied.value.code == "revision_superseded"
    with pytest.raises(PlatformError) as denied:
        platform.submit_workflow_role(binding, "implementer", "task", "workflow-consumer", "stale")
    assert denied.value.code == "revision_superseded"


@pytest.mark.parametrize("operator", [False, True])
def test_flat_submission_cannot_bypass_workflow_only_grant(installed, operator):
    platform, _ = installed
    with pytest.raises(PlatformError) as denied:
        platform.submit("inline_reviewer-grant", "labelled public", "operator" if operator else "workflow-consumer",
            "flat", "C0", is_operator=operator)
    assert denied.value.code == "workflow_binding_required"


def test_offline_direct_binding_requires_owner_registration(installed):
    platform, _ = installed
    document = direct()
    document["roles"]["implementer"]["grant"] = "inline_reviewer-grant"
    bundle, generation = platform.active()
    binding = compile_workflow(document, bundle, requirements_ref=revision_ref(),
        session_policy_ref=revision_ref("session-policy"), installation_id="workflow-test",
        generation=generation, input_class="C0")
    with pytest.raises(PlatformError) as denied:
        platform.submit_workflow_role(binding, "implementer", "public task", "workflow-consumer", "unbound")
    assert denied.value.code == "workflow_not_bound"
    with pytest.raises(PlatformError):
        platform.bind_workflow(document, revision_ref(), revision_ref("session-policy"),
            generation, input_class="C0", actor="workflow-consumer")
    authorized = platform.bind_workflow(document, revision_ref(), revision_ref("session-policy"),
        generation, input_class="C0")
    assert authorized == binding
    job = platform.submit_workflow_role(authorized, "implementer", "public task", "workflow-consumer", "bound")
    assert job.status == "queued"


def test_worker_rechecks_revoked_source_before_attempting_public_job(installed):
    platform, binding = installed
    source = complete_source(platform, binding)
    release = approve_output(platform, **approval(platform, source))
    review = platform.submit_workflow_role(binding, "inline_reviewer", "", "workflow-consumer", "review",
        source_release=release["release_id"], transfer_id="inline")
    platform.inhibit("implementer-grant", "Revoked after enqueue")
    assert Worker(platform).claim() is None
    blocked = platform.get_job(review.id)
    assert blocked.status == "blocked" and blocked.attempts == 0
    assert blocked.error_code == "grant_inhibited"


def test_operator_requires_explicit_output_approver_scope(installed):
    platform, binding = installed
    source = complete_source(platform, binding)
    with pytest.raises(PlatformError):
        approve_output(platform, **approval(platform, source, actor="operator", is_operator=True))


def test_http_binding_requires_author_and_strict_request(installed):
    platform, binding = installed
    body = {"workflow": mixed_definition(), "requirements_ref": revision_ref(),
            "session_policy_ref": revision_ref("session-policy"),
            "expected_generation": binding["bundle_generation"], "input_class": "C3"}
    assert request(platform, "POST", "/api/workflows/bind", token=CALLER, json=body).status_code == 403
    response = request(platform, "POST", "/api/workflows/bind", json=body)
    assert response.status_code == 200 and response.json() == binding
    body["unrecognized_authority"] = True
    assert request(platform, "POST", "/api/workflows/bind", json=body).status_code == 422
    duplicate = '{"workflow":{},"workflow":{}}'
    assert request(platform, "POST", "/api/workflows/validate", content=duplicate).status_code == 422


def test_http_output_release_and_public_role_delivery(installed):
    platform, binding = installed
    source = complete_source(platform, binding)
    args = approval(platform, source)
    args.pop("actor")
    response = request(platform, "POST", "/api/output-releases", token=APPROVER, json=args)
    assert response.status_code == 201
    release = response.json()
    assert len(release["release_id"]) == 72
    payload = {"binding": binding, "role": "inline_reviewer", "idempotency_key": "http-review",
               "source_release": release["release_id"], "transfer_id": "inline"}
    response = request(platform, "POST", "/api/workflows/jobs", token=CALLER, json=payload)
    assert response.status_code == 201
    with platform.db.sessions() as session:
        job = session.scalar(select(Job).where(Job.receipt == response.json()["receipt"]))
        assert job.task == args["content"]
    payload.update(task="UNREVIEWED-ADDITION", idempotency_key="http-invalid")
    assert request(platform, "POST", "/api/workflows/jobs", token=CALLER, json=payload).status_code == 422


def test_mcp_class_aware_content_and_scoped_private_reader(installed):
    platform, binding = installed
    source = complete_source(platform, binding)
    private = mcp(platform, "task_status", {"receipt": source.receipt})["structuredContent"]
    assert private["status"] == "available" and "result" not in private
    assert private["reader"] == {"tool": "read_result", "receipt": source.receipt}
    assert mcp(platform, "read_result", {"receipt": source.receipt})["isError"]
    authorized = mcp(platform, "read_result", {"receipt": source.receipt}, token=APPROVER)
    assert not authorized["isError"]
    assert "SENSITIVE-RAW-CANDIDATE" in json.dumps(authorized["structuredContent"])
    started = mcp(platform, "begin_task", {"grant_id": "public-direct", "task": "public candidate",
        "input_class": "C0", "idempotency_key": "mcp-public"})
    receipt = started["structuredContent"]["receipt"]
    assert asyncio.run(Worker(platform).run_once())
    public = mcp(platform, "task_status", {"receipt": receipt})["structuredContent"]
    assert public["status"] == "available" and "result" in public and "reader" not in public
    assert "public candidate" in json.dumps(public)


@pytest.mark.parametrize("field", ["task", "idempotency_key", "grant_id", "input_class"])
def test_mcp_rejects_non_utf8_submission_without_server_failure(installed, field):
    platform, _ = installed
    args = {"grant_id": "public-direct", "task": "ordinary task", "idempotency_key": "utf8",
            "input_class": "C0"}
    args[field] = "\ud800"
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "begin_task", "arguments": args}}
    response = request(platform, "POST", "/mcp", token=CALLER,
                       content=json.dumps(payload, ensure_ascii=True))
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is True
    assert "ud800" not in response.text
    with platform.db.sessions() as session:
        assert not session.scalars(select(Job)).all()


@pytest.mark.parametrize("field", ["task", "idempotency_key"])
def test_workflow_http_rejects_non_utf8_before_job_creation(installed, field):
    platform, binding = installed
    payload = {"binding": binding, "role": "implementer", "task": "ordinary task",
               "idempotency_key": "utf8"}
    payload[field] = "\ud800"
    response = request(platform, "POST", "/api/workflows/jobs", token=CALLER,
                       content=json.dumps(payload, ensure_ascii=True))
    assert response.status_code == 422
    assert "ud800" not in response.text
    with platform.db.sessions() as session:
        assert not session.scalars(select(Job)).all()


@pytest.mark.parametrize("name", ["task_status", "read_result"])
def test_mcp_rejects_non_utf8_receipt_without_server_failure(installed, name):
    platform, _ = installed
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": name, "arguments": {"receipt": "\ud800"}}}
    response = request(platform, "POST", "/mcp", token=APPROVER,
                       content=json.dumps(payload, ensure_ascii=True))
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is True
    assert "ud800" not in response.text
