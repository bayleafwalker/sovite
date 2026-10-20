"""Schema foundation; gateway execution and identity verification arrive in later PRs."""
import asyncio
import copy
import json
from pathlib import Path

import httpx
import pytest
import yaml
from jsonschema import Draft202012Validator

from sovite.adapters import AdapterRunner, RuntimeFailure
from sovite.policy import (SCHEMA, SCHEMA_V1ALPHA2, PolicyError, bundle_generation,
                           flow_projection, load_bundle, resolve_recipe, validate_bundle)


ROOT = Path(__file__).parents[1]
DIGEST = "sha256:" + "1" * 64


@pytest.fixture
def bundle():
    bundle = load_bundle(ROOT / "config/platform.local.yaml")[0]
    bundle.update(apiVersion="sovite.platform/v1alpha2", mode="enforced", restricted_class="C3")
    for destination in bundle["destinations"]:
        destination.update(trust_tier="operator-owned", retention={
            "prompt_logging": False, "response_logging": False, "persistent_context": False,
            "in_memory_cache": {"scope": "job", "max_seconds": 300,
                                "clear_on_cancel": True, "clear_on_session_close": True},
        })
    inference = copy.deepcopy(bundle["destinations"][0])
    inference["id"] = "inference"
    bundle["destinations"].append(inference)
    bundle["connector_roles"] = [{"id": "inference-helper", "destination": "inference",
                                  "helper_image_digest": DIGEST, "engine_image_digest": DIGEST}]
    bundle["models"][0].update(
        adapter="openai-compatible", model="approved-model", destination="inference", enabled=True,
        connection={"kind": "model-gateway", "enrollment_role": "inference-helper"},
        identity={"protocol": "helper-signed-v1", "pinning": "required",
                  "engine_version": "approved-version", "weights_manifest_digest": DIGEST},
        wire_api="chat", max_concurrency=1, timeout_seconds=120,
        first_token_timeout_seconds=60, cancel_bound_seconds=6,
    )
    bundle["harnesses"][0].update(kind="tool-agent", digest=DIGEST)
    for grant in bundle["grants"]:
        grant["availability_wait_seconds"] = 900
    return validate_bundle(bundle)


@pytest.mark.parametrize("filename,generation", [
    ("platform.local.yaml", "2758d96f24d9d89fd23d34b247bafa871983572b4d276f426f80c299ee81b8fe"),
    ("platform.ollama.yaml", "58fffe62a9c72afcd022ac562d691d4991c3d94dd23933bbb121d899e9074204"),
    ("platform.kubernetes.yaml", "8a676c0d9470acb39c3f30a039ac34242735c0d8ab5617b7174f47aeeb35976d"),
])
def test_v1alpha1_generation_unchanged(filename, generation):
    path = ROOT / "config" / filename
    bundle, actual = load_bundle(path)
    assert actual == "sha256:" + generation
    assert bundle == yaml.safe_load(path.read_text())
    assert "connector_roles" not in resolve_recipe(bundle, bundle["grants"][0]["id"])
    assert set(flow_projection(bundle)) == {"generation", "edges", "readiness"}


@pytest.mark.parametrize("filename,schema", [("platform.schema.json", SCHEMA),
                                             ("platform.v1alpha2.schema.json", SCHEMA_V1ALPHA2)])
def test_exported_schemas_match_compiler(filename, schema):
    assert json.loads((ROOT / "config" / filename).read_text()) == schema
    Draft202012Validator.check_schema(schema)


def test_gateway_bundle_validates_without_legacy_defaults(bundle, tmp_path):
    path = tmp_path / "gateway.yaml"
    path.write_text(yaml.safe_dump(bundle))
    compiled, generation = load_bundle(path)
    assert compiled == bundle
    assert generation == bundle_generation(bundle)
    recipe = resolve_recipe(compiled, "incident-forensics")
    assert recipe["connector_roles"] == bundle["connector_roles"]
    assert recipe["restricted_class"] == "C3"
    recipe["connector_roles"][0]["destination"] = "changed"
    assert bundle["connector_roles"][0]["destination"] == "inference"


def test_EXACT_BINDING_role_destination_mismatch(bundle):
    bundle["connector_roles"][0]["destination"] = "private-runtime"
    with pytest.raises(PolicyError, match="same destination"):
        validate_bundle(bundle)


