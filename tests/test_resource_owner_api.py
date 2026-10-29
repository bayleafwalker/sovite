"""Resource owner API remains attached to the live execution and output authority."""
import base64
import hashlib
import json
import time
from dataclasses import replace

from fastapi.testclient import TestClient

from sovite.api import create_app
from sovite.db import ResourceGatewayEnrollment
from test_migrations import database  # noqa: F401
from test_policy_v1alpha2 import bundle as gateway_bundle  # noqa: F401
from test_worker_remote_boundary import (TOKEN, admit_agent_http_owner,
                                         pull_and_start)
from test_execution_leases import owner  # noqa: F401


OPERATOR = "synthetic-resource-operator-token-000000"
TENANT = "tenant-a"
WORKSPACE = "workspace-a"
DOMAIN = "pods-a"
OBSERVATION = "pod-observation-a"
POLICY_REVISION = "sha256:" + "a" * 64


def test_enrolled_gateway_flow_binds_scope_fence_output_and_fresh_reader(owner):
    admit_agent_http_owner(owner)
    owner.platform.submit("native-code-review", "synthetic resource task", "operations",
                          "resource-api-submit", input_class="C2")
    settings = replace(owner.platform.settings, operator_token=OPERATOR,
                       execution_tokens={"actual-workload": TOKEN})
    client = TestClient(create_app(settings=settings, platform=owner.platform))
    admin = {"Authorization": f"Bearer {OPERATOR}"}
    worker = {"Authorization": f"Bearer {TOKEN}"}
    job, identity, _ = pull_and_start(client, owner)

    scope = {"workload_id": "actual-workload", "gateway_id": "resource-gateway-a", "tenant_id": TENANT,
             "workspace_id": WORKSPACE, "domain_id": DOMAIN,
             "observation_id": OBSERVATION, "namespace": "application",
             "pod_name": "web-0"}
    assert client.put("/internal/resource-owner/v1/workload-scopes", headers=admin,
                      json=scope).status_code == 200
    enrollment = client.post("/internal/resource-owner/v1/enrollments", headers=admin,
        json={"gateway_id": "resource-gateway-a", "tenant_id": TENANT,
              "workspace_id": WORKSPACE, "domain_id": DOMAIN,
              "expires_at": time.time() + 3600})
    assert enrollment.status_code == 200
    gateway_token = enrollment.json()["bearer_token"]
    gateway = {"Authorization": f"Bearer {gateway_token}"}
    expired_creation = client.post("/internal/resource-owner/v1/enrollments", headers=admin,
        json={"gateway_id": "expired-gateway", "tenant_id": TENANT,
              "workspace_id": WORKSPACE, "domain_id": DOMAIN,
              "expires_at": time.time() - 1})
    assert expired_creation.status_code == 422
    foreign = client.post("/internal/resource-owner/v1/enrollments", headers=admin,
        json={"gateway_id": "foreign-gateway", "tenant_id": "foreign-tenant",
              "workspace_id": WORKSPACE, "domain_id": DOMAIN,
              "expires_at": time.time() + 3600})
    assert foreign.status_code == 200
    foreign_tenant = client.post("/internal/resource-owner/v1/enrollments", headers=admin,
        json={"gateway_id": "resource-gateway-a", "tenant_id": "foreign-tenant",
              "workspace_id": WORKSPACE, "domain_id": DOMAIN,
              "expires_at": time.time() + 3600})
    foreign_domain = client.post("/internal/resource-owner/v1/enrollments", headers=admin,
        json={"gateway_id": "resource-gateway-a", "tenant_id": TENANT,
              "workspace_id": WORKSPACE, "domain_id": "foreign-domain",
              "expires_at": time.time() + 3600})
    expired = client.post("/internal/resource-owner/v1/enrollments", headers=admin,
        json={"gateway_id": "resource-gateway-a", "tenant_id": TENANT,
              "workspace_id": WORKSPACE, "domain_id": DOMAIN,
              "expires_at": time.time() + 3600})
    with owner.platform.db.sessions.begin() as session:
        session.get(ResourceGatewayEnrollment, expired.json()["enrollment_id"]).expires_at = time.time() - 1

    submit_body = {"job_id": job["job_id"], "instance_id": owner.instance,
        "generation": job["generation"], "fence": job["fence"],
        "operation_id": "resource-request-1"}
    wrong_fence = dict(submit_body, fence="0" * 32, operation_id="resource-wrong-fence")
    assert client.post("/internal/resource-owner/v1/requests", headers=worker,
                       json=wrong_fence).status_code == 403
    wrong_instance = dict(submit_body, instance_id="foreign-instance",
                          operation_id="resource-wrong-instance")
    assert client.post("/internal/resource-owner/v1/requests", headers=worker,
                       json=wrong_instance).status_code == 403
    submitted = client.post("/internal/resource-owner/v1/requests", headers=worker,
                            json=submit_body)
    assert submitted.status_code == 200
    request_id = submitted.json()["request_id"]
    assert submitted.json()["fence_fingerprint"] == "sha256:" + hashlib.sha256(job["fence"].encode()).hexdigest()

    polled = client.post("/internal/resource-owner/v1/poll", headers=gateway, json={})
    assert polled.status_code == 200
    request = next(item for item in polled.json()["requests"] if item["request_id"] == request_id)
    assert request["principal_id"] == "operations"
    assert request["audience"] == "resource-gateway"
    assert request["tenant_id"] == TENANT and request["workspace_id"] == WORKSPACE
    assert request["domain_id"] == DOMAIN and request["observation_id"] == OBSERVATION
    assert request["fence_fingerprint"] == submitted.json()["fence_fingerprint"]
    assert "fence" not in request
    foreign_gateway = {"Authorization": "Bearer " + foreign.json()["bearer_token"]}
    assert client.post("/internal/resource-owner/v1/poll", headers=foreign_gateway,
                       json={}).json() == {"requests": []}
    for enrollment_row in (foreign_tenant, foreign_domain):
        response = client.post("/internal/resource-owner/v1/poll",
            headers={"Authorization": "Bearer " + enrollment_row.json()["bearer_token"]}, json={})
        assert response.json() == {"requests": []}
    assert client.post("/internal/resource-owner/v1/poll",
        headers={"Authorization": "Bearer " + expired.json()["bearer_token"]}, json={}).status_code == 403

    binding = {"request_id": request_id, "fence_fingerprint": request["fence_fingerprint"]}
    assert client.post("/internal/resource-owner/v1/current", headers=gateway,
                       json=binding).json() == {"current": True}
    changed_scope = dict(scope, observation_id="foreign-observation")
    assert client.put("/internal/resource-owner/v1/workload-scopes", headers=admin,
                      json=changed_scope).status_code == 200
    assert client.post("/internal/resource-owner/v1/current", headers=gateway,
                       json=binding).json() == {"current": False}
    assert client.put("/internal/resource-owner/v1/workload-scopes", headers=admin,
                      json=scope).status_code == 200

    projection = {"schema_version": "sovite-resource-pod-observation/v1",
        "workspace_id": WORKSPACE, "resource_domain_id": DOMAIN,
        "observation_id": OBSERVATION, "namespace": "application",
        "pod_name": "web-0", "phase": "Running"}
    content = json.dumps(projection, sort_keys=True, ensure_ascii=True,
                         separators=(",", ":")).encode()
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    result_body = {**binding, "observation_digest": digest,
        "observation_b64": base64.b64encode(content).decode(), "release": "allowed",
        "policy_revision": POLICY_REVISION, "destination": "workbench", "actor": "operations"}
    noncanonical = json.dumps(projection).encode()
    assert client.post('/internal/resource-owner/v1/result', headers=gateway,
        json={**result_body, 'observation_b64': base64.b64encode(noncanonical).decode(),
              'observation_digest': 'sha256:' + hashlib.sha256(noncanonical).hexdigest()}).status_code == 422
    invalid_digest = dict(result_body, observation_digest="sha256:" + "0" * 64)
    assert client.post("/internal/resource-owner/v1/result", headers=gateway,
                       json=invalid_digest).status_code == 422
    withheld = client.post("/internal/resource-owner/v1/result", headers=gateway, json=result_body)
    assert withheld.json() == {"accepted": False, "state": "withheld"}

    grant = client.post("/internal/resource-owner/v1/output-grants", headers=admin,
        json={**{k: scope[k] for k in ("workload_id", "tenant_id", "workspace_id", "domain_id", "observation_id")},
              "principal_id": "operations", "policy_revision": POLICY_REVISION})
    assert grant.status_code == 200
    for mutation in ({"actor": "foreign-principal"},
                     {"policy_revision": "sha256:" + "b" * 64},
                     {"destination": "external"}):
        denied = client.post("/internal/resource-owner/v1/result", headers=gateway,
                             json={**result_body, **mutation})
        assert denied.json() == {"accepted": False, "state": "withheld"}
    accepted = client.post("/internal/resource-owner/v1/result", headers=gateway, json=result_body)
    assert accepted.json() == {"accepted": True, "state": "stored"}
    assert client.post("/internal/resource-owner/v1/result", headers=gateway,
                       json=result_body).json() == accepted.json()
    changed_projection = dict(projection, phase="Succeeded")
    changed_content = json.dumps(changed_projection, sort_keys=True, ensure_ascii=True,
                                 separators=(",", ":")).encode()
    changed_result = dict(result_body,
        observation_digest="sha256:" + hashlib.sha256(changed_content).hexdigest(),
        observation_b64=base64.b64encode(changed_content).decode())
    assert client.post("/internal/resource-owner/v1/result", headers=gateway,
                       json=changed_result).json() == {"accepted": False, "state": "conflict"}

    reader = {"request_id": request_id, "instance_id": owner.instance,
        "job_id": job["job_id"], "generation": job["generation"],
        "fence": job["fence"], "operation_id": "resource-result-read-1"}
    read = client.post("/internal/resource-owner/v1/result/read", headers=worker, json=reader)
    assert read.status_code == 200
    assert read.json()["observation"] == projection
    assert read.json()["sha256"] == digest
    assert client.delete(f"/internal/resource-owner/v1/output-grants/{grant.json()['grant_id']}",
                         headers=admin).status_code == 200
    assert client.post("/internal/resource-owner/v1/result/read", headers=worker,
                       json={**reader, "operation_id": "resource-result-read-revoked"}).status_code == 404
    handed_back = client.post("/internal/execution/v1/handback", headers=worker,
        json={**identity, "operation_id": "resource-owner-handback"})
    assert handed_back.status_code == 200
    assert client.post("/internal/resource-owner/v1/result/read", headers=worker,
        json={**reader, "operation_id": "resource-result-read-after-handback"}).status_code == 403
    assert client.post("/internal/resource-owner/v1/current", headers=gateway,
                       json=binding).json() == {"current": False}
    assert client.delete(f"/internal/resource-owner/v1/enrollments/{enrollment.json()['enrollment_id']}",
                         headers=admin).status_code == 200
    assert client.post("/internal/resource-owner/v1/current", headers=gateway,
                       json=binding).status_code == 403
