"""Keep the concrete authoring examples and their pinned local inputs usable."""
import hashlib
import json
from pathlib import Path

import pytest

from sovite import policy
from sovite.workflows import compile_workflow, load_workflow, validate_revision_ref


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "config" / "workflows"
CASES = (
    ("software-delivery", "software-requirements"),
    ("data-engineering", "data-requirements"),
    ("one-shot", "one-shot-requirements"),
)


def read_reference(path):
    reference = validate_revision_ref(json.loads(path.read_text()))
    content_path = ROOT / reference["resource_id"]
    assert content_path.is_relative_to(EXAMPLES)
    actual = "sha256:" + hashlib.sha256(content_path.read_bytes()).hexdigest()
    assert reference["content_digest"] == actual
    assert reference["revision"] == actual
    return reference


@pytest.mark.parametrize("path", sorted(EXAMPLES.rglob("*.ref.json")), ids=lambda path: path.stem)
def test_example_references_pin_actual_content(path):
    read_reference(path)


@pytest.mark.parametrize("name,requirements", CASES)
def test_examples_bind_to_the_delivery_bundle(name, requirements):
    definition, revision = load_workflow(EXAMPLES / f"{name}.yaml")
    bundle, _ = policy.load_bundle(ROOT / "config" / "platform.delivery.yaml")
    binding = compile_workflow(
        definition, bundle,
        requirements_ref=read_reference(EXAMPLES / "inputs" / f"{requirements}.ref.json"),
        session_policy_ref=read_reference(EXAMPLES / "inputs" / "session-policy.ref.json"),
        installation_id="example-delivery-installation", input_class="C0",
    )
    assert binding["workflow_revision"] == revision
    assert binding["input_class"] == "C0"
    assert binding["dispatch_authorized"] is False
    for domain in definition.get("domains", []):
        for reference in domain["procedures"]:
            ref_path = (ROOT / reference["resource_id"]).with_suffix(".ref.json")
            assert reference == read_reference(ref_path)
    if definition["mode"] == "coordinated":
        assert binding["transfers"]["repository-candidate"]["contract_id"] == "publish-candidate"
        assert binding["transfers"]["vuoro-finding"]["contract_id"] == "publish-finding"


def test_delivery_output_contracts_pin_the_release_procedure():
    bundle, _ = policy.load_bundle(ROOT / "config" / "platform.delivery.yaml")
    procedure = read_reference(EXAMPLES / "procedures" / "output-release.ref.json")
    contracts = {contract["id"]: contract for contract in bundle["output_contracts"]}
    for contract_id in ("publish-candidate", "publish-finding"):
        assert contracts[contract_id]["procedure_ref"] == procedure
