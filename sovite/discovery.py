"""Caller-scoped, whitelisted observations of installed execution candidates.

No task is submitted or content fetched. Declared support and signed installation
qualification do not attest account readiness, capacity or human authentication.
Admission and every later owner boundary continue to recheck actual authority.
"""
from __future__ import annotations

import time

from . import policy
from .flow import FlowError, destination_boundary, resolve_input_class
from .qualification import current_qualification
from .service import PlatformError


def list_candidates(platform, caller: str, input_class: str | None = None) -> dict:
    if not platform.settings.caller_tokens.get(caller):
        raise PlatformError("forbidden", "A current caller identity is required", 403)
    with platform.db.sessions.begin() as session:
        # A single owner snapshot: activation/inhibits cannot interleave while
        # assembling a caller's classified projection. This reserves no work.
        platform.db.authority_lock(session)
        bundle, generation = platform.active(session)
        selected = None
        ranks = {item["id"]: item["rank"] for item in bundle["data_classes"]}
        detail_class = bundle.get("discovery_class", max(ranks, key=ranks.get))
        required = (platform.settings.require_qualification or bool(platform.settings.hosting_envelope_path)
                    or bundle.get("mode") == "enforced")
        qualification_status = None
        candidates = []
        details_visible = False
        for grant in sorted(bundle["grants"], key=lambda item: item["id"]):
            if caller not in grant["callers"]:
                continue
            receipt_class = grant.get("receipt_class", min(ranks, key=ranks.get))
            clearance = grant.get("caller_clearance", receipt_class)
            if ranks[clearance] < ranks[detail_class]:
                candidates.append({"grant_id": grant["id"], "return_contract": grant["return_contract"],
                                   "details": "clearance_required"})
                continue
            details_visible = True
            if selected is None:
                try:
                    selected = resolve_input_class(bundle, input_class)
                except (FlowError, TypeError) as exc:
                    raise PlatformError("invalid_input_class", "The input class is unavailable", 422) from exc
                qualification = current_qualification(session, platform.settings, generation,
                                                      bundle.get("default_profile", "local"))
                qualification_status = "valid_record" if qualification else "missing" if required else "not_required"
            recipe = policy.resolve_recipe(bundle, grant["id"])
            model, harness, agent = (recipe[key] for key in ("model", "harness", "agent"))
            destination = next(item for item in recipe["destinations"] if item["id"] == model["destination"])
            capabilities = sorted(harness.get("capabilities", []))
            reason = None
            try:
                platform.authorize(session, grant["id"], caller, selected, generation=generation)
            except PlatformError as exc:
                reason = exc.code
            candidates.append({
                "grant_id": grant["id"],
                "declared": {
                    "agent_id": agent["id"], "model_id": model["id"],
                    "model_adapter": model["adapter"], "harness_id": harness["id"],
                    "harness_kind": harness["kind"], "capabilities": capabilities,
                    "interactive_authentication_support": "interactive-authentication" in capabilities,
                    "inference_boundary": destination_boundary(destination),
                    "input_class_ceiling": grant["input_class"], "output_class": grant["output_class"],
                    "return_contract": grant["return_contract"],
                    "requires_workflow_binding": grant.get("workflow_only", False),
                    "model_max_concurrency": model.get("max_concurrency"),
                },
                "observed": {
                    "admission": ("bound_workflow_required" if reason == "workflow_binding_required" else
                                  "blocked" if reason else "permitted_at_observation"), "reason": reason,
                    "qualification": qualification_status,
                    "caller_can_receive_content": (reason is None and grant["return_contract"] == "class-aware" and
                                                   ranks[clearance] >= ranks[grant["output_class"]]),
                    "human_presence_verified": False, "capacity": "not_probed",
                },
            })
        result = {"scope": "Caller-visible bindings; detailed metadata requires installed clearance. Admission rechecks authority; capacity and model-gateway runtime availability are not probed.",
                  "candidates": candidates}
        if details_visible:
            result.update(generation=generation, observed_at=time.time(), input_class=selected, detail_class=detail_class)
        return result
