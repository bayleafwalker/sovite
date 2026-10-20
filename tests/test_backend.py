from __future__ import annotations

import asyncio
import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from sovite import policy
from sovite.adapters import AdapterRunner, RuntimeFailure
from sovite.api import create_app
from sovite.cli import assessment
from sovite.db import AuditEvent, Job, Qualification
from sovite.qualification import initialize_keys, is_qualified, issue_qualification, verify_qualification, runtime_settings_fingerprint
from sovite.service import Platform, PlatformError
from sovite.settings import Settings
from sovite.worker import Worker, healthy_worker_count


ROOT = Path(__file__).resolve().parents[1]
OPERATOR = "operator-test-token-0000000000000000"
CALLER = "caller-test-token-00000000000000000"
OTHER = "other-caller-test-token-00000000000"


@pytest.fixture
def setup(tmp_path):
    bundle, _ = policy.load_bundle(ROOT / "config/platform.local.yaml")
    config_path = tmp_path / "bundle.yaml"
    config_path.write_text(yaml.safe_dump(bundle))
    settings = Settings(database_url=f"sqlite:///{tmp_path}/control.db", bundle_path=str(config_path), artifact_path=str(tmp_path / "artifacts"), operator_token=OPERATOR, caller_tokens={"cloud-codex": CALLER, "operations": OTHER}, public_origin="http://testserver", secure_cookies=False, require_qualification=False, qualification_private_key=str(tmp_path / "private.pem"), qualification_public_keys=[str(tmp_path / "public.pem")])
    platform = Platform(settings)
    platform.initialize()
    return settings, platform, bundle


def bearer(token):
    return {"Authorization": "Bearer " + token}


def rpc(client, name, arguments, token=CALLER):
    return client.post("/mcp", headers=bearer(token), json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})


def test_receipt_never_contains_private_content_and_artifact_requires_operator(setup):
    settings, platform, _ = setup
    canary = "SECRET-CANARY-DO-NOT-RELEASE"
    with TestClient(create_app(settings, platform)) as client:
        response = rpc(client, "begin_task", {"grant_id": "incident-forensics", "task": canary, "idempotency_key": "r1"})
        assert response.status_code == 200
        public = response.json()["result"]["structuredContent"]
        assert set(public) == {"receipt", "status"}
        assert public["status"] == "pending" and canary not in response.text
        asyncio.run(Worker(platform).run_once())
        status = rpc(client, "task_status", {"receipt": public["receipt"]})
        assert status.json()["result"]["structuredContent"]["status"] == "available"
        assert canary not in status.text
        outsider = rpc(client, "task_status", {"receipt": public["receipt"]}, token=OTHER)
        assert outsider.json()["result"]["isError"] is True
        assert client.get("/api/jobs", headers=bearer(CALLER)).status_code == 403
        jobs = client.get("/api/jobs", headers=bearer(OPERATOR)).json()["items"]
        artifact = client.get(f"/api/jobs/{jobs[0]['id']}/artifact", headers=bearer(OPERATOR))
        assert artifact.status_code == 200 and canary in artifact.text
        assert client.get(f"/api/jobs/{jobs[0]['id']}/artifact", headers=bearer(CALLER)).status_code == 403
        assert client.get("/api/audit", headers=bearer(OPERATOR)).json()["verification"]["valid"]
        assert canary not in client.get("/api/audit", headers=bearer(OPERATOR)).text


def test_rpc_surface_rejects_result_fetch_and_unknown_fields(setup):
    settings, platform, _ = setup
    with TestClient(create_app(settings, platform)) as client:
        for method in ("tasks/get", "resources/read", "sampling/createMessage", "evil/notify"):
            response = client.post("/mcp", headers=bearer(CALLER), json={"jsonrpc": "2.0", "id": 1, "method": method, "params": {"sensitive": "PAYLOAD"}})
            assert response.json()["error"]["code"] == -32601
            assert "PAYLOAD" not in response.text
        extra = rpc(client, "begin_task", {"grant_id": "incident-forensics", "task": "test", "idempotency_key": "key", "model": "unapproved-cloud"})
        assert extra.json()["error"]["code"] == -32602
        assert rpc(client, "begin_task", {}, token=OPERATOR).status_code == 403
        assert client.get("/mcp", headers=bearer(CALLER)).status_code == 405


