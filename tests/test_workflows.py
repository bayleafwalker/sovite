import copy
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from sovite import policy
from sovite.workflows import (SCHEMA, WorkflowError, compile_workflow, delivery_role_names, load_workflow,
                              validate_workflow, workflow_revision)


def revision_ref(name="requirements", revision="r1"):
    return {"owner": "work-owner", "resource_id": name, "revision": revision,
            "content_digest": "sha256:" + hashlib.sha256(f"{name}:{revision}".encode()).hexdigest()}


def coordinated():
    return {
        "apiVersion": "sovite.dev/v1alpha1", "kind": "DeliveryWorkflow", "id": "delivery",
        "mode": "coordinated", "planner": {"mode": "external", "finite": True,
            "role": "product_planner", "required_inputs": ["intent", "constraints", "decision_policy"],
            "output_contract": "requirements-release", "interaction": "decision-records",
            "discovery_roles": ["researcher"]},
        "roles": {role: {"grant": f"{role}-grant"} for role in
                  ("orchestrator", "implementer", "inline_reviewer", "holistic_reviewer", "researcher", "product_planner")},
        "implementation": {"mode": "requirements-driven"},
        "decomposition": {"unit": "coherent-sections", "dependencies": "explicit"},
        "product_decisions": {"blocked_work": "pause", "unaffected_work": "continue", "target": "decision-records"},
        "review": {
            "inline": {"independent": True, "acceptance_basis": "requirements",
                "derive_before_candidate": True, "max_substantive_failures": 2,
                "nits_nonblocking_after_pass": 1},
            "holistic": {"independent": True, "scope": "candidate"}},
        "escalation": {"target": "block_queue"}, "completion": {"route": "human_review"},
        "transfers": [
            {"id": name, "from_role": source, "target": {"kind": "role", "role": target}}
            for name, source, target in (
                ("implement", "orchestrator", "implementer"),
                ("inline", "implementer", "inline_reviewer"),
                ("rework", "inline_reviewer", "implementer"),
                ("inline-outcome", "inline_reviewer", "orchestrator"),
                ("holistic", "orchestrator", "holistic_reviewer"),
                ("holistic-outcome", "holistic_reviewer", "orchestrator"),
            )
        ],
        "domains": [{"id": "data-engineering", "procedures": [revision_ref("sandbox-procedure")],
            "required_evidence": [{"id": "lineage", "stage": "implementation",
                                   "description": "Affected-field lineage"}]}],
    }


def direct():
    return {
        "apiVersion": "sovite.dev/v1alpha1", "kind": "DeliveryWorkflow", "id": "one-shot",
        "mode": "direct", "planner": {"mode": "none", "finite": True},
        "roles": {"implementer": {"grant": "implementer-grant"}},
        "implementation": {"mode": "requirements-driven"},
        "completion": {"route": "return"},
        "transfers": [],
    }


def installed_bundle():
    roles = coordinated()["roles"]
    return {
        "apiVersion": "sovite.platform/v1alpha1", "kind": "PlatformBundle",
        "metadata": {"name": "test", "display_name": "Test"}, "mode": "development",
        "data_classes": [{"id": "C0", "rank": 0}, {"id": "C3", "rank": 3}],
        "destinations": [{"id": "execution", "kind": "local", "clearance": "C3"}],
        "models": [{"id": "model", "adapter": "ollama", "model": "test-model",
                    "destination": "execution", "endpoint": "http://localhost:11434"}],
        "harnesses": [{"id": "harness", "kind": "tool-agent"}], "tools": [],
        "agents": [{"id": role, "harness": "harness", "model": "model", "tools": [],
                    "instructions": f"Perform the {role} role."} for role in roles],
        "compute": [{"id": "workers", "kind": "local", "destination": "execution"}],
        "grants": [{"id": f"{role}-grant", "agent": role, "callers": ["workflow-consumer"],
                    "input_class": "C0", "output_class": "C0", "compute": "workers",
                    "state_destination": "execution", "telemetry_destination": "execution",
                    "return_contract": "reference"} for role in roles],
        "profiles": [{"id": "test", "components": ["worker"]}],
    }


def bind(document=None, bundle=None, **kwargs):
    args = {"requirements_ref": revision_ref(), "session_policy_ref": revision_ref("session-policy"),
            "installation_id": "installation-a", "input_class": "C0"}
    args.update(kwargs)
    return compile_workflow(document or coordinated(), bundle or installed_bundle(), **args)


