"""Exercise the versioned agent-http-v1 worker API through the real owner.

The shared ``owner`` fixture uses SQLite and the disposable PostgreSQL fixture;
PostgreSQL cases skip only when its disposable URL is absent.
"""
import json

from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient
import yaml

from sovite import policy
from sovite.api import create_app
from sovite.lease_service import LeaseService
from test_execution_leases import owner, snapshot, submit  # noqa: F401
from test_migrations import database  # noqa: F401
from test_policy_v1alpha2 import bundle as gateway_bundle  # noqa: F401


TOKEN = "synthetic-execution-token-000000000000"
OTHER_TOKEN = "synthetic-other-execution-token-000000"


def admit_agent_http_owner(owner):
    bundle, _ = policy.load_bundle("config/platform.ollama.yaml")
    bundle["mode"] = "development"
    target = Path(owner.platform.settings.bundle_path).with_name("agent-http-profile.yaml")
    target.write_text(yaml.safe_dump(bundle))
    owner.platform.reload(str(target), role="operator")
    owner.service = LeaseService(owner.platform, "remote-api-test", {})


def execution_client(owner):
    settings = replace(
        owner.platform.settings,
        execution_tokens={"actual-workload": TOKEN, "other-workload": OTHER_TOKEN},
    )
    return TestClient(create_app(settings=settings, platform=owner.platform))


def pull_and_start(client, owner):
    pull_body = {"instance_id": owner.instance, "operation_id": "pull-http-1"}
    response = client.post(
        "/internal/execution/v1/pull",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json=pull_body,
    )
    assert response.status_code == 200
    reserved = response.json()
    assert reserved["state"] == "reserved"
    job = reserved["job"]
    assert "resolved" not in job
    assert job["recipe"] == {
        "profile": "agent-http-v1",
        "agent_id": "code-review",
        "endpoint": "http://native-harness:8090/run",
        "cancel_endpoint": "http://native-harness:8090/cancel",
        "credential_env": "SOVITE_HARNESS_TOKEN",
        "idempotent": False,
        "timeout_seconds": 900,
    }
    pull_replay = client.post(
        "/internal/execution/v1/pull",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json=pull_body,
    )
    assert pull_replay.status_code == 200
    assert pull_replay.json() == reserved
    body = {
        "instance_id": owner.instance,
        "operation_id": "start-http-1",
        "job_id": job["job_id"],
        "generation": job["generation"],
        "fence": job["fence"],
    }
    started_response = client.post(
        "/internal/execution/v1/start",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json=body,
    )
    assert started_response.status_code == 200
    started = started_response.json()
    assert started["state"] == "started"
    assert "execution" not in started
    return job, body, started


def test_remote_mutations_reconcile_by_same_operation_key_and_exact_fence(owner):
    admit_agent_http_owner(owner)
    owner.platform.submit("native-code-review", "PRIVATE-O2-SENTINEL synthetic task", "operations", "submit-http-1", input_class="C2")
    client = execution_client(owner)
    # The owner fixture has already run Platform.initialize().  Do not enter
    # the ASGI lifespan here: it would initialize the same database a second
    # time and this test needs only the real HTTP request/owner boundary.
    try:
        job, identity, started = pull_and_start(client, owner)
        replay = dict(identity, operation_id="start-http-replay")
        replay_response = client.post(
            "/internal/execution/v1/start",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=replay,
        )
        assert replay_response.status_code == 200
        assert replay_response.json() == {
            "state": "already_started",
            "job_id": job["job_id"],
            "generation": job["generation"],
            "fence": job["fence"],
        }
        assert "execution" not in replay_response.json()

        status_body = {key: identity[key] for key in ("instance_id", "job_id", "generation", "fence")}
        status = client.post(
            "/internal/execution/v1/status",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=status_body,
        )
        assert status.status_code == 200
        assert status.json()["state"] == "current"
        assert status.json()["deadline"] == started["deadline"]
        assert "task" not in status.json() and "resolved" not in status.json()

        renew_body = dict(status_body, operation_id="renew-http-1")
        first = client.post(
            "/internal/execution/v1/renew",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=renew_body,
        )
        replayed = client.post(
            "/internal/execution/v1/renew",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=renew_body,
        )
        assert first.status_code == replayed.status_code == 200
        assert first.json() == replayed.json()
        assert first.json()["state"] == "renewed"
        assert first.json()["deadline"] == started["deadline"]

        lookup = client.post(
            "/internal/execution/v1/lookup",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"instance_id": owner.instance, "operation_id": "renew-http-1"},
        )
        assert lookup.status_code == 200
        assert lookup.json() == {
            "state": "found",
            "operation_id": "renew-http-1",
            "operation_kind": "renew",
            "job_id": job["job_id"],
            "fence": job["fence"],
            "result_state": "renewed",
        }
        assert "result" not in lookup.json() and "task" not in lookup.json()
    finally:
        client.close()