def test_idempotency_scoped_to_caller_and_conflict_detected(setup):
    _, platform, _ = setup
    first = platform.submit("incident-forensics", "first", "cloud-codex", "same")
    assert platform.submit("incident-forensics", "first", "cloud-codex", "same").id == first.id
    with pytest.raises(PlatformError, match="different request"):
        platform.submit("incident-forensics", "different", "cloud-codex", "same")
    assert platform.submit("incident-forensics", "first", "operations", "same").id != first.id
    with pytest.raises(PlatformError):
        platform.submit("incident-forensics", "task", "operator", "faked-operator")
    assert platform.submit("incident-forensics", "task", "operator", "real-operator", is_operator=True)


def test_cookie_auth_requires_same_origin_mutations_and_no_local_storage_token(setup):
    settings, platform, _ = setup
    with TestClient(create_app(settings, platform)) as client:
        signed = client.post("/api/auth/session", json={"token": OPERATOR})
        assert signed.status_code == 200
        assert "httponly" in signed.headers["set-cookie"].lower()
        assert "samesite=strict" in signed.headers["set-cookie"].lower()
        assert client.get("/api/overview").status_code == 200
        assert client.post("/api/jobs", json={"grant_id": "incident-forensics", "task": "hello"}).status_code == 403
        assert client.post("/api/jobs", headers={"Origin": "https://evil.example"}, json={"grant_id": "incident-forensics", "task": "hello"}).status_code == 403
        assert client.post("/api/jobs", headers={"Origin": "http://testserver"}, json={"grant_id": "incident-forensics", "task": "hello"}).status_code == 201
        assert client.delete("/api/auth/session", headers={"Origin": "http://testserver"}).status_code == 200
        assert client.get("/api/overview").status_code == 401


def test_revocation_between_artifact_commit_and_release_is_fail_closed(setup):
    _, platform, _ = setup
    job = platform.submit("incident-forensics", "private", "cloud-codex", "r")
    worker = Worker(platform)
    claimed = worker.claim()
    assert claimed.id == job.id
    artifact = platform.store.put(job.id, {"answer": "PRIVATE"})
    platform.inhibit(job.grant_id, "Operator revocation during execution")
    worker.finish(job.id, artifact=artifact)
    row = platform.get_job(job.id)
    assert row.status == "blocked" and row.public_projection is None
    assert row.artifact_key is not None
    assert platform.public_status(job.receipt, job.caller)["status"] == "unavailable"
    with pytest.raises(PlatformError):
        platform.submit(job.grant_id, "again", job.caller, "new")


def test_configuration_change_blocks_existing_jobs_and_does_not_clear_inhibit(setup):
    settings, platform, bundle = setup
    first = platform.submit("incident-forensics", "private", "cloud-codex", "r")
    platform.inhibit("change-assessment", "Persistent operational inhibit")
    bundle["metadata"]["version"] = "2"
    Path(settings.bundle_path).write_text(yaml.safe_dump(bundle))
    platform.reload(settings.bundle_path)
    assert platform.get_job(first.id).status == "blocked"
    assert platform.public_status(first.receipt, first.caller)["status"] == "unavailable"
    with pytest.raises(PlatformError) as denied:
        platform.submit("change-assessment", "x", "operations", "r2")
    assert denied.value.code == "grant_inhibited"


