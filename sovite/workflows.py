"""Validate delivery workflow definitions and bind them to installed capabilities.

Definitions have their own content revision. They are not PlatformBundles, and
compiling one neither activates a bundle nor dispatches work. A binding records
the exact requirements, policy and configured capabilities for a consumer to
execute under its existing authority. It does not grant that authority.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from . import flow, policy


class WorkflowError(ValueError):
    """A definition or binding cannot be represented by this workflow contract."""

    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


API_VERSION = "sovite.dev/v1alpha1"
COORDINATED_ROLES = ("orchestrator", "implementer", "inline_reviewer", "holistic_reviewer")
COORDINATED_TRANSITIONS = {
    ("orchestrator", "implementer"),
    ("implementer", "inline_reviewer"),
    ("inline_reviewer", "implementer"),
    ("inline_reviewer", "orchestrator"),
    ("orchestrator", "holistic_reviewer"),
    ("holistic_reviewer", "orchestrator"),
}
ID = copy.deepcopy(policy.ID)
DIGEST = copy.deepcopy(policy.DIGEST)
TEXT = {"type": "string", "minLength": 1, "maxLength": 16000, "pattern": r"\S"}
REF_TEXT = copy.deepcopy(policy.REF_TEXT)


def _object(required: list[str], properties: dict) -> dict:
    return {"type": "object", "required": required, "properties": properties,
            "additionalProperties": False}


REVISION_REF_SCHEMA = copy.deepcopy(policy.REVISION_REF_SCHEMA)
ROLE_SCHEMA = _object(["grant"], {"grant": ID, "input_class": ID})
TRANSFER_TARGET_SCHEMA = {"oneOf": [
    _object(["kind", "role"], {"kind": {"const": "role"}, "role": ID}),
    _object(["kind", "destination"], {
        "kind": {"enum": ["repository-candidate", "vuoro-finding"]}, "destination": ID,
    }),
]}
TRANSFER_SCHEMA = _object(["id", "from_role", "target"], {
    "id": ID, "from_role": ID, "target": TRANSFER_TARGET_SCHEMA,
    "output_contract": ID,
})
EVIDENCE_SCHEMA = _object(["id", "stage", "description"], {
    "id": ID,
    "stage": {"enum": ["implementation", "inline_review", "holistic_review", "completion"]},
    "description": TEXT,
})
DOMAIN_SCHEMA = _object(["id", "procedures", "required_evidence"], {
    "id": ID,
    "procedures": {"type": "array", "items": {"$ref": "#/$defs/revision_ref"},
                   "uniqueItems": True, "maxItems": 100},
    "required_evidence": {"type": "array", "items": EVIDENCE_SCHEMA, "maxItems": 100},
})

SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "urn:sovite:delivery-workflow:v1alpha1",
    "title": "DeliveryWorkflow",
    "$defs": {"revision_ref": REVISION_REF_SCHEMA},
    **_object(["apiVersion", "kind", "id", "mode", "planner", "roles", "implementation", "completion", "transfers"], {
        "apiVersion": {"const": API_VERSION},
        "kind": {"const": "DeliveryWorkflow"},
        "id": ID,
        "description": TEXT,
        "mode": {"enum": ["coordinated", "direct"]},
        "planner": _object(["mode", "finite"], {
            "mode": {"enum": ["external", "none"]},
            "finite": {"const": True},
            "role": ID,
            "required_inputs": {"type": "array", "items": ID, "uniqueItems": True,
                                "minItems": 1, "maxItems": 100},
            "output_contract": {"const": "requirements-release"},
            "interaction": {"enum": ["interactive", "decision-records"]},
            "discovery_roles": {"type": "array", "items": ID, "uniqueItems": True, "maxItems": 32},
        }),
        "roles": {"type": "object", "minProperties": 1, "maxProperties": 37,
                  "propertyNames": ID, "additionalProperties": ROLE_SCHEMA},
        "implementation": _object(["mode"], {"mode": {"const": "requirements-driven"}}),
        "decomposition": _object(["unit", "dependencies"], {
            "unit": {"const": "coherent-sections"}, "dependencies": {"const": "explicit"},
        }),
        "product_decisions": _object(["blocked_work", "unaffected_work", "target"], {
            "blocked_work": {"const": "pause"}, "unaffected_work": {"const": "continue"},
            "target": {"enum": ["interactive", "decision-records"]},
        }),
        "review": _object(["inline", "holistic"], {
            "model_diversity": {"enum": ["preferred", "required", "none"]},
            "inline": _object(["independent", "acceptance_basis", "derive_before_candidate",
                               "max_substantive_failures", "nits_nonblocking_after_pass"], {
                "independent": {"const": True},
                "acceptance_basis": {"const": "requirements"},
                "derive_before_candidate": {"const": True},
                "max_substantive_failures": {
                    "type": "integer", "minimum": 1, "maximum": 10,
                    "description": "Escalate on this many reviews with substantive failures, including the initial review if it fails.",
                },
                "nits_nonblocking_after_pass": {
                    "type": "integer", "minimum": 1, "maximum": 2,
                    "description": "Nits become nonblocking from this review pass; this does not relax substantive acceptance.",
                },
            }),
            "holistic": _object(["independent", "scope"], {
                "independent": {"const": True}, "scope": {"const": "candidate"},
            }),
        }),
        "escalation": _object(["target"], {
            "target": {"enum": ["block_queue", "interactive_orchestrator"]},
        }),
        "completion": _object(["route"], {
            "route": {"enum": ["return", "human_review", "interactive_orchestrator", "merge"]},
        }),
        "domains": {"type": "array", "items": DOMAIN_SCHEMA, "maxItems": 32},
        "transfers": {"type": "array", "items": TRANSFER_SCHEMA, "maxItems": 200},
    }),
    "allOf": [
        {"if": {"properties": {"mode": {"const": "coordinated"}}}, "then": {
            "required": ["review", "escalation", "decomposition", "product_decisions"],
            "properties": {
                "roles": {"required": list(COORDINATED_ROLES)},
                "completion": {"properties": {"route": {
                    "enum": ["human_review", "interactive_orchestrator", "merge"]}}},
            },
        }},
        {"if": {"properties": {"mode": {"const": "direct"}}}, "then": {
            "properties": {
                "planner": {"properties": {"mode": {"const": "none"}}},
                "roles": {"required": ["implementer"], "maxProperties": 1},
                "completion": {"properties": {"route": {
                    "enum": ["return", "human_review", "interactive_orchestrator"]}}},
            },
            "not": {"anyOf": [{"required": ["review"]}, {"required": ["decomposition"]}]},
        }},
        {"if": {"properties": {"planner": {"properties": {"mode": {"const": "external"}}}}},
         "then": {"properties": {"planner": {
             "required": ["role", "required_inputs", "output_contract", "interaction"],
         }}}},
        {"if": {"properties": {"planner": {"properties": {"mode": {"const": "none"}}}}},
         "then": {"properties": {"planner": {
             "not": {"anyOf": [{"required": [field]} for field in
                               ("role", "required_inputs", "output_contract", "interaction", "discovery_roles")]},
         }}}},
    ],
}


def _canonical(document: dict) -> bytes:
    try:
        return json.dumps(document, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError) as exc:
        raise WorkflowError("invalid_workflow", "Workflow values must be finite JSON values") from exc


def _digest(document: dict) -> str:
    return "sha256:" + hashlib.sha256(_canonical(document)).hexdigest()


def _same_model_selection(first: dict, second: dict) -> bool:
    """Adapter changes cannot turn identical weights or names into diversity."""
    if first.get("digest") and second.get("digest"):
        return first["digest"] == second["digest"]
    # A provider selection is not immutable-weight evidence. If either side
    # lacks a digest, conservatively compare the explicit selection itself.
    return first["model"] == second["model"]


def _validate(schema: dict, document, code: str) -> None:
    errors = sorted(Draft202012Validator(schema).iter_errors(document),
                    key=lambda error: str(list(error.path)))
    if errors:
        error = errors[0]
        path = ".".join(map(str, error.absolute_path)) or "workflow"
        raise WorkflowError(code, f"{path}: {error.message}")


def validate_revision_ref(reference: dict, label: str = "reference") -> dict:
    """Validate identity only; the named owner remains authoritative for content."""
    _canonical(reference)
    try:
        _validate(REVISION_REF_SCHEMA, reference, "invalid_revision_ref")
    except WorkflowError as exc:
        raise WorkflowError(exc.code, f"{label}: {exc.message}") from exc
    return copy.deepcopy(reference)


def validate_workflow(document: dict) -> dict:
    """Return a validated independent definition, without resolving or executing it."""
    if len(_canonical(document)) > 2_000_000:
        raise WorkflowError("invalid_workflow", "Workflow exceeds the 2 MB configuration limit")
    _validate(SCHEMA, document, "invalid_workflow")
    planner = document["planner"]
    discovery = set(planner.get("discovery_roles", []))
    if discovery.intersection(COORDINATED_ROLES):
        raise WorkflowError("invalid_workflow", "Discovery roles must be separate named roles")
    planner_roles = {planner["role"]} if planner["mode"] == "external" else set()
    if planner_roles.intersection(set(COORDINATED_ROLES) | discovery):
        raise WorkflowError("invalid_workflow", "The external planner requires its own named role")
    required = set(COORDINATED_ROLES) if document["mode"] == "coordinated" else {"implementer"}
    if set(document["roles"]) != required | discovery | planner_roles:
        raise WorkflowError("invalid_workflow", "Roles must match delivery, external planner and declared discovery roles")
    transfers = document["transfers"]
    ids = [transfer["id"] for transfer in transfers]
    if len(ids) != len(set(ids)):
        raise WorkflowError("invalid_workflow", "Transfer identifiers must be unique")
    transitions = set()
    for transfer in transfers:
        if transfer["from_role"] not in required:
            raise WorkflowError("invalid_workflow", "Transfer sources must be delivery roles")
        if transfer["target"]["kind"] == "role":
            target_role = transfer["target"]["role"]
            if target_role not in required or target_role == transfer["from_role"]:
                raise WorkflowError("invalid_workflow", "Transfer targets must be a different delivery role")
            transitions.add((transfer["from_role"], target_role))
    if document["mode"] == "coordinated" and not COORDINATED_TRANSITIONS <= transitions:
        raise WorkflowError("invalid_workflow", "Declare every coordinated implementation, review and rework transfer")
    domain_ids = [domain["id"] for domain in document.get("domains", [])]
    if len(domain_ids) != len(set(domain_ids)):
        raise WorkflowError("invalid_workflow", "Domain identifiers must be unique")
    for domain in document.get("domains", []):
        evidence_ids = [evidence["id"] for evidence in domain["required_evidence"]]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise WorkflowError("invalid_workflow", f"Domain {domain['id']}: evidence identifiers must be unique")
        if not domain["procedures"] and not evidence_ids:
            raise WorkflowError("invalid_workflow", f"Domain {domain['id']}: declare a procedure or evidence requirement")
        if document["mode"] == "direct" and any(
                evidence["stage"] in {"inline_review", "holistic_review"}
                for evidence in domain["required_evidence"]):
            raise WorkflowError("invalid_workflow", "Direct workflows cannot require an omitted review stage")
    return copy.deepcopy(document)


def workflow_revision(document: dict) -> str:
    return _digest(validate_workflow(document))


def delivery_role_names(document: dict) -> tuple[str, ...]:
    """Roles available after requirements handoff; authoring roles are excluded."""
    definition = validate_workflow(document)
    return COORDINATED_ROLES if definition["mode"] == "coordinated" else ("implementer",)


def load_workflow(path: str | Path) -> tuple[dict, str]:
    raw = Path(path).read_bytes()
    if len(raw) > 2_000_000:
        raise WorkflowError("invalid_workflow", "Workflow exceeds the 2 MB configuration limit")
    try:
        document = yaml.load(raw, Loader=policy.UniqueLoader)
    except (yaml.YAMLError, policy.PolicyError, ValueError, RecursionError) as exc:
        raise WorkflowError("invalid_workflow", "Workflow is not valid YAML or contains duplicate keys") from exc
    document = validate_workflow(document)
    return document, _digest(document)


def _compile_transfers(definition: dict, bundle: dict, roles: dict, input_class: str) -> dict:
    ranks = flow.class_ranks(bundle)
    destinations = {item["id"]: item for item in bundle["destinations"]}
    contracts = {item["id"]: item for item in bundle.get("output_contracts", [])}
    compiled = {}
    for transfer in definition["transfers"]:
        source = roles[transfer["from_role"]]
        target = transfer["target"]
        target_role = roles[target["role"]] if target["kind"] == "role" else None
        destination_id = target_role["model_destination"] if target_role else target["destination"]
        if destination_id not in destinations:
            raise WorkflowError("invalid_transfer", f"Transfer {transfer['id']}: destination is not installed")
        destination = destinations[destination_id]
        target_class = target_role["input_class"] if target_role else destination["clearance"]
        lower_role = target_role is not None and ranks[target_class] < ranks[input_class]
        crosses_boundary = (source["model_boundary"] == "private"
                            and flow.destination_boundary(destination) == "public")
        lowers_class = ranks[source["output_class"]] > ranks[target_class]
        needs_release = lower_role or crosses_boundary or lowers_class or "output_contract" in transfer
        contract = contracts.get(transfer.get("output_contract"))
        if "output_contract" in transfer and contract is None:
            raise WorkflowError("invalid_transfer", f"Transfer {transfer['id']}: output contract is not installed")
        if needs_release and contract is None:
            raise WorkflowError("output_release_required",
                                f"Transfer {transfer['id']}: an installed output contract is required")
        transferred_class = source["output_class"]
        if contract:
            if (ranks[contract["source_class"]] < ranks[source["output_class"]]
                    or ranks[contract["output_class"]] > ranks[target_class]
                    or destination_id not in contract["destinations"]):
                raise WorkflowError("invalid_transfer", f"Transfer {transfer['id']}: output contract does not cover this flow")
            transferred_class = contract["output_class"]
        try:
            flow.check_destination(bundle, destination, transferred_class)
        except flow.FlowError as exc:
            raise WorkflowError("invalid_transfer", f"Transfer {transfer['id']}: {exc}") from exc
        if target_role and needs_release:
            target_role["requires_output_release"] = True
        compiled[transfer["id"]] = {
            "from_role": transfer["from_role"], "target": copy.deepcopy(target),
            "contract_id": contract["id"] if contract else None,
            "contract_revision": flow.contract_revision(contract) if contract else None,
            "requires_output_release": needs_release,
        }
    return compiled


def compile_workflow(document: dict, bundle: dict, *, requirements_ref: dict,
                     session_policy_ref: dict, installation_id: str,
                     generation: str | None = None, input_class: str | None = None) -> dict:
    """Bind a definition to an authoritative installed bundle snapshot.

    ``generation`` is supplied by the installation owner when its generation also
    binds a hosting envelope. It must never come from an untrusted bind request.
    Missing input classification selects the most restrictive class in the
    installation. A lower-class role can receive only an approved output release;
    a contract declaration alone neither lowers data nor permits publication.
    """
    definition = validate_workflow(document)
    requirements = validate_revision_ref(requirements_ref, "requirements_ref")
    session_policy = validate_revision_ref(session_policy_ref, "session_policy_ref")
    _validate(REF_TEXT, installation_id, "invalid_binding")
    try:
        installed = policy.validate_bundle(bundle)
        installed_generation = generation if generation is not None else policy.bundle_generation(installed)
    except (policy.PolicyError, ValueError, TypeError) as exc:
        raise WorkflowError("invalid_bundle", "Workflow binding requires a valid installed bundle") from exc
    _validate(DIGEST, installed_generation, "invalid_binding")
    try:
        selected_class = flow.resolve_input_class(installed, input_class)
    except flow.FlowError as exc:
        raise WorkflowError("invalid_input_class", str(exc)) from exc
    ranks = flow.class_ranks(installed)
    delivery = set(delivery_role_names(definition))
    roles = {}
    recipes = {}
    for role, selection in sorted(definition["roles"].items()):
        try:
            recipe = policy.resolve_recipe(installed, selection["grant"])
        except policy.PolicyError as exc:
            raise WorkflowError("unknown_workflow_grant",
                                f"Role {role}: grant {selection['grant']} is not installed") from exc
        recipes[role] = recipe
        try:
            admission = flow.check_role_input(installed, selection["grant"], selection.get("input_class", selected_class))
        except flow.FlowError as exc:
            raise WorkflowError("role_input_denied", f"Role {role}: {exc}") from exc
        roles[role] = {
            "grant_id": recipe["grant"]["id"], "agent_id": recipe["agent"]["id"],
            "harness_id": recipe["harness"]["id"], "harness_kind": recipe["harness"]["kind"],
            "model_id": recipe["model"]["id"], "model_adapter": recipe["model"]["adapter"],
            "model_destination": recipe["model"]["destination"],
            "compute_id": recipe["compute"]["id"], "compute_destination": recipe["compute"]["destination"],
            **admission,
            "requires_output_release": role in delivery and ranks[admission["input_class"]] < ranks[selected_class],
        }
    entry_role = "orchestrator" if definition["mode"] == "coordinated" else "implementer"
    if ranks[roles[entry_role]["input_class"]] < ranks[selected_class]:
        raise WorkflowError("role_input_denied", "The entry role must accept the original requirements input class")
    if definition["mode"] == "coordinated":
        # Separate configured agents preserve role-specific instructions. This
        # declaration does not prove separate runtime sessions or independent work.
        review_agents = [roles[role]["agent_id"] for role in
                         ("implementer", "inline_reviewer", "holistic_reviewer")]
        if len(set(review_agents)) != 3:
            raise WorkflowError("reviewer_not_independent",
                                "Implementation, inline review and holistic review require distinct agent definitions")
        if definition["review"].get("model_diversity") == "required":
            if any(_same_model_selection(recipes[reviewer]["model"], recipes["implementer"]["model"])
                   for reviewer in ("inline_reviewer", "holistic_reviewer")):
                raise WorkflowError("review_model_diversity_required",
                                    "Required reviewer diversity needs each reviewer to differ from the implementer")
    transfers = _compile_transfers(definition, installed, roles, selected_class)
    for role, resolved in roles.items():
        if resolved["requires_output_release"] and not recipes[role]["grant"].get("workflow_only", False):
            raise WorkflowError(
                "workflow_only_required",
                f"Role {role}: a release-gated role requires a workflow-only grant",
            )
    binding = {
        "apiVersion": API_VERSION, "kind": "DeliveryWorkflowBinding",
        "workflow_revision": _digest(definition), "definition": definition,
        "installation_id": installation_id, "bundle_generation": installed_generation,
        "input_class": selected_class, "transfers": transfers,
        "roles": roles, "requirements_ref": requirements, "session_policy_ref": session_policy,
        "dispatch_authorized": False,
    }
    binding["binding_digest"] = _digest(binding)
    return binding