def canonical_digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                         allow_nan=False).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def test_binding_is_reproducible_and_separate_from_bundle_activation():
    document, bundle = coordinated(), installed_bundle()
    originals = copy.deepcopy((document, bundle))
    result = bind(document, bundle)
    assert result == bind(document, bundle)
    assert (document, bundle) == originals
    assert result["workflow_revision"] == workflow_revision(document)
    assert result["bundle_generation"] == policy.bundle_generation(bundle)
    assert result["installation_id"] == "installation-a"
    assert result["input_class"] == "C0"
    assert result["dispatch_authorized"] is False
    payload = {key: value for key, value in result.items() if key != "binding_digest"}
    assert result["binding_digest"] == canonical_digest(payload)
    assert result["roles"]["implementer"]["agent_id"] == "implementer"
    assert "instructions" not in result["roles"]["implementer"]
    result["definition"]["id"] = "changed"
    assert document["id"] == "delivery"


def test_revision_depends_on_content_not_mapping_order():
    document = coordinated()
    assert workflow_revision(document) == workflow_revision(dict(reversed(list(document.items()))))
    altered = copy.deepcopy(document)
    altered["review"]["inline"]["max_substantive_failures"] = 3
    assert workflow_revision(document) != workflow_revision(altered)


def test_all_binding_identities_affect_digest():
    initial = bind()
    variants = [
        bind(requirements_ref=revision_ref(revision="r2")),
        bind(session_policy_ref=revision_ref("session-policy", "r2")),
        bind(installation_id="installation-b"),
        bind(generation="sha256:" + "a" * 64),
    ]
    assert all(other["binding_digest"] != initial["binding_digest"] for other in variants)
    assert all(other["workflow_revision"] == initial["workflow_revision"] for other in variants)


@pytest.mark.parametrize("where,key,value", [
    ((), "dispatch_authorized", True),
    (("roles", "implementer"), "model", "escape-installed-grant"),
    (("review", "inline"), "max_substantive_failures", 0),
    (("review", "inline"), "max_substantive_failures", True),
    (("review", "inline"), "nits_nonblocking_after_pass", 3),
    (("review", "inline"), "independent", False),
    (("review", "inline"), "derive_before_candidate", False),
    (("review", "holistic"), "scope", "implementation-transcript"),
    (("planner",), "finite", False),
])
def test_rejects_unknown_authority_and_invalid_review_policy(where, key, value):
    document = coordinated()
    target = document
    for part in where:
        target = target[part]
    target[key] = value
    with pytest.raises(WorkflowError):
        validate_workflow(document)


def test_rejects_missing_and_orphan_discovery_roles():
    document = coordinated()
    document["planner"]["discovery_roles"] = ["missing"]
    with pytest.raises(WorkflowError, match="Roles must match"):
        validate_workflow(document)
    document["planner"]["discovery_roles"] = ["implementer"]
    with pytest.raises(WorkflowError, match="Discovery roles"):
        validate_workflow(document)


def test_external_planner_has_concrete_inputs_role_and_finite_handoff():
    for field in ("role", "required_inputs", "output_contract", "interaction"):
        document = coordinated()
        del document["planner"][field]
        with pytest.raises(WorkflowError):
            validate_workflow(document)
    document = coordinated()
    document["planner"]["role"] = "orchestrator"
    with pytest.raises(WorkflowError, match="own named role"):
        validate_workflow(document)
    assert set(delivery_role_names(coordinated())) == {
        "orchestrator", "implementer", "inline_reviewer", "holistic_reviewer"}
    assert delivery_role_names(direct()) == ("implementer",)


def test_review_roles_require_distinct_configured_agents_not_distinct_models():
    result = bind()
    assert len({role["model_id"] for role in result["roles"].values()}) == 1
    bundle = installed_bundle()
    next(grant for grant in bundle["grants"] if grant["id"] == "inline_reviewer-grant")["agent"] = "implementer"
    with pytest.raises(WorkflowError) as raised:
        bind(bundle=bundle)
    assert raised.value.code == "reviewer_not_independent"


