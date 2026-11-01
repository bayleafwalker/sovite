"""Synthetic structural vectors; none is an observation or qualification."""
import ast
import json
from pathlib import Path

import pytest

from sovite.collection_evidence_contract import InvalidReceipt, RECIPES, parse_receipt


def D(char):
    return char * 64


def vector_p():
    return {
        "schema_version": "sovite.collection-recipe-receipt/v1", "evidence_kind": "pre-dispatch-denial",
        "recipe_id": "native-config-isolation", "recipe_version": "1", "manifest_sha256": D("0"),
        "fixture_sha256": D("1"), "build_sha256": D("2"), "session_digest": D("3"),
        "deployment_sha256": D("4"), "started_ms": 1000, "ended_ms": 1001, "uncertainty_ms": 1,
        "outcome": "fail", "positive_observation_sha256": D("5"), "control_observation_sha256": D("6"),
        "custody_sha256": [D("7")], "provider_request_count": 0, "claim_scope": "native-config-isolation",
        "session_id": "example-session", "epoch": 1, "epoch_purpose": "execution",
        "operation_id": "example-operation", "resource_sha256": D("8"), "no_dispatch_observation_sha256": D("9"),
    }


def encode(value):
    return json.dumps(value, separators=(",", ":")).encode()


def variants():
    p = vector_p()
    u = p.copy()
    u.update(evidence_kind="dispatch-unknown", outcome="unknown", provider_request_count=None,
             positive_observation_sha256=None, control_observation_sha256=None,
             intent_sha256=D("a"), possible_dispatch_observation_sha256=D("b"))
    del u["no_dispatch_observation_sha256"]
    g = p.copy()
    g.update(evidence_kind="guest-effect", recipe_id="exact-binding", claim_scope="exact-binding",
             intent_sha256=D("a"), ack_sha256=D("b"), quiescence_sha256=None, import_sha256=None)
    del g["no_dispatch_observation_sha256"]
    ledger = p.copy()
    ledger.update(evidence_kind="isolated-ledger", recipe_id="bounded-usage", claim_scope="bounded-usage",
                  test_environment_sha256=D("a"), db_audit_sha256=D("b"),
                  zero_provider_observation_sha256=D("c"), installed_gate_observation_sha256=D("d"))
    for key in ("session_id", "epoch", "epoch_purpose", "operation_id", "resource_sha256", "no_dispatch_observation_sha256"):
        del ledger[key]
    return p, u, g, ledger