def test_remote_complete_is_idempotent_and_foreign_workload_cannot_observe(owner):
    admit_agent_http_owner(owner)
    owner.platform.submit("native-code-review", "PRIVATE-O2-SENTINEL synthetic task", "operations", "submit-http-1", input_class="C2")
    client = execution_client(owner)
    try:
        job, identity, _ = pull_and_start(client, owner)
        status_body = {key: identity[key] for key in ("instance_id", "job_id", "generation", "fence")}
        foreign = client.post(
            "/internal/execution/v1/status",
            headers={"Authorization": f"Bearer {OTHER_TOKEN}"},
            json=status_body,
        )
        assert foreign.status_code == 403
        assert "PRIVATE-O2-SENTINEL" not in foreign.text

        completion = dict(
            status_body,
            operation_id="complete-http-1",
            outcome="completed",
            result_json="{\"output\":\"synthetic result\"}",
            error_code=None,
        )
        before = snapshot(owner.platform)
        invalid_inner_json = client.post(
            "/internal/execution/v1/complete",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=dict(completion, result_json='{"output":1,"output":2}'),
        )
        assert invalid_inner_json.status_code == 422
        assert snapshot(owner.platform) == before
        first = client.post(
            "/internal/execution/v1/complete",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=completion,
        )
        assert first.status_code == 200
        committed = snapshot(owner.platform)
        replayed = client.post(
            "/internal/execution/v1/complete",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=completion,
        )
        assert replayed.status_code == 200
        assert replayed.json() == first.json()
        assert first.json()["state"] == "completed"
        assert before["jobs"] != committed["jobs"]
        assert len(committed["worker_execution_operations"]) == len(before["worker_execution_operations"]) + 1

        changed = dict(completion, result_json="{\"output\":\"changed\"}")
        conflict = client.post(
            "/internal/execution/v1/complete",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=changed,
        )
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "idempotency_conflict"
        assert snapshot(owner.platform) == committed

        found = client.post(
            "/internal/execution/v1/lookup",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"instance_id": owner.instance, "operation_id": "complete-http-1"},
        )
        assert found.status_code == 200
        assert found.json()["result_state"] == "completed"
        assert "output" not in found.text
    finally:
        client.close()


def test_remote_handback_replay_and_stale_fence_do_not_change_successor(owner):
    admit_agent_http_owner(owner)
    owner.platform.submit("native-code-review", "PRIVATE-O2-SENTINEL synthetic task", "operations", "submit-http-1", input_class="C2")
    client = execution_client(owner)
    try:
        job, identity, _ = pull_and_start(client, owner)
        body = {
            "instance_id": identity["instance_id"],
            "operation_id": "handback-http-1",
            "job_id": identity["job_id"],
            "generation": identity["generation"],
            "fence": identity["fence"],
        }
        first = client.post(
            "/internal/execution/v1/handback",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=body,
        )
        replay = client.post(
            "/internal/execution/v1/handback",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=body,
        )
        assert first.status_code == replay.status_code == 200
        assert replay.json() == first.json()
        assert first.json()["state"] == "handed_back"

        stale = dict(body, operation_id="late-renew-http")
        refused = client.post(
            "/internal/execution/v1/renew",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=stale,
        )
        assert refused.status_code == 409
        assert refused.json()["detail"]["code"] == "execution_fenced"
        assert owner.platform.get_job(job["job_id"]).status in {"retry_wait", "blocked", "cancelled"}
    finally:
        client.close()


