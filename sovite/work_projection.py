"""Pure validation and canonical encoding for dormant Work projection v1.

The pure validator provides representation checks only. The private comparator
reads synthetic owner rows; it supplies no authorization or source verification.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping


SCHEMA_VERSION = "sovite-work-projection/v1"
DIGEST_DOMAIN = b"SOVITE:WORK-PROJECTION:v1\x00"
MAX_I64 = (1 << 63) - 1
MAX_AUDIT_SEQUENCE = (1 << 31) - 1
MAX_JOB_JSON_BYTES = 1048576
MAX_JOB_JSON_DEPTH = 32
MAX_JOB_JSON_NODES = 20000

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


class _WorkProjectionUnavailable(RuntimeError):
    """A private, redacted refusal to compare incomplete or untrusted source rows."""

    def __init__(self):
        super().__init__("Work projection is unavailable for canonical-row comparison")


class _WorkProjectionBusy(_WorkProjectionUnavailable):
    """A recovery candidate's exact Job or lease row was skipped as locked."""


_CANONICAL_ROWS_AGREE = "canonical_rows_agree"
_BINDING_TABLE = "program_work_projection_bindings"
_BINDING_COLUMNS = frozenset({
    "scope_id", "call_id", "subject_id", "work_job_id", "attempt_id", "slot_id",
    "purpose", "policy_revision", "policy_digest", "projection_revision",
    "projection_digest", "audit_sequence",
})


def _projection_id(value: Any) -> bool:
    return type(value) is str and _IDENTIFIER_RE.fullmatch(value) is not None


def _db_int(value: Any, *, positive: bool = False, maximum: int = MAX_I64) -> bool:
    return (type(value) is int and 0 <= value <= maximum
            and (not positive or value > 0))


def _raw_job_object(raw: Any) -> dict[str, Any]:
    if type(raw) is not str:
        raise WorkProjectionError("stored Job JSON is not raw text")
    if len(raw.encode("utf-8")) > MAX_JOB_JSON_BYTES:
        raise WorkProjectionError("stored Job JSON exceeds byte bound")
    depth = 0
    in_string = escaped = False
    for char in raw:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > MAX_JOB_JSON_DEPTH:
                raise WorkProjectionError("stored Job JSON exceeds depth bound")
        elif char in "]}":
            depth -= 1
            if depth < 0:
                raise WorkProjectionError("stored Job JSON is invalid")
    try:
        value = json.loads(raw, object_pairs_hook=_duplicate_rejecting_object,
                           parse_constant=_reject_json_constant)
    except WorkProjectionError:
        raise
    except (json.JSONDecodeError, UnicodeError, TypeError, ValueError, RecursionError) as error:
        raise WorkProjectionError("stored Job JSON is invalid") from error
    if type(value) is not dict:
        raise WorkProjectionError("stored Job JSON is not an object")
    pending = [value]
    count = 0
    while pending:
        item = pending.pop()
        count += 1
        if count > MAX_JOB_JSON_NODES:
            raise WorkProjectionError("stored Job JSON exceeds node bound")
        if type(item) is dict:
            pending.extend(item.values())
        elif type(item) is list:
            pending.extend(item)
    return value


def _projection_from_job(raw: Any, *, call_id: str) -> WorkProjectionV1:
    selected = [item for item in work_projections_from_job(raw) if item.call_id == call_id]
    if len(selected) != 1:
        raise WorkProjectionError("stored Work projection call is absent or duplicated")
    return selected[0]


def work_projections_from_job(raw: Any, *, allow_absent: bool = False) -> tuple[WorkProjectionV1, ...]:
    """Parse the complete bounded projection array from stored Job JSON."""
    root = _raw_job_object(raw)
    run = root.get("repository_run")
    has_array = type(run) is dict and "program_ledger_projections_v1" in run
    entries = run.get("program_ledger_projections_v1") if has_array else None
    if not has_array and allow_absent:
        return ()
    if type(entries) is not list:
        raise WorkProjectionError("stored Work projection array is absent")
    projections: list[WorkProjectionV1] = []
    previous: bytes | None = None
    seen: set[str] = set()
    for entry in entries:
        projection = validate_payload(entry)
        encoded_call = projection.call_id.encode("utf-8")
        if projection.call_id in seen or (previous is not None and encoded_call <= previous):
            raise WorkProjectionError("stored Work projection calls are not sorted and unique")
        seen.add(projection.call_id)
        previous = encoded_call
        projections.append(projection)
    return tuple(projections)


