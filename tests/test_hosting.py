from __future__ import annotations

import base64
import copy
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from sovite import cli, control_client, policy
from sovite.api import create_app
from sovite.db import BundleRevision, MetadataRow, Qualification, canonical
from sovite.hosting import SCHEMA, DESTINATION, compile_bundle, envelope_binding, load_envelope
from sovite.qualification import (ROLE_CHECKS, current_qualification, export_qualification,
                                  import_qualification, issue_qualification, key_digest,
                                  runtime_settings_fingerprint, verify_qualification,
                                  write_qualification, validate_issuers)
from sovite.service import Platform, PlatformError
from sovite.settings import Settings
from sovite.worker import Worker


ROOT = Path(__file__).parents[1]
PIN = "sha256:" + "1" * 64
OPERATOR = "operator-000000000000000000000000"
AUTHOR = "author-00000000000000000000000000"
ACTIVATOR = "activator-00000000000000000000000"
CALLER = "caller-00000000000000000000000000"


def build_hosted(tmp_path):
    bundle = policy.load_bundle(ROOT / "config/platform.local.yaml")[0]
    bundle.update(apiVersion="sovite.platform/v1alpha2", mode="enforced", restricted_class="C3")
    for destination in bundle["destinations"]:
        destination.update(kind="private", clearance="C1", allowed_hosts=[destination["id"] + ".workspace.svc"], trust_tier="operator-owned", retention={
            "prompt_logging": False, "response_logging": False, "persistent_context": False,
            "in_memory_cache": {"scope": "job", "max_seconds": 300, "clear_on_cancel": True, "clear_on_session_close": True}})
    bundle["connector_roles"] = [{"id": "helper", "destination": "private-runtime", "helper_image_digest": PIN, "engine_image_digest": PIN}]
    bundle["models"][0].update(adapter="openai-compatible", enabled=True, wire_api="chat",
        connection={"kind": "model-gateway", "enrollment_role": "helper"}, identity={"protocol": "helper-signed-v1", "pinning": "required", "engine_version": "1", "weights_manifest_digest": PIN},
        max_concurrency=1, timeout_seconds=120, first_token_timeout_seconds=60, cancel_bound_seconds=6)
    bundle["harnesses"][0].update(kind="tool-agent", digest=PIN)
    bundle["compute"][0].update(kind="kubernetes", namespace="workspace")
    for grant in bundle["grants"]:
        grant.update(input_class="C1", output_class="C1", availability_wait_seconds=900)
    envelope = {"apiVersion": "sovite.hosting/v1", "workspace": "workspace-a", "installation_id": "installation-a", "revision": 1,
        "placement": {"namespace": "workspace", "resources": {"max_replicas": 1, "max_cpu_millis": 1000, "max_memory_bytes": 536870912}},
        "allowed_data_classes": ["C0", "C1"], "destinations": [copy.deepcopy({key: value for key, value in destination.items() if key in DESTINATION["properties"]}) for destination in bundle["destinations"]],
        "connector_kinds": ["model-gateway"], "connector_image_digests": [PIN], "harnesses": copy.deepcopy(bundle["harnesses"])}
    path = tmp_path / "envelope.json"
    path.write_text(json.dumps(envelope))
    bundle_path = tmp_path / "bundle.yaml"
    bundle_path.write_text(yaml.safe_dump(bundle))
    settings = Settings(database_url=f"sqlite:///{tmp_path}/control.db", bundle_path=str(bundle_path), artifact_path=str(tmp_path / "artifacts"),
        hosting_envelope_path=str(path), operator_token=OPERATOR, author_token=AUTHOR, activator_token=ACTIVATOR,
        caller_tokens={"cloud-codex": CALLER}, public_origin="http://testserver", secure_cookies=False)
    for role in ("platform", "tenant"):
        key = Ed25519PrivateKey.generate()
        private = tmp_path / (role + "-private.pem")
        public = tmp_path / (role + "-public.pem")
        private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        public.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
        entry = {"path": str(public), "role": role, "expires_at": time.time() + 86400}
        if role == "tenant":
            entry["workspace"] = envelope["workspace"]
        settings.qualification_issuers.append(entry)
    platform = Platform(settings)
    platform.initialize()
    generation = platform.compile(bundle, "author")
    platform.activate(generation, "activator")
    return settings, platform, bundle, envelope


@pytest.fixture
def hosted(tmp_path):
    return build_hosted(tmp_path)


def control_transport(client):
    """Forward control-client requests into an in-process application instance."""
    def handle(request):
        headers = {key: value for key, value in request.headers.items() if key.lower() not in {"host", "content-length"}}
        response = client.request(request.method, str(request.url), content=request.read(), headers=headers)
        return httpx.Response(response.status_code, headers=list(response.headers.items()), content=response.content)
    return httpx.MockTransport(handle)


def evidence(settings, generation, role):
    return {"generation": generation, "runtime_binding": runtime_settings_fingerprint(settings), "profile": "local", "evidence_kind": "deployment", "scope": "Policy fixture only; no deployment measurements", "checks": [{"name": name, "status": "passed"} for name in sorted(ROLE_CHECKS[role])]}


def signing(settings, role):
    return replace(settings, qualification_private_key=str(Path(settings.hosting_envelope_path).parent / (role + "-private.pem")))


def qualify(hosted, role, *, store=True):
    settings, platform, _, _ = hosted
    generation = platform.active()[1]
    return issue_qualification(platform.db if store else None, signing(settings, role), generation, "local", evidence(settings, generation, role), role=role)


def resign(row, settings, role):
    private = serialization.load_pem_private_key(Path(signing(settings, role).qualification_private_key).read_bytes(), password=None)
    row.signature = base64.b64encode(private.sign(canonical(row.payload))).decode()


def deny_dispatch(platform):
    with pytest.raises(PlatformError):
        platform.submit("incident-forensics", "PRIVATE", "cloud-codex", "denied", input_class="C1")