def test_remote_api_auth_and_strict_body_fail_before_owner_effect(owner):
    from sovite.execution_contract.runtime import OPERATIONS, load_bundle

    admit_agent_http_owner(owner)
    owner.platform.submit("native-code-review", "PRIVATE-O2-SENTINEL synthetic task", "operations", "submit-http-1", input_class="C2")
    before = snapshot(owner.platform)
    contract = load_bundle()
    client = execution_client(owner)
    try:
        origin_denied = client.post(
            "/internal/execution/v1/pull",
            headers={"Authorization": f"Bearer {TOKEN}", "Origin": "https://other.example"},
            json={"instance_id": owner.instance, "operation_id": "pull-origin-denied"},
        )
        assert origin_denied.status_code == 403
        contract.validate_response(OPERATIONS["pull"], 403, origin_denied.json())
        invalid_length = client.post(
            "/internal/execution/v1/pull",
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json",
                     "Content-Length": "not-a-number"},
            content=b"{}",
        )
        assert invalid_length.status_code == 422
        contract.validate_response(OPERATIONS["pull"], 422, invalid_length.json())
        missing_auth = client.post(
            "/internal/execution/v1/pull",
            json={"instance_id": owner.instance, "operation_id": "pull-no-auth"},
        )
        assert missing_auth.status_code == 403
        malformed = client.post(
            "/internal/execution/v1/pull",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"instance_id": owner.instance, "operation_id": "pull-invalid", "workload_id": "admin"},
        )
        assert malformed.status_code == 422
        assert "workload_id" not in malformed.text
        contract.validate_response(OPERATIONS["pull"], 422, malformed.json())
        oversized = client.post(
            "/internal/execution/v1/pull",
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
            content=(b'{"instance_id":"' + owner.instance.encode()
                     + b'","operation_id":"pull-too-large","padding":"'
                     + b"x" * 65_536 + b'"}'),
        )
        assert oversized.status_code == 413
        contract.validate_response(OPERATIONS["pull"], 413, oversized.json())
    finally:
        client.close()
    assert snapshot(owner.platform) == before


def test_execution_platform_error_omits_receipt_and_matches_contract(owner, monkeypatch):
    from sovite.execution_contract.runtime import OPERATIONS, load_bundle
    from sovite.service import PlatformError

    def rejected(*_args, **_kwargs):
        raise PlatformError("idempotency_conflict", "operation conflict", 409,
                            receipt="PRIVATE-RECEIPT-SENTINEL")

    monkeypatch.setattr(LeaseService, "reserve", rejected)
    client = execution_client(owner)
    try:
        response = client.post(
            "/internal/execution/v1/pull",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"instance_id": owner.instance, "operation_id": "pull-platform-error"},
        )
        assert response.status_code == 409
        assert "PRIVATE-RECEIPT-SENTINEL" not in response.text
        assert "receipt" not in response.json()["detail"]
        load_bundle().validate_response(OPERATIONS["pull"], 409, response.json())
    finally:
        client.close()


def test_first_external_profile_does_not_reserve_other_harness_types(owner):
    submit(owner)
    before = snapshot(owner.platform)
    client = execution_client(owner)
    try:
        response = client.post(
            "/internal/execution/v1/pull",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"instance_id": owner.instance, "operation_id": "pull-agent-http-only"},
        )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "execution_profile_unavailable"
        assert "PRIVATE-O2-SENTINEL" not in response.text
    finally:
        client.close()
    assert snapshot(owner.platform) == before

    # Existing in-process workers keep access to the owner's other harnesses.
    local = owner.service.reserve(
        principal=owner.principal,
        instance_id=owner.instance,
        operation_id="local-pull-after-profile-filter",
    )
    assert local["state"] == "reserved"
    assert isinstance(local["job"]["resolved"], dict)


