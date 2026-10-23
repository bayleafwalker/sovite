"""Caller discovery is a labelled observation, never execution or release authority."""
import copy
import json
from pathlib import Path

import pytest
import yaml

from sovite.service import PlatformError
from test_result_access import installed as base_installed, configure, complete
from test_workflow_admission import request
from test_workflow_admission import installed as mixed_installed


@pytest.fixture
def installed(base_installed):
    platform, bundle = base_installed
    bundle["discovery_class"] = "C1"
    configure(platform, bundle)
    return platform, bundle


def discover(platform, caller="cloud-codex", **arguments):
    response = request(platform, "POST", "/mcp", token=platform.settings.caller_tokens[caller],
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "list_candidates", "arguments": arguments}})
    assert response.status_code == 200
    return response.json()["result"]


def test_discovery_is_caller_scoped_and_redacts_internal_configuration(installed):
    platform, bundle = installed
    bundle["agents"][0]["instructions"] = "PRIVATE-INSTRUCTIONS-CANARY"
    bundle["harnesses"][0]["description"] = "PRIVATE-ENDPOINT-CANARY"
    configure(platform, bundle)
    result = discover(platform)
    assert not result["isError"]
    data = result["structuredContent"]
    assert [c["grant_id"] for c in data["candidates"]] == ["incident-forensics"]
    raw = json.dumps(data)
    assert "change-assessment" not in raw
    assert "PRIVATE-INSTRUCTIONS-CANARY" not in raw
    assert "PRIVATE-ENDPOINT-CANARY" not in raw
    assert "credential" not in raw and "endpoint" not in raw
    assert data["generation"] == platform.active()[1]
    assert data["input_class"] == "C3"
    assert discover(platform, "reader")["structuredContent"]["candidates"] == []


def test_return_contract_clearance_and_declared_capabilities_are_facts(installed):
    platform, bundle = installed
    bundle["harnesses"][0]["capabilities"].append("interactive-authentication")
    configure(platform, bundle, caller_clearance="C1")
    candidate = discover(platform)["structuredContent"]["candidates"][0]
    assert candidate["declared"]["return_contract"] == "class-aware"
    assert candidate["observed"]["caller_can_receive_content"] is False
    assert candidate["declared"]["interactive_authentication_support"] is True
    assert candidate["observed"]["human_presence_verified"] is False
    assert candidate["observed"]["capacity"] == "not_probed"
    assert candidate["observed"]["qualification"] == "not_required"
    configure(platform, bundle, caller_clearance="C3")
    assert discover(platform)["structuredContent"]["candidates"][0]["observed"]["caller_can_receive_content"]
    configure(platform, bundle, return_contract="reference")
    assert not discover(platform)["structuredContent"]["candidates"][0]["observed"]["caller_can_receive_content"]


def test_listing_does_not_bypass_current_inhibit(installed):
    platform, _ = installed
    before = discover(platform)["structuredContent"]["candidates"][0]
    assert before["observed"]["admission"] == "permitted_at_observation"
    platform.inhibit("incident-forensics", "owner paused this route")
    after = discover(platform)["structuredContent"]["candidates"][0]
    assert after["observed"]["admission"] == "blocked"
    assert after["observed"]["reason"] == "grant_inhibited"
    with pytest.raises(PlatformError, match="inhibited"):
        platform.submit("incident-forensics", "task", "cloud-codex", "after-listing")


def test_operator_cannot_use_caller_discovery(installed):
    platform, _ = installed
    result = request(platform, "POST", "/mcp", token=platform.settings.operator_token,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_candidates"}})
    assert result.status_code == 403


def test_unknown_input_class_and_extra_preferences_are_refused(installed):
    platform, _ = installed
    assert discover(platform, input_class="missing-class")["isError"]
    response = request(platform, "POST", "/mcp", token=platform.settings.caller_tokens["cloud-codex"],
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "list_candidates", "arguments": {"spend_this_week": 500}}})
    assert response.json()["error"]["code"] == -32602


def test_qualification_is_not_fabricated_when_required(installed):
    platform, _ = installed
    platform.settings.require_qualification = True
    candidate = discover(platform)["structuredContent"]["candidates"][0]
    assert candidate["observed"]["qualification"] == "missing"
    assert candidate["observed"]["reason"] == "qualification_required"


def test_public_route_requires_explicit_permitted_class(mixed_installed):
    platform, _ = mixed_installed
    bundle, _ = platform.active()
    bundle = copy.deepcopy(bundle)
    # This fixture installs only C0/C3. The owner deliberately labels this
    # synthetic discovery projection public; no rank/name is invented.
    bundle["discovery_class"] = "C0"
    for grant in bundle["grants"]:
        if "workflow-consumer" in grant["callers"]:
            grant["caller_clearance"] = "C0"
    Path(platform.settings.bundle_path).write_text(yaml.safe_dump(bundle))
    platform.reload(platform.settings.bundle_path)
    current = discover(platform, "workflow-consumer")["structuredContent"]
    public = next(c for c in current["candidates"] if c["grant_id"] == "public-direct")
    assert current["input_class"] == "C3"
    assert public["declared"]["inference_boundary"] == "public"
    assert public["observed"]["admission"] == "blocked"
    explicit = discover(platform, "workflow-consumer", input_class="C0")["structuredContent"]
    public = next(c for c in explicit["candidates"] if c["grant_id"] == "public-direct")
    assert public["observed"]["admission"] == "permitted_at_observation"
    bound = next(c for c in explicit["candidates"] if c["grant_id"] == "inline_reviewer-grant")
    assert bound["declared"]["requires_workflow_binding"] is True
    assert bound["observed"]["reason"] == "workflow_binding_required"
    assert bound["observed"]["admission"] == "bound_workflow_required"


def test_caller_below_installed_discovery_class_gets_only_minimal_binding(installed):
    platform, bundle = installed
    configure(platform, bundle, caller_clearance="C0", receipt_class="C0")
    data = discover(platform)["structuredContent"]
    assert "detail_class" not in data
    assert "generation" not in data and "observed_at" not in data
    assert data["candidates"] == [{"grant_id": "incident-forensics", "return_contract": "class-aware",
                                   "details": "clearance_required"}]


def test_missing_discovery_class_defaults_to_highest_without_mutating_bundle(installed):
    platform, bundle = installed
    bundle.pop("discovery_class")
    configure(platform, bundle, caller_clearance="C1")
    assert discover(platform)["structuredContent"]["candidates"][0]["details"] == "clearance_required"
    assert "discovery_class" not in platform.active()[0]


def test_discovery_class_must_resolve(installed):
    platform, bundle = installed
    bundle["discovery_class"] = "not-installed"
    with pytest.raises(ValueError, match="discovery_class"):
        configure(platform, bundle)


def test_no_grants_or_clearance_cannot_probe_class_membership(installed):
    platform, bundle = installed
    reference = discover(platform, "reader", input_class="C1")["structuredContent"]
    assert discover(platform, "reader", input_class="not-installed")["structuredContent"] == reference
    configure(platform, bundle, caller_clearance="C0", receipt_class="C0")
    reference = discover(platform, input_class="C1")["structuredContent"]
    assert discover(platform, input_class="not-installed")["structuredContent"] == reference
