"""Pure syntax and content identity for collection evidence receipts.

Parsing does not authenticate observations, prove custody, or grant authority.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

SCHEMA = "sovite.collection-recipe-receipt/v1"
DOMAIN = SCHEMA.encode("ascii") + b"\0"
MAX_BYTES = 16_384
MAX_INTEGER = 2**53 - 1
RECIPES = frozenset({
    "per-attempt-isolation", "credential-containment", "exact-binding",
    "durable-checkpoint-and-candidate", "predecessor-reconciliation", "bounded-usage",
    "native-rpc-routing", "native-supervisor-containment", "native-config-isolation",
    "vm-process-lifecycle", "candidate-quiescence-import",
})
_COMMON = frozenset({
    "schema_version", "evidence_kind", "recipe_id", "recipe_version", "manifest_sha256",
    "fixture_sha256", "build_sha256", "session_digest", "deployment_sha256", "started_ms",
    "ended_ms", "uncertainty_ms", "outcome", "positive_observation_sha256",
    "control_observation_sha256", "custody_sha256", "provider_request_count", "claim_scope",
})
_SUBJECT = frozenset({"session_id", "epoch", "epoch_purpose", "operation_id", "resource_sha256"})
_VARIANTS = {
    "guest-effect": _SUBJECT | {"intent_sha256", "ack_sha256", "quiescence_sha256", "import_sha256"},
    "pre-dispatch-denial": _SUBJECT | {"no_dispatch_observation_sha256"},
    "dispatch-unknown": _SUBJECT | {"intent_sha256", "possible_dispatch_observation_sha256"},
    "isolated-ledger": frozenset({"test_environment_sha256", "db_audit_sha256",
                                   "zero_provider_observation_sha256", "installed_gate_observation_sha256"}),
}
_DIGEST = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z", re.ASCII)


class InvalidReceipt(ValueError):
    """A bounded receipt is not structurally valid."""


@dataclass(frozen=True)
class ReceiptIdentity:
    """Canonical content identity only, never a verification result."""
    canonical_bytes: bytes
    sha256: str


def _reject(*_args):
    raise InvalidReceipt("invalid receipt")


def _preflight(raw: bytes) -> None:
    # Bound nesting before invoking the JSON decoder, including malformed inputs.
    stack = []
    elements = 0
    quoted = escaped = False
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
            continue
        if stack and stack[-1][0] == 91 and stack[-1][1] and byte not in (9, 10, 13, 32, 93):
            elements += 1
            if elements > 16:
                _reject()
            stack[-1][1] = False
        if byte == 34:
            quoted = True
        elif byte in (91, 123):
            stack.append([byte, byte == 91])
            if len(stack) > 4:
                _reject()
        elif byte in (93, 125):
            if not stack:
                _reject()
            stack.pop()
        elif byte == 44 and stack and stack[-1][0] == 91:
            stack[-1][1] = True
    if quoted or stack:
        _reject()


def _decode(raw: bytes):
    if type(raw) is not bytes or not 1 <= len(raw) <= MAX_BYTES or raw.startswith(b"\xef\xbb\xbf"):
        _reject()
    _preflight(raw)
    members = 0

    def pairs(items):
        nonlocal members
        members += len(items)
        if members > 40:
            _reject()
        result = {}
        for key, value in items:
            if key in result:
                _reject()
            result[key] = value
        return result

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                          parse_float=_reject, parse_constant=_reject)
    except (ValueError, UnicodeError, RecursionError):
        _reject()


def _digest(value):
    return type(value) is str and _DIGEST.fullmatch(value) is not None


def _integer(value, minimum=0, maximum=MAX_INTEGER):
    return type(value) is int and minimum <= value <= maximum


def parse_receipt(raw: bytes) -> ReceiptIdentity:
    """Validate one receipt and return canonical bytes and domain-separated digest.

    This function checks structure alone, including claimed no-dispatch and
    evidence references; it does not resolve or verify any referenced evidence.
    """
    value = _decode(raw)
    if type(value) is not dict:
        _reject()
    tag = value.get("evidence_kind")
    if type(tag) is not str or tag not in _VARIANTS or set(value) != _COMMON | _VARIANTS[tag]:
        _reject()
    if (value["schema_version"] != SCHEMA or value["recipe_version"] != "1"
            or type(value["recipe_id"]) is not str or value["recipe_id"] not in RECIPES
            or value["claim_scope"] != value["recipe_id"]
            or value["outcome"] not in ("pass", "fail", "unknown")):
        _reject()
    if not all(_digest(value[k]) for k in ("manifest_sha256", "fixture_sha256", "build_sha256",
                                            "session_digest", "deployment_sha256")):
        _reject()
    for key in ("positive_observation_sha256", "control_observation_sha256"):
        if not _digest(value[key]) and not (tag == "dispatch-unknown" and value[key] is None):
            _reject()
    start, end, uncertainty = value["started_ms"], value["ended_ms"], value["uncertainty_ms"]
    if (not _integer(start, 1) or not _integer(end, 1) or not _integer(uncertainty, 0, 2000)
            or end < start or end - start > 120_000):
        _reject()
    count = value["provider_request_count"]
    if (tag == "dispatch-unknown" and count is not None) or (tag != "dispatch-unknown" and not _integer(count, 0, 0)):
        _reject()
    custody = value["custody_sha256"]
    if (type(custody) is not list or not 1 <= len(custody) <= 16
            or not all(_digest(item) for item in custody)
            or any(a >= b for a, b in zip(custody, custody[1:]))):
        _reject()
    if tag != "isolated-ledger":
        if (any(type(value[k]) is not str or not _ID.fullmatch(value[k]) for k in ("session_id", "operation_id"))
                or not _integer(value["epoch"], 1) or value["epoch_purpose"] not in ("execution", "recovery")
                or not _digest(value["resource_sha256"])):
            _reject()
    if tag == "guest-effect":
        if not all(_digest(value[k]) for k in ("intent_sha256", "ack_sha256")):
            _reject()
        for key in ("quiescence_sha256", "import_sha256"):
            if value["recipe_id"] == "candidate-quiescence-import":
                if not _digest(value[key]):
                    _reject()
            elif value[key] is not None:
                _reject()
    elif tag == "pre-dispatch-denial":
        if not _digest(value["no_dispatch_observation_sha256"]):
            _reject()
    elif tag == "dispatch-unknown":
        if value["outcome"] != "unknown" or not all(_digest(value[k]) for k in ("intent_sha256", "possible_dispatch_observation_sha256")):
            _reject()
    elif value["recipe_id"] != "bounded-usage" or not all(_digest(value[k]) for k in _VARIANTS[tag]):
        _reject()
    canonical = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    return ReceiptIdentity(canonical, hashlib.sha256(DOMAIN + canonical).hexdigest())