def test_first_external_profile_returns_only_the_admitted_agent_http_recipe(owner):
    admit_agent_http_owner(owner)
    owner.platform.submit(
        "native-code-review",
        "PRIVATE-O2-SENTINEL synthetic task",
        "operations",
        "submit-http-profile-1",
        input_class="C2",
    )
    client = execution_client(owner)
    try:
        response = client.post(
            "/internal/execution/v1/pull",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"instance_id": owner.instance, "operation_id": "pull-http-profile-1"},
        )
        assert response.status_code == 200
        reserved = response.json()
        assert reserved["state"] == "reserved"
        job = reserved["job"]
        assert set(job) == {"job_id", "generation", "fence", "lease_until", "task", "recipe"}
        assert job["recipe"] == {
            "profile": "agent-http-v1",
            "agent_id": "code-review",
            "endpoint": "http://native-harness:8090/run",
            "cancel_endpoint": "http://native-harness:8090/cancel",
            "credential_env": "SOVITE_HARNESS_TOKEN",
            "idempotent": False,
            "timeout_seconds": 900,
        }
        for private_value in ("resolved", "model", "provider", "authentication", "caller", "grant"):
            assert private_value not in job
            assert job["task"] == "PRIVATE-O2-SENTINEL synthetic task"
    finally:
        client.close()


def test_execution_client_uses_versioned_contract_body_and_json_result():
    import httpx
    from sovite.execution_client import ExecutionClient, ExecutionContext

    observed = {}

    def respond(request):
        observed["path"] = request.url.path
        observed["authorization"] = request.headers.get("authorization")
        observed["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "state": "completed",
            "job_id": "job-client-1",
            "generation": "sha256:" + "a" * 64,
            "fence": "b" * 32,
            "error_code": None,
        })

    client = ExecutionClient(
        "https://execution.example", "test-execution-token-value-123456",
        transport=httpx.MockTransport(respond),
    )
    result = client.complete(
        ExecutionContext("worker-1", "complete-client-1", "job-client-1",
                         "sha256:" + "a" * 64, "b" * 32),
        outcome="completed", result={"ok": True},
    )
    assert result["state"] == "completed"
    assert observed == {
        "path": "/internal/execution/v1/complete",
        "authorization": "Bearer test-execution-token-value-123456",
        "body": {
            "instance_id": "worker-1",
            "operation_id": "complete-client-1",
            "job_id": "job-client-1",
            "generation": "sha256:" + "a" * 64,
            "fence": "b" * 32,
            "outcome": "completed",
            "result_json": "{\"ok\":true}",
            "error_code": None,
        },
    }


def test_execution_client_real_http_contract_lifecycle(owner):
    import httpx
    from sovite.execution_client import ExecutionClient, ExecutionContext

    admit_agent_http_owner(owner)
    owner.platform.submit(
        "native-code-review", "generated contract lifecycle", "operations",
        "submit-generated-client", input_class="C2",
    )
    app_client = execution_client(owner)

    def forward(request):
        response = app_client.request(
            request.method, request.url.path, content=request.content,
            headers={key: value for key, value in request.headers.items()
                     if key.lower() not in {"host", "content-length", "accept-encoding"}},
        )
        return httpx.Response(response.status_code, headers=response.headers,
                              content=response.content, request=request)

    client = ExecutionClient(
        "https://execution.example", TOKEN,
        transport=httpx.MockTransport(forward),
    )
    try:
        reserved = client.pull(owner.instance, "pull-generated-client")
        assert reserved["state"] == "reserved"
        job = reserved["job"]
        identity = (owner.instance, job["job_id"], job["generation"], job["fence"])
        start_context = ExecutionContext(identity[0], "start-generated-client", *identity[1:])
        started = client.start(start_context)
        assert started["state"] == "started"
        assert client.status(start_context)["state"] == "current"
        complete_context = ExecutionContext(identity[0], "complete-generated-client", *identity[1:])
        completed = client.complete(
            complete_context, outcome="completed", result={"client": "generated-contract"},
        )
        assert completed["state"] == "completed"
        found = client.lookup(owner.instance, complete_context.operation_id)
        assert found["state"] == "found"
        assert found["result_state"] == "completed"
    finally:
        app_client.close()


