"""Offline workflow authoring must not initialize runtime authority or storage."""
import copy
import json
import sys
from pathlib import Path

import pytest
import yaml

from sovite import cli, policy
from sovite.hosting import DESTINATION
from sovite.workflows import load_workflow
from test_workflows import direct, installed_bundle, revision_ref


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / "config" / "workflows"


@pytest.fixture(autouse=True)
def no_runtime(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Offline workflow commands must not initialize runtime settings or storage")

    monkeypatch.delenv("SOVITE_HOSTING_ENVELOPE", raising=False)
    monkeypatch.setattr(cli.Settings, "from_env", forbidden)
    monkeypatch.setattr(cli.Settings, "from_database_env", forbidden)
    monkeypatch.setattr(cli, "Platform", forbidden)
    monkeypatch.setattr(cli, "Database", forbidden)


def invoke(monkeypatch, arguments):
    monkeypatch.setattr(sys, "argv", ["sovite", "workflow", *map(str, arguments)])
    cli.main()


def example_arguments():
    return [
        "compile", WORKFLOWS / "software-delivery.yaml",
        "--bundle", ROOT / "config" / "platform.delivery.yaml",
        "--installation-id", "offline-example",
        "--requirements-ref", WORKFLOWS / "inputs" / "software-requirements.ref.json",
        "--session-policy-ref", WORKFLOWS / "inputs" / "session-policy.ref.json",
        "--input-class", "C0",
    ]


def test_validate_is_definition_only_without_runtime_or_host_access(monkeypatch, capsys):
    # A definition-only check must not read even a configured hosting envelope.
    monkeypatch.setenv("SOVITE_HOSTING_ENVELOPE", "/nonexistent/host-envelope.json")
    path = WORKFLOWS / "one-shot.yaml"
    invoke(monkeypatch, ["validate", path])
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "valid": True, "workflow_revision": load_workflow(path)[1], "mode": "direct",
        "scope": "Workflow definition only; no installed capability or execution is asserted",
    }


def test_compile_outputs_identical_finite_json_preview_without_runtime(monkeypatch, capsys, tmp_path):
    output = tmp_path / "binding.json"
    invoke(monkeypatch, [*example_arguments(), "--output", output])
    captured = capsys.readouterr()
    assert not captured.err
    assert output.read_text(encoding="utf-8") == captured.out
    result = json.loads(captured.out)
    assert result["kind"] == "DeliveryWorkflowBinding"
    assert result["dispatch_authorized"] is False
    assert result["installation_id"] == "offline-example"
    assert result["input_class"] == "C0"
    assert result["bundle_generation"] == policy.load_bundle(ROOT / "config/platform.delivery.yaml")[1]
    assert result["requirements_ref"] == json.loads(
        (WORKFLOWS / "inputs/software-requirements.ref.json").read_text()
    )


def test_compile_refuses_to_replace_an_existing_output(monkeypatch, capsys, tmp_path):
    output = tmp_path / "binding.json"
    output.write_bytes(b"previous owner record\n")
    with pytest.raises(SystemExit) as failure:
        invoke(monkeypatch, [*example_arguments(), "--output", output])
    assert failure.value.code == 1
    assert output.read_bytes() == b"previous owner record\n"
    captured = capsys.readouterr()
    assert not captured.out
    assert "FileExistsError" in captured.err


def test_missing_input_class_does_not_silently_select_public_grant_ceiling(monkeypatch, capsys, tmp_path):
    output = tmp_path / "binding.json"
    arguments = example_arguments()[:-2]
    with pytest.raises(SystemExit) as failure:
        invoke(monkeypatch, [*arguments, "--output", output])
    assert failure.value.code == 1
    assert not output.exists()
    assert not capsys.readouterr().out


@pytest.mark.parametrize("raw", [
    '{"owner":"first","owner":"second"}',
    '{"owner":NaN}',
])
def test_reference_loader_refuses_ambiguous_or_nonfinite_json(monkeypatch, capsys, tmp_path, raw):
    reference = tmp_path / "requirements.json"
    reference.write_text(raw)
    output = tmp_path / "binding.json"
    arguments = example_arguments()
    arguments[arguments.index("--requirements-ref") + 1] = reference
    with pytest.raises(SystemExit) as failure:
        invoke(monkeypatch, [*arguments, "--output", output])
    assert failure.value.code == 1
    assert not output.exists()
    assert not capsys.readouterr().out


