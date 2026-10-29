"""Resource owner API remains attached to the live execution and output authority."""
import base64
import hashlib
import json
import time
import yaml
import pytest
from pathlib import Path
from sovite import policy
from sovite.lease_service import LeaseService
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


@pytest.mark.parametrize("low_destination", [True, False])
def test_enrolled_gateway_flow_binds_scope_fence_output_and_fresh_reader(owner, low_destination):
    bundle, _ = policy.load_bundle("config/platform.ollama.yaml")
    bundle["mode"] = "development"
    if low_destination:
        bundle["destinations"].append({"id":"low-runtime", "kind":"private", "clearance":"C0", "allowed_hosts":["ollama", "native-harness"]})
        next(m for m in bundle["models"] if m["id"] == "native-reasoner")["destination"] = "low-runtime"
        grant = next(g for g in bundle["grants"] if g["id"] == "native-code-review")
        grant["input_class"] = "C0"; grant["output_class"] = "C0"
    config = Path(owner.platform.settings.bundle_path).with_name("review-low-runtime.yaml")
    config.write_text(yaml.safe_dump(bundle))
    owner.platform.reload(str(config), role="operator")
    owner.service = LeaseService(owner.platform, "remote-api-test", {})
    owner.platform.submit("native-code-review", "synthetic resource task", "operations",
                          "resource-api-submit", input_class="C0")
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
    assert polled.json()["requests"] == []
    binding = {"request_id": request_id, "fence_fingerprint": submitted.json()["fence_fingerprint"], "policy_revision": POLICY_REVISION, "destination": "workbench", "actor": "operations"}
    grant = client.post("/internal/resource-owner/v1/output-grants", headers=admin,
        json={**{k: scope[k] for k in ("workload_id", "tenant_id", "workspace_id", "domain_id", "observation_id")}, "principal_id": "operations", "policy_revision": POLICY_REVISION})
    assert grant.status_code == 200
    assert client.post("/internal/resource-owner/v1/current", headers=gateway, json=binding).json() == {"current": False}