def test_idle_pull_lookup_preserves_exact_owner_result_and_scope(owner, tmp_path):
    admit_agent_http_owner(owner)
    client = execution_client(owner)
    key = {"instance_id": owner.instance, "operation_id": "idle-recovery-http"}
    headers = {"Authorization": f"Bearer {TOKEN}"}
    try:
        pulled = client.post("/internal/execution/v1/pull", headers=headers, json=key)
        assert pulled.status_code == 200
        assert pulled.json() == {"state": "idle"}
        before = snapshot(owner.platform)
        found = client.post("/internal/execution/v1/lookup", headers=headers, json=key)
        assert found.status_code == 200
        assert found.json() == {"state": "found", "operation_id": key["operation_id"],
            "operation_kind": "pull", "job_id": None, "fence": None, "result_state": "idle"}
        assert snapshot(owner.platform) == before
        import httpx
        from sovite.execution_client import ExecutionClient
        from sovite.remote_journal import ExecutionJournal
        from sovite.worker import RemoteWorker
        def send(request):
            response = client.request(request.method, request.url.path,
                headers=dict(request.headers), content=request.content)
            return httpx.Response(response.status_code, content=response.content,
                headers={"content-type": "application/json"})
        remote = ExecutionClient("https://control.test", TOKEN,
            transport=httpx.MockTransport(send))
        path = tmp_path / "pending-idle.json"
        journal = ExecutionJournal(path)
        worker = RemoteWorker(remote, owner.instance, journal=journal)
        worker._journal_write("pending_pull", key["operation_id"], "pull")
        journal.close()  # A process boundary retains the pending operation key.
        journal = ExecutionJournal(path)
        restarted = RemoteWorker(remote, owner.instance, journal=journal)
        assert restarted._startup_reconcile() is True
        assert journal.load() is None
        assert not restarted.stop.is_set()
        journal.close()
        assert snapshot(owner.platform) == before
        denied = client.post("/internal/execution/v1/lookup",
            headers={"Authorization": f"Bearer {OTHER_TOKEN}"}, json=key)
        assert denied.status_code == 404
        foreign = client.post("/internal/execution/v1/lookup", headers=headers,
            json=dict(key, instance_id="another-instance"))
        assert foreign.status_code == 404
    finally:
        client.close()


def test_lookup_nullable_idle_fields_do_not_accept_corrupt_bindings(owner):
    from sqlalchemy import select
    from sovite.db import WorkerExecutionOperation
    admit_agent_http_owner(owner)
    client = execution_client(owner)
    key = {"instance_id": owner.instance, "operation_id": "invalid-idle-lookup-http"}
    headers = {"Authorization": f"Bearer {TOKEN}"}
    try:
        assert client.post("/internal/execution/v1/pull", headers=headers, json=key).json() == {"state": "idle"}
        for kind, result, job_id, fence in [
            ("pull", {"state": "reserved"}, None, None),
            ("renew", {"state": "idle"}, None, None),
            ("pull", {"state": "idle"}, "unexpected-job", "a" * 32),
            ("pull", {"state": "reserved"}, "job", None),
            ("pull", {"state": "idle", "unexpected": True}, None, None),
        ]:
            with owner.platform.db.sessions.begin() as session:
                row = session.scalar(select(WorkerExecutionOperation).where(
                    WorkerExecutionOperation.instance_id == key["instance_id"],
                    WorkerExecutionOperation.operation_id == key["operation_id"]))
                row.operation_kind, row.result, row.job_id, row.fence = kind, result, job_id, fence
            before = snapshot(owner.platform)
            response = client.post("/internal/execution/v1/lookup", headers=headers, json=key)
            assert response.status_code == 503
            assert snapshot(owner.platform) == before
    finally:
        client.close()