@pytest.mark.parametrize("section,field", [("models", "connection"), ("connector_roles", "destination")])
def test_connector_references_must_resolve(bundle, section, field):
    if field == "connection":
        bundle[section][0][field]["enrollment_role"] = "missing"
    else:
        bundle[section][0][field] = "missing"
    with pytest.raises(PolicyError, match="unresolved"):
        validate_bundle(bundle)


def test_connector_role_ids_are_unique(bundle):
    bundle["connector_roles"].append(copy.deepcopy(bundle["connector_roles"][0]))
    with pytest.raises(PolicyError, match="duplicate identifiers"):
        validate_bundle(bundle)


@pytest.mark.parametrize("source", ["input", "output", "tool-result"])
def test_rented_restricted_context_requires_explicit_clearance(bundle, source):
    destination = bundle["destinations"][-1]
    destination["trust_tier"] = "rented"
    grant = bundle["grants"][0]
    grant["input_class"] = "C3" if source == "input" else "C2"
    if source == "tool-result":
        bundle["tools"] = [{"id": "restricted-read", "kind": "kubernetes-read", "input_class": "C3",
                            "output_class": "C3", "destination": "private-runtime",
                            "allowed_operations": ["list-pods"]}]
        bundle["agents"][0]["tools"] = ["restricted-read"]
    with pytest.raises(PolicyError, match="explicit restricted_clearance for C3"):
        validate_bundle(bundle)
    destination["restricted_clearance"] = {"classes": ["C3"], "rationale": "Approved rented placement",
                                           "authorization_ref": "restricted-inference-policy"}
    validate_bundle(bundle)


def test_tool_result_cannot_be_hidden_by_lower_output_declaration(bundle):
    bundle["grants"][0].update(input_class="C2", output_class="C2")
    bundle["tools"] = [{"id": "restricted-read", "kind": "kubernetes-read", "input_class": "C3",
                        "output_class": "C3", "destination": "private-runtime",
                        "allowed_operations": ["list-pods"]}]
    bundle["agents"][0]["tools"] = ["restricted-read"]
    with pytest.raises(PolicyError, match="cannot lower"):
        validate_bundle(bundle)


@pytest.mark.parametrize("purpose", ["execution", "state", "tool", "telemetry"])
def test_rented_clearance_applies_to_every_receiving_flow(bundle, purpose):
    destination = {"execution": "private-runtime", "state": "private-state",
                   "tool": "private-runtime", "telemetry": "operations"}[purpose]
    next(d for d in bundle["destinations"] if d["id"] == destination)["trust_tier"] = "rented"
    if purpose == "tool":
        bundle["compute"][0]["destination"] = "inference"
        bundle["tools"] = [{"id": "restricted-read", "kind": "kubernetes-read", "input_class": "C3",
                            "output_class": "C3", "destination": destination,
                            "allowed_operations": ["list-pods"]}]
        bundle["agents"][0]["tools"] = ["restricted-read"]
    if purpose == "telemetry":
        next(d for d in bundle["destinations"] if d["id"] == destination)["clearance"] = "C3"
        bundle["grants"][0]["telemetry_class"] = "C3"
    with pytest.raises(PolicyError, match="explicit restricted_clearance"):
        validate_bundle(bundle)


def test_restricted_threshold_compares_ranks_and_clearance_names_exact_class(bundle):
    bundle["data_classes"].append({"id": "MoreRestricted", "rank": 4})
    destination = bundle["destinations"][-1]
    destination.update(trust_tier="rented", clearance="MoreRestricted", restricted_clearance={
        "classes": ["C3"], "rationale": "Approved C3", "authorization_ref": "policy"})
    bundle["grants"][0]["output_class"] = "MoreRestricted"
    for other in bundle["destinations"][:2]:
        other["clearance"] = "MoreRestricted"
    with pytest.raises(PolicyError, match="explicit restricted_clearance for MoreRestricted"):
        validate_bundle(bundle)
    destination["restricted_clearance"]["classes"].append("MoreRestricted")
    validate_bundle(bundle)