def test_signed_qualification_tampering_and_trust_are_checked(setup):
    settings, platform, bundle = setup
    settings.require_qualification = True
    with pytest.raises(PlatformError):
        platform.submit("incident-forensics", "private", "cloud-codex", "before")
    initialize_keys(settings)
    generation = platform.active()[1]
    evidence = {"generation": generation, "profile": "local", "scope": "Test fixture only", "checks": [{"name": "receipt-boundary", "status": "passed"}]}
    row = issue_qualification(platform.db, settings, generation, "local", evidence)
    assert verify_qualification(row, settings)
    assert platform.submit("incident-forensics", "private", "cloud-codex", "after")
    with platform.db.sessions.begin() as session:
        changed = session.get(Qualification, row.id)
        changed.issuer = "different-issuer"
    with platform.db.sessions() as session:
        assert not verify_qualification(session.get(Qualification, row.id), settings)
        assert not is_qualified(session, settings, generation, "local")


def runtime_bundle(settings, platform, bundle, *, tools=False, readonly=True):
    bundle["models"][0] = {"id": "assessment-model", "adapter": "ollama", "model": "test-model", "endpoint": "http://localhost:11434", "destination": "private-runtime", "parameters": {}}
    bundle["harnesses"][0]["kind"] = "tool-agent"
    if tools:
        bundle["tools"] = [{"id": "cluster", "kind": "http-json", "endpoint": "http://localhost:9000/tool", "input_class": "C3", "output_class": "C3", "destination": "private-runtime", "allowed_operations": ["inspect"], "read_only": readonly}]
        # Only the C3 agent may use the C3 tool; the second agent remains tool-free.
        bundle["agents"][0]["tools"] = ["cluster"]
    Path(settings.bundle_path).write_text(yaml.safe_dump(bundle))
    platform.reload(settings.bundle_path)


def test_ollama_outage_queues_without_changing_model_or_endpoint(setup):
    settings, platform, bundle = setup
    runtime_bundle(settings, platform, bundle)
    seen = []
    def handler(request):
        seen.append((str(request.url), json.loads(request.content)["model"]))
        if len(seen) == 1:
            raise httpx.ConnectError("sensitive upstream diagnostic", request=request)
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "private model answer"}})
    job = platform.submit("incident-forensics", "task", "cloud-codex", "r")
    worker = Worker(platform, AdapterRunner(httpx.MockTransport(handler)))
    asyncio.run(worker.run_once())
    assert platform.get_job(job.id).status == "retry_wait"
    with platform.db.sessions.begin() as session:
        session.get(Job, job.id).next_attempt_at = 0
    asyncio.run(worker.run_once())
    assert platform.get_job(job.id).status == "completed"
    assert seen == [("http://localhost:11434/api/chat", "test-model")] * 2


def test_mutating_tool_failure_never_replays_whole_agent(setup):
    settings, platform, bundle = setup
    runtime_bundle(settings, platform, bundle, tools=True, readonly=False)
    calls = []
    def handler(request):
        calls.append(str(request.url))
        if request.url.port == 11434:
            return httpx.Response(200, json={"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "cluster", "arguments": {"operation": "inspect", "arguments": {}}}}]}})
        return httpx.Response(503, json={"private_error": "may have committed"})
    job = platform.submit("incident-forensics", "task", "cloud-codex", "r")
    worker = Worker(platform, AdapterRunner(httpx.MockTransport(handler)))
    asyncio.run(worker.run_once())
    assert platform.get_job(job.id).status == "blocked"
    assert platform.get_job(job.id).error_code == "execution_uncertain"
    assert asyncio.run(worker.run_once()) is False
    assert len(calls) == 2


def test_malformed_tool_call_is_not_executed_and_model_can_correct_it(setup):
    settings, platform, bundle = setup
    runtime_bundle(settings, platform, bundle, tools=True, readonly=True)
    chats, tool_requests = [], []
    def handler(request):
        if request.url.port != 11434:
            tool_requests.append(json.loads(request.content))
            return httpx.Response(200, json={"pods": 1})
        chats.append(json.loads(request.content)["messages"])
        if len(chats) == 1:
            call = {"operation": "inspect", "namespace": "elsewhere"}
        elif len(chats) == 2:
            call = {"operation": "inspect", "arguments": {}}
        else:
            return httpx.Response(200, json={"message": {"role": "assistant", "content": "one pod"}})
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "cluster", "arguments": call}}]}})
    job = platform.submit("incident-forensics", "task", "cloud-codex", "r")
    asyncio.run(Worker(platform, AdapterRunner(httpx.MockTransport(handler))).run_once())
    assert platform.get_job(job.id).status == "completed"
    assert json.loads(chats[1][-1]["content"])["error"] == "invalid_tool_arguments"
    assert tool_requests == [{"operation": "inspect", "arguments": {}}]