def lock_work_projection_set(session, projections: tuple[WorkProjectionV1, ...], *,
                             additional_work_calls=(), legacy_batch_keys=(),
                             expected_job_id=None, include_bindings=True,
                             nonblocking_owner_ids=()) -> None:
    """Prelock a multi-call Work projection set by global row class order.

    Per-call comparison below may repeat SELECT FOR UPDATE queries, but those
    rows are already locked for the full set. This prevents acquiring a later
    scope after an earlier call has reached Job, lease, or ledger lock classes.
    """
    from sqlalchemy import and_, or_, select

    from .collection_transaction import _owner_transaction_is_active
    from .db import Base

    if not _owner_transaction_is_active(session):
        raise _WorkProjectionUnavailable()
    tables = Base.metadata.tables
    additional_work_calls = tuple(additional_work_calls)
    legacy_batch_keys = tuple(legacy_batch_keys)
    nonblocking_owner_ids = frozenset(nonblocking_owner_ids)
    if (any(type(value) is not str or not value for value in nonblocking_owner_ids)
            or nonblocking_owner_ids and len(nonblocking_owner_ids) != 1):
        raise _WorkProjectionUnavailable()
    if (not projections and not additional_work_calls and not legacy_batch_keys
            and not nonblocking_owner_ids):
        return
    if any(len(item) != 6 or item[4] != "work" or
           any(type(item[i]) is not str or not item[i] for i in (0, 1, 2, 3, 5))
           for item in additional_work_calls):
        raise _WorkProjectionUnavailable()
    if any(len(item) != 2 or any(type(value) is not str or not value for value in item)
           for item in legacy_batch_keys):
        raise _WorkProjectionUnavailable()
    scope_ids = sorted({item.scope_id for item in projections} |
                       {item[0] for item in additional_work_calls} |
                       {item[0] for item in legacy_batch_keys}, key=lambda value: value.encode("utf-8"))
    calls = sorted({(item.scope_id, item.call_id) for item in projections} |
                   {(item[0], item[1]) for item in additional_work_calls},
                   key=lambda key: (key[0].encode("utf-8"), key[1].encode("utf-8")))
    policies = {(item.scope_id, int(item.policy_revision)) for item in projections}

    # The existing comparator obtains policy and Job selectors from the
    # canonical intent, not from the projection. Discover those candidate
    # selectors before taking any class lock so even a malformed join cannot
    # ask the comparator to acquire an earlier class after a later one.
    intent_t = tables["program_call_intents"]
    candidates = {}
    expected_additional = {(item[0], item[1]): item for item in additional_work_calls}
    for scope_id, call_id in calls:
        candidates[(scope_id, call_id)] = session.execute(select(intent_t).where(
                intent_t.c.scope_id == scope_id,
                intent_t.c.call_id == call_id)).mappings().all()
    for (scope_id, _), rows in candidates.items():
        for row in rows:
            selector = expected_additional.get((scope_id, _))
            if selector is not None and (row["work_job_id"], row["subject_id"], row["purpose"]) != (selector[5], selector[3], selector[4]):
                raise _WorkProjectionUnavailable()
            if expected_job_id is not None and (row["work_job_id"], row["subject_id"], row["purpose"]) != (expected_job_id, expected_job_id, "work"):
                raise _WorkProjectionUnavailable()
            if expected_job_id is not None and type(row["policy_revision"]) is not int:
                raise _WorkProjectionUnavailable()
            if type(row["policy_revision"]) is int:
                policies.add((scope_id, row["policy_revision"]))

    if nonblocking_owner_ids:
        discovered_job_ids = {item.work_job_id for item in projections}
        discovered_job_ids.update(item[5] for item in additional_work_calls)
        for rows in candidates.values():
            discovered_job_ids.update(row["work_job_id"] for row in rows
                                      if type(row["work_job_id"]) is str)
        if discovered_job_ids - nonblocking_owner_ids:
            raise _WorkProjectionUnavailable()

    batch_t = tables.get("program_census_batches")
    batch_discovered, batch_links_discovered = {}, {}
    if legacy_batch_keys:
        if batch_t is None:
            raise _WorkProjectionUnavailable()
        link_t = tables.get("program_legacy_links")
        if link_t is None:
            raise _WorkProjectionUnavailable()
        for key in sorted(set(legacy_batch_keys), key=lambda value: tuple(v.encode("utf-8") for v in value)):
            batch = session.execute(select(batch_t).where(
                batch_t.c.scope_id == key[0], batch_t.c.batch_id == key[1])).mappings().one_or_none()
            if batch is None or type(batch["policy_revision"]) is not int:
                raise _WorkProjectionUnavailable()
            batch_discovered[key] = batch
            policies.add((key[0], batch["policy_revision"]))
            links = session.execute(select(link_t).where(
                link_t.c.scope_id == key[0], link_t.c.batch_id == key[1])).mappings().all()
            links.sort(key=lambda row: row["link_id"].encode("utf-8") if type(row["link_id"]) is str else b"\xff")
            batch_links_discovered[key] = links

    def key_order(key):
        return tuple(value.encode("utf-8") if type(value) is str else value for value in key)

    def lock_keys(table, columns, keys, *, skip_locked=False):
        locked = {}
        for key in sorted(set(keys), key=key_order):
            predicate = and_(*(column == value for column, value in zip(columns, key)))
            rows = session.execute(select(table).where(predicate).with_for_update(
                skip_locked=skip_locked)).mappings().all()
            if len(rows) > 1:
                raise _WorkProjectionUnavailable()
            if skip_locked and not rows:
                present = session.execute(select(columns[0]).where(predicate)).first()
                if present is not None:
                    raise _WorkProjectionBusy()
            locked[key] = rows[0] if rows else None
        return locked

    scope_t = tables["program_budget_scopes"]
    gate_t = tables["program_gate_state"]
    policy_t = tables["program_policy_revisions"]
    registry_t = tables["program_subject_registry"]
    subject_t = tables["program_budget_subjects"]
    job_t = tables["jobs"]
    lease_t = tables["worker_execution_leases"]
    reservation_t = tables["program_reservations"]
    slot_t = tables["program_slots"]
    receipt_t = tables["program_call_receipts"]
    evidence_t = tables["program_overflow_evidence"]
    binding_t = tables.get("program_work_projection_bindings")
    if include_bindings and binding_t is None:
        raise _WorkProjectionUnavailable()
    if not include_bindings and projections:
        raise _WorkProjectionUnavailable()

    lock_keys(scope_t, (scope_t.c.id,), ((scope_id,) for scope_id in scope_ids))
    gate_rows = lock_keys(gate_t, (gate_t.c.scope_id,),
                          ((scope_id,) for scope_id in scope_ids))
    policies.update((row["scope_id"], row["policy_revision"]) for row in gate_rows.values()
                    if row is not None and type(row["policy_revision"]) is int)
    lock_keys(policy_t, (policy_t.c.scope_id, policy_t.c.revision), policies)
    if batch_t is not None:
        locked_batches = lock_keys(batch_t, (batch_t.c.scope_id, batch_t.c.batch_id), legacy_batch_keys)
        if any(locked_batches[key] != batch_discovered.get(key) for key in legacy_batch_keys):
            raise _WorkProjectionUnavailable()
    registry_keys = {(item.scope_id, item.subject_id, item.purpose) for item in projections}
    subject_keys = {(item.scope_id, item.subject_id) for item in projections}
    job_ids = {item.work_job_id for item in projections}
    registry_keys.update((item[0], item[3], "work") for item in additional_work_calls)
    subject_keys.update((item[0], item[3]) for item in additional_work_calls)
    job_ids.update(item[5] for item in additional_work_calls)
    for (scope_id, _), rows in candidates.items():
        for row in rows:
            canonical_job = row["work_job_id"]
            if type(canonical_job) is str:
                # The comparator reaches these rows only after validating a
                # Work purpose and a bounded Job identifier. Prelocking an
                # unexpected but valid selector preserves class order; its
                # later identity comparison still rejects the mismatched join.
                registry_keys.add((scope_id, canonical_job, "work"))
                subject_keys.add((scope_id, canonical_job))
                job_ids.add(canonical_job)
    job_ids.update(nonblocking_owner_ids)
    lock_keys(registry_t, (registry_t.c.scope_id, registry_t.c.subject_id, registry_t.c.purpose),
              registry_keys)
    lock_keys(subject_t, (subject_t.c.scope_id, subject_t.c.subject_id), subject_keys)

    job_ids = sorted(job_ids, key=lambda value: value.encode("utf-8"))
    owner_job_ids = set(job_ids) & nonblocking_owner_ids
    if nonblocking_owner_ids and set(job_ids) != nonblocking_owner_ids:
        # Recovery's nonblocking mode owns exactly its candidate Job. Any
        # wider owner selector would violate the one-candidate lock boundary.
        raise _WorkProjectionUnavailable()
    locked_jobs = lock_keys(job_t, (job_t.c.id,), ((job_id,) for job_id in job_ids),
                            skip_locked=bool(owner_job_ids))
    if owner_job_ids and any(locked_jobs.get((job_id,)) is None for job_id in owner_job_ids):
        raise _WorkProjectionUnavailable()
    locked_leases = lock_keys(lease_t, (lease_t.c.job_id,), ((job_id,) for job_id in job_ids),
                              skip_locked=bool(owner_job_ids))
    if owner_job_ids and any(locked_leases.get((job_id,)) is None for job_id in owner_job_ids):
        raise _WorkProjectionUnavailable()

    lock_keys(reservation_t, (reservation_t.c.scope_id, reservation_t.c.call_id), calls)

    # Slots can be selected by either call or slot identity. Discover their
    # complete key set, lock exact rows in UTF-8 order, then detect predicate
    # drift before proceeding to the next class.
    slot_matches = {}
    slot_keys = set()
    for scope_id, call_id in calls:
        projected = next((item.slot_id for item in projections
                          if item.scope_id == scope_id and item.call_id == call_id), None)
        extra = expected_additional.get((scope_id, call_id))
        slot_ids = {value for value in (projected, extra[2] if extra else None) if value is not None}
        if len(slot_ids) != 1:
            raise _WorkProjectionUnavailable()
        slot_id = next(iter(slot_ids))
        matching = session.execute(select(slot_t.c.scope_id, slot_t.c.slot_id).where(
            slot_t.c.scope_id == scope_id,
            or_(slot_t.c.call_id == call_id, slot_t.c.slot_id == slot_id),
        )).all()
        keys = {(row.scope_id, row.slot_id) for row in matching}
        slot_matches[(scope_id, call_id)] = (slot_id, keys)
        slot_keys.update(keys)
    lock_keys(slot_t, (slot_t.c.scope_id, slot_t.c.slot_id), slot_keys)
    for (scope_id, call_id), (slot_id, keys) in slot_matches.items():
        reread = session.execute(select(slot_t.c.scope_id, slot_t.c.slot_id).where(
            slot_t.c.scope_id == scope_id,
            or_(slot_t.c.call_id == call_id, slot_t.c.slot_id == slot_id),
        )).all()
        if {(row.scope_id, row.slot_id) for row in reread} != keys:
            raise _WorkProjectionUnavailable()

    locked_intents = lock_keys(intent_t, (intent_t.c.scope_id, intent_t.c.call_id), calls)
    for key, expected in candidates.items():
        actual = locked_intents.get(key)
        rows = expected
        if len(rows) > 1 or bool(rows) != (actual is not None):
            raise _WorkProjectionUnavailable()
        if rows and dict(actual) != dict(rows[0]):
            raise _WorkProjectionUnavailable()
        selector = expected_additional.get(key)
        if selector is not None and actual is not None and (
                actual["work_job_id"], actual["subject_id"], actual["purpose"]) != (
                    selector[5], selector[3], selector[4]):
            raise _WorkProjectionUnavailable()

    # Optional rows are keyed from the call predicate so all existing rows are
    # locked in exact key order; their cardinality and contents are validated
    # by the canonical comparator below.
    receipt_keys = set()
    evidence_keys = set()
    for scope_id, call_id in calls:
        receipt_keys.update((row.scope_id, row.call_id, row.receipt_id) for row in session.execute(
            select(receipt_t.c.scope_id, receipt_t.c.call_id, receipt_t.c.receipt_id).where(
                receipt_t.c.scope_id == scope_id, receipt_t.c.call_id == call_id)).all())
        evidence_keys.update((row.scope_id, row.evidence_id) for row in session.execute(
            select(evidence_t.c.scope_id, evidence_t.c.evidence_id).where(
                evidence_t.c.scope_id == scope_id, evidence_t.c.call_id == call_id)).all())
    lock_keys(receipt_t, (receipt_t.c.scope_id, receipt_t.c.call_id, receipt_t.c.receipt_id), receipt_keys)
    lock_keys(evidence_t, (evidence_t.c.scope_id, evidence_t.c.evidence_id), evidence_keys)
    if include_bindings:
        lock_keys(binding_t, (binding_t.c.scope_id, binding_t.c.call_id), calls)
    if legacy_batch_keys:
        link_t = tables["program_legacy_links"]
        locked_link_keys = set()
        for key, links in batch_links_discovered.items():
            locked_link_keys.update((key[0], key[1], row["link_id"]) for row in links)
        locked_links = {}
        for key in sorted(locked_link_keys, key=lambda value: tuple(v.encode("utf-8") for v in value)):
            rows = session.execute(select(link_t).where(
                link_t.c.scope_id == key[0], link_t.c.batch_id == key[1],
                link_t.c.link_id == key[2]).with_for_update()).mappings().all()
            if len(rows) != 1:
                raise _WorkProjectionUnavailable()
            locked_links[key] = rows[0]
        for batch_key, discovered in batch_links_discovered.items():
            reread = session.execute(select(link_t).where(
                link_t.c.scope_id == batch_key[0], link_t.c.batch_id == batch_key[1])).mappings().all()
            reread.sort(key=lambda row: row["link_id"].encode("utf-8") if type(row["link_id"]) is str else b"\xff")
            if reread != discovered or any(locked_links.get((batch_key[0], batch_key[1], row["link_id"])) != row
                                           for row in discovered):
                raise _WorkProjectionUnavailable()