@pytest.mark.parametrize("field,value,message", [
    ("restricted_class", "missing", "unresolved"),
    ("classes", ["missing"], "unresolved"), ("classes", ["C2"], "restricted-or-higher"),
    ("classes", [], "non-empty"), ("rationale", " ", "does not match"),
    ("authorization_ref", "", "non-empty"),
])
def test_invalid_restricted_clearance(bundle, field, value, message):
    clearance = {"classes": ["C3"], "rationale": "Reviewed", "authorization_ref": "policy"}
    bundle["destinations"][-1]["restricted_clearance"] = clearance
    if field == "restricted_class":
        bundle[field] = value
    else:
        clearance[field] = value
    with pytest.raises(PolicyError, match=message):
        validate_bundle(bundle)


@pytest.mark.parametrize("index", range(4))
@pytest.mark.parametrize("field", ["trust_tier", "retention"])
def test_every_destination_requires_trust_and_retention(bundle, index, field):
    del bundle["destinations"][index][field]
    with pytest.raises(PolicyError, match=field + ".*required"):
        validate_bundle(bundle)


@pytest.mark.parametrize("trust_tier", ["operator-owned", "rented", "provider"])
@pytest.mark.parametrize("field", ["prompt_logging", "response_logging", "persistent_context"])
def test_inference_retention_disables_payload_persistence(bundle, trust_tier, field):
    bundle["destinations"][-1]["trust_tier"] = trust_tier
    bundle["destinations"][-1]["retention"][field] = True
    with pytest.raises(PolicyError, match="inference retention"):
        validate_bundle(bundle)


@pytest.mark.parametrize("field", ["prompt_logging", "response_logging", "persistent_context", "in_memory_cache"])
def test_retention_declarations_are_complete(bundle, field):
    del bundle["destinations"][-1]["retention"][field]
    with pytest.raises(PolicyError, match=field + ".*required"):
        validate_bundle(bundle)


@pytest.mark.parametrize("field,value", [("scope", "global"), ("max_seconds", 0), ("max_seconds", float("inf")),
                                         ("clear_on_cancel", "yes"), ("clear_on_session_close", "yes")])
def test_cache_declarations_are_bounded_and_explicit(bundle, field, value):
    bundle["destinations"][-1]["retention"]["in_memory_cache"][field] = value
    with pytest.raises(PolicyError, match=field):
        validate_bundle(bundle)


def test_unsupported_identity_protocol(bundle):
    bundle["models"][0]["identity"]["protocol"] = "unsigned-v1"
    with pytest.raises(PolicyError, match="helper-signed-v1"):
        validate_bundle(bundle)


@pytest.mark.parametrize("field", ["engine_version", "weights_manifest_digest"])
def test_PIN_BEFORE_PROMPT_required_identity_pins(bundle, field):
    del bundle["models"][0]["identity"][field]
    with pytest.raises(PolicyError, match="required identity pins"):
        validate_bundle(bundle)


@pytest.mark.parametrize("field", ["helper_image_digest", "engine_image_digest"])
def test_PIN_BEFORE_PROMPT_required_role_image_pins(bundle, field):
    del bundle["connector_roles"][0][field]
    with pytest.raises(PolicyError, match=field + ".*required"):
        validate_bundle(bundle)


def test_PIN_BEFORE_PROMPT_unpinned_only_in_development(bundle):
    bundle["models"][0]["identity"] = {"protocol": "helper-signed-v1", "pinning": "unpinned-development"}
    with pytest.raises(PolicyError, match="enforced gateway identity"):
        validate_bundle(bundle)
    bundle["mode"] = "development"
    validate_bundle(bundle)
    assert flow_projection(bundle)["models"][0]["identity"]["pinning"] == "unpinned-development"


@pytest.mark.parametrize("field,value", [("digest", DIGEST), ("identity_protocol", "ollama"),
                                         ("identity_endpoint", "http://localhost:11434")])
def test_ambiguous_legacy_and_new_identity_rejected(bundle, field, value):
    bundle["models"][0][field] = value
    with pytest.raises(PolicyError, match="ambiguous legacy/new identity"):
        validate_bundle(bundle)


@pytest.mark.parametrize("field,value", [("endpoint", "https://upstream.example/v1"),
                                         ("base_url", "https://upstream.example/v1"),
                                         ("api_key_env", "UPSTREAM_TOKEN"), ("credential", "secret-ref"),
                                         ("tls_trust", "upstream-trust")])