def test_kubernetes_read_arguments_are_correctable_not_fatal(setup, monkeypatch):
    settings, platform, bundle = setup
    runtime_bundle(settings, platform, bundle)
    bundle["tools"] = [{"id": "kube", "kind": "kubernetes-read", "endpoint": "http://localhost:6443", "namespace": "sovite", "input_class": "C3", "output_class": "C3",
                        "destination": "private-runtime", "allowed_operations": ["list-pods"], "read_only": True, "api_key_env": "TEST_KUBE_TOKEN"}]
    bundle["agents"][0]["tools"] = ["kube"]
    Path(settings.bundle_path).write_text(yaml.safe_dump(bundle))
    platform.reload(settings.bundle_path)
    monkeypatch.setenv("TEST_KUBE_TOKEN", "t")
    chats, api = [], []
    def handler(request):
        if request.url.port == 6443:
            api.append(request.url.path)
            return httpx.Response(200, json={"items": [{"metadata": {"name": "sovite-worker-1"}}]})
        body = json.loads(request.content)
        chats.append(body)
        call = [{"operation": "list-pods", "arguments": {"namespace": "sovite"}}, {"operation": "list-pods", "arguments": {}}]
        if len(chats) <= 2:
            return httpx.Response(200, json={"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "kube", "arguments": call[len(chats) - 1]}}]}})
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "sovite-worker-1"}})
    job = platform.submit("incident-forensics", "task", "cloud-codex", "r")
    asyncio.run(Worker(platform, AdapterRunner(httpx.MockTransport(handler))).run_once())
    assert platform.get_job(job.id).status == "completed", platform.get_job(job.id).error_code
    schema = chats[0]["tools"][0]["function"]["parameters"]["properties"]["arguments"]
    assert schema["maxProperties"] == 0
    assert json.loads(chats[1]["messages"][-1]["content"])["error"] == "invalid_tool_arguments"
    assert api == ["/api/v1/namespaces/sovite/pods"]


def test_concurrent_workers_cannot_claim_same_task(setup):
    _, platform, _ = setup
    job = platform.submit("incident-forensics", "task", "cloud-codex", "r")
    workers = [Worker(platform), Worker(platform)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda worker: worker.claim(), workers))
    assert sum(row is not None for row in results) == 1
    assert next(row for row in results if row).id == job.id


def test_compute_pool_binding_and_late_lease_holder_cannot_publish(setup):
    settings, platform, _ = setup
    job = platform.submit("incident-forensics", "task", "cloud-codex", "r")
    wrong = Platform(replace(settings, compute_id="different-pool"))
    assert Worker(wrong).claim() is None
    worker = Worker(platform)
    assert worker.claim().id == job.id
    with platform.db.sessions.begin() as session:
        session.get(Job, job.id).lease_until = time.time() - 1
    second = Worker(platform)
    assert second.claim().id == job.id
    worker.finish(job.id, artifact=platform.store.put(job.id, {"answer": "stale"}))
    assert platform.get_job(job.id).public_projection is None
    second.finish(job.id, artifact=platform.store.put(job.id, {"answer": "current"}))
    assert platform.get_job(job.id).status == "completed"