def test_hosted_compile_and_exported_schema(hosted):
    settings, platform, bundle, envelope = hosted
    Draft202012Validator.check_schema(SCHEMA)
    assert json.loads((ROOT / "config/hosting.schema.json").read_text()) == SCHEMA
    assert load_envelope(settings.hosting_envelope_path) == envelope
    assert platform.active()[1] == compile_bundle(bundle, envelope)[1] != policy.bundle_generation(bundle)
    deny_dispatch(platform)



def test_native_harness_binds_installed_hosting_envelope(hosted, monkeypatch, tmp_path):
    from sovite.adapters import AdapterRunner
    from sovite.harness_api import create_app as create_harness

    settings, _, bundle, envelope = hosted
    for model in bundle["models"]:
        model["wire_api"] = "responses"
    for harness in bundle["harnesses"]:
        harness["kind"] = "native-codex"
    envelope["harnesses"] = copy.deepcopy(bundle["harnesses"])
    Path(settings.bundle_path).write_text(yaml.safe_dump(bundle))
    Path(settings.hosting_envelope_path).write_text(json.dumps(envelope))
    generation = compile_bundle(bundle, envelope)[1]
    agent_id = bundle["agents"][0]["id"]
    executions = []

    async def fake_native(self, recipe, task, job_id, *, isolated=False):
        executions.append((recipe["generation"], task, job_id, isolated))
        return {"answer": "HOSTED TEST RESULT"}

    monkeypatch.setattr(AdapterRunner, "run_native", fake_native)
    monkeypatch.setenv("SOVITE_BUNDLE_PATH", settings.bundle_path)
    monkeypatch.setenv("SOVITE_HOSTING_ENVELOPE", settings.hosting_envelope_path)
    monkeypatch.setenv("SOVITE_HARNESS_AGENT_ID", agent_id)
    monkeypatch.setenv("SOVITE_HARNESS_TOKEN", CALLER)
    monkeypatch.setenv("SOVITE_HARNESS_STATE_PATH", str(tmp_path / "native"))
    body = {"task_id": "hosted-task", "agent_id": agent_id,
            "configuration_revision": generation, "input": {"task": "inspect"}}
    headers = {"Authorization": "Bearer " + CALLER, "Idempotency-Key": "hosted-task"}
    stale_envelope = {**envelope, "revision": envelope["revision"] + 1}
    with TestClient(create_harness()) as client:
        for mismatch in (policy.bundle_generation(bundle), compile_bundle(bundle, stale_envelope)[1]):
            wrong = {**body, "configuration_revision": mismatch}
            assert client.post("/run", headers=headers, json=wrong).json() == {"code": "binding_mismatch"}
            cancel = {key: value for key, value in wrong.items() if key != "input"}
            assert client.post("/cancel", headers=headers, json=cancel).status_code == 409
        assert executions == []
        response = client.post("/run", headers=headers, json=body)
        assert response.status_code == 200
        assert response.json() == {"answer": "HOSTED TEST RESULT"}
    assert executions == [(generation, "inspect", "hosted-task", True)]


@pytest.mark.parametrize("field", ["input_class", "output_class", "telemetry_class", "receipt_class", "caller_clearance"])
@pytest.mark.parametrize("data_class", ["C2", "C3"])
def test_hostile_high_data_classes(hosted, field, data_class):
    _, platform, bundle, envelope = hosted
    bundle["grants"][0][field] = data_class
    with pytest.raises(policy.PolicyError):
        compile_bundle(bundle, envelope)
    deny_dispatch(platform)


@pytest.mark.parametrize("path,value", [
    (("modelPeers",), ["external"]), (("modelCredentials",), {"secret": "ref"}), (("provider_egress",), True),
    (("models", 0, "endpoint"), "https://provider.example/v1"),
    (("compute", 0, "namespace"), "foreign"), (("compute", 0, "profile", "worker_image"), "arbitrary:latest"),
    (("compute", 0, "profile", "node_selector"), {"private": "node"}),
    (("compute", 0, "profile", "cpu"), "2"), (("compute", 0, "profile", "memory"), "1Gi"), (("compute", 0, "profile", "replicas"), 2),
    (("destinations", 0, "allowed_hosts"), ["foreign.other.svc"]), (("destinations", 0, "kind"), "cloud"),
    (("destinations", 0, "trust_tier"), "provider"),
    (("destinations", 0, "clearance"), "C3"),
    (("harnesses", 0, "image"), "arbitrary:latest"), (("harnesses", 0, "executable"), "/bin/sh"),
    (("harnesses", 0, "endpoint"), "https://provider.example/run"), (("harnesses", 0, "api_key_env"), "CLOUD_SECRET"),
    (("connector_roles", 0, "helper_image_digest"), "sha256:" + "2" * 64),
    (("models", 0, "connection", "kind"), "direct-provider"), (("unknown",), True),
])
def test_hostile_bundle_envelope_negatives(hosted, path, value):
    _, _, bundle, envelope = hosted
    target = bundle
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(policy.PolicyError):
        compile_bundle(bundle, envelope)


def test_hosted_tools_and_unapproved_c1_refused(hosted):
    _, _, bundle, envelope = hosted
    envelope["allowed_data_classes"] = ["C0"]
    with pytest.raises(ValueError):
        compile_bundle(bundle, envelope)
    envelope["allowed_data_classes"] = ["C0", "C1"]
    bundle["tools"] = [{"id": "protected", "kind": "kubernetes-read", "namespace": "workspace", "input_class": "C1", "output_class": "C1", "destination": "private-runtime", "allowed_operations": ["list-pods"]}]
    with pytest.raises(policy.PolicyError, match="tools"):
        compile_bundle(bundle, envelope)


