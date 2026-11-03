"""Pure validation and canonical encoding for dormant Work projection v1.

This module validates representation and integrity only. A valid digest is not
source authentication, authorization, or evidence that any ledger row exists.
It has no database, provider, network, credential, or dispatch dependencies.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping


SCHEMA_VERSION = "sovite-work-projection/v1"
DIGEST_DOMAIN = b"SOVITE:WORK-PROJECTION:v1\x00"
MAX_I64 = (1 << 63) - 1
MAX_AUDIT_SEQUENCE = (1 << 31) - 1

_FIELDS = frozenset({
    "schema_version", "scope_id", "work_job_id", "subject_id", "attempt_id",
    "call_id", "slot_id", "effect_key", "receipt_id", "evidence_id", "purpose",
    "lease_digest", "request_digest", "policy_digest", "receipt_digest",
    "allowance_unit", "policy_revision", "period_start_ms", "period_end_ms",
    "projection_revision", "reserved_usd_micro", "reserved_allowance_micro",
    "audit_sequence", "actual_usd_micro", "actual_allowance_micro", "intent_state",
    "reservation_state", "slot_state",
})
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,95}\Z", re.ASCII)
_UINT_RE = re.compile(r"(?:0|[1-9][0-9]*)\Z", re.ASCII)
_HEX256_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_REQUIRED_IDENTIFIERS = (
    "scope_id", "work_job_id", "subject_id", "attempt_id", "call_id", "slot_id", "effect_key",
)
_NUMERIC_FIELDS = (
    "policy_revision", "period_start_ms", "period_end_ms", "projection_revision",
    "reserved_usd_micro", "reserved_allowance_micro",
)


class WorkProjectionError(ValueError):
    """The supplied projection is malformed or inconsistent."""


@dataclass(frozen=True, slots=True)
class WorkProjectionV1:
    schema_version: str
    scope_id: str
    work_job_id: str
    subject_id: str
    attempt_id: str
    call_id: str
    slot_id: str
    effect_key: str
    receipt_id: str | None
    evidence_id: str | None
    purpose: str
    lease_digest: str
    request_digest: str
    policy_digest: str
    receipt_digest: str | None
    allowance_unit: str
    policy_revision: str
    period_start_ms: str
    period_end_ms: str
    projection_revision: str
    reserved_usd_micro: str
    reserved_allowance_micro: str
    audit_sequence: str
    actual_usd_micro: str | None
    actual_allowance_micro: str | None
    intent_state: str
    reservation_state: str
    slot_state: str

    def to_payload(self) -> dict[str, str | None]:
        """Return the complete closed JSON payload, including explicit nulls."""
        return asdict(self)


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WorkProjectionError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise WorkProjectionError("non-JSON numeric constant")


def _parse_uint(value: Any, field: str, *, positive: bool = False, maximum: int = MAX_I64) -> int:
    if type(value) is not str or _UINT_RE.fullmatch(value) is None:
        raise WorkProjectionError(f"invalid canonical integer field: {field}")
    if len(value) > len(str(maximum)):
        raise WorkProjectionError(f"integer field out of range: {field}")
    parsed = int(value)
    if parsed > maximum or (positive and parsed == 0):
        raise WorkProjectionError(f"integer field out of range: {field}")
    return parsed


def validate_payload(payload: Mapping[str, Any]) -> WorkProjectionV1:
    """Validate exact fields, value types, numeric grammar, identity and states."""
    if not isinstance(payload, Mapping):
        raise WorkProjectionError("projection must be an object")
    keys = set(payload)
    if keys != _FIELDS:
        missing, extra = _FIELDS - keys, keys - _FIELDS
        details = []
        if missing:
            details.append("missing fields")
        if extra:
            details.append("unknown fields")
        raise WorkProjectionError("projection has " + " and ".join(details))

    values = dict(payload)
    if values["schema_version"] != SCHEMA_VERSION or type(values["schema_version"]) is not str:
        raise WorkProjectionError("unsupported projection schema version")
    for name in _REQUIRED_IDENTIFIERS:
        value = values[name]
        if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
            raise WorkProjectionError(f"invalid identifier field: {name}")
    if values["subject_id"] != values["work_job_id"]:
        raise WorkProjectionError("subject_id must equal work_job_id")

    for name in ("receipt_id", "evidence_id"):
        value = values[name]
        if value is not None and (type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None):
            raise WorkProjectionError(f"invalid nullable identifier field: {name}")
    if type(values["purpose"]) is not str or values["purpose"] != "work":
        raise WorkProjectionError("purpose must be work")
    if type(values["allowance_unit"]) is not str or _IDENTIFIER_RE.fullmatch(values["allowance_unit"]) is None:
        raise WorkProjectionError("invalid allowance_unit")

    for name in ("lease_digest", "request_digest", "policy_digest"):
        if type(values[name]) is not str or _HEX256_RE.fullmatch(values[name]) is None:
            raise WorkProjectionError(f"invalid SHA-256 field: {name}")
    for name in ("receipt_digest",):
        value = values[name]
        if value is not None and (type(value) is not str or _HEX256_RE.fullmatch(value) is None):
            raise WorkProjectionError(f"invalid SHA-256 field: {name}")

    parsed_numeric = {
        name: _parse_uint(values[name], name, positive=name in {"policy_revision", "projection_revision"})
        for name in _NUMERIC_FIELDS
    }
    if parsed_numeric["period_end_ms"] <= parsed_numeric["period_start_ms"]:
        raise WorkProjectionError("period_end_ms must exceed period_start_ms")
    _parse_uint(values["audit_sequence"], "audit_sequence", positive=True, maximum=MAX_AUDIT_SEQUENCE)
    actuals = (values["actual_usd_micro"], values["actual_allowance_micro"])
    for name, value in zip(("actual_usd_micro", "actual_allowance_micro"), actuals):
        if value is not None:
            _parse_uint(value, name)
    if (actuals[0] is None) != (actuals[1] is None):
        raise WorkProjectionError("actual amounts must both be null or both be present")

    receipt_id, receipt_digest, evidence_id = (
        values["receipt_id"], values["receipt_digest"], values["evidence_id"]
    )
    if (receipt_id is None) != (receipt_digest is None):
        raise WorkProjectionError("receipt_id and receipt_digest must be paired")
    if evidence_id is not None:
        raise WorkProjectionError("overflow evidence is not representable in Work projection v1")

    state = (values["intent_state"], values["reservation_state"], values["slot_state"])
    if state == ("committed", "reserved", "held"):
        if actuals[0] is not None or receipt_id is not None or evidence_id is not None:
            raise WorkProjectionError("committed reservation cannot contain actuals or receipt")
    elif state == ("known", "settled", "released"):
        if actuals[0] is None or receipt_id is None:
            raise WorkProjectionError("settled state requires actuals and a receipt")
        if (_parse_uint(actuals[0], "actual_usd_micro") > _parse_uint(values["reserved_usd_micro"], "reserved_usd_micro")
                or _parse_uint(actuals[1], "actual_allowance_micro") >
                _parse_uint(values["reserved_allowance_micro"], "reserved_allowance_micro")):
            raise WorkProjectionError("settled actual exceeds reservation")
    elif state == ("unknown", "unknown", "unknown"):
        if actuals[0] is not None and receipt_id is None:
            raise WorkProjectionError("known actuals in unknown state require a receipt")
        if actuals[0] is None and evidence_id is not None:
            raise WorkProjectionError("unknown-outcome receipt cannot have overflow evidence")
    else:
        raise WorkProjectionError("unsupported obligation state combination")

    return WorkProjectionV1(**values)


def parse_raw_json(raw: str | bytes | bytearray) -> WorkProjectionV1:
    """Parse untrusted raw JSON while rejecting duplicate keys at every depth."""
    if isinstance(raw, (bytes, bytearray)):
        try:
            text = bytes(raw).decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise WorkProjectionError("projection JSON must be UTF-8") from error
    elif type(raw) is str:
        text = raw
    else:
        raise WorkProjectionError("projection JSON must be text or UTF-8 bytes")
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=_reject_json_constant,
        )
    except WorkProjectionError:
        raise
    except (json.JSONDecodeError, UnicodeError, TypeError, ValueError, RecursionError) as error:
        raise WorkProjectionError("invalid projection JSON") from error
    return validate_payload(parsed)


def canonical_bytes(payload: WorkProjectionV1 | Mapping[str, Any]) -> bytes:
    """Serialize a validated payload using the v1 restricted JCS-compatible form."""
    projection = payload if isinstance(payload, WorkProjectionV1) else validate_payload(payload)
    checked = validate_payload(projection.to_payload())
    encoded = json.dumps(
        checked.to_payload(), ensure_ascii=False, allow_nan=False,
        separators=(",", ":"), sort_keys=True,
    )
    return encoded.encode("utf-8")


def projection_digest(payload: WorkProjectionV1 | Mapping[str, Any]) -> str:
    """Return the lowercase domain-separated SHA-256 integrity digest."""
    return hashlib.sha256(DIGEST_DOMAIN + canonical_bytes(payload)).hexdigest()


def verify_digest(payload: WorkProjectionV1 | Mapping[str, Any], expected: str) -> WorkProjectionV1:
    """Validate and compare a stored digest; this does not authenticate source data."""
    projection = payload if isinstance(payload, WorkProjectionV1) else validate_payload(payload)
    if type(expected) is not str or _HEX256_RE.fullmatch(expected) is None:
        raise WorkProjectionError("invalid projection digest")
    if not hmac.compare_digest(projection_digest(projection), expected):
        raise WorkProjectionError("projection digest mismatch")
    return projection