def test_model_digest_mismatch_prevents_inference():
    seen = []
    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"models": [{"name": "test-model", "digest": "b" * 64}]})
    runner = AdapterRunner(httpx.MockTransport(handler))
    model = {"adapter": "ollama", "model": "test-model", "endpoint": "http://localhost:11434", "digest": "sha256:" + "a" * 64}
    with pytest.raises(RuntimeFailure) as failure:
        asyncio.run(runner.verify_model_identity(model, True))
    assert failure.value.code == "model_identity_mismatch"
    assert seen == ["http://localhost:11434/api/tags"]


def test_pinned_model_trust_replaces_system_roots_in_both_adapters(monkeypatch, tmp_path):
    import ssl
    import subprocess
    from sovite import adapters
    model = {"id": "remote", "adapter": "openai-compatible", "wire_api": "responses", "model": "m",
             "endpoint": "https://192.0.2.20:8021/v1", "tls_trust": "model-host"}
    monkeypatch.setenv("SOVITE_MODEL_TRUST_DIR", str(tmp_path))
    with pytest.raises(RuntimeFailure) as failure:
        adapters.model_verify(model)
    assert failure.value.code == "model_trust_missing"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-days", "1",
                    "-subj", "/CN=t", "-keyout", str(tmp_path / "k.pem"), "-out", str(tmp_path / "model-host.pem")], check=True, capture_output=True)
    context = adapters.model_verify(model)
    assert isinstance(context, ssl.SSLContext) and len(context.get_ca_certs()) == 1
    assert adapters.model_verify({"id": "plain"}) is True
    seen = {}
    async def fake_exec(*command, env, **kwargs):
        seen.update(env)
        raise FileNotFoundError
    monkeypatch.setattr(adapters.asyncio, "create_subprocess_exec", fake_exec)
    recipe = {"harness": {}, "model": model, "agent": {"id": "a"}, "generation": "g", "mode": "development"}
    with pytest.raises(RuntimeFailure, match="native_runtime_unavailable"):
        asyncio.run(AdapterRunner().native_codex(recipe, "task", "job"))
    assert seen["CODEX_CA_CERTIFICATE"] == str(tmp_path / "model-host.pem")


def test_audit_and_artifact_tampering_are_detected(setup):
    _, platform, _ = setup
    job = platform.submit("incident-forensics", "task", "cloud-codex", "r")
    asyncio.run(Worker(platform).run_once())
    row = platform.get_job(job.id)
    Path(platform.settings.artifact_path, row.artifact_key).write_text('{"tampered":true}')
    with pytest.raises(ValueError, match="digest mismatch"):
        platform.store.get(row.artifact_key, row.artifact_sha256)
    with platform.db.sessions.begin() as session:
        event = session.get(AuditEvent, 1)
        event.detail = {"modified": True}
    assert platform.db.verify_audit()["valid"] is False


def test_builtin_assessment_runs_and_explicitly_limits_its_evidence(setup, tmp_path):
    settings, _, _ = setup
    evidence = asyncio.run(assessment(settings, str(tmp_path / "evidence.json")))
    assert evidence["evidence_kind"] == "synthetic-integration"
    assert "Does not assess" in evidence["scope"]
    assert all(check["status"] == "passed" for check in evidence["checks"])


def test_authorization_checked_again_before_tool_call(setup):
    settings, platform, bundle = setup
    runtime_bundle(settings, platform, bundle, tools=True)
    calls = []
    def handler(request):
        calls.append(str(request.url))
        platform.inhibit("incident-forensics", "Revoke after model reasoning before tool call")
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "cluster", "arguments": {"operation": "inspect", "arguments": {}}}}]}})
    job = platform.submit("incident-forensics", "task", "cloud-codex", "r")
    asyncio.run(Worker(platform, AdapterRunner(httpx.MockTransport(handler))).run_once())
    assert len(calls) == 1
    assert platform.get_job(job.id).status == "blocked"


