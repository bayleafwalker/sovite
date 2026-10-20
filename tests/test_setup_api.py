"""Setup credentials are durable proposal authority, never execution authority."""
import asyncio
import copy
import hashlib
import os
import uuid
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select

from sovite.api import create_app
from sovite.db import AuditEvent, SetupPrincipal
from sovite.service import Platform
from sovite.settings import Settings
from sovite.worker import Worker

OP = "operator-test-token-0000000000000000"
CALLER = "caller-test-token-00000000000000000"


def auth(token):
    return {"Authorization": "Bearer " + token}


@pytest.fixture(params=["sqlite", "postgresql"])
def setup(tmp_path, request):
    settings = Settings(database_url=f"sqlite:///{tmp_path}/state.db",
        artifact_path=str(tmp_path / "artifacts"), bundle_path=str(Path(__file__).resolve().parents[1] / "config/platform.local.yaml"),
        operator_token=OP, caller_tokens={"cloud-codex": CALLER}, require_qualification=False,
        setup_api_enabled=True, setup_installation_id="bayleaf-local")
    admin = None
    if request.param == "postgresql":
        url = os.environ.get("SOVITE_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("SOVITE_TEST_POSTGRES_URL is unset")
        admin = create_engine(url)
        schema = "setup_test_" + uuid.uuid4().hex
        with admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        settings = replace(settings, database_url=url, database_schema=schema)
    platform = Platform(settings)
    try:
        with TestClient(create_app(settings, platform)) as client:
            yield settings, platform, client
    finally:
        platform.db.engine.dispose()
        if admin:
            with admin.begin() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
            admin.dispose()


def mint(client):
    envelope = {"models": [{"id": "maintenance-model", "name": "Maintenance model", "adapter": "demo",
        "model": "synthetic-assessment-v1", "destination": "private-runtime"}],
        "harnesses": ["assessment-harness"], "grants": ["incident-forensics"], "max_steps": 4}
    response = client.post("/api/setup/principals", headers=auth(OP), json={"name": "bayleaf setup agent", "envelope": envelope})
    assert response.status_code == 201, response.text
    return response.json()


def proposal(client, principal, **changes):
    token = principal["token"]
    context = client.get("/api/setup/context", headers=auth(token)).json()
    body = {"idempotency_key": "maintenance-setup", "base_generation": context["generation"],
        "model": principal["envelope"]["models"][0], "agent_id": "maintenance-helper", "agent_name": "Maintenance helper",
        "harness": "assessment-harness", "instructions": "Summarize supplied household maintenance observations with source identifiers; do not invent actions.",
        "max_steps": 4, "grant": "incident-forensics"}
    body.update(changes)
    return body


def draft(client, principal, **changes):
    return client.post("/api/setup/drafts", headers=auth(principal["token"]), json=proposal(client, principal, **changes))


def activate(client, row, token=OP):
    return client.post(f"/api/setup/drafts/{row['id']}/activate", headers=auth(token), json={"generation": row["generation"]})


def test_setup_to_worker_receipt_and_private_artifact(setup):
    settings, platform, client = setup
    principal = mint(client)
    row = draft(client, principal).json()
    assert "bundle" not in row
    assert platform.active()[1] == row["base_generation"]
    assert activate(client, row, principal["token"]).status_code == 401
    inspected = client.get(f"/api/setup/drafts/{row['id']}", headers=auth(OP)).json()
    assert inspected["bundle"]["agents"][-1]["id"] == "maintenance-helper"
    assert activate(client, row).status_code == 200
    assert activate(client, row).status_code == 200
    assert platform.active()[1] == row["generation"]
    # Demonstrate a blocked execution and a fresh retry with an independent caller.
    platform.inhibit("incident-forensics", "Demonstrate blocked execution")
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "begin_task", "arguments": {
        "grant_id": "incident-forensics", "task": "Source M1: kitchen tap drips after closing. Identify observations and next checks.", "idempotency_key": "maintenance-run"}}}
    blocked = client.post("/mcp", headers=auth(CALLER), json=rpc)
    assert blocked.json()["result"]["isError"] is True
    platform.inhibit("incident-forensics", "Retry after operator review", False)
    accepted = client.post("/mcp", headers=auth(CALLER), json=rpc).json()["result"]["structuredContent"]
    assert set(accepted) == {"receipt", "status"}
    assert asyncio.run(Worker(platform).run_once())
    assert platform.public_status(accepted["receipt"], "cloud-codex")["status"] == "available"
    job = client.get("/api/jobs", headers=auth(OP)).json()["items"][0]
    assert client.get(f"/api/jobs/{job['id']}/artifact", headers=auth(CALLER)).status_code == 403
    artifact = client.get(f"/api/jobs/{job['id']}/artifact", headers=auth(OP))
    assert artifact.status_code == 200 and "kitchen tap" in artifact.text
    assert platform.db.verify_audit()["valid"]
    with platform.db.sessions() as session:
        stored = session.get(SetupPrincipal, principal["id"])
        assert stored.token_hash == hashlib.sha256(principal["token"].encode()).hexdigest()
        assert principal["token"] not in repr([e.detail for e in session.scalars(select(AuditEvent))])