@pytest.mark.parametrize("path,value", [(("extra",), True), (("placement", "extra"), True), (("revision",), 0),
    (("allowed_data_classes",), ["C0", "C2"]), (("connector_kinds",), ["provider"]),
    (("destinations", 0, "allowed_hosts"), ["provider.example"]), (("harnesses", 0, "executable"), "/bin/sh")])
def test_envelope_strict_and_service_only(hosted, path, value):
    settings, _, _, envelope = hosted
    target = envelope
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    Path(settings.hosting_envelope_path).write_text(json.dumps(envelope))
    with pytest.raises(ValueError):
        load_envelope(settings.hosting_envelope_path)


@pytest.mark.parametrize("raw", [b'{"workspace":"one","workspace":"two"}', b'{"revision":NaN}', b' ' * 262145, b'[' * 2000])
def test_envelope_bounded_json(hosted, raw):
    settings = hosted[0]
    Path(settings.hosting_envelope_path).write_bytes(raw)
    with pytest.raises(ValueError):
        load_envelope(settings.hosting_envelope_path)


@pytest.mark.parametrize("role", ["platform", "tenant"])
def test_single_role_refuses_dispatch(hosted, role):
    settings, platform, _, _ = hosted
    row = qualify(hosted, role)
    assert verify_qualification(row, settings)
    with platform.db.sessions() as session:
        assert current_qualification(session, settings, row.generation) is None
    deny_dispatch(platform)
    qualify(hosted, "tenant" if role == "platform" else "platform")
    assert platform.submit("incident-forensics", "fixture", "cloud-codex", "qualified", input_class="C1").generation == row.generation


@pytest.mark.parametrize("signer,claimed", [("tenant", "platform"), ("platform", "tenant")])
def test_signer_cannot_substitute_role(hosted, signer, claimed):
    settings, platform, _, _ = hosted
    row = qualify(hosted, signer, store=False)
    row.payload["role"] = claimed
    row.payload["evidence"] = evidence(settings, row.generation, claimed)
    resign(row, settings, signer)
    assert not verify_qualification(row, settings)
    with platform.db.sessions.begin() as session:
        session.add(row)
    qualify(hosted, signer)
    deny_dispatch(platform)


@pytest.mark.parametrize("role,missing", [(role, name) for role, names in ROLE_CHECKS.items() for name in sorted(names)])
def test_incomplete_role_evidence_refuses_dispatch(hosted, role, missing):
    settings, platform, _, _ = hosted
    row = qualify(hosted, role, store=False)
    row.payload["evidence"]["checks"] = [check for check in row.payload["evidence"]["checks"] if check["name"] != missing]
    resign(row, settings, role)
    assert not verify_qualification(row, settings)
    with platform.db.sessions.begin() as session:
        session.add(row)
    qualify(hosted, "tenant" if role == "platform" else "platform")
    deny_dispatch(platform)
    with pytest.raises(ValueError, match="complete"):
        issue_qualification(None, signing(settings, role), row.generation, "local", row.payload["evidence"], role=role)


@pytest.mark.parametrize("role", ["platform", "tenant"])
@pytest.mark.parametrize("failure", ["expired_record", "expired_key", "future_key", "revoked_key", "wrong_workspace", "not_run", "failed", "duplicate_check"])
def test_invalid_qualification_refuses_dispatch(hosted, role, failure):
    settings, platform, _, _ = hosted
    row = qualify(hosted, role, store=False)
    entry = next(entry for entry in settings.qualification_issuers if entry["role"] == role)
    if failure == "expired_record":
        row.expires_at = row.payload["expires_at"] = time.time() - 1
    elif failure == "expired_key":
        entry["expires_at"] = time.time() - 1
    elif failure == "future_key":
        entry["not_before"] = time.time() + 60
    elif failure == "revoked_key":
        settings.qualification_revoked_keys = [key_digest(row.public_key)]
    elif failure == "wrong_workspace":
        row.payload["workspace"] = "other-workspace"
    elif failure == "duplicate_check":
        row.payload["evidence"]["checks"].append(copy.deepcopy(row.payload["evidence"]["checks"][0]))
    else:
        row.payload["evidence"]["checks"][0]["status"] = failure
    resign(row, settings, role)
    assert not verify_qualification(row, settings)
    with platform.db.sessions.begin() as session:
        session.add(row)
    other = "tenant" if role == "platform" else "platform"
    qualify(hosted, other)
    deny_dispatch(platform)


def test_tenant_key_workspace_scope_and_dual_use_key_refused(hosted):
    settings, _, _, _ = hosted
    row = qualify(hosted, "tenant", store=False)
    tenant = settings.qualification_issuers[1]
    tenant["workspace"] = "other-workspace"
    assert not verify_qualification(row, settings)
    tenant["workspace"] = "workspace-a"
    settings.qualification_issuers[0]["path"] = tenant["path"]
    assert not verify_qualification(row, settings)


@pytest.mark.parametrize("change", ["workspace", "installation_id", "revision", "resources", "namespace", "missing_envelope"])
def test_changed_hosting_binding_refuses_dispatch(hosted, change):
    settings, platform, bundle, envelope = hosted
    rows = [qualify(hosted, role) for role in ("platform", "tenant")]
    before = runtime_settings_fingerprint(settings)
    if change == "resources":
        envelope["placement"]["resources"]["max_cpu_millis"] = 2000
    elif change == "namespace":
        envelope["placement"]["namespace"] = "newspace"
        for destination in envelope["destinations"]:
            destination["allowed_hosts"] = [host.replace(".workspace.", ".newspace.") for host in destination["allowed_hosts"]]
    elif change == "revision":
        envelope[change] += 1
    elif change != "missing_envelope":
        envelope[change] = "changed"
    if change == "missing_envelope":
        Path(settings.hosting_envelope_path).unlink()
    else:
        Path(settings.hosting_envelope_path).write_text(json.dumps(envelope))
        assert runtime_settings_fingerprint(settings) != before
    assert all(not verify_qualification(row, settings) for row in rows)
    with platform.db.sessions() as session:
        assert current_qualification(session, settings, rows[0].generation) is None
    deny_dispatch(platform)
    if change in {"resources", "revision", "workspace", "installation_id"}:
        generation = platform.compile(bundle, "author")
        assert generation != rows[0].generation
        platform.activate(generation, "activator")
        deny_dispatch(platform)


