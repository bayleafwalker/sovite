"""Exercise the provisional remote worker API through the real owner.

The shared ``owner`` fixture uses the schema-five SQLite/PostgreSQL fixture;
PostgreSQL cases are collected and skip only when its disposable URL is absent.
These tests cover the ungenerated P08 wire surface while its generated contract
and release qualification remain open.
"""

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
        "/internal/execution/pull",
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
        "/internal/execution/pull",
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
        "/internal/execution/start",
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
            "/internal/execution/start",
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
            "/internal/execution/status",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=status_body,
        )
        assert status.status_code == 200
        assert status.json()["state"] == "current"
        assert status.json()["deadline"] == started["deadline"]
        assert "task" not in status.json() and "resolved" not in status.json()

        renew_body = dict(status_body, operation_id="renew-http-1")
        first = client.post(
            "/internal/execution/renew",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=renew_body,
        )
        replayed = client.post(
            "/internal/execution/renew",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=renew_body,
        )
        assert first.status_code == replayed.status_code == 200
        assert first.json() == replayed.json()
        assert first.json()["state"] == "renewed"
        assert first.json()["deadline"] == started["deadline"]

        lookup = client.post(
            "/internal/execution/lookup",
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
            "/internal/execution/status",
            headers={"Authorization": f"Bearer {OTHER_TOKEN}"},
            json=status_body,
        )
        assert foreign.status_code == 403
        assert "PRIVATE-O2-SENTINEL" not in foreign.text

        completion = dict(
            status_body,
            operation_id="complete-http-1",
            outcome="completed",
            result={"output": "synthetic result"},
            error_code=None,
        )
        before = snapshot(owner.platform)
        first = client.post(
            "/internal/execution/complete",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=completion,
        )
        assert first.status_code == 200
        committed = snapshot(owner.platform)
        replayed = client.post(
            "/internal/execution/complete",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=completion,
        )
        assert replayed.status_code == 200
        assert replayed.json() == first.json()
        assert first.json()["state"] == "completed"
        assert before["jobs"] != committed["jobs"]
        assert len(committed["worker_execution_operations"]) == len(before["worker_execution_operations"]) + 1

        changed = dict(completion, result={"output": "changed"})
        conflict = client.post(
            "/internal/execution/complete",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=changed,
        )
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "idempotency_conflict"
        assert snapshot(owner.platform) == committed

        found = client.post(
            "/internal/execution/lookup",
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
            "/internal/execution/handback",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=body,
        )
        replay = client.post(
            "/internal/execution/handback",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=body,
        )
        assert first.status_code == replay.status_code == 200
        assert replay.json() == first.json()
        assert first.json()["state"] == "handed_back"

        stale = dict(body, operation_id="late-renew-http")
        refused = client.post(
            "/internal/execution/renew",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=stale,
        )
        assert refused.status_code == 409
        assert refused.json()["detail"]["code"] == "execution_fenced"
        assert owner.platform.get_job(job["job_id"]).status in {"retry_wait", "blocked", "cancelled"}
    finally:
        client.close()


def test_remote_api_auth_and_strict_body_fail_before_owner_effect(owner):
    admit_agent_http_owner(owner)
    owner.platform.submit("native-code-review", "PRIVATE-O2-SENTINEL synthetic task", "operations", "submit-http-1", input_class="C2")
    before = snapshot(owner.platform)
    client = execution_client(owner)
    try:
        missing_auth = client.post(
            "/internal/execution/pull",
            json={"instance_id": owner.instance, "operation_id": "pull-no-auth"},
        )
        assert missing_auth.status_code == 403
        malformed = client.post(
            "/internal/execution/pull",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"instance_id": owner.instance, "operation_id": "pull-invalid", "workload_id": "admin"},
        )
        assert malformed.status_code == 422
        assert "workload_id" not in malformed.text
    finally:
        client.close()
    assert snapshot(owner.platform) == before


def test_first_external_profile_does_not_reserve_other_harness_types(owner):
    submit(owner)
    before = snapshot(owner.platform)
    client = execution_client(owner)
    try:
        response = client.post(
            "/internal/execution/pull",
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
            "/internal/execution/pull",
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