@pytest.mark.parametrize("mutation", ["endpoint", "model", "harness", "grant", "steps", "tools"])
def test_envelope_cannot_be_widened(setup, mutation):
    _, _, client = setup
    principal = mint(client)
    body = proposal(client, principal)
    if mutation == "endpoint":
        body["model"] = {**body["model"], "endpoint": "http://unapproved.example"}
    elif mutation == "model":
        body["model"] = {**body["model"], "model": "different"}
    elif mutation == "steps":
        body["max_steps"] = 5
    elif mutation == "tools":
        body["tools"] = ["shell"]
    else:
        body[mutation] = "unapproved"
    response = client.post("/api/setup/drafts", headers=auth(principal["token"]), json=body)
    assert response.status_code in {403, 422}


def test_restart_isolation_revocation_expiry_and_stale_activation(setup):
    settings, platform, client = setup
    a, b = mint(client), mint(client)
    row = draft(client, a).json()
    assert client.get(f"/api/setup/my-drafts/{row['id']}", headers=auth(b["token"])).status_code == 404
    assert client.get(f"/api/setup/my-drafts/{row['id']}", headers=auth(a["token"])).status_code == 200
    for path in ("/api/jobs", "/api/audit", "/api/overview"):
        assert client.get(path, headers=auth(a["token"])).status_code == 401
    assert client.post("/api/setup/principals", headers=auth(a["token"]), json={"name": "self", "envelope": a["envelope"]}).status_code == 401
    restarted = Platform(settings)
    with TestClient(create_app(settings, restarted)) as other:
        assert other.get("/api/setup/context", headers=auth(a["token"])).status_code == 200
    foreign_settings = replace(settings, setup_installation_id="another-installation")
    with TestClient(create_app(foreign_settings, Platform(foreign_settings))) as foreign:
        assert foreign.get("/api/setup/context", headers=auth(a["token"])).status_code == 403
    with platform.db.sessions.begin() as session:
        session.get(SetupPrincipal, a["id"]).expires_at = time.time() - 1
    assert activate(client, row).status_code == 403
    assert client.get("/api/setup/context", headers=auth(a["token"])).status_code == 403
    row_b = draft(client, b).json()
    assert client.post(f"/api/setup/principals/{b['id']}/revoke", headers=auth(OP)).status_code == 200
    assert activate(client, row_b).status_code == 403
    assert client.post("/api/setup/drafts", headers=auth(b["token"]), json=proposal_body(row_b, b)).status_code == 403


def proposal_body(row, principal):
    return {"idempotency_key": "again", "base_generation": row["base_generation"], "model": principal["envelope"]["models"][0],
        "agent_id": "other-agent", "agent_name": "Other", "harness": "assessment-harness", "instructions": "Read only", "max_steps": 1, "grant": "incident-forensics"}


def test_idempotency_stale_base_and_no_reactivation(setup, tmp_path):
    _, platform, client = setup
    principal = mint(client)
    body = proposal(client, principal)
    row = client.post("/api/setup/drafts", headers=auth(principal["token"]), json=body).json()
    assert client.post("/api/setup/drafts", headers=auth(principal["token"]), json=body).json() == row
    assert draft(client, principal, instructions="changed").status_code == 409
    assert client.post(f"/api/setup/drafts/{row['id']}/activate", headers=auth(OP), json={"generation": "wrong"}).status_code == 409
    assert activate(client, row).status_code == 200
    platform.reload(platform.settings.bundle_path)
    assert activate(client, row).status_code == 409
    second = mint(client)
    stale = draft(client, second).json()
    import yaml
    bundle = copy.deepcopy(platform.active()[0])
    bundle["metadata"]["version"] = "changed"
    path = tmp_path / "changed.yaml"
    path.write_text(yaml.safe_dump(bundle))
    platform.reload(str(path))
    assert activate(client, stale).status_code == 409
    assert client.post("/api/setup/drafts", headers=auth(second["token"]), json=proposal_body(stale, second)).status_code == 409