def test_author_cannot_activate_and_operator_has_no_hosted_policy_role(hosted):
    settings, platform, bundle, _ = hosted
    old = platform.active()[1]
    bundle["metadata"]["version"] = "draft"
    with TestClient(create_app(settings, platform)) as client:
        assert client.get("/api/auth/session", headers={"Authorization": "Bearer " + CALLER}).status_code == 403
        assert client.delete("/api/auth/session", headers={"Authorization": "Bearer " + CALLER}).status_code == 403
        response = client.post("/api/bundles", headers={"Authorization": "Bearer " + AUTHOR}, json={"bundle": bundle})
        assert response.status_code == 201
        generation = response.json()["generation"]
        assert platform.active()[1] == old
        for token in (AUTHOR, OPERATOR, CALLER):
            assert client.post(f"/api/bundles/{generation}/activate", headers={"Authorization": "Bearer " + token}).status_code == 403
        for token in (ACTIVATOR, OPERATOR, CALLER):
            assert client.post("/api/bundles", headers={"Authorization": "Bearer " + token}, json={"bundle": bundle}).status_code == 403
        assert client.post("/api/auth/session", json={"token": AUTHOR}).status_code == 200
        assert client.get("/api/auth/session").json()["role"] == "author"
        assert client.post(f"/api/bundles/{generation}/activate", headers={"Origin": settings.public_origin}).status_code == 403
        client.delete("/api/auth/session", headers={"Origin": settings.public_origin})
        assert client.post("/api/auth/session", json={"token": ACTIVATOR}).status_code == 200
        assert client.post(f"/api/bundles/{generation}/activate").status_code == 403
        assert client.post(f"/api/bundles/{generation}/activate", headers={"Origin": settings.public_origin}).status_code == 200
        assert platform.active()[1] == generation
    for role in ("author", "operator"):
        with pytest.raises(PlatformError):
            platform.reload(settings.bundle_path, role=role)


def test_first_hosted_startup_does_not_activate(hosted):
    settings, platform, _, _ = hosted
    with platform.db.sessions.begin() as session:
        session.delete(session.get(MetadataRow, "active_generation"))
    platform.initialize()
    with pytest.raises(PlatformError, match="no active"):
        platform.active()
    with pytest.raises(PlatformError):
        platform.activate("sha256:" + "0" * 64, "activator")


def test_signed_import_export_roundtrip_without_runtime_private_keys(hosted, tmp_path):
    settings, platform, _, _ = hosted
    generation = platform.active()[1]
    settings.qualification_private_key = str(tmp_path / "absent-private.pem")
    for role in ("platform", "tenant"):
        row = qualify(hosted, role, store=False)
        path = tmp_path / (role + ".json")
        write_qualification(row, str(path))
        imported = import_qualification(platform.db, settings, generation, str(path))
        assert verify_qualification(imported, settings)
        output = tmp_path / (role + "-exported.json")
        export_qualification(platform.db, row.id, str(output))
        assert json.loads(output.read_text()) == json.loads(path.read_text())
        with pytest.raises(ValueError, match="already"):
            import_qualification(platform.db, settings, generation, str(path))
    assert platform.submit("incident-forensics", "test", "cloud-codex", "roundtrip", input_class="C1")


@pytest.mark.parametrize("change", ["signature", "public_key_id", "workspace", "role", "runtime_binding", "generation", "hosting", "profile", "evidence", "unknown", "expires_at"])
def test_signed_import_refusals(hosted, tmp_path, change):
    settings, platform, _, _ = hosted
    row = qualify(hosted, "tenant", store=False)
    path = tmp_path / "import.json"
    write_qualification(row, str(path))
    document = json.loads(path.read_text())
    if change in {"signature", "public_key_id"}:
        document[change] = "forged"
    elif change == "evidence":
        document["payload"][change]["checks"] = []
    elif change == "expires_at":
        document["payload"][change] = time.time() - 1
    else:
        document["payload"][change] = "foreign"
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        import_qualification(platform.db, settings, row.generation, str(path))
    deny_dispatch(platform)


def test_cross_workspace_tenant_replay_refuses_import(hosted, tmp_path):
    settings, platform, bundle, envelope = hosted
    row = qualify(hosted, "tenant", store=False)
    path = tmp_path / "tenant.json"
    write_qualification(row, str(path))
    envelope["workspace"] = "workspace-b"
    Path(settings.hosting_envelope_path).write_text(json.dumps(envelope))
    generation = platform.compile(bundle, "author")
    platform.activate(generation, "activator")
    with pytest.raises(ValueError, match="generation"):
        import_qualification(platform.db, settings, generation, str(path))
    row.payload.update(workspace="workspace-b", generation=generation, hosting=envelope_binding(envelope), runtime_binding=runtime_settings_fingerprint(settings))
    row.generation = generation
    row.payload["evidence"] = evidence(settings, generation, "tenant")
    resign(row, settings, "tenant")
    write_qualification(row, str(path))
    with pytest.raises(ValueError, match="not trusted"):
        import_qualification(platform.db, settings, generation, str(path))
    deny_dispatch(platform)