@pytest.fixture
def hosted_files(tmp_path, monkeypatch):
    pin = "sha256:" + "1" * 64
    bundle = installed_bundle()
    bundle.update(apiVersion="sovite.platform/v1alpha2", mode="enforced", restricted_class="C3")
    destination = bundle["destinations"][0]
    destination.update(
        kind="private", boundary="private", clearance="C0",
        allowed_hosts=["execution.workspace.svc"], trust_tier="operator-owned",
        retention={
            "prompt_logging": False, "response_logging": False, "persistent_context": False,
            "in_memory_cache": {"scope": "job", "max_seconds": 300,
                               "clear_on_cancel": True, "clear_on_session_close": True},
        },
    )
    bundle["connector_roles"] = [{
        "id": "helper", "destination": "execution",
        "helper_image_digest": pin, "engine_image_digest": pin,
    }]
    model = bundle["models"][0]
    model.pop("endpoint")
    model.update(
        adapter="openai-compatible", enabled=True, wire_api="chat",
        connection={"kind": "model-gateway", "enrollment_role": "helper"},
        identity={"protocol": "helper-signed-v1", "pinning": "required",
                  "engine_version": "1", "weights_manifest_digest": pin},
        max_concurrency=1, timeout_seconds=120, first_token_timeout_seconds=60,
        cancel_bound_seconds=6,
    )
    bundle["harnesses"][0]["digest"] = pin
    bundle["compute"][0].update(
        kind="kubernetes", namespace="workspace",
        profile={"replicas": 1, "cpu": "1", "memory": "512Mi"},
    )
    for grant in bundle["grants"]:
        grant["availability_wait_seconds"] = 900
    envelope = {
        "apiVersion": "sovite.hosting/v1", "workspace": "workspace-a",
        "installation_id": "hosted-installation", "revision": 1,
        "placement": {"namespace": "workspace", "resources": {
            "max_replicas": 1, "max_cpu_millis": 1000, "max_memory_bytes": 536870912,
        }},
        "allowed_data_classes": ["C0"],
        "destinations": [{
            key: copy.deepcopy(value) for key, value in destination.items()
            if key in DESTINATION["properties"]
        }],
        "connector_kinds": ["model-gateway"], "connector_image_digests": [pin],
        "harnesses": copy.deepcopy(bundle["harnesses"]),
    }
    bundle_path = tmp_path / "bundle.yaml"
    bundle_path.write_text(yaml.safe_dump(bundle))
    envelope_path = tmp_path / "envelope.json"
    envelope_path.write_text(json.dumps(envelope))
    workflow_path = tmp_path / "workflow.yaml"
    workflow_path.write_text(yaml.safe_dump(direct()))
    requirements_path = tmp_path / "requirements.json"
    requirements_path.write_text(json.dumps(revision_ref()))
    session_policy_path = tmp_path / "session-policy.json"
    session_policy_path.write_text(json.dumps(revision_ref("policy")))
    monkeypatch.setenv("SOVITE_HOSTING_ENVELOPE", str(envelope_path))
    arguments = [
        "compile", workflow_path, "--bundle", bundle_path,
        "--installation-id", envelope["installation_id"],
        "--requirements-ref", requirements_path,
        "--session-policy-ref", session_policy_path, "--input-class", "C0",
    ]
    return arguments, bundle, envelope, envelope_path


def test_hosted_compile_binds_envelope_generation_without_database(monkeypatch, capsys, hosted_files):
    arguments, bundle, envelope, envelope_path = hosted_files
    invoke(monkeypatch, arguments)
    first = json.loads(capsys.readouterr().out)
    assert first["installation_id"] == envelope["installation_id"]
    assert first["bundle_generation"] != policy.bundle_generation(bundle)
    envelope["revision"] += 1
    envelope_path.write_text(json.dumps(envelope))
    invoke(monkeypatch, arguments)
    second = json.loads(capsys.readouterr().out)
    assert first["workflow_revision"] == second["workflow_revision"]
    assert first["bundle_generation"] != second["bundle_generation"]
    assert first["binding_digest"] != second["binding_digest"]


def test_hosted_compile_cannot_replace_owner_installation_identity(monkeypatch, capsys, hosted_files):
    arguments, _, _, _ = hosted_files
    arguments[arguments.index("--installation-id") + 1] = "other-installation"
    with pytest.raises(SystemExit) as failure:
        invoke(monkeypatch, arguments)
    assert failure.value.code == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert "Installation identity does not match the hosting envelope" in captured.err