def test_unknown_grant_cannot_bind():
    document = coordinated()
    document["roles"]["implementer"]["grant"] = "uninstalled"
    with pytest.raises(WorkflowError) as raised:
        bind(document)
    assert raised.value.code == "unknown_workflow_grant"


@pytest.mark.parametrize("kind,adapter,endpoint", [
    ("local", "ollama", "http://localhost:11434"),
    ("cloud", "openai-compatible", "https://api.provider.example/v1"),
])
def test_provider_and_location_are_binding_data_not_workflow_modes(kind, adapter, endpoint):
    bundle = installed_bundle()
    bundle["destinations"].append({"id": "inference", "kind": kind, "clearance": "C0"})
    bundle["models"][0].update({"adapter": adapter, "endpoint": endpoint, "destination": "inference"})
    result = bind(bundle=bundle)
    assert result["roles"]["implementer"]["model_adapter"] == adapter
    assert result["roles"]["implementer"]["model_destination"] == "inference"


def test_binding_disabled_grant_does_not_claim_dispatch_authority():
    bundle = installed_bundle()
    bundle["grants"][0]["enabled"] = False
    with pytest.raises(WorkflowError) as raised:
        bind(bundle=bundle)
    assert raised.value.code == "role_input_denied"


def test_missing_input_class_defaults_highest_bundle_class_not_grant_ceiling():
    # Every grant permits only C0, but the installation also knows C3.
    # Omitting the run label must not quietly classify it as C0.
    with pytest.raises(WorkflowError) as raised:
        bind(input_class=None)
    assert raised.value.code == "role_input_denied"
    bundle = installed_bundle()
    for grant in bundle["grants"]:
        grant.update(input_class="C3", output_class="C3")
    result = bind(bundle=bundle, input_class=None)
    assert result["input_class"] == "C3"
    assert result["roles"]["implementer"]["input_class"] == "C3"


def test_authoring_roles_default_run_class_and_require_explicit_separate_public_context():
    document, bundle = coordinated(), installed_bundle()
    for grant in bundle["grants"]:
        if grant["agent"] not in {"product_planner", "researcher"}:
            grant.update(input_class="C3", output_class="C3")
    with pytest.raises(WorkflowError) as raised:
        bind(document, bundle, input_class="C3")
    assert raised.value.code == "role_input_denied"
    for role in ("product_planner", "researcher"):
        document["roles"][role]["input_class"] = "C0"
    result = bind(document, bundle, input_class="C3")
    assert result["input_class"] == "C3"
    assert result["roles"]["product_planner"]["input_class"] == "C0"
    assert "product_planner" not in delivery_role_names(document)


def mixed_graph():
    document, bundle = coordinated(), installed_bundle()
    for grant in bundle["grants"]:
        grant.update(input_class="C3", output_class="C3")
    bundle["destinations"].append({"id": "public-model", "kind": "cloud", "clearance": "C0"})
    bundle["models"].append({"id": "public-review", "adapter": "openai-compatible",
                             "model": "review-model", "destination": "public-model",
                             "endpoint": "https://api.provider.example/v1"})
    next(agent for agent in bundle["agents"] if agent["id"] == "inline_reviewer")["model"] = "public-review"
    next(grant for grant in bundle["grants"] if grant["agent"] == "inline_reviewer").update(
        input_class="C0", output_class="C0", workflow_only=True)
    document["roles"]["inline_reviewer"]["input_class"] = "C0"
    bundle["output_contracts"] = [{
        "id": "public-review-context", "source_class": "C3", "output_class": "C0",
        "destinations": ["public-model"], "approvers": ["release-owner"],
        "procedure_ref": revision_ref("review-context-redaction"),
    }]
    next(transfer for transfer in document["transfers"] if transfer["id"] == "inline")["output_contract"] = "public-review-context"
    return document, bundle


def test_mixed_graph_binds_explicit_released_context_without_lowering_run_class():
    document, bundle = mixed_graph()
    result = bind(document, bundle, input_class="C3")
    reviewer = result["roles"]["inline_reviewer"]
    assert result["input_class"] == "C3"
    assert reviewer["input_class"] == "C0"
    assert reviewer["model_boundary"] == "public"
    assert reviewer["requires_output_release"] is True
    transfer = result["transfers"]["inline"]
    assert transfer["contract_id"] == "public-review-context"
    assert transfer["contract_revision"] == canonical_digest(bundle["output_contracts"][0])
    assert transfer["requires_output_release"] is True
    assert result["dispatch_authorized"] is False