def test_revocation_rechecked_at_claim_and_release(hosted):
    settings, platform, _, _ = hosted
    qualify(hosted, "platform")
    tenant = qualify(hosted, "tenant")
    first = platform.submit("incident-forensics", "private", "cloud-codex", "first", input_class="C1")
    settings.qualification_revoked_keys = [key_digest(tenant.public_key)]
    worker = Worker(platform)
    assert worker.claim() is None
    assert platform.get_job(first.id).status == "blocked"
    settings.qualification_revoked_keys = []
    second = platform.submit("incident-forensics", "private", "cloud-codex", "second", input_class="C1")
    assert worker.claim().id == second.id
    artifact = platform.store.put(second.id, {"private": "result"})
    settings.qualification_revoked_keys = [key_digest(tenant.public_key)]
    worker.finish(second.id, artifact=artifact)
    assert platform.get_job(second.id).status == "blocked"
    assert platform.public_status(second.receipt, second.caller)["status"] == "unavailable"


def test_offline_sign_and_import_cli(hosted, tmp_path, monkeypatch, capsys):
    settings, platform, _, _ = hosted
    generation = platform.active()[1]
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(evidence(settings, generation, "tenant")))
    output = tmp_path / "offline.json"
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls, **kwargs: signing(settings, "tenant")))
    with monkeypatch.context() as isolated:
        isolated.setattr(Platform, "initialize", lambda self: pytest.fail("Offline signing must not initialize runtime or open DB"))
        isolated.setattr(cli, "Platform", lambda *args: pytest.fail("Offline signing must not construct the runtime"))
        isolated.setattr(sys, "argv", ["sovite", "qualify", "--role", "tenant", "--evidence", str(evidence_path), "--output", str(output)])
        cli.main()
    monkeypatch.setattr(sys, "argv", ["sovite", "qualification-import", str(output)])
    cli.main()
    row_id = json.loads(output.read_text())["payload"]["id"]
    exported = tmp_path / "exported.json"
    monkeypatch.setattr(sys, "argv", ["sovite", "qualification-export", row_id, "--output", str(exported)])
    cli.main()
    assert json.loads(exported.read_text()) == json.loads(output.read_text())
    capsys.readouterr()


def test_hosted_cli_reload_refused_for_all_roles(hosted, monkeypatch, capsys):
    settings, platform, bundle, _ = hosted
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: settings))
    monkeypatch.setattr(sys, "argv", ["sovite", "reload"])
    old = platform.active()[1]
    bundle["metadata"]["version"] = "cli"
    Path(settings.bundle_path).write_text(yaml.safe_dump(bundle))
    for token in ("", AUTHOR, OPERATOR, ACTIVATOR):
        monkeypatch.setenv("SOVITE_ADMIN_TOKEN", token)
        with pytest.raises(SystemExit):
            cli.main()
        assert platform.active()[1] == old
    capsys.readouterr()


def test_nonhosted_generation_hashes_and_operator_qualification_unchanged(tmp_path):
    pins = {"local": "2758d96f24d9d89fd23d34b247bafa871983572b4d276f426f80c299ee81b8fe", "ollama": "58fffe62a9c72afcd022ac562d691d4991c3d94dd23933bbb121d899e9074204", "kubernetes": "8a676c0d9470acb39c3f30a039ac34242735c0d8ab5617b7174f47aeeb35976d"}
    for name, pin in pins.items():
        bundle = policy.load_bundle(ROOT / ("config/platform." + name + ".yaml"))[0]
        assert compile_bundle(bundle)[1] == "sha256:" + pin
    settings = Settings(database_url=f"sqlite:///{tmp_path}/legacy.db", artifact_path=str(tmp_path / "artifacts"), bundle_path=str(ROOT / "config/platform.local.yaml"), qualification_private_key=str(tmp_path / "operator.pem"), qualification_public_keys=[str(tmp_path / "operator.pub")])
    from sovite.qualification import initialize_keys
    initialize_keys(settings)
    platform = Platform(settings)
    platform.initialize()
    generation = platform.active()[1]
    row = issue_qualification(platform.db, settings, generation, "local", {"generation": generation, "profile": "local", "scope": "Legacy synthetic fixture", "checks": [{"name": "receipt-boundary", "status": "passed"}]})
    assert row.payload["schema"] == "sovite.qualification/v1" and "role" not in row.payload
    assert verify_qualification(row, settings)
    assert platform.submit("incident-forensics", "private", "cloud-codex", "legacy")
    assert platform.reload(settings.bundle_path) == generation


def test_nonhosted_runtime_fingerprint_pinned():
    settings = Settings(database_url="sqlite:///:memory:", artifact_path="/artifacts")
    assert runtime_settings_fingerprint(settings) == "sha256:93ce0e96f43e1007b056413f7003aa04ed50d0373769c9b8c3a2dc154325d7e7"
    assert runtime_settings_fingerprint(replace(settings, author_token=AUTHOR, activator_token=ACTIVATOR)) == runtime_settings_fingerprint(settings)


def test_resource_ceiling_covers_all_pools_and_replicas(hosted):
    _, _, bundle, envelope = hosted
    other = copy.deepcopy(bundle["compute"][0])
    other["id"] = "extra"
    bundle["compute"].append(other)
    with pytest.raises(policy.PolicyError, match="Aggregate"):
        compile_bundle(bundle, envelope)
    bundle["compute"].pop()
    envelope["placement"]["resources"]["max_replicas"] = 2
    bundle["compute"][0]["profile"]["replicas"] = 2
    with pytest.raises(policy.PolicyError, match="Aggregate"):
        compile_bundle(bundle, envelope)