def test_canonical_vectors_and_permuted_transport():
    expected = ["43794bd76adda17545e3543d33e71c205a413eaaa046e3e2c1a4596d733b9893",
                "734f9a5c5ca125f452952f8325834da02f60740b068a40214a99582a4a79a394",
                "a2caffcccfb61ab645c8a1c0e2edfaa3566e50f3d76b158573683d188c8b078b",
                "c9307abbbb6909293ebb370b901c9acc90df619dfbac9bf23763a7ea3b5bf3f8"]
    for value, digest in zip(variants(), expected):
        identity = parse_receipt(encode(value))
        assert identity.sha256 == digest
        assert parse_receipt(json.dumps(dict(reversed(list(value.items())),), indent=2).encode()) == identity
    p = variants()[0]
    assert parse_receipt(encode(p)).canonical_bytes == json.dumps(p, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def test_recipe_set_is_exact():
    assert RECIPES == {
        "per-attempt-isolation", "credential-containment", "exact-binding", "durable-checkpoint-and-candidate",
        "predecessor-reconciliation", "bounded-usage", "native-rpc-routing", "native-supervisor-containment",
        "native-config-isolation", "vm-process-lifecycle", "candidate-quiescence-import",
    }


@pytest.mark.parametrize("recipe", sorted(RECIPES))
def test_recipe_identifiers_are_closed(recipe):
    p = vector_p()
    p.update(recipe_id=recipe, claim_scope=recipe)
    assert parse_receipt(encode(p)).canonical_bytes


@pytest.mark.parametrize("key,bad", [
    ("schema_version", "other"), ("recipe_version", "2"), ("recipe_id", "other"),
    ("claim_scope", "other"), ("outcome", "yes"), ("evidence_kind", "other"),
    ("manifest_sha256", D("A")), ("session_id", "bad/path"), ("operation_id", "x" * 201),
    ("epoch", True), ("epoch", 0), ("epoch_purpose", "other"), ("started_ms", 0),
    ("started_ms", True), ("ended_ms", 2**53), ("uncertainty_ms", 2001),
    ("provider_request_count", True), ("provider_request_count", 1),
    ("custody_sha256", []), ("custody_sha256", [D("7"), D("7")]),
    ("custody_sha256", [D("8"), D("7")]), ("custody_sha256", [D("a")] * 17),
])
def test_bad_fields_refused(key, bad):
    p = vector_p()
    p[key] = bad
    with pytest.raises(InvalidReceipt):
        parse_receipt(encode(p))


def test_boundaries_and_closed_variants():
    p, u, g, ledger = variants()
    p.update(started_ms=1, ended_ms=120001, uncertainty_ms=2000, epoch=2**53 - 1)
    p["custody_sha256"] = [f"{i:064x}" for i in range(16)]
    parse_receipt(encode(p))
    for changes in ({"ended_ms": 120002}, {"ended_ms": 0}, {"extra": None}, {"ack_sha256": D("a")}):
        q = p | changes
        with pytest.raises(InvalidReceipt):
            parse_receipt(encode(q))
    for key in p:
        q = p.copy()
        del q[key]
        with pytest.raises(InvalidReceipt):
            parse_receipt(encode(q))
    for changes in ({"outcome": "pass"}, {"ack_sha256": D("a")}, {"provider_request_count": 0}):
        with pytest.raises(InvalidReceipt):
            parse_receipt(encode(u | changes))
    assert parse_receipt(encode(u))  # syntax cannot prove whether dispatch happened
    for key in ("ack_sha256", "intent_sha256"):
        q = g.copy()
        del q[key]
        with pytest.raises(InvalidReceipt):
            parse_receipt(encode(q))
    g.update(recipe_id="candidate-quiescence-import", claim_scope="candidate-quiescence-import")
    with pytest.raises(InvalidReceipt):
        parse_receipt(encode(g))
    g.update(quiescence_sha256=D("a"), import_sha256=D("b"))
    parse_receipt(encode(g))
    ledger.update(recipe_id="exact-binding", claim_scope="exact-binding")
    with pytest.raises(InvalidReceipt):
        parse_receipt(encode(ledger))


@pytest.mark.parametrize("raw", [b"", b"[]", b"\xef\xbb\xbf{}", b"{} true", b"\xff", b'{"x":1,"x":2}',
                                 b'{"a":{"b":1,"b":2}}', b'{"x":1e0}', b'{"x":NaN}',
                                 b'{"x":"\\ud800"}', b'[[[[[0]]]]]', b" " * 16385])
def test_invalid_json_refused(raw):
    with pytest.raises(InvalidReceipt):
        parse_receipt(raw)


def test_duplicate_key_on_otherwise_valid_receipt_is_refused():
    raw = encode(vector_p())
    duplicate = raw[:-1] + b',"schema_version":"sovite.collection-recipe-receipt/v1"}'
    with pytest.raises(InvalidReceipt):
        parse_receipt(duplicate)


def test_semantic_identity_changes_and_false_denial_is_only_syntax():
    p = vector_p()
    original = parse_receipt(encode(p))
    p["resource_sha256"] = D("a")
    assert parse_receipt(encode(p)).sha256 != original.sha256
    # A claimed denial is accepted structurally without accessing any evidence.
    assert parse_receipt(encode(p)).canonical_bytes


def test_no_production_import_or_route():
    root = Path(__file__).resolve().parents[1] / "sovite"
    for path in root.rglob("*.py"):
        if path.name == "collection_evidence_contract.py":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all("collection_evidence_contract" not in alias.name for alias in node.names)
            if isinstance(node, ast.ImportFrom):
                assert "collection_evidence_contract" not in (node.module or "")


def test_predecode_array_and_member_limits():
    from sovite.collection_evidence_contract import _decode, _preflight

    _preflight(b'[' + b','.join([b'0'] * 16) + b']')
    with pytest.raises(InvalidReceipt):
        _preflight(b'[' + b','.join([b'0'] * 17) + b']')
    _preflight(b'[[0,1],[2,3]]')  # two outer and four inner elements
    with pytest.raises(InvalidReceipt):
        _preflight(b'[[' + b','.join([b'0'] * 15) + b'],0]')
    def members(n):
        return ('{' + ','.join(f'"k{i}":0' for i in range(n)) + '}').encode()
    assert len(_decode(members(40))) == 40
    with pytest.raises(InvalidReceipt):
        _decode(members(41))


@pytest.mark.parametrize("old,new", [
    ('return type(value) is str and _DIGEST.fullmatch(value) is not None', 'return True'),
    ('return type(value) is int and minimum <= value <= maximum', 'return True'),
    ('set(value) != _COMMON | _VARIANTS[tag]', 'False'),
    ('if key in result:', 'if False:'),
    ('value["claim_scope"] != value["recipe_id"]', 'False'),
    ('value["outcome"] != "unknown" or not all', 'False or not all'),
    ('tag == "dispatch-unknown" and count is not None', 'False'),
    ('any(a >= b for a, b in zip(custody, custody[1:]))', 'False'),
    ('end - start > 120_000', 'False'),
    ('if elements > 16:', 'if False:'),
    ('if members > 40:', 'if False:'),
])
def test_guard_mutations_are_detected(tmp_path, old, new):
    """A disabled guard must make the corresponding negative suite fail."""
    import os
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    source = (root / 'sovite/collection_evidence_contract.py').read_text()
    assert source.count(old) == 1
    (tmp_path / 'sovite').mkdir()
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'sovite/__init__.py').write_text('')
    (tmp_path / 'sovite/collection_evidence_contract.py').write_text(source.replace(old, new, 1))
    (tmp_path / 'tests/test_collection_evidence_contract.py').write_text(Path(__file__).read_text())
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '-q', '-x', 'tests/test_collection_evidence_contract.py',
         '-k', 'not guard_mutations and not no_production_import_or_route'],
        cwd=tmp_path, env={**os.environ, 'PYTHONPATH': str(tmp_path), 'PYTHONDONTWRITEBYTECODE': '1'},
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 1 and 'failed' in result.stdout, result.stdout + result.stderr