def test_enforced_mode_rejects_disabled_gate_and_synthetic_evidence(setup):
    settings, platform, bundle = setup
    runtime_bundle(settings, platform, bundle)
    bundle["mode"] = "enforced"
    bundle["models"][0]["digest"] = "sha256:" + "a" * 64
    bundle["harnesses"][0]["digest"] = "sha256:" + "b" * 64
    Path(settings.bundle_path).write_text(yaml.safe_dump(bundle))
    with pytest.raises(PlatformError) as no_bypass:
        platform.reload(settings.bundle_path)
    assert no_bypass.value.code == "qualification_cannot_be_disabled"
    settings.require_qualification = True
    generation = platform.reload(settings.bundle_path)
    initialize_keys(settings)
    evidence = {"generation": generation, "runtime_binding": runtime_settings_fingerprint(settings), "profile": "local", "scope": "Mechanism test fixture; not real deployment evidence", "evidence_kind": "synthetic-integration", "checks": [{"name": "receipt-boundary", "status": "passed"}]}
    issue_qualification(platform.db, settings, generation, "local", evidence)
    with platform.db.sessions() as session:
        assert not is_qualified(session, settings, generation, "local")
    with pytest.raises(PlatformError):
        Platform(replace(settings, require_qualification=False)).initialize()


def native_test_app(monkeypatch, tmp_path):
    from sovite.harness_api import create_app as create_harness
    monkeypatch.setenv("SOVITE_BUNDLE_PATH", str(ROOT / "config/platform.ollama.yaml"))
    monkeypatch.setenv("SOVITE_HARNESS_AGENT_ID", "code-review")
    monkeypatch.setenv("SOVITE_HARNESS_TOKEN", CALLER)
    monkeypatch.setenv("SOVITE_HARNESS_STATE_PATH", str(tmp_path / "native"))
    generation = policy.load_bundle(ROOT / "config/platform.ollama.yaml")[1]
    return create_harness(), generation


def test_native_service_fixed_binding_and_durable_idempotency(monkeypatch, tmp_path):
    executions = []
    async def fake_native(self, recipe, task, job_id, *, isolated=False):
        executions.append((task, job_id, isolated))
        return {"answer": "PRIVATE NATIVE RESULT"}
    monkeypatch.setattr(AdapterRunner, "native_codex", fake_native)
    app, generation = native_test_app(monkeypatch, tmp_path)
    body = {"task_id": "fixed-task", "agent_id": "code-review", "configuration_revision": generation, "input": {"task": "inspect repository"}}
    headers = {**bearer(CALLER), "Idempotency-Key": "fixed-task"}
    with TestClient(app) as client:
        assert client.post("/run", json=body).status_code == 401
        wrong = copy.deepcopy(body)
        wrong["input"]["endpoint"] = "http://unapproved.example"
        assert client.post("/run", headers=headers, json=wrong).status_code == 422
        first = client.post("/run", headers=headers, json=body)
        second = client.post("/run", headers=headers, json=body)
        assert first.status_code == second.status_code == 200
        assert first.json() == second.json() == {"answer": "PRIVATE NATIVE RESULT"}
        assert executions == [("inspect repository", "fixed-task", True)]
    restarted, _ = native_test_app(monkeypatch, tmp_path)
    with TestClient(restarted) as client:
        assert client.post("/run", headers=headers, json=body).status_code == 200
    assert len(executions) == 1