def test_gateway_cannot_carry_upstream_endpoint_or_credential(bundle, field, value):
    bundle["models"][0][field] = value
    with pytest.raises(PolicyError, match="cannot carry|Additional properties"):
        validate_bundle(bundle)


@pytest.mark.parametrize("adapter", ["ollama", "openai-compatible"])
def test_enforced_v2_has_no_direct_endpoint_escape(bundle, adapter):
    model = bundle["models"][0]
    for field in ("connection", "identity", "first_token_timeout_seconds", "cancel_bound_seconds"):
        del model[field]
    model.update(adapter=adapter, endpoint="http://localhost:11434", digest=DIGEST, identity_protocol="ollama")
    with pytest.raises(PolicyError, match="direct endpoints are development-only"):
        validate_bundle(bundle)
    bundle["mode"] = "development"
    validate_bundle(bundle)


def test_new_identity_cannot_be_silently_ignored_by_legacy_reader(bundle):
    del bundle["models"][0]["connection"]
    bundle["mode"] = "development"
    with pytest.raises(PolicyError, match="require a model-gateway connection"):
        validate_bundle(bundle)


@pytest.mark.parametrize("kind,adapter,wire_api,message", [
    ("tool-agent", "openai-compatible", "responses", "tool-agent requires wire_api chat"),
    ("native-codex", "openai-compatible", "chat", "native Codex requires.*responses"),
    ("native-codex", "ollama", "chat", "native Codex requires.*responses"),
    ("tool-agent", "ollama", "responses", "adapter and wire_api mismatch"),
])
def test_wire_api_mismatch(bundle, kind, adapter, wire_api, message):
    bundle["harnesses"][0]["kind"] = kind
    bundle["models"][0].update(adapter=adapter, wire_api=wire_api)
    with pytest.raises(PolicyError, match=message):
        validate_bundle(bundle)


def test_native_gateway_requires_responses_without_translation(bundle):
    bundle["harnesses"][0]["kind"] = "native-codex"
    bundle["models"][0]["wire_api"] = "responses"
    validate_bundle(bundle)


def test_external_native_harness_requires_responses(bundle):
    bundle["harnesses"][0].update(kind="agent-http", endpoint="http://localhost:8090/run", capabilities=["native-codex"])
    with pytest.raises(PolicyError, match="native Codex requires.*responses"):
        validate_bundle(bundle)
    bundle["models"][0]["wire_api"] = "responses"
    validate_bundle(bundle)


@pytest.mark.parametrize("field", ["enabled", "identity", "wire_api", "max_concurrency", "timeout_seconds",
                                   "first_token_timeout_seconds", "cancel_bound_seconds"])
def test_gateway_operational_declarations_are_required(bundle, field):
    del bundle["models"][0][field]
    with pytest.raises(PolicyError, match=field + ".*required"):
        validate_bundle(bundle)


@pytest.mark.parametrize("field,value", [("timeout_seconds", 0), ("first_token_timeout_seconds", 0),
                                         ("cancel_bound_seconds", 0), ("first_token_timeout_seconds", 121),
                                         ("cancel_bound_seconds", 121)])
def test_BOUNDED_STOP_deadlines_positive_and_consistent(bundle, field, value):
    bundle["models"][0][field] = value
    with pytest.raises(PolicyError, match="minimum|cannot exceed"):
        validate_bundle(bundle)


@pytest.mark.parametrize("value", [None, 0, -1, float("inf"), float("nan"), "900"])
def test_AVAILABILITY_NOT_COMPETENCE_queue_wait_must_be_finite(bundle, value):
    bundle["grants"][0]["availability_wait_seconds"] = value
    with pytest.raises(PolicyError, match="availability_wait_seconds"):
        validate_bundle(bundle)


@pytest.mark.parametrize("implicit_queue", [False, True])
def test_gateway_bundle_requires_wait_even_for_disabled_or_legacy_grant(bundle, implicit_queue):
    legacy = copy.deepcopy(bundle["models"][0])
    legacy.update(id="legacy", endpoint="http://localhost:11434")
    for field in ("connection", "identity", "first_token_timeout_seconds", "cancel_bound_seconds"):
        del legacy[field]
    bundle["models"].append(legacy)
    bundle["mode"] = "development"
    bundle["agents"][1]["model"] = "legacy"
    grant = bundle["grants"][1]
    grant["enabled"] = False
    del grant["availability_wait_seconds"]
    if implicit_queue:
        del grant["on_outage"]
    with pytest.raises(PolicyError, match="every queue grant"):
        validate_bundle(bundle)