def test_disabled_by_default_and_requires_installation(setup):
    settings, _, _ = setup
    disabled = replace(settings, setup_api_enabled=False)
    with TestClient(create_app(disabled, Platform(disabled))) as client:
        assert client.get("/api/setup/context").status_code == 404
    with pytest.raises(ValueError, match="installation identity"):
        replace(settings, setup_installation_id="")


def test_revoke_and_activation_share_the_authority_boundary(setup):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    settings, platform, client = setup
    principal = mint(client)
    row = draft(client, principal).json()
    barrier = Barrier(2)
    def operation(is_activation):
        other_platform = Platform(settings)
        try:
            with TestClient(create_app(settings, other_platform)) as other:
                barrier.wait(timeout=10)
                if is_activation:
                    return activate(other, row).status_code
                return other.post(f"/api/setup/principals/{principal['id']}/revoke", headers=auth(OP)).status_code
        finally:
            other_platform.db.engine.dispose()
    with ThreadPoolExecutor(max_workers=2) as pool:
        activated = pool.submit(operation, True)
        revoked = pool.submit(operation, False)
        activation_status, revocation_status = activated.result(), revoked.result()
    assert revocation_status == 200 and activation_status in {200, 403}
    with platform.db.sessions() as session:
        events = list(session.scalars(select(AuditEvent).order_by(AuditEvent.sequence)))
    revocation_sequence = next(e.sequence for e in events if e.action == "setup.principal_revoked")
    activations = [e for e in events if e.action == "setup.draft_activated"]
    if activation_status == 200:
        assert len(activations) == 1 and activations[0].sequence < revocation_sequence
    else:
        assert not activations and platform.active()[1] == row["base_generation"]
    assert activate(client, row).status_code == 403
    assert platform.db.verify_audit()["valid"]


def test_non_finite_approved_model_is_rejected_without_persistence(setup):
    import json
    _, platform, client = setup
    principal = mint(client)
    envelope = principal["envelope"]
    model = envelope["models"][0]
    model.update(adapter="ollama", endpoint="http://localhost:11434", parameters={"temperature": float("nan")})
    response = client.post("/api/setup/principals", headers={**auth(OP), "Content-Type": "application/json"},
        content=json.dumps({"name": "non-finite", "envelope": envelope}))
    assert response.status_code == 422
    with platform.db.sessions() as session:
        assert len(list(session.scalars(select(SetupPrincipal)))) == 1


def test_operator_can_recover_mint_inventory_without_credential_disclosure(setup):
    _, _, client = setup
    principal = mint(client)
    response = client.get("/api/setup/principals", headers=auth(OP))
    assert response.status_code == 200
    assert response.json()["items"][0]["id"] == principal["id"]
    assert "token" not in response.text and "token_hash" not in response.text
    assert principal["token"] not in response.text
    assert client.get("/api/setup/principals", headers=auth(principal["token"])).status_code == 401


def test_setup_enablement_and_installation_are_qualification_bound(setup):
    from sovite.qualification import runtime_settings_fingerprint
    settings, _, _ = setup
    enabled = runtime_settings_fingerprint(settings)
    assert enabled != runtime_settings_fingerprint(replace(settings, setup_api_enabled=False))
    assert enabled != runtime_settings_fingerprint(replace(settings, setup_installation_id="other-installation"))
    assert runtime_settings_fingerprint(replace(settings, setup_api_enabled=False)) == runtime_settings_fingerprint(
        replace(settings, setup_api_enabled=False, setup_installation_id="unused"))


def test_local_setup_cannot_enable_in_hosted_mode(setup):
    settings, _, _ = setup
    with pytest.raises(ValueError, match="local-only"):
        replace(settings, hosting_envelope_path="host-controlled-envelope.json")
    # Even mutation of an already constructed settings object cannot install routes.
    settings.hosting_envelope_path = "host-controlled-envelope.json"
    app = create_app(settings, Platform(settings))
    assert not any(route.path.startswith('/api/setup/') for route in app.routes)
