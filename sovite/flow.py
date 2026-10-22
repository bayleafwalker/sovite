"""Data classification and declared boundary checks shared by admission and binding."""
from __future__ import annotations

import hashlib
import json


class FlowError(ValueError):
    def __init__(self, message: str, *, code: str = "class_denied"):
        super().__init__(message)
        self.code = code


def class_ranks(bundle: dict) -> dict[str, int]:
    return {item["id"]: item["rank"] for item in bundle["data_classes"]}


def most_restrictive_class(bundle: dict) -> str:
    return max(class_ranks(bundle), key=class_ranks(bundle).__getitem__)


def resolve_input_class(bundle: dict, value: str | None) -> str:
    selected = most_restrictive_class(bundle) if value is None else value
    if not isinstance(selected, str) or selected not in class_ranks(bundle):
        raise FlowError("Unknown input class")
    return selected


def destination_boundary(destination: dict) -> str:
    return destination.get("boundary", "public" if destination["kind"] == "cloud" else "private")


def restricted(bundle: dict, class_id: str) -> bool:
    ranks = class_ranks(bundle)
    threshold = bundle.get("restricted_class", most_restrictive_class(bundle))
    return ranks[class_id] >= ranks[threshold]


def check_destination(bundle: dict, destination: dict, class_id: str) -> None:
    ranks = class_ranks(bundle)
    if ranks[class_id] > ranks[destination["clearance"]]:
        raise FlowError("Destination clearance is insufficient", code="binding_context_denied")
    if restricted(bundle, class_id) and destination_boundary(destination) != "private":
        raise FlowError("Restricted data requires a private boundary", code="binding_context_denied")


def check_role_input(bundle: dict, grant_id: str, input_class: str | None) -> dict:
    """Check a role before dispatch; no caller authority or qualification is granted."""
    selected = resolve_input_class(bundle, input_class)
    ranks = class_ranks(bundle)
    grants = {item["id"]: item for item in bundle["grants"]}
    if grant_id not in grants or not grants[grant_id].get("enabled", True):
        raise FlowError("Role grant is unavailable", code="grant_unavailable")
    grant = grants[grant_id]
    if ranks[selected] > ranks[grant["input_class"]]:
        raise FlowError("Input class exceeds the role grant")
    agents = {item["id"]: item for item in bundle["agents"]}
    models = {item["id"]: item for item in bundle["models"]}
    destinations = {item["id"]: item for item in bundle["destinations"]}
    model = models[agents[grant["agent"]]["model"]]
    if not model.get("enabled", True):
        raise FlowError("Role model is disabled", code="binding_disabled")
    destination = destinations[model["destination"]]
    # Declared output and tool context can be more sensitive than the initial input.
    output = grant["output_class"]
    context = max((selected, output), key=ranks.__getitem__)
    check_destination(bundle, destination, context)
    return {"input_class": selected, "output_class": output,
            "model_boundary": destination_boundary(destination)}


def contract_revision(contract: dict) -> str:
    body = json.dumps(contract, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()
    return "sha256:" + hashlib.sha256(body).hexdigest()


def validate_output_contracts(bundle: dict) -> None:
    ranks = class_ranks(bundle)
    destinations = {item["id"]: item for item in bundle["destinations"]}
    seen = set()
    for contract in bundle.get("output_contracts", []):
        if contract["id"] in seen:
            raise FlowError("Duplicate output contract")
        seen.add(contract["id"])
        source, output = contract["source_class"], contract["output_class"]
        if source not in ranks or output not in ranks or ranks[output] > ranks[source]:
            raise FlowError("Output contract must name known classes and cannot raise its source ceiling")
        for name in contract["destinations"]:
            if name not in destinations:
                raise FlowError("Unknown output contract destination")
            check_destination(bundle, destinations[name], output)