@pytest.mark.parametrize("method", ["run", "verify_model_identity", "tool_agent", "native_codex", "agent_http"])
def test_EXACT_BINDING_gateway_dispatch_refused_before_any_io(bundle, method, monkeypatch):
    recipe = resolve_recipe(bundle, "incident-forensics")
    def unexpected_io(*args, **kwargs):
        pytest.fail("PIN_BEFORE_PROMPT: unfinished gateway must not send context or launch a process")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", unexpected_io)
    runner = AdapterRunner(httpx.MockTransport(unexpected_io))
    operation = getattr(runner, method)
    with pytest.raises(RuntimeFailure, match="model_gateway_adapter_unavailable") as error:
        asyncio.run(operation(recipe["model"], True) if method == "verify_model_identity"
                    else operation(recipe, "PRIVATE-CANARY", "job"))
    assert not error.value.retryable


@pytest.mark.parametrize("path,value", [
    (("restricted_class",), "C2"),
    (("destinations", 3, "trust_tier"), "provider"),
    (("destinations", 3, "clearance"), "C2"),
    (("destinations", 3, "retention", "prompt_logging"), True),
    (("destinations", 3, "retention", "response_logging"), True),
    (("destinations", 3, "retention", "persistent_context"), True),
    (("destinations", 3, "retention", "in_memory_cache", "scope"), "request"),
    (("destinations", 3, "retention", "in_memory_cache", "max_seconds"), 60),
    (("destinations", 3, "retention", "in_memory_cache", "clear_on_cancel"), False),
    (("destinations", 3, "retention", "in_memory_cache", "clear_on_session_close"), False),
    (("destinations", 3, "restricted_clearance", "classes"), ["C2", "C3"]),
    (("destinations", 3, "restricted_clearance", "rationale"), "Changed rationale"),
    (("destinations", 3, "restricted_clearance", "authorization_ref"), "changed-policy"),
    (("connector_roles", 0, "id"), "changed-role"),
    (("connector_roles", 0, "destination"), "private-runtime"),
    (("connector_roles", 0, "helper_image_digest"), "sha256:" + "2" * 64),
    (("connector_roles", 0, "engine_image_digest"), "sha256:" + "2" * 64),
    (("models", 0, "enabled"), False),
    (("models", 0, "destination"), "private-runtime"),
    (("models", 0, "connection", "kind"), "future-gateway"),
    (("models", 0, "connection", "enrollment_role"), "changed-role"),
    (("models", 0, "identity", "protocol"), "future-protocol"),
    (("models", 0, "identity", "pinning"), "unpinned-development"),
    (("models", 0, "identity", "engine_version"), "changed-version"),
    (("models", 0, "identity", "weights_manifest_digest"), "sha256:" + "2" * 64),
    (("models", 0, "wire_api"), "responses"),
    (("models", 0, "max_concurrency"), 2),
    (("models", 0, "timeout_seconds"), 180),
    (("models", 0, "first_token_timeout_seconds"), 90),
    (("models", 0, "cancel_bound_seconds"), 5),
    (("grants", 0, "availability_wait_seconds"), 1200),
    (("grants", 0, "timeout_seconds"), 500),
])
def test_generation_and_flow_projection_cover_every_new_declaration(bundle, path, value):
    bundle["destinations"][-1]["restricted_clearance"] = {
        "classes": ["C3"], "rationale": "Approved placement", "authorization_ref": "policy"}
    before = flow_projection(bundle)
    changed = copy.deepcopy(bundle)
    target = changed
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    if path == ("connector_roles", 0, "id"):
        changed["models"][0]["connection"]["enrollment_role"] = value
    if path == ("models", 0, "connection", "enrollment_role"):
        changed["connector_roles"][0]["id"] = value
    after = flow_projection(changed)
    assert before["generation"] != after["generation"]
    for key in ("destinations", "models", "connector_roles", "grants", "restricted_class"):
        assert after[key] == changed[key]
    assert {key: value for key, value in before.items() if key != "generation"} != {
        key: value for key, value in after.items() if key != "generation"}
    after["models"][0]["enabled"] = "changed"
    assert changed["models"][0]["enabled"] in (True, False)
