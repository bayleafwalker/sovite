"""Regression tests for the boundaries a valid bundle must not cross."""
import copy
from pathlib import Path

import pytest

from sovite.policy import PolicyError, authorize_job, bundle_generation, load_bundle, resolve_recipe, validate_bundle


@pytest.fixture
def bundle():
    return load_bundle(Path(__file__).parents[1] / "config/platform.local.yaml")[0]


def test_profiles_resolve_without_sharing_mutable_config():
    for path in (Path(__file__).parents[1] / "config").glob("*.yaml"):
        bundle, generation = load_bundle(path)
        for grant in bundle["grants"]:
            recipe = resolve_recipe(bundle, grant["id"])
            assert recipe["generation"] == generation
            recipe["grant"]["enabled"] = False
            assert grant.get("enabled", True)


def test_caller_cannot_impersonate_operator(bundle):
    with pytest.raises(PolicyError, match="unavailable"):
        authorize_job(bundle, "incident-forensics", "operator", "C3")
    assert authorize_job(bundle, "incident-forensics", "operator", "C3", is_operator=True)


def test_operator_still_cannot_bypass_class_or_inhibit_in_config(bundle):
    with pytest.raises(PolicyError):
        authorize_job(bundle, "change-assessment", "operator", "C3", is_operator=True)
    bundle["grants"][0]["enabled"] = False
    with pytest.raises(PolicyError):
        authorize_job(bundle, "incident-forensics", "operator", "C3", is_operator=True)


@pytest.mark.parametrize("section,field", [("models", "destination"), ("compute", "destination"),
                                            ("grants", "state_destination")])
def test_all_private_context_destinations_require_clearance(bundle, section, field):
    bundle["destinations"].append({"id": "cloud", "clearance": "C1", "kind": "cloud"})
    bundle[section][0][field] = "cloud"
    with pytest.raises(PolicyError, match="cannot receive"):
        validate_bundle(bundle)


def test_read_only_tool_is_not_automatically_low_class(bundle):
    bundle["tools"] = [{"id": "k8s", "kind": "kubernetes-read", "input_class": "C3", "output_class": "C3",
                         "destination": "private-runtime", "allowed_operations": ["list-pods"], "read_only": True}]
    bundle["agents"][1]["tools"] = ["k8s"]
    bundle["models"][0].update({"adapter": "ollama", "model": "qwen3:4b", "endpoint": "http://localhost:11434"})
    bundle["harnesses"][0]["kind"] = "tool-agent"
    with pytest.raises(PolicyError, match="cannot lower"):
        validate_bundle(bundle)


def test_tool_arguments_are_also_a_data_crossing(bundle):
    bundle["tools"] = [{"id": "metrics", "kind": "http-json", "endpoint": "http://localhost:8091/run",
                         "input_class": "C1", "output_class": "C1", "destination": "private-runtime",
                         "allowed_operations": ["count"], "read_only": True}]
    bundle["agents"][0]["tools"] = ["metrics"]
    bundle["models"][0].update({"adapter": "ollama", "model": "qwen3:4b", "endpoint": "http://localhost:11434"})
    bundle["harnesses"][0]["kind"] = "tool-agent"
    with pytest.raises(PolicyError, match="cannot receive this agent's arguments"):
        validate_bundle(bundle)


@pytest.mark.parametrize("mutation", [
    lambda b: b["grants"][0].update(return_contract="prose"),
    lambda b: b["grants"][0].update(on_outage="cloud-fallback"),
    lambda b: b["grants"][0].update(unknown_permit=True),
    lambda b: b["destinations"][0].update(allowed_hosts=["*"]),
    lambda b: b["agents"][0].update(model="missing"),
    lambda b: b["models"].append(copy.deepcopy(b["models"][0])),
    lambda b: b["data_classes"][0].update(rank=3),
    lambda b: b.update(mode="enforced"),
])
def test_unsafe_or_ambiguous_configuration_fails_closed(bundle, mutation):
    mutation(bundle)
    with pytest.raises(PolicyError):
        validate_bundle(bundle)


def test_host_binding_cannot_hide_credentials_or_change_destination(bundle):
    model = bundle["models"][0]
    model.update(adapter="ollama", endpoint="http://user:secret@localhost:11434")
    bundle["harnesses"][0]["kind"] = "tool-agent"
    with pytest.raises(PolicyError, match="embedded credentials"):
        validate_bundle(bundle)
    model["endpoint"] = "http://unauthorized.example:11434"
    with pytest.raises(PolicyError, match="outside its declared"):
        validate_bundle(bundle)


def test_generation_covers_binding_and_tools_not_only_model(bundle):
    original = bundle_generation(bundle)
    bundle["agents"][0]["instructions"] = "Changed agent package"
    assert bundle_generation(bundle) != original
    new = bundle_generation(bundle)
    bundle["grants"][0]["callers"].append("new-caller")
    assert bundle_generation(bundle) != new


def test_yaml_duplicate_keys_rejected(tmp_path):
    path = tmp_path / "duplicate.yaml"
    path.write_text("kind: PlatformBundle\nkind: SomethingElse\n")
    with pytest.raises(PolicyError, match="duplicate"):
        load_bundle(path)