@pytest.mark.parametrize("failure", ["role", "evidence", "expired_key", "revoked_key", "runtime_binding", "expired_record"])
def test_correctly_signed_hostile_import_refused(hosted, tmp_path, failure):
    settings, platform, _, _ = hosted
    row = qualify(hosted, "tenant", store=False)
    if failure == "role":
        row.payload["role"] = "platform"
        row.payload["evidence"] = evidence(settings, row.generation, "platform")
    elif failure == "evidence":
        row.payload["evidence"]["checks"] = []
    elif failure == "expired_key":
        settings.qualification_issuers[1]["expires_at"] = time.time() - 1
    elif failure == "revoked_key":
        settings.qualification_revoked_keys = [key_digest(row.public_key)]
    elif failure == "runtime_binding":
        row.payload["runtime_binding"] = "sha256:" + "0" * 64
    else:
        row.expires_at = row.payload["expires_at"] = time.time() - 1
    resign(row, settings, "tenant")
    path = tmp_path / "hostile.json"
    write_qualification(row, str(path))
    with pytest.raises(ValueError, match="Invalid qualification evidence" if failure == "evidence" else "not trusted"):
        import_qualification(platform.db, settings, row.generation, str(path))
    with platform.db.sessions() as session:
        assert session.get(Qualification, row.id) is None
    deny_dispatch(platform)


def test_hosted_cannot_disable_qualification(hosted):
    settings, platform, _, _ = hosted
    qualify(hosted, "platform")
    qualify(hosted, "tenant")
    settings.require_qualification = False
    deny_dispatch(platform)
    with pytest.raises(PlatformError):
        platform.reload(settings.bundle_path, role="activator")


def test_hosted_settings_require_separate_roles_and_scoped_keys(hosted, monkeypatch):
    settings, _, _, _ = hosted
    for key, value in {
        "SOVITE_OPERATOR_TOKEN": OPERATOR, "SOVITE_AUTHOR_TOKEN": AUTHOR, "SOVITE_ACTIVATOR_TOKEN": ACTIVATOR,
        "SOVITE_HOSTING_ENVELOPE": settings.hosting_envelope_path,
        "SOVITE_QUALIFICATION_ISSUERS": json.dumps(settings.qualification_issuers),
    }.items():
        monkeypatch.setenv(key, value)
    result = Settings.from_env()
    assert result.hosting_envelope_path == settings.hosting_envelope_path
    assert result.qualification_issuers == settings.qualification_issuers
    with monkeypatch.context() as bad:
        bad.setenv("SOVITE_ACTIVATOR_TOKEN", AUTHOR)
        with pytest.raises(ValueError, match="distinct"):
            Settings.from_env()
    with monkeypatch.context() as bad:
        bad.setenv("SOVITE_REQUIRE_QUALIFICATION", "false")
        with pytest.raises(ValueError, match="Hosted"):
            Settings.from_env()
    with monkeypatch.context() as bad:
        bad.setenv("SOVITE_QUALIFICATION_ISSUERS", '[{"path":"tenant.pem","role":"tenant","expires_at":9999999999}]')
        with pytest.raises(ValueError, match="workspace"):
            Settings.from_env()
    with monkeypatch.context() as bad:
        bad.setenv("SOVITE_QUALIFICATION_REVOKED_KEYS", '["invalid"]')
        with pytest.raises(ValueError, match="digests"):
            Settings.from_env()


def test_offline_configuration_needs_no_runtime_credentials(hosted, monkeypatch):
    settings = hosted[0]
    for name in ("OPERATOR_TOKEN", "AUTHOR_TOKEN", "ACTIVATOR_TOKEN", "CALLER_TOKENS"):
        monkeypatch.delenv("SOVITE_" + name, raising=False)
    monkeypatch.setenv("SOVITE_HOSTING_ENVELOPE", settings.hosting_envelope_path)
    offline = Settings.from_env(require_auth=False)
    assert not offline.operator_token and not offline.author_token and not offline.activator_token
    assert runtime_settings_fingerprint(offline)
    with pytest.raises(ValueError, match="OPERATOR_TOKEN"):
        Settings.from_env()


def test_database_worker_has_its_own_credential_free_settings_role(monkeypatch):
    for name in (
        "SOVITE_OPERATOR_TOKEN", "SOVITE_AUTHOR_TOKEN", "SOVITE_ACTIVATOR_TOKEN",
        "SOVITE_CALLER_TOKENS", "SOVITE_AUTHORITY_TOKEN", "SOVITE_EXECUTION_TOKENS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SOVITE_DATABASE_URL", "sqlite:///:memory:")

    worker_settings = Settings.from_env(auth_role="worker")
    assert worker_settings.database_url == "sqlite:///:memory:"
    assert worker_settings.operator_token == ""
    assert worker_settings.author_token == ""
    assert worker_settings.activator_token == ""
    assert worker_settings.caller_tokens == {}
    assert worker_settings.authority_token == ""
    assert worker_settings.execution_tokens == {}
    with pytest.raises(ValueError, match="SOVITE_OPERATOR_TOKEN"):
        Settings.from_env()