def test_every_incoming_edge_to_lower_class_reviewer_requires_release():
    document, bundle = mixed_graph()
    document["transfers"].append({"id": "requirements-direct", "from_role": "orchestrator",
                                   "target": {"kind": "role", "role": "inline_reviewer"}})
    with pytest.raises(WorkflowError) as raised:
        bind(document, bundle, input_class="C3")
    assert raised.value.code == "output_release_required"


def test_release_gated_role_requires_workflow_only_grant_to_prevent_raw_job_bypass():
    document, bundle = mixed_graph()
    reviewer_grant = next(grant for grant in bundle["grants"] if grant["agent"] == "inline_reviewer")
    reviewer_grant["workflow_only"] = False
    with pytest.raises(WorkflowError) as raised:
        bind(document, bundle, input_class="C3")
    assert raised.value.code == "workflow_only_required"
    reviewer_grant["workflow_only"] = True
    result = bind(document, bundle, input_class="C3")
    assert result["roles"]["inline_reviewer"]["requires_output_release"] is True


def test_private_to_public_same_class_requires_output_contract():
    document, bundle = mixed_graph()
    for grant in bundle["grants"]:
        grant.update(input_class="C0", output_class="C0")
    del next(transfer for transfer in document["transfers"] if transfer["id"] == "inline")["output_contract"]
    with pytest.raises(WorkflowError) as raised:
        bind(document, bundle)
    assert raised.value.code == "output_release_required"


@pytest.mark.parametrize("change", ["missing-contract", "wrong-destination", "insufficient-source"])
def test_output_contract_must_cover_declared_source_and_target(change):
    document, bundle = mixed_graph()
    if change == "missing-contract":
        bundle["output_contracts"] = []
    elif change == "wrong-destination":
        bundle["output_contracts"][0]["destinations"] = ["execution"]
    else:
        bundle["output_contracts"][0]["source_class"] = "C0"
    with pytest.raises(WorkflowError) as raised:
        bind(document, bundle, input_class="C3")
    assert raised.value.code == "invalid_transfer"


@pytest.mark.parametrize("kind", ["repository-candidate", "vuoro-finding"])
def test_external_public_outputs_require_content_pinned_release_contract(kind):
    document, bundle = mixed_graph()
    document["transfers"].append({"id": "external", "from_role": "implementer",
        "target": {"kind": kind, "destination": "public-model"}})
    with pytest.raises(WorkflowError) as raised:
        bind(document, bundle, input_class="C3")
    assert raised.value.code == "output_release_required"
    document["transfers"][-1]["output_contract"] = "public-review-context"
    result = bind(document, bundle, input_class="C3")
    assert result["transfers"]["external"]["target"]["kind"] == kind
    assert result["transfers"]["external"]["requires_output_release"] is True


def test_coordinated_transfers_cannot_omit_rework_path_or_use_authoring_roles():
    document = coordinated()
    document["transfers"] = [transfer for transfer in document["transfers"] if transfer["id"] != "rework"]
    with pytest.raises(WorkflowError, match="every coordinated"):
        validate_workflow(document)
    document = coordinated()
    document["transfers"][0]["from_role"] = "product_planner"
    with pytest.raises(WorkflowError, match="delivery roles"):
        validate_workflow(document)


def test_required_diversity_never_implicitly_changes_model_or_boundary():
    document = coordinated()
    document["review"]["model_diversity"] = "preferred"
    assert bind(document)["dispatch_authorized"] is False
    document["review"]["model_diversity"] = "required"
    with pytest.raises(WorkflowError) as raised:
        bind(document)
    assert raised.value.code == "review_model_diversity_required"
    bundle = installed_bundle()
    model = copy.deepcopy(bundle["models"][0])
    model.update(id="review-model", model="review-selection")
    bundle["models"].append(model)
    for role in ("inline_reviewer", "holistic_reviewer"):
        next(agent for agent in bundle["agents"] if agent["id"] == role)["model"] = model["id"]
    # Two admitted models suffice: both independent reviewers can use model B.
    assert bind(document, bundle)["dispatch_authorized"] is False