def _compare_actual_rows(session, *, scope_id: str, call_id: str,
                         allow_cancel_requested: bool = False,
                         allow_interrupted_attempt: bool = False,
                         allow_preentry_cancelled_attempt: bool = False,
                         finish_attempt_states: frozenset[str] | None = None) -> str:
    """Implementation for the private owner-transaction-only comparator."""
    from sqlalchemy import MetaData, Table, and_, cast, func, inspect, or_, select, Text, LargeBinary

    from .collection_transaction import _owner_transaction_is_active
    from .db import Base, digest
    from .lease_service import execution_owner_key

    if not _owner_transaction_is_active(session):
        raise _WorkProjectionUnavailable()
    if (not _projection_id(scope_id) or not _projection_id(call_id)
            or session.get_bind().dialect.name not in {"sqlite", "postgresql"}):
        raise _WorkProjectionUnavailable()

    connection = session.connection()
    tables = Base.metadata.tables

    def known(name: str):
        table = tables.get(name)
        if table is None:
            raise _WorkProjectionUnavailable()
        return table

    scope_t = known("program_budget_scopes")
    gate_t = known("program_gate_state")
    policy_t = known("program_policy_revisions")
    registry_t = known("program_subject_registry")
    subject_t = known("program_budget_subjects")
    job_t = known("jobs")
    lease_t = known("worker_execution_leases")
    reservation_t = known("program_reservations")
    slot_t = known("program_slots")
    intent_t = known("program_call_intents")
    receipt_t = known("program_call_receipts")
    evidence_t = known("program_overflow_evidence")
    audit_t = known("audit_events")

    def locked_rows(table, *predicates, order=()):
        statement = select(table).where(*predicates)
        if order:
            statement = statement.order_by(*order)
        return session.execute(statement.with_for_update()).mappings().all()

    def one_locked(table, *predicates):
        rows = locked_rows(table, *predicates)
        if len(rows) != 1:
            raise _WorkProjectionUnavailable()
        return rows[0]

    def check_audit_sequences(*values):
        sequences = set(values)
        if any(not _db_int(value, positive=True, maximum=MAX_AUDIT_SEQUENCE) for value in sequences):
            raise _WorkProjectionUnavailable()
        found = set(session.execute(select(audit_t.c.sequence).where(
            audit_t.c.sequence.in_(sequences))).scalars())
        if found != sequences:
            raise _WorkProjectionUnavailable()

    # Owner lock order: scope, gate, immutable policy revisions, registry,
    # subject, all Job rows, all lease rows, reservation, slot, intent,
    # receipts, evidence, then the proposed projection binding. The owner
    # transaction already holds the audit-head serialization boundary.
    scope = one_locked(scope_t, scope_t.c.id == scope_id)
    gate = one_locked(gate_t, gate_t.c.scope_id == scope_id)
    if gate["mode"] not in {"disabled", "census", "active", "inhibited"}:
        raise _WorkProjectionUnavailable()

    # This is discovery only. The full intent is re-read after every earlier
    # lock class has been acquired.
    candidate_rows = session.execute(select(
        intent_t.c.scope_id, intent_t.c.call_id, intent_t.c.subject_id,
        intent_t.c.purpose, intent_t.c.work_job_id, intent_t.c.work_attempt_id,
        intent_t.c.work_lease_digest, intent_t.c.policy_revision,
        intent_t.c.policy_digest,
    ).where(intent_t.c.scope_id == scope_id, intent_t.c.call_id == call_id)).mappings().all()
    if len(candidate_rows) != 1:
        raise _WorkProjectionUnavailable()
    candidate = candidate_rows[0]
    job_id = candidate["work_job_id"]
    if (candidate["purpose"] != "work" or candidate["subject_id"] != job_id
            or not _projection_id(job_id) or not _projection_id(candidate["work_attempt_id"])
            or not _db_int(candidate["policy_revision"], positive=True)
            or type(candidate["policy_digest"]) is not str
            or _HEX256_RE.fullmatch(candidate["policy_digest"]) is None):
        raise _WorkProjectionUnavailable()
    gate_revision = gate["policy_revision"]
    gate_digest = gate["policy_digest"]
    if (not _db_int(gate_revision, positive=True) or type(gate_digest) is not str
            or _HEX256_RE.fullmatch(gate_digest) is None):
        raise _WorkProjectionUnavailable()
    policy_revisions = sorted({candidate["policy_revision"], gate_revision})
    policy_rows = session.execute(select(policy_t).where(
        policy_t.c.scope_id == scope_id,
        policy_t.c.revision.in_(policy_revisions),
    ).order_by(policy_t.c.revision).with_for_update()).mappings().all()
    policies = {row["revision"]: row for row in policy_rows}
    if (set(policies) != set(policy_revisions)
            or policies[gate_revision]["policy_digest"] != gate_digest
            or policies[candidate["policy_revision"]]["policy_digest"] != candidate["policy_digest"]):
        raise _WorkProjectionUnavailable()
    policy = policies[candidate["policy_revision"]]

    registry = one_locked(registry_t, registry_t.c.scope_id == scope_id,
                          registry_t.c.subject_id == job_id, registry_t.c.purpose == "work")
    subject = one_locked(subject_t, subject_t.c.scope_id == scope_id,
                         subject_t.c.subject_id == job_id)
    if (registry["scope_id"] != scope_id or registry["subject_id"] != job_id
            or registry["purpose"] != "work" or subject["purpose"] != "work"
            or registry["binding_digest"] != subject["binding_digest"]):
        raise _WorkProjectionUnavailable()

    # Lock and fetch only fixed internal tables. resolved is cast to raw text;
    # ORM JSON decoding would erase duplicate-key evidence. JSONB and unknown
    # storage types are intentionally unsupported.
    schema_map = session.get_bind().get_execution_options().get("schema_translate_map", {})
    binding_schema = schema_map.get(None) if isinstance(schema_map, Mapping) else None
    inspector = inspect(connection)
    job_columns = inspector.get_columns("jobs", schema=binding_schema)
    resolved_column = next((item for item in job_columns if item["name"] == "resolved"), None)
    storage_type = str(resolved_column["type"]).lower() if resolved_column else ""
    dialect = session.get_bind().dialect.name
    if ((dialect == "sqlite" and storage_type != "json")
            or (dialect == "postgresql" and storage_type != "json")):
        raise _WorkProjectionUnavailable()
    if dialect == "postgresql":
        encoding = session.execute(select(func.current_setting("client_encoding"))).scalar_one()
        if encoding.upper().replace("-", "") != "UTF8":
            raise _WorkProjectionUnavailable()
        size_expr = func.octet_length(cast(job_t.c.resolved, Text))
    else:
        if connection.exec_driver_sql("PRAGMA encoding").scalar_one().upper().replace("-", "") != "UTF8":
            raise _WorkProjectionUnavailable()
        size_expr = func.length(cast(job_t.c.resolved, LargeBinary))
    size = session.execute(select(size_expr).where(job_t.c.id == job_id).with_for_update()).scalar_one_or_none()
    if type(size) is not int or not 0 < size <= MAX_JOB_JSON_BYTES:
        raise _WorkProjectionUnavailable()
    job_columns_to_read = [job_t.c[name] for name in
                           ("id", "status", "cancel_requested", "generation", "lease_owner", "lease_until")]
    job_statement = select(*job_columns_to_read,
                            cast(job_t.c.resolved, Text).label("_resolved_raw")).where(
        job_t.c.id == job_id).with_for_update()
    job_rows = session.execute(job_statement).mappings().all()
    if len(job_rows) != 1:
        raise _WorkProjectionUnavailable()
    job = job_rows[0]
    lease = one_locked(lease_t, lease_t.c.job_id == job_id)

    try:
        projection = _projection_from_job(job["_resolved_raw"], call_id=call_id)
    except (WorkProjectionError, TypeError, ValueError):
        raise _WorkProjectionUnavailable() from None
    if projection.work_job_id != job_id or projection.scope_id != scope_id:
        raise _WorkProjectionUnavailable()

    reservation = one_locked(reservation_t, reservation_t.c.scope_id == scope_id,
                             reservation_t.c.call_id == call_id)
    slot_rows = locked_rows(slot_t, slot_t.c.scope_id == scope_id,
                            or_(slot_t.c.call_id == call_id,
                                slot_t.c.slot_id == projection.slot_id),
                            order=(slot_t.c.slot_id,))
    if len(slot_rows) != 1:
        raise _WorkProjectionUnavailable()
    slot = slot_rows[0]
    intent = one_locked(intent_t, intent_t.c.scope_id == scope_id,
                        intent_t.c.call_id == call_id)

    if (intent["scope_id"] != scope_id or intent["call_id"] != call_id
            or intent["purpose"] != "work" or intent["subject_id"] != job_id
            or intent["work_job_id"] != job_id
            or intent["work_attempt_id"] != projection.attempt_id
            or intent["work_lease_digest"] != projection.lease_digest
            or intent["effect_key"] != projection.effect_key
            or intent["request_digest"] != projection.request_digest
            or intent["policy_revision"] != int(projection.policy_revision)
            or intent["policy_digest"] != projection.policy_digest
            or intent["policy_revision"] != candidate["policy_revision"]
            or intent["policy_digest"] != candidate["policy_digest"]):
        raise _WorkProjectionUnavailable()

    # Check the Work attempt and currently stored lease fields as canonical
    # row agreement only. This is not a fence, authority, or source-freshness
    # assertion.
    lease_until = lease["lease_until"]
    deadline = lease["execution_deadline"]
    job_lease_until = job["lease_until"]
    if (job["status"] != "running"
            or bool(job["cancel_requested"]) and not allow_cancel_requested
            or lease["phase"] != "started" or lease["generation"] != job["generation"]
            or type(lease["workload_id"]) is not str or type(lease["instance_id"]) is not str
            or type(lease["generation"]) is not str or type(lease["fence"]) is not str
            or type(job_lease_until) not in (int, float) or not math.isfinite(job_lease_until)
            or type(lease_until) not in (int, float) or not math.isfinite(lease_until)
            or type(deadline) not in (int, float) or not math.isfinite(deadline)
            or job_lease_until != lease_until
            or job["lease_owner"] != execution_owner_key(
                workload_id=lease["workload_id"], instance_id=lease["instance_id"])
            or min(lease_until, deadline) * 1000 < int(intent["valid_until_ms"])):
        raise _WorkProjectionUnavailable()
    fence_digest = hashlib.sha256(lease["fence"].encode("utf-8")).hexdigest()
    run = _raw_job_object(job["_resolved_raw"]).get("repository_run")
    attempts = run.get("attempts") if type(run) is dict else None
    if type(attempts) is not list or not attempts:
        raise _WorkProjectionUnavailable()
    matching = [entry for entry in attempts
                if type(entry) is dict and entry.get("id") == projection.attempt_id]
    expected_lease_digest = digest({
        "job_id": job_id, "attempt_id": projection.attempt_id,
        "workload_id": lease["workload_id"], "instance_id": lease["instance_id"],
        "generation": lease["generation"], "fence_sha256": fence_digest,
    })
    attempt_state = matching[0].get("state") if len(matching) == 1 else None
    active_attempt_states = {"preparing", "running", "awaiting_activation"}
    hand_back_attempt_state = (
        allow_interrupted_attempt and attempt_state == "interrupted"
        or allow_preentry_cancelled_attempt and attempt_state in {"cancelled", "activation_cancelled"}
    )
    known_finish_states = {
        "completed", "ineligible", "interrupted", "cancelled",
        "activation_cancelled", "failed", "uncertain", "awaiting_activation",
    }
    known_finish_states = frozenset(known_finish_states)
    if (finish_attempt_states is not None
            and (type(finish_attempt_states) is not frozenset
                 or not finish_attempt_states
                 or not finish_attempt_states <= known_finish_states)):
        raise _WorkProjectionUnavailable()
    if (len(matching) != 1 or attempts[-1] is not matching[0]
            or not (attempt_state in finish_attempt_states if finish_attempt_states is not None
                    else attempt_state in active_attempt_states or hand_back_attempt_state)
            or matching[0].get("fence_sha256") != fence_digest
            or expected_lease_digest != projection.lease_digest
            or intent["work_lease_digest"] != expected_lease_digest):
        raise _WorkProjectionUnavailable()

    if (projection.subject_id != job_id or reservation["scope_id"] != scope_id
            or reservation["call_id"] != call_id or reservation["subject_id"] != job_id
            or reservation["purpose"] != "work" or reservation["effect_key"] != projection.effect_key
            or slot["scope_id"] != scope_id or slot["slot_id"] != projection.slot_id
            or slot["call_id"] != call_id or slot["subject_id"] != job_id
            or intent["subject_id"] != job_id):
        raise _WorkProjectionUnavailable()
    if (scope["policy_revision"] != gate_revision or scope["policy_digest"] != gate_digest
            or policy["scope_id"] != scope_id or policy["revision"] != int(projection.policy_revision)
            or policy["policy_digest"] != projection.policy_digest or policy["currency"] != "USD"
            or policy["allowance_unit"] != projection.allowance_unit
            or policy["window_start_ms"] != int(projection.period_start_ms)
            or policy["window_end_ms"] != int(projection.period_end_ms)
            or scope["id"] != scope_id):
        raise _WorkProjectionUnavailable()
    if (intent["price_digest"] != policy["price_policy_digest"]
            or intent["meter_digest"] != policy["meter_policy_digest"]
            or intent["permit_digest"] != policy["permit_policy_digest"]
            or intent["source_identity_digest"] != policy["source_identity_digest"]
            or intent["source_identity_digest"] != digest({
                "source_code": intent["source_code"], "issuer_code": intent["issuer_code"]})
            or reservation["price_digest"] != intent["price_digest"]
            or reservation["meter_digest"] != intent["meter_digest"]
            or reservation["permit_digest"] != intent["permit_digest"]
            or reservation["permit_expires_ms"] != intent["valid_until_ms"]
            or type(intent["observed_ms"]) is not int or type(intent["valid_until_ms"]) is not int
            or not policy["window_start_ms"] <= intent["observed_ms"] < policy["window_end_ms"]
            or intent["valid_until_ms"] > policy["window_end_ms"]):
        raise _WorkProjectionUnavailable()

    expected_digest = projection_digest(projection)
    try:
        receipts = locked_rows(receipt_t, receipt_t.c.scope_id == scope_id,
                               receipt_t.c.call_id == call_id,
                               order=(receipt_t.c.receipt_id,))
    except Exception:
        raise _WorkProjectionUnavailable() from None
    if len(receipts) > 1:
        raise _WorkProjectionUnavailable()
    receipt = receipts[0] if receipts else None
    if projection.receipt_id is None:
        if receipt is not None:
            raise _WorkProjectionUnavailable()
    else:
        if (receipt is None or receipt["receipt_id"] != projection.receipt_id
                or receipt["receipt_digest"] != projection.receipt_digest
                or receipt["allowance_unit"] != projection.allowance_unit
                or receipt["currency"] != "USD"
                or receipt["source_code"] != intent["source_code"]
                or receipt["issuer_code"] != intent["issuer_code"]
                or receipt["source_identity_digest"] != intent["source_identity_digest"]
                or receipt["outcome"] not in {"known", "unknown"}
                or receipt["evidence_id"] is not None
                or not _db_int(receipt["observed_ms"])
                or not _db_int(receipt["custody_bytes"], positive=True, maximum=1048576)
                or type(receipt["custody_ref"]) is not str
                or not 1 <= len(receipt["custody_ref"].encode("utf-8")) <= 512
                or type(receipt["source_digest"]) is not str
                or _HEX256_RE.fullmatch(receipt["source_digest"]) is None
                or type(receipt["source_identity_digest"]) is not str
                or _HEX256_RE.fullmatch(receipt["source_identity_digest"]) is None):
            raise _WorkProjectionUnavailable()
        if receipt["outcome"] == "known":
            if (not _db_int(receipt["actual_usd_micro"])
                    or not _db_int(receipt["actual_allowance_micro"])
                    or projection.actual_usd_micro != str(receipt["actual_usd_micro"])
                    or projection.actual_allowance_micro != str(receipt["actual_allowance_micro"])):
                raise _WorkProjectionUnavailable()
            receipt_actuals = (str(receipt["actual_usd_micro"]), str(receipt["actual_allowance_micro"]))
        else:
            if (receipt["actual_usd_micro"] is not None or receipt["actual_allowance_micro"] is not None
                    or projection.actual_usd_micro is not None):
                raise _WorkProjectionUnavailable()
            receipt_actuals = (None, None)
        stored_receipt_digest = digest(dict(
            scope_id=scope_id, call_id=call_id, receipt_id=receipt["receipt_id"],
            actual_usd=receipt_actuals[0], actual_allowance=receipt_actuals[1],
            currency=receipt["currency"], allowance_unit=receipt["allowance_unit"],
            source_code=receipt["source_code"], issuer_code=receipt["issuer_code"],
            source_digest=receipt["source_digest"],
            source_identity_digest=receipt["source_identity_digest"],
            custody_ref=receipt["custody_ref"], custody_bytes=receipt["custody_bytes"],
            observed_ms=receipt["observed_ms"],
        ))
        if stored_receipt_digest != receipt["receipt_digest"]:
            raise _WorkProjectionUnavailable()

    evidence_call_predicates = [evidence_t.c.call_id == call_id]
    if projection.evidence_id is not None:
        evidence_call_predicates.append(evidence_t.c.evidence_id == projection.evidence_id)
    evidence_rows = locked_rows(evidence_t, and_(
        evidence_t.c.scope_id == scope_id, or_(*evidence_call_predicates)),
                                order=(evidence_t.c.evidence_id,))
    if evidence_rows:
        # v1 deliberately cannot represent overflow/custody evidence.
        raise _WorkProjectionUnavailable()

    try:
        binding_t = Table(_BINDING_TABLE, MetaData(), schema=binding_schema,
                          autoload_with=connection)
    except Exception:
        raise _WorkProjectionUnavailable() from None
    if (set(binding_t.c.keys()) != _BINDING_COLUMNS
            or [column.name for column in binding_t.primary_key.columns] != ["scope_id", "call_id"]):
        raise _WorkProjectionUnavailable()
    binding_rows = locked_rows(binding_t, binding_t.c.scope_id == scope_id,
                               binding_t.c.call_id == call_id)
    if len(binding_rows) != 1:
        raise _WorkProjectionUnavailable()
    binding = binding_rows[0]
    if (binding["scope_id"] != projection.scope_id or binding["call_id"] != projection.call_id
            or binding["subject_id"] != projection.subject_id
            or binding["work_job_id"] != projection.work_job_id
            or binding["attempt_id"] != projection.attempt_id
            or binding["slot_id"] != projection.slot_id or binding["purpose"] != "work"
            or binding["policy_revision"] != int(projection.policy_revision)
            or binding["policy_digest"] != projection.policy_digest
            or binding["projection_revision"] != int(projection.projection_revision)
            or binding["projection_digest"] != expected_digest
            or binding["audit_sequence"] != int(projection.audit_sequence)):
        raise _WorkProjectionUnavailable()

    if (reservation["state"] != projection.reservation_state
            or slot["state"] != projection.slot_state
            or intent["state"] != projection.intent_state
            or not _db_int(reservation["reserved_usd_micro"])
            or not _db_int(reservation["reserved_allowance_micro"])
            or str(reservation["reserved_usd_micro"]) != projection.reserved_usd_micro
            or str(reservation["reserved_allowance_micro"]) != projection.reserved_allowance_micro
            or (reservation["actual_usd_micro"] is None) != (reservation["actual_allowance_micro"] is None)
            or any(value is not None and not _db_int(value) for value in
                   (reservation["actual_usd_micro"], reservation["actual_allowance_micro"]))
            or (None if reservation["actual_usd_micro"] is None else str(reservation["actual_usd_micro"]))
                != projection.actual_usd_micro
            or (None if reservation["actual_allowance_micro"] is None else str(reservation["actual_allowance_micro"]))
                != projection.actual_allowance_micro
            or not _db_int(intent["audit_sequence"], positive=True, maximum=MAX_AUDIT_SEQUENCE)
            or not _db_int(reservation["audit_sequence"], positive=True, maximum=MAX_AUDIT_SEQUENCE)
            or not _db_int(slot["audit_sequence"], positive=True, maximum=MAX_AUDIT_SEQUENCE)
            or intent["audit_sequence"] != int(projection.audit_sequence)
            or reservation["audit_sequence"] != int(projection.audit_sequence)
            or slot["audit_sequence"] != int(projection.audit_sequence)
            or not _db_int(binding["audit_sequence"], positive=True, maximum=MAX_AUDIT_SEQUENCE)):
        raise _WorkProjectionUnavailable()
    if receipt is not None and (not _db_int(receipt["audit_sequence"], positive=True,
                                            maximum=MAX_AUDIT_SEQUENCE)
                                or receipt["audit_sequence"] != int(projection.audit_sequence)):
        raise _WorkProjectionUnavailable()

    check_audit_sequences(
        gate["audit_sequence"], policy["audit_sequence"], registry["audit_sequence"],
        reservation["audit_sequence"], slot["audit_sequence"], intent["audit_sequence"],
        binding["audit_sequence"], *( () if receipt is None else (receipt["audit_sequence"],) ),
        *(row["audit_sequence"] for row in policies.values() if row is not policy),
    )
    if not hmac.compare_digest(expected_digest, binding["projection_digest"]):
        raise _WorkProjectionUnavailable()
    return _CANONICAL_ROWS_AGREE