def test_worker_command_loads_worker_role_without_relaxing_server_auth(monkeypatch):
    for name in (
        "SOVITE_OPERATOR_TOKEN", "SOVITE_AUTHOR_TOKEN", "SOVITE_ACTIVATOR_TOKEN",
        "SOVITE_CALLER_TOKENS", "SOVITE_AUTHORITY_TOKEN", "SOVITE_EXECUTION_TOKENS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SOVITE_DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setattr(sys, "argv", ["sovite", "worker"])
    observed = []

    class FakePlatform:
        def __init__(self, settings):
            self.settings = settings

        def initialize(self):
            observed.append("initialized")

    class FakeStop:
        def set(self):
            pass

    class FakeWorker:
        def __init__(self, platform):
            assert platform.settings.operator_token == ""
            self.stop = FakeStop()

        async def run(self):
            observed.append("worker_started")

    monkeypatch.setattr(cli, "Platform", FakePlatform)
    monkeypatch.setattr(cli, "Worker", FakeWorker)
    cli.main()
    assert observed == ["initialized", "worker_started"]


def test_worker_health_command_loads_credential_free_role_and_server_stays_protected(
    tmp_path, monkeypatch, capsys
):
    for name in (
        "SOVITE_OPERATOR_TOKEN", "SOVITE_AUTHOR_TOKEN", "SOVITE_ACTIVATOR_TOKEN",
        "SOVITE_CALLER_TOKENS", "SOVITE_AUTHORITY_TOKEN", "SOVITE_EXECUTION_TOKENS",
    ):
        monkeypatch.delenv(name, raising=False)
    database_url = f"sqlite:///{tmp_path / 'health.sqlite'}"
    artifact_path = str(tmp_path / "artifacts")
    platform = Platform(Settings(
        database_url=database_url, operator_token="", artifact_path=artifact_path,
        require_qualification=False,
    ))
    platform.initialize()
    Worker(platform).heartbeat()
    monkeypatch.setenv("SOVITE_DATABASE_URL", database_url)
    monkeypatch.setenv("SOVITE_ARTIFACT_PATH", artifact_path)
    monkeypatch.setenv("SOVITE_REQUIRE_QUALIFICATION", "false")
    monkeypatch.setattr(sys, "argv", ["sovite", "worker-health"])
    cli.main()
    output = json.loads(capsys.readouterr().out)
    assert output["healthy_workers"] == 1
    assert output["compute_id"] == "local"

    monkeypatch.setattr(sys, "argv", ["sovite", "serve"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    assert "SOVITE_OPERATOR_TOKEN" in capsys.readouterr().err


def test_review_1_standalone_has_no_bundle_mutation_api(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path}/standalone.db", operator_token=OPERATOR,
                        public_origin="http://testserver", secure_cookies=False)
    platform = Platform(settings)
    with TestClient(create_app(settings, platform)) as client:
        bundle, generation = platform.active()
        assert not any(route.path.startswith("/api/bundles") for route in client.app.routes)
        for cookie in (False, True):
            if cookie:
                assert client.post("/api/auth/session", json={"token": OPERATOR}).status_code == 200
            headers = {"Origin": settings.public_origin} if cookie else {"Authorization": "Bearer " + OPERATOR}
            assert client.post("/api/bundles", headers=headers, json={"bundle": bundle}).status_code == 404
            assert client.post(f"/api/bundles/{generation}/activate", headers=headers).status_code == 404
        with pytest.raises(PlatformError, match="Activator"):
            platform.policy_role("operator", "activator", api=True)


def test_review_2a_hosted_reload_refused(hosted):
    settings, platform, bundle, envelope = hosted
    before = platform.active()[1]
    bundle["metadata"]["version"] = "must-remain-uncompiled"
    Path(settings.bundle_path).write_text(yaml.safe_dump(bundle))
    generation = compile_bundle(bundle, envelope)[1]
    for role in ("activator", "author", "operator"):
        with pytest.raises(PlatformError, match="compile.*activate"):
            platform.reload(settings.bundle_path, role=role)
    assert platform.active()[1] == before
    with platform.db.sessions() as session:
        assert session.get(BundleRevision, generation) is None


def cli_env(monkeypatch, settings):
    for name in list(os.environ):
        if name.startswith("SOVITE_"):
            monkeypatch.delenv(name)
    for name, value in {
        "DATABASE_URL": settings.database_url, "BUNDLE_PATH": settings.bundle_path,
        "ARTIFACT_PATH": settings.artifact_path, "HOSTING_ENVELOPE": settings.hosting_envelope_path,
        "QUALIFICATION_ISSUERS": json.dumps(settings.qualification_issuers),
    }.items():
        monkeypatch.setenv("SOVITE_" + name, value)


def test_review_2b_cli_only_author_credentials_cannot_activate(hosted, monkeypatch, capsys):
    settings, platform, bundle, _ = hosted
    cli_env(monkeypatch, settings)
    monkeypatch.setenv("SOVITE_CONTROL_URL", "https://control.example")
    monkeypatch.setenv("SOVITE_AUTHOR_TOKEN", AUTHOR)
    monkeypatch.setenv("SOVITE_ADMIN_TOKEN", AUTHOR)
    bundle["metadata"]["version"] = "author-draft"
    Path(settings.bundle_path).write_text(yaml.safe_dump(bundle))
    before = platform.active()[1]
    with TestClient(create_app(settings, platform)) as client:
        monkeypatch.setattr(control_client, "_client", lambda *, verify, transport=None: httpx.Client(
            transport=control_transport(client), trust_env=False, follow_redirects=False, verify=False))
        monkeypatch.setattr(sys, "argv", ["sovite", "compile"])
        cli.main()
        generation = json.loads(capsys.readouterr().out)["generation"]
        assert generation != before and platform.active()[1] == before
        # The selected role variable is authoritative: an author token there is refused
        # by the server rather than falling back to the configured activator token.
        monkeypatch.setenv("SOVITE_ACTIVATOR_TOKEN", AUTHOR)
        monkeypatch.setattr(sys, "argv", ["sovite", "activate", generation])
        with pytest.raises(SystemExit) as error:
            cli.main()
        assert error.value.code == 1 and platform.active()[1] == before
        capsys.readouterr()
        monkeypatch.delenv("SOVITE_AUTHOR_TOKEN")
        monkeypatch.setenv("SOVITE_ACTIVATOR_TOKEN", ACTIVATOR)
        monkeypatch.setenv("SOVITE_ADMIN_TOKEN", ACTIVATOR)
        cli.main()
        assert json.loads(capsys.readouterr().out)["generation"] == generation
        assert platform.active()[1] == generation


def test_review_3_standalone_outputs_preserved(tmp_path, monkeypatch, capsys):
    settings = Settings(database_url=f"sqlite:///{tmp_path}/standalone.db", operator_token=OPERATOR)
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/flows", headers={"Authorization": "Bearer " + OPERATOR}).json() == policy.flow_projection(policy.load_bundle(settings.bundle_path)[0])
    cli_env(monkeypatch, replace(settings, hosting_envelope_path=""))
    monkeypatch.setenv("SOVITE_BUNDLE_PATH", "/missing/ignored-for-standalone-validate.yaml")
    monkeypatch.setattr(sys, "argv", ["sovite", "validate"])
    cli.main()
    assert json.loads(capsys.readouterr().out)["generation"] == policy.load_bundle("config/platform.local.yaml")[1]


@pytest.mark.parametrize("command", [["keys", "init"], ["runtime-binding"]])
def test_review_3_standalone_keys_runtime_binding_require_operator(tmp_path, monkeypatch, capsys, command):
    settings = Settings(database_url=f"sqlite:///{tmp_path}/standalone.db")
    cli_env(monkeypatch, replace(settings, hosting_envelope_path=""))
    monkeypatch.setenv("SOVITE_QUALIFICATION_PRIVATE_KEY", str(tmp_path / "private.pem"))
    monkeypatch.setenv("SOVITE_QUALIFICATION_PUBLIC_KEYS", json.dumps([str(tmp_path / "public.pem")]))
    monkeypatch.setattr(sys, "argv", ["sovite", *command])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1 and "OPERATOR_TOKEN" in capsys.readouterr().err
    monkeypatch.setenv("SOVITE_OPERATOR_TOKEN", OPERATOR)
    cli.main()
    assert capsys.readouterr().out


@pytest.mark.parametrize("field", ["prompt_logging", "response_logging", "persistent_context", "in_memory_cache"])
def test_review_4_hosted_retention_gap(hosted, field):
    _, _, bundle, envelope = hosted
    # private-state is not an inference destination; the base v2 policy allows logging there.
    destination = next(item for item in bundle["destinations"] if item["id"] == "private-state")
    if field == "in_memory_cache":
        destination["retention"][field]["max_seconds"] += 1
    else:
        destination["retention"][field] = True
    policy.validate_bundle(bundle)
    with pytest.raises(policy.PolicyError, match="Destination exceeds"):
        compile_bundle(bundle, envelope)


def test_review_5_offline_qualify_output_requires_enforced_runtime_binding(hosted, tmp_path, monkeypatch, capsys):
    settings, platform, bundle, _ = hosted
    # Standalone enforced v2 was the bypass: hosted evidence already required a binding.
    settings = replace(signing(settings, "tenant"), hosting_envelope_path=None,
                       qualification_issuers=[], qualification_public_keys=[settings.qualification_issuers[1]["path"]])
    generation = policy.bundle_generation(bundle)
    checks = {"generation": generation, "profile": "local", "scope": "Regression fixture", "checks": [{"name": "runtime-identity", "status": "passed"}]}
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(checks))
    output = tmp_path / "qualification.json"
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls, **kwargs: settings))
    monkeypatch.setattr(sys, "argv", ["sovite", "qualify", "--evidence", str(evidence_path), "--output", str(output)])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1 and "bind the runtime" in capsys.readouterr().err
    assert not output.exists()
    checks["runtime_binding"] = runtime_settings_fingerprint(settings)
    evidence_path.write_text(json.dumps(checks))
    cli.main()
    assert output.exists()