def test_native_service_cancels_running_and_queued_execution(monkeypatch, tmp_path):
    async def scenario():
        entered = asyncio.Event()
        invoked = []
        async def fake_native(self, recipe, task, job_id, *, isolated=False):
            invoked.append(job_id)
            entered.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(AdapterRunner, "native_codex", fake_native)
        app, generation = native_test_app(monkeypatch, tmp_path)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://harness") as client:
            def payload(task_id):
                return {"task_id": task_id, "agent_id": "code-review", "configuration_revision": generation, "input": {"task": "inspect"}}
            one = asyncio.create_task(client.post("/run", headers={**bearer(CALLER), "Idempotency-Key": "one"}, json=payload("one")))
            await asyncio.wait_for(entered.wait(), 2)
            two = asyncio.create_task(client.post("/run", headers={**bearer(CALLER), "Idempotency-Key": "two"}, json=payload("two")))
            await asyncio.sleep(0.02)
            for task_id in ("two", "one"):
                cancel = {key: value for key, value in payload(task_id).items() if key != "input"}
                response = await client.post("/cancel", headers=bearer(CALLER), json=cancel)
                assert response.status_code == 200
            responses = await asyncio.wait_for(asyncio.gather(one, two), 2)
            assert [response.status_code for response in responses] == [409, 409]
            assert invoked == ["one"]
    asyncio.run(scenario())


def test_qualification_runtime_binding_detects_placement_changes_but_not_secret_rotation(setup):
    settings, platform, _ = setup
    settings.require_qualification = True
    initialize_keys(settings)
    generation = platform.active()[1]
    evidence = {"generation": generation, "profile": "local", "scope": "Runtime binding mechanism fixture", "checks": [{"name": "binding", "status": "passed"}]}
    row = issue_qualification(platform.db, settings, generation, "local", evidence)
    assert verify_qualification(row, settings)
    assert not verify_qualification(row, replace(settings, public_origin="https://different.example"))
    assert not verify_qualification(row, replace(settings, artifact_path=settings.artifact_path + "-different"))
    assert verify_qualification(row, replace(settings, operator_token="rotated-operator-token-000000000000000", compute_id="second-pool"))
    postgres = replace(settings, database_url="postgresql+psycopg://worker:password-a@database:5432/control")
    rotated = replace(postgres, database_url="postgresql+psycopg://worker:password-b@database:5432/control")
    assert runtime_settings_fingerprint(postgres) == runtime_settings_fingerprint(rotated)
    changed_host = replace(postgres, database_url="postgresql+psycopg://worker:password-a@other:5432/control")
    assert runtime_settings_fingerprint(postgres) != runtime_settings_fingerprint(changed_host)


def test_worker_health_requires_this_compute_pool_and_host(setup):
    settings, platform, _ = setup
    other_pool = Platform(replace(settings, compute_id="another-pool"))
    Worker(other_pool).heartbeat()
    assert healthy_worker_count(platform) == 0
    Worker(platform).heartbeat()
    assert healthy_worker_count(platform) == 1
    assert healthy_worker_count(platform, hostname="different-pod") == 0


def test_empty_model_answer_fails_and_thinking_control_is_top_level(setup):
    settings, platform, bundle = setup
    runtime_bundle(settings, platform, bundle)
    bundle["models"][0]["parameters"]["think"] = False
    Path(settings.bundle_path).write_text(yaml.safe_dump(bundle))
    platform.reload(settings.bundle_path)
    seen = []
    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        return httpx.Response(200, json={"message": {"role": "assistant", "thinking": "Budget consumed before producing an answer.", "content": "  "}, "done": True, "done_reason": "length"})
    job = platform.submit("incident-forensics", "task", "cloud-codex", "empty-result")
    asyncio.run(Worker(platform, AdapterRunner(httpx.MockTransport(handler))).run_once())
    assert seen[0]["think"] is False
    assert "think" not in seen[0]["options"]
    row = platform.get_job(job.id)
    assert row.status == "failed" and row.error_code == "model_empty_result"
    assert row.public_projection is None and row.artifact_key is None
    assert platform.public_status(row.receipt, row.caller)["status"] == "unavailable"