def _compare_actual_work_projection(session, *, scope_id: str, call_id: str,
                                    allow_cancel_requested: bool = False,
                                    allow_interrupted_attempt: bool = False,
                                    allow_preentry_cancelled_attempt: bool = False,
                                    finish_attempt_states: frozenset[str] | None = None) -> str:
    """Compare one actual Work call within an existing owner transaction.

    This private comparator accepts selectors, never caller-provided rows or
    mappings. It is read-only and returns only a bounded agreement label. A
    matching digest/row set is not source freshness, fence, authority, or proof
    that a receipt is genuine. In particular, this does not establish that all
    database writers use the owner lock boundary. It requires a running Job and
    started lease; it does not compare or authorize legacy display consumers.
    """
    try:
        return _compare_actual_rows(session, scope_id=scope_id, call_id=call_id,
                                    allow_cancel_requested=allow_cancel_requested,
                                    allow_interrupted_attempt=allow_interrupted_attempt,
                                    allow_preentry_cancelled_attempt=allow_preentry_cancelled_attempt,
                                    finish_attempt_states=finish_attempt_states)
    except _WorkProjectionUnavailable:
        raise
    except Exception:
        # Do not expose raw Job JSON, provider/custody data, SQL details, or
        # database errors through this dormant comparison boundary.
        raise _WorkProjectionUnavailable() from None