@pytest.mark.parametrize("created", ["before_not_before", "at_not_before", "at_expires_at"])
def test_review_6_trusted_key_rejects_backdated_signatures(hosted, created):
    settings = hosted[0]
    row = qualify(hosted, "tenant", store=False)
    now = time.time()
    entry = settings.qualification_issuers[1]
    entry.update(not_before=now - 100, expires_at=now + 100)
    timestamp = {"before_not_before": now - 101, "at_not_before": now - 100, "at_expires_at": now + 100}[created]
    row.created_at = row.payload["created_at"] = timestamp
    resign(row, settings, "tenant")
    assert verify_qualification(row, settings, now=now + 50) == (created == "at_not_before")


@pytest.mark.parametrize("field,path", [("ID", ("workspace",)), ("DIGEST", ("connector_image_digests", 0)), ("NAMESPACE", ("placement", "namespace"))])
def test_review_7_envelope_schema_patterns_reject_trailing_newlines(hosted, field, path):
    settings, _, _, envelope = hosted
    target = envelope
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] += "\n"
    schema = json.loads((ROOT / "config/hosting.schema.json").read_text())
    assert not Draft202012Validator(schema).is_valid(envelope), field
    Path(settings.hosting_envelope_path).write_text(json.dumps(envelope))
    with pytest.raises(ValueError):
        load_envelope(settings.hosting_envelope_path)


def test_review_8_same_key_under_two_roles_raises_at_startup(hosted, monkeypatch):
    settings, _, _, _ = hosted
    # A second file with the same key must also be detected.
    Path(settings.qualification_issuers[0]["path"]).write_text(Path(settings.qualification_issuers[1]["path"]).read_text())
    with pytest.raises(ValueError, match="(?i)key.*(duplicate|one issuer|multiple|more than one)"):
        validate_issuers(settings)
    cli_env(monkeypatch, settings)
    with pytest.raises(ValueError, match="(?i)key.*(duplicate|one issuer|multiple|more than one)"):
        Settings.from_env(require_auth=False)
    with pytest.raises(ValueError, match="(?i)key.*(duplicate|one issuer|multiple|more than one)"):
        Platform(settings).initialize()


@pytest.mark.parametrize("role", ["author_token", "activator_token"])
def test_authority_credential_cannot_equal_a_policy_role(role):
    shared = "shared-role-and-authority-credential"
    with pytest.raises(ValueError, match="authority credential must be dedicated"):
        Settings(operator_token="o" * 24, authority_token=shared, **{role: shared})


def test_role_scoped_cli_does_not_load_authority_credential(monkeypatch):
    monkeypatch.setenv("SOVITE_AUTHOR_TOKEN", "a" * 24)
    monkeypatch.setenv("SOVITE_AUTHORITY_TOKEN", "z" * 24)
    assert Settings.from_env(auth_role="author").authority_token == ""