@pytest.mark.parametrize("digests", ["both", "one", "neither"])
def test_required_diversity_cannot_use_adapter_change_to_disguise_same_model(digests):
    document, bundle = coordinated(), installed_bundle()
    document["review"]["model_diversity"] = "required"
    implementer_model = bundle["models"][0]
    if digests != "neither":
        implementer_model["digest"] = "sha256:" + "a" * 64
    review_model = copy.deepcopy(implementer_model)
    review_model.update(id="review-route", adapter="openai-compatible",
                        endpoint="http://localhost:11434/v1")
    if digests == "both":
        # A serving alias cannot conceal that both routes name identical weights.
        review_model["model"] = "different-serving-alias"
    else:
        review_model.pop("digest", None)
    bundle["models"].append(review_model)
    for role in ("inline_reviewer", "holistic_reviewer"):
        next(agent for agent in bundle["agents"] if agent["id"] == role)["model"] = review_model["id"]
    with pytest.raises(WorkflowError) as raised:
        bind(document, bundle)
    assert raised.value.code == "review_model_diversity_required"


def test_direct_workflow_is_one_implementation_role_with_no_review_graph():
    result = bind(direct())
    assert set(result["roles"]) == {"implementer"}
    assert "review" not in result["definition"]
    document = direct()
    document["review"] = coordinated()["review"]
    with pytest.raises(WorkflowError):
        validate_workflow(document)
    document = direct()
    document["completion"]["route"] = "merge"
    with pytest.raises(WorkflowError):
        validate_workflow(document)


def test_domain_obligations_have_unique_ids_and_present_stages():
    document = coordinated()
    document["domains"].append(copy.deepcopy(document["domains"][0]))
    with pytest.raises(WorkflowError, match="Domain identifiers"):
        validate_workflow(document)
    document = coordinated()
    document["domains"][0]["required_evidence"] *= 2
    with pytest.raises(WorkflowError, match="evidence identifiers"):
        validate_workflow(document)
    document = direct()
    document["domains"] = coordinated()["domains"]
    document["domains"][0]["required_evidence"][0]["stage"] = "inline_review"
    with pytest.raises(WorkflowError, match="omitted review stage"):
        validate_workflow(document)


@pytest.mark.parametrize("reference", [
    None, {}, {"uri": "mutable-latest"}, {**revision_ref(), "content_digest": "not-a-digest"},
    {**revision_ref(), "owner": " "}, {**revision_ref(), "approval": True},
    {**revision_ref(), "owner": " owner "}, {**revision_ref(), "revision": "rev\n7"},
    {**revision_ref(), "resource_id": "requirements\x7fhidden"},
])
def test_binding_requires_complete_immutable_owner_references(reference):
    with pytest.raises(WorkflowError):
        bind(requirements_ref=reference)


@pytest.mark.parametrize("value", [" installation", "installation ", "installation\n", "installation\x00id"])
def test_portable_installation_identity_rejects_whitespace_and_control_characters(value):
    with pytest.raises(WorkflowError):
        bind(installation_id=value)


def test_domain_and_output_contract_references_use_the_same_portable_identity_schema():
    document = coordinated()
    document["domains"][0]["procedures"][0]["owner"] = " workspace"
    with pytest.raises(WorkflowError):
        validate_workflow(document)
    _, bundle = mixed_graph()
    bundle["output_contracts"][0]["procedure_ref"]["revision"] = "revision\n7"
    with pytest.raises(policy.PolicyError):
        policy.validate_bundle(bundle)
    # Interior ordinary spaces are identities, not silently trimmed content.
    reference = revision_ref()
    reference["owner"] = "workspace owner"
    assert bind(requirements_ref=reference)["requirements_ref"] == reference


def test_yaml_load_rejects_duplicate_and_non_json_values(tmp_path):
    path = tmp_path / "workflow.yaml"
    path.write_text(yaml.safe_dump(coordinated()))
    document, revision = load_workflow(path)
    assert revision == workflow_revision(document)
    path.write_text(path.read_text() + "id: duplicate\n")
    with pytest.raises(WorkflowError, match="duplicate keys"):
        load_workflow(path)
    document["description"] = float("nan")
    with pytest.raises(WorkflowError, match="finite JSON"):
        validate_workflow(document)


def test_published_schema_matches_runtime_schema():
    path = Path(__file__).resolve().parents[1] / "config" / "delivery-workflow.schema.json"
    assert json.loads(path.read_text()) == SCHEMA
