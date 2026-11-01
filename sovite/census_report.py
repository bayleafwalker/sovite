"""Private, read-only fixture census diagnostics; never cutover authority.

No product route imports this module. This scanner is for disposable test
fixtures only and does not authenticate source data or external writer fences.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import re

from sqlalchemy import JSON, LargeBinary, Numeric, Text, cast, func, select, tuple_

from .db import Base, DatabaseSchemaError
from .collection_identity import create_request_identity

_MAX_PAGE = 128
_MAX_ROWS = 10000
_MAX_TOTAL_ROWS = 50000
_MAX_FIELD_BYTES = 65536
_MAX_KEY_BYTES = 96
_MAX_TOTAL_BYTES = 16 * 1024 * 1024
_MAX_JSON_DEPTH = 32
_MAX_JSON_NODES = 10000

# Only selected fields are materialized; task bodies, bearer material and raw
# external evidence are intentionally absent. JSON fields are fetched as text.
_FIELDS = {
    "jobs": ("id", "status", "resolved"),
    "worker_execution_leases": ("job_id", "phase", "generation"),
    "program_budget_scopes": ("id", "policy_revision", "policy_digest", "window_start_ms", "window_end_ms", "usd_limit_micro", "allowance_limit_micro", "slot_limit"),
    "program_budget_subjects": ("scope_id", "subject_id", "purpose", "binding_digest"),
    "program_subject_registry": ("purpose", "subject_id", "scope_id", "binding_digest", "audit_sequence"),
    "program_reservations": ("scope_id", "call_id", "subject_id", "purpose", "state", "reserved_usd_micro", "reserved_allowance_micro", "actual_usd_micro", "actual_allowance_micro", "audit_sequence"),
    "program_slots": ("scope_id", "slot_id", "subject_id", "call_id", "state", "audit_sequence"),
    "program_policy_revisions": ("scope_id", "revision", "policy_digest", "currency", "allowance_unit", "window_start_ms", "window_end_ms", "usd_limit_micro", "allowance_limit_micro", "slot_limit", "audit_sequence"),
    "program_gate_state": ("scope_id", "policy_revision", "policy_digest", "cutover_batch_id", "cutover_generation", "cutover_digest", "cutover_fence_digest", "cutover_owner_epoch", "mode", "audit_sequence"),
    "program_census_batches": ("scope_id", "batch_id", "policy_revision", "policy_digest", "census_generation", "snapshot_digest", "fence_digest", "owner_epoch", "state", "audit_sequence"),
    "program_legacy_links": ("scope_id", "batch_id", "link_id", "job_id", "original_attempt_id", "original_call_id", "original_slot_id", "subject_id", "call_id", "slot_id", "audit_sequence"),
    "program_call_intents": ("scope_id", "call_id", "subject_id", "purpose", "policy_revision", "policy_digest", "work_job_id", "work_attempt_id", "work_lease_digest", "collection_session_id", "collection_epoch", "collection_context_digest", "state", "audit_sequence"),
    "program_call_receipts": ("scope_id", "call_id", "receipt_id", "receipt_digest", "outcome", "actual_usd_micro", "actual_allowance_micro", "evidence_id", "audit_sequence"),
    "program_overflow_evidence": ("scope_id", "evidence_id", "kind", "call_id", "receipt_digest", "policy_revision", "policy_digest", "snapshot_digest", "usd_decimal", "allowance_decimal", "audit_sequence"),
    "collection_sessions": ("id", "scope_id", "state", "current_epoch", "immutable_payload", "immutable_digest", "principal_id", "workspace_id", "repository_id", "generation", "suite_version", "suite_digest", "deployment_digest", "permit_digest", "deadline_ms", "retain_until_ms", "schema_version", "seed_digest", "base_sha", "paths_digest", "purpose"),
    "collection_epoch_guards": ("session_id", "active_epoch", "revision"),
    "collection_epochs": ("session_id", "epoch", "epoch_purpose", "state"),
    "collection_enrollments": ("id", "session_id", "original_epoch"),
    "collection_operations": ("session_id", "operation_id", "epoch", "epoch_purpose", "enrollment_id", "state", "audit_sequence"),
    "collection_acknowledgements": ("enrollment_id", "sequence", "session_id", "operation_id", "audit_sequence"),
    "collection_checkpoints": ("session_id", "sequence", "audit_sequence"),
    "collection_observations": ("session_id", "sequence", "audit_sequence"),
    "collection_retention_refs": ("session_id", "reference_id", "kind", "hold", "checkpoint_sequence", "observation_sequence", "ack_enrollment_id", "ack_sequence", "qualification_id", "audit_sequence"),
    "qualifications": ("id",),
    "audit_events": ("sequence",),
    "platform_metadata": ("key",),
}


class CensusIncomplete(RuntimeError):
    """The fixture scan could not produce a bounded complete diagnostic."""


def _octets(column, dialect):
    if dialect == "sqlite":
        return func.coalesce(func.length(cast(column, LargeBinary)), 0)
    return func.coalesce(func.octet_length(cast(column, Text)), 0)


def _preflight(connection, table, names, dialect):
    columns = [table.c[name] for name in names]
    sizes = [_octets(col, dialect) for col in columns]
    fields = [func.count()]
    for size in sizes:
        fields.extend((func.coalesce(func.max(size), 0), func.coalesce(func.sum(cast(size, Numeric)), 0)))
    values = connection.execute(select(*fields).select_from(table)).one()
    count = int(values[0])
    if count > _MAX_ROWS:
        raise CensusIncomplete("census incomplete")
    total = 0
    primary = {col.name for col in table.primary_key}
    for index, col in enumerate(columns):
        maximum, amount = int(values[1 + index * 2]), int(values[2 + index * 2])
        if maximum > (_MAX_KEY_BYTES if col.name in primary else _MAX_FIELD_BYTES):
            raise CensusIncomplete("census incomplete")
        total += amount
        if total > _MAX_TOTAL_BYTES:
            raise CensusIncomplete("census incomplete")
    return count, total


def _key_order(column, dialect):
    if column.type._type_affinity in (Text,):
        return column.collate("BINARY" if dialect == "sqlite" else "C")
    # String and Text have different affinities on some SQLAlchemy versions.
    from sqlalchemy import String
    if column.type._type_affinity is String:
        return column.collate("BINARY" if dialect == "sqlite" else "C")
    return column


def _normal_key(values):
    output = []
    for value in values:
        if type(value) is str:
            raw = value.encode("utf-8")
            if not raw or len(raw) > _MAX_KEY_BYTES:
                raise CensusIncomplete("census incomplete")
            output.append(raw)
        elif type(value) is int:
            output.append(value)
        else:
            raise CensusIncomplete("census incomplete")
    return tuple(output)


def _scan_stream(connection, table, names, dialect, page_size, expected):
    primary = list(table.primary_key)
    if not primary or any(col.name not in names for col in primary):
        raise CensusIncomplete("census incomplete")
    order = [_key_order(col, dialect) for col in primary]
    selected = [cast(table.c[name], Text).label(name) if isinstance(table.c[name].type, JSON)
                else table.c[name] for name in names]
    rows, last, previous, encoded_total = [], None, None, 0
    stream = hashlib.sha256()
    while True:
        stmt = select(*selected).select_from(table).order_by(*order).limit(page_size)
        if last is not None:
            stmt = stmt.where(tuple_(*order) > tuple_(*last))
        page = connection.execute(stmt).mappings().all()
        if not page:
            break
        for row in page:
            key = tuple(row[col.name] for col in primary)
            normalized = _normal_key(key)
            if previous is not None and normalized <= previous:
                raise CensusIncomplete("census incomplete")
            previous, last = normalized, key
            encoded = json.dumps(dict(row), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                 allow_nan=False).encode("utf-8")
            encoded_total += len(encoded)
            if encoded_total > _MAX_TOTAL_BYTES:
                raise CensusIncomplete("census incomplete")
            stream.update(len(encoded).to_bytes(8, "big"))
            stream.update(encoded)
            rows.append(dict(row))
            if len(rows) > expected:
                raise CensusIncomplete("census incomplete")
    if len(rows) != expected:
        raise CensusIncomplete("census incomplete")
    return rows, stream.hexdigest(), encoded_total


def _json_pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result


class _BoundExceeded(ValueError):
    pass


class _JsonDecimal:
    def __init__(self, raw):
        self.raw = raw


def _bounded_json_prescan(value):
    """Conservatively bound nesting and lexical tokens before materialization.

    JSON's own decoder still performs complete grammar and escape validation.
    Strings, object keys, scalars, and containers each count toward the bound.
    """
    stack, tokens, index = [], 0, 0
    while index < len(value):
        char = value[index]
        if char in " \t\r\n,:":
            index += 1
            continue
        if char == '"':
            tokens += 1
            index += 1
            closed = False
            while index < len(value):
                if value[index] == "\\":
                    index += 2
                    continue
                if value[index] == '"':
                    index += 1
                    closed = True
                    break
                index += 1
            if not closed:
                raise ValueError("invalid")
        elif char in "[{":
            tokens += 1
            stack.append(char)
            if len(stack) > _MAX_JSON_DEPTH:
                raise _BoundExceeded("bounded")
            index += 1
        elif char in "]}":
            if not stack or (stack.pop(), char) not in (("[", "]"), ("{", "}")):
                raise ValueError("invalid")
            index += 1
        elif char in "-0123456789tfn":
            tokens += 1
            start = index
            while index < len(value) and value[index] not in " \t\r\n,:[]{}":
                index += 1
            if index == start:
                raise ValueError("invalid")
        else:
            raise ValueError("invalid")
        if tokens > _MAX_JSON_NODES:
            raise _BoundExceeded("bounded")
    if stack:
        raise ValueError("invalid")


def _decode_json(value):
    if type(value) is not str:
        raise ValueError("invalid")
    _bounded_json_prescan(value)
    return json.loads(value, object_pairs_hook=_json_pairs, parse_float=_JsonDecimal,
                      parse_int=lambda raw: int(raw) if len(raw) <= 128 else (_ for _ in ()).throw(_BoundExceeded("bounded")),
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid")))


def _foreign_key_issues(data):
    """Count missing selected FK references; no row data leaves this module."""
    count = 0
    uninspected = 0
    for name, rows in data.items():
        table = Base.metadata.tables[name]
        for constraint in table.foreign_key_constraints:
            elements = list(constraint.elements)
            target_name = elements[0].column.table.name
            local = [element.parent.name for element in elements]
            target = [element.column.name for element in elements]
            if (target_name not in data or any(field not in _FIELDS[name] for field in local)
                    or any(field not in _FIELDS[target_name] for field in target)):
                uninspected += len(rows)
                continue
            existing = {tuple(row[field] for field in target) for row in data[target_name]}
            for row in rows:
                key = tuple(row[field] for field in local)
                if any(value is None for value in key):
                    continue
                if key not in existing:
                    count += 1
    return count, uninspected


def _classify(data):
    issues = Counter()
    jobs = {row["id"]: row for row in data["jobs"]}
    subjects = data["program_budget_subjects"]
    registries = data["program_subject_registry"]
    reservations = data["program_reservations"]
    slots = data["program_slots"]
    sessions = {row["id"]: row for row in data["collection_sessions"]}
    guards = {row["session_id"]: row for row in data["collection_epoch_guards"]}
    epochs = {(row["session_id"], row["epoch"]): row for row in data["collection_epochs"]}
    source_calls, source_slots, source_attempts = set(), set(), set()
    source_counts = Counter()
    source_records = {}
    repair_parents, repair_roots = {}, {}
    issues["work_scope_unclassified"] = len(jobs)
    issues["projection_unverifiable"] = len(jobs)
    for job_id, row in jobs.items():
        try:
            resolved = _decode_json(row["resolved"])
        except _BoundExceeded:
            raise
        except (ValueError, TypeError, RecursionError, OverflowError):
            issues["malformed_work_json"] += 1
            continue
        if type(resolved) is not dict:
            issues["malformed_work_json"] += 1
            continue
        run = resolved.get("repository_run")
        if run is None:
            continue
        if type(run) is not dict:
            issues["malformed_work_json"] += 1
            continue
        for field, destination in (("budget_reservations", source_calls), ("active_slots", source_slots),
                                   ("attempts", source_attempts)):
            values = run.get(field, [])
            if type(values) is not list:
                issues["malformed_work_json"] += 1
                continue
            for item in values:
                identity = item if field == "active_slots" else item.get("call_id" if field == "budget_reservations" else "id") if type(item) is dict else None
                if type(identity) is not str or not identity or len(identity.encode("utf-8")) > _MAX_KEY_BYTES:
                    issues["malformed_work_identity"] += 1
                    continue
                key = (job_id, identity)
                source_counts[(field, *key)] += 1
                if field == "budget_reservations" and type(item) is dict:
                    source_records[key] = item
                    if item.get("state") not in {"reserved", "settled", "unknown"}:
                        issues["malformed_reservation_state"] += 1
                    for amount_name in ("reserved_usd", "actual_usd"):
                        if amount_name in item and item[amount_name] is not None:
                            issues["legacy_amounts_unverified"] += 1
                            amount = item[amount_name]
                            raw = amount.raw if isinstance(amount, _JsonDecimal) else str(amount) if type(amount) in (int, str) else ""
                            if len(raw) > 128 or re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", raw) is None:
                                issues["legacy_numeric_ambiguous"] += 1
                    if "quota" in item:
                        issues["legacy_amounts_unverified"] += 1
                if field == "attempts" and type(item) is dict:
                    issues["attempt_markers_unverified"] += 1
                    if item.get("state") not in {"preparing", "running", "awaiting_activation", "interrupted", "uncertain", "failed", "cancelled", "completed", "settled"}:
                        issues["malformed_attempt_state"] += 1
                if key in destination:
                    issues["duplicate_work_identity"] += 1
                destination.add(key)
        draft_calls = run.get("draft_calls", [])
        if type(draft_calls) is not list:
            issues["malformed_patch_marker"] += 1
        else:
            seen_drafts = set()
            for marker in draft_calls:
                issues["patch_markers_unverified"] += 1
                if (type(marker) is not dict or type(marker.get("call_id")) is not str
                        or not marker.get("call_id") or marker.get("state") not in {"calling", "unknown", "settled"}
                        or marker.get("call_id") in seen_drafts):
                    issues["malformed_patch_marker"] += 1
                elif type(marker) is dict:
                    seen_drafts.add(marker["call_id"])
        repair = run.get("repair")
        if repair is not None:
            if type(repair) is not dict or type(repair.get("parent_run_id")) is not str:
                issues["malformed_repair"] += 1
            else:
                repair_parents[job_id] = repair["parent_run_id"]
                root = repair.get("root_run_id")
                repair_roots[job_id] = root
                if type(root) is not str or root not in jobs:
                    issues["missing_repair_root"] += 1
    resolved_roots = {}
    for job_id, parent in repair_parents.items():
        if parent not in jobs:
            issues["missing_repair_parent"] += 1
        if job_id in resolved_roots:
            continue
        path, local, current = [], set(), job_id
        while current in repair_parents and current not in resolved_roots and current not in local:
            local.add(current)
            path.append(current)
            current = repair_parents[current]
        if current in local:
            issues["cyclic_repair"] += 1
            root = None
        elif current in resolved_roots:
            root = resolved_roots[current]
        else:
            root = current if current in jobs else None
        for node in reversed(path):
            resolved_roots[node] = root
            if root is not None and repair_roots.get(node) != root:
                issues["repair_root_mismatch"] += 1
    # Caller and scope continuity are not established from this projection.
    issues["repair_owner_unverified"] = len(repair_parents)
    subject_bindings = defaultdict(set)
    for row in subjects:
        subject_bindings[row["subject_id"]].add((row["scope_id"], row["purpose"]))
    issues["subject_collisions"] = sum(len(bindings) > 1 for bindings in subject_bindings.values())
    registry_by_id = {row["subject_id"]: row for row in registries}
    for row in subjects:
        registry = registry_by_id.get(row["subject_id"])
        if registry is None:
            issues["subject_without_registry"] += 1
        elif (registry["scope_id"], registry["purpose"], registry["binding_digest"]) != (
                row["scope_id"], row["purpose"], row["binding_digest"]):
            issues["registry_mismatch"] += 1
        if row["purpose"] == "collection":
            session = sessions.get(row["subject_id"])
            guard = guards.get(row["subject_id"])
            if session is None:
                issues["collection_subject_without_session"] += 1
            elif session["scope_id"] != row["scope_id"]:
                issues["collection_scope_mismatch"] += 1
            elif guard is None:
                issues["collection_guard_missing"] += 1
            elif guard["active_epoch"] != session["current_epoch"]:
                issues["collection_epoch_mismatch"] += 1
            elif session["current_epoch"] is not None and (session["id"], session["current_epoch"]) not in epochs:
                issues["collection_epoch_missing"] += 1
    policies = {(row["scope_id"], row["revision"]): row for row in data["program_policy_revisions"]}
    for row in data["collection_sessions"]:
        try:
            payload = _decode_json(row["immutable_payload"])
            if (type(payload) is not dict
                    or set(payload) != {"schema_version", "request_canonical", "request_digest", "policy"}
                    or payload["schema_version"] != "sovite.collection-binding/v1"
                    or type(payload["policy"]) is not dict):
                issues["collection_immutable_mismatch"] += 1
                continue
            _decode_json(payload["request_canonical"])
            request, request_canonical, request_digest = create_request_identity(payload["request_canonical"].encode("utf-8"))
            if (request_canonical != payload["request_canonical"] or request_digest != payload["request_digest"]
                    or any(request[name] != row[name] for name in ("schema_version", "workspace_id", "repository_id", "generation", "suite_version", "suite_digest", "deployment_digest", "permit_digest", "deadline_ms"))
                    or request["retain_until_ms"] > row["retain_until_ms"]):
                issues["collection_immutable_mismatch"] += 1
                continue
            canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            digest = hashlib.sha256(b"sovite.collection.immutable/v1\0" + canonical.encode()).hexdigest()
            policy_payload = payload["policy"]
            if (digest != row["immutable_digest"] or policy_payload.get("scope_id") != row["scope_id"]
                    or row["purpose"] != "operator_c0_collection"
                    or any(policy_payload.get(name) != row[name] for name in ("seed_digest", "base_sha", "paths_digest"))
                    or type(policy_payload.get("principals")) is not dict
                    or policy_payload["principals"].get("collection-operator") != row["principal_id"]):
                issues["collection_immutable_mismatch"] += 1
            elif "scope_policy_revision" not in policy_payload or "scope_policy_digest" not in policy_payload:
                issues["collection_policy_unverified"] += 1
            else:
                policy = policies.get((row["scope_id"], policy_payload["scope_policy_revision"]))
                if policy is None:
                    issues["collection_policy_unverified"] += 1
                elif policy["policy_digest"] != policy_payload["scope_policy_digest"]:
                    issues["collection_policy_mismatch"] += 1
        except _BoundExceeded:
            raise
        except (ValueError, TypeError, RecursionError, OverflowError):
            issues["malformed_collection_json"] += 1
    reservation_map = {(row["scope_id"], row["call_id"]): row for row in reservations}
    slot_map = {(row["scope_id"], row["slot_id"]): row for row in slots}
    slots_by_call = defaultdict(list)
    for row in slots:
        slots_by_call[(row["scope_id"], row["call_id"])].append(row)
        reservation = reservation_map.get((row["scope_id"], row["call_id"]))
        if reservation is None or reservation["subject_id"] != row["subject_id"]:
            issues["slot_reservation_mismatch"] += 1
    for row in reservations:
        amounts = tuple(row[name] for name in ("reserved_usd_micro", "reserved_allowance_micro", "actual_usd_micro", "actual_allowance_micro"))
        if (any(value is not None and (type(value) is not int or value < 0) for value in amounts)
                or amounts[0] is None or amounts[1] is None or ((amounts[2] is None) != (amounts[3] is None))):
            issues["invalid_canonical_amount"] += 1
        if row["state"] not in {"reserved", "settled", "unknown"}:
            issues["invalid_canonical_state"] += 1
        if ((row["state"] == "settled" and (amounts[2] is None or amounts[3] is None))
                or (row["state"] == "reserved" and (amounts[2] is not None or amounts[3] is not None))):
            issues["incoherent_reservation_state"] += 1
        if (row["state"] == "settled" and all(type(value) is int for value in amounts)
                and (amounts[2] > amounts[0] or amounts[3] > amounts[1])):
            issues["settled_reservation_overrun"] += 1
        matching = slots_by_call.get((row["scope_id"], row["call_id"]), [])
        if len(matching) != 1 or matching[0]["subject_id"] != row["subject_id"]:
            issues["reservation_slot_mismatch"] += 1
        elif (row["state"], matching[0]["state"]) not in {("reserved", "held"), ("settled", "released"), ("unknown", "unknown")}:
            issues["incoherent_reservation_slot"] += 1
        if row["purpose"] == "collection" and row["subject_id"] not in jobs:
            issues["collection_obligations_without_job"] += 1
    link_targets = Counter((row["scope_id"], row["call_id"]) for row in data["program_legacy_links"])
    source_targets = Counter((row["scope_id"], row["job_id"], row["original_attempt_id"], row["original_call_id"], row["original_slot_id"])
                             for row in data["program_legacy_links"])
    ambiguous_source_calls = Counter((row["job_id"], row["original_call_id"]) for row in data["program_legacy_links"])
    matched_sources, matched_targets = set(), set()
    for link in data["program_legacy_links"]:
        target = (link["scope_id"], link["call_id"])
        source = (link["scope_id"], link["job_id"], link["original_attempt_id"], link["original_call_id"], link["original_slot_id"])
        reservation = reservation_map.get(target)
        slot = slot_map.get((link["scope_id"], link["slot_id"]))
        registry = registry_by_id.get(link["subject_id"])
        source_record = source_records.get((link["job_id"], link["original_call_id"]))
        valid = (link_targets[target] == 1 and source_targets[source] == 1
                 and ambiguous_source_calls[(link["job_id"], link["original_call_id"])] == 1
                 and type(source_record) is dict
                 and source_record.get("attempt_id") == link["original_attempt_id"]
                 and source_record.get("slot_id") == link["original_slot_id"]
                 and link["job_id"] == link["subject_id"] and link["job_id"] in jobs
                 and reservation is not None and reservation["subject_id"] == link["subject_id"]
                 and reservation["purpose"] == "work" and slot is not None
                 and slot["call_id"] == link["call_id"] and slot["subject_id"] == link["subject_id"]
                 and registry is not None and registry["scope_id"] == link["scope_id"] and registry["purpose"] == "work"
                 and (link["job_id"], link["original_call_id"]) in source_calls
                 and (link["job_id"], link["original_slot_id"]) in source_slots
                 and (link["job_id"], link["original_attempt_id"]) in source_attempts
                 and source_counts[("budget_reservations", link["job_id"], link["original_call_id"])] == 1
                 and source_counts[("active_slots", link["job_id"], link["original_slot_id"])] == 1
                 and source_counts[("attempts", link["job_id"], link["original_attempt_id"])] == 1)
        if valid:
            matched_sources.add((link["job_id"], link["original_call_id"]))
            matched_targets.add(target)
        else:
            issues["legacy_link_unresolved"] += 1
    issues["duplicate_legacy_targets"] = sum(value - 1 for value in link_targets.values() if value > 1)
    matched_slots = {(link["job_id"], link["original_slot_id"]) for link in data["program_legacy_links"]
                     if (link["scope_id"], link["call_id"]) in matched_targets}
    issues["unmatched_work_slots"] = len(source_slots - matched_slots)
    attempt_jobs = {item[0] for item in source_attempts}
    issues["work_calls_without_attempts"] = sum(job_id not in attempt_jobs for job_id, _ in source_calls)
    for row in data["collection_retention_refs"]:
        if (row["ack_enrollment_id"] is None) != (row["ack_sequence"] is None):
            issues["partial_retention_ack"] += 1
    for row in data["program_gate_state"]:
        values = tuple(row[name] for name in ("cutover_batch_id", "cutover_generation", "cutover_digest", "cutover_fence_digest", "cutover_owner_epoch"))
        if any(value is None for value in values) and any(value is not None for value in values):
            issues["partial_gate_binding"] += 1
        if row["mode"] not in {"disabled", "census", "active", "inhibited"} or (row["mode"] == "active" and any(value is None for value in values)):
            issues["invalid_gate_state"] += 1
    intent_map = {(row["scope_id"], row["call_id"]): row for row in data["program_call_intents"]}
    issues["settled_without_intent_unverified"] = sum(
        row["state"] == "settled" and (row["scope_id"], row["call_id"]) not in intent_map
        for row in reservations)
    receipt_groups = defaultdict(list)
    for row in data["program_call_receipts"]:
        receipt_groups[(row["scope_id"], row["call_id"])].append(row)
    for key, intent in intent_map.items():
        reservation = reservation_map.get(key)
        matching = slots_by_call.get(key, [])
        if (reservation is None or len(matching) != 1
                or (reservation["state"], matching[0]["state"], intent["state"]) not in {
                    ("reserved", "held", "committed"), ("settled", "released", "known"), ("unknown", "unknown", "unknown")}):
            issues["incoherent_intent_state"] += 1
    for key in intent_map.keys() | receipt_groups.keys():
        receipts = receipt_groups[key]
        reservation = reservation_map.get(key)
        intent = intent_map.get(key)
        outcomes = Counter(row["outcome"] for row in receipts)
        if (reservation is None or intent is None or len(receipts) > 2
                or outcomes["known"] > 1 or outcomes["unknown"] > 1
                or (reservation["state"] == "reserved" and receipts)
                or (reservation["state"] == "settled" and outcomes["unknown"])
                or (reservation["state"] == "settled" and not outcomes["known"])):
            issues["incoherent_receipt_set"] += 1
        if reservation is not None:
            for receipt in receipts:
                if (receipt["outcome"] == "known" and receipt["evidence_id"] is not None
                        and (reservation["actual_usd_micro"] is not None or reservation["actual_allowance_micro"] is not None)):
                    issues["receipt_reservation_amount_mismatch"] += 1
                if receipt["outcome"] == "known" and receipt["evidence_id"] is None and (
                        receipt["actual_usd_micro"], receipt["actual_allowance_micro"]) != (
                        reservation["actual_usd_micro"], reservation["actual_allowance_micro"]):
                    issues["receipt_reservation_amount_mismatch"] += 1
    for row in data["program_call_intents"]:
        work = (row["work_job_id"], row["work_attempt_id"], row["work_lease_digest"])
        collection = (row["collection_session_id"], row["collection_epoch"], row["collection_context_digest"])
        if not ((row["purpose"] == "work" and all(value is not None for value in work)
                 and all(value is None for value in collection) and row["subject_id"] == row["work_job_id"])
                or (row["purpose"] == "collection" and all(value is None for value in work)
                    and all(value is not None for value in collection) and row["subject_id"] == row["collection_session_id"])):
            issues["invalid_intent_branch"] += 1
    for row in data["program_call_receipts"]:
        if row["outcome"] not in {"known", "unknown"}:
            issues["invalid_receipt_branch"] += 1
        if any(value is not None and (type(value) is not int or value < 0) for value in
               (row["actual_usd_micro"], row["actual_allowance_micro"])):
            issues["invalid_canonical_amount"] += 1
        if (row["actual_usd_micro"] is None) != (row["actual_allowance_micro"] is None):
            issues["partial_receipt_amount"] += 1
        if row["outcome"] == "unknown" and (row["actual_usd_micro"] is not None or row["evidence_id"] is not None):
            issues["invalid_receipt_branch"] += 1
        if row["outcome"] == "known" and ((row["actual_usd_micro"] is None) == (row["evidence_id"] is None)):
            issues["invalid_receipt_branch"] += 1
    for row in data["program_overflow_evidence"]:
        call_branch = row["call_id"] is not None and row["receipt_digest"] is not None and all(row[name] is None for name in ("policy_revision", "policy_digest", "snapshot_digest"))
        aggregate_branch = row["call_id"] is None and row["receipt_digest"] is None and all(row[name] is not None for name in ("policy_revision", "policy_digest", "snapshot_digest"))
        if not ((row["kind"] == "call" and call_branch) or (row["kind"] == "aggregate" and aggregate_branch)):
            issues["invalid_overflow_branch"] += 1
        for name in ("usd_decimal", "allowance_decimal"):
            value = row[name]
            if type(value) is not str or len(value) > 128 or re.fullmatch(r"(?:0|[1-9][0-9]*)", value) is None:
                issues["invalid_canonical_amount"] += 1
    fk_missing, fk_uninspected = _foreign_key_issues(data)
    issues["missing_selected_fk"] = fk_missing
    issues["uninspected_fk"] = fk_uninspected
    # Counters overlap: the report is neither a balance nor a set of free slots.
    return {
        "work_candidate_calls": len(source_calls),
        "canonical_obligations": len(reservations),
        "canonical_slots": len(slots),
        "deduplicated_work_matches": len(matched_targets),
        "unmatched_work_calls": len(source_calls - matched_sources),
        "unmatched_canonical_obligations": len(reservations) - len(matched_targets),
        "issues": dict(sorted(issues.items())),
    }


def _capture_fixture_report(database, *, page_size=64):
    """Read one bounded fixture snapshot and return non-authoritative counts.

    No result is returned on an incomplete scan. All exception messages are
    fixed and discard database, data and source details.
    """
    if type(page_size) is not int or not 1 <= page_size <= _MAX_PAGE:
        raise CensusIncomplete("census incomplete")
    connection = None
    original_query_only = None
    try:
        connection = database.engine.connect()
        dialect = connection.dialect.name
        if dialect == "sqlite":
            original_query_only = connection.exec_driver_sql("PRAGMA query_only").scalar_one()
            if original_query_only not in (0, 1):
                raise CensusIncomplete("census incomplete")
            if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 1:
                raise CensusIncomplete("census incomplete")
            connection.exec_driver_sql("PRAGMA query_only=ON")
            connection.commit()
            connection.exec_driver_sql("BEGIN")
            connection.exec_driver_sql("SELECT COUNT(*) FROM sqlite_master").scalar_one()
        elif dialect == "postgresql":
            connection = connection.execution_options(isolation_level="REPEATABLE READ")
            connection.begin()
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            connection.execute(select(func.count()).select_from(Base.metadata.tables["platform_metadata"])).scalar_one()
            if connection.exec_driver_sql("SHOW client_encoding").scalar_one().upper() != "UTF8":
                raise CensusIncomplete("census incomplete")
        else:
            raise CensusIncomplete("census incomplete")
        metadata = Base.metadata.tables["platform_metadata"]
        # Before Database._version/_audit_head can deserialize their JSON values,
        # bound both metadata key and value on this same snapshot connection.
        _preflight(connection, metadata, ("key", "value"), dialect)
        required_values = connection.execute(select(metadata.c.key, cast(metadata.c.value, Text).label("value")).where(
            metadata.c.key.in_(("schema_version", "audit_head")))).mappings().all()
        if len(required_values) != 2:
            raise CensusIncomplete("census incomplete")
        for row in required_values:
            _decode_json(row["value"])
        version = database._version(connection)
        if version != database.schema_version:
            raise CensusIncomplete("census incomplete")
        database._structure(connection, Base.metadata.sorted_tables)
        database._audit_head(connection)
        counts, sizes, all_count, all_bytes = {}, {}, 0, 0
        for name, names in _FIELDS.items():
            count, size = _preflight(connection, Base.metadata.tables[name], names, dialect)
            counts[name], sizes[name] = count, size
            all_count += count
            all_bytes += size
            if all_count > _MAX_TOTAL_ROWS or all_bytes > _MAX_TOTAL_BYTES:
                raise CensusIncomplete("census incomplete")
        data, digests, encoded_bytes = {}, {}, 0
        for name, names in _FIELDS.items():
            rows, digest, size = _scan_stream(connection, Base.metadata.tables[name], names, dialect, page_size, counts[name])
            encoded_bytes += size
            if encoded_bytes > _MAX_TOTAL_BYTES:
                raise CensusIncomplete("census incomplete")
            data[name], digests[name] = rows, digest
        report = {
            "report_version": "fixture-census-diagnostic/v1",
            "dialect": dialect,
            "schema_version": version,
            "scan_complete": True,
            "source_auth": "unverified",
            "fence": "unverified",
            "projection_verification": "unavailable",
            "external_custody": "uninspected",
            "cutover_ready": False,
            "stream_counts": counts,
            "stream_digests": digests,
            "diagnostics": _classify(data),
        }
        connection.rollback()
        return report
    except Exception:
        if connection is not None:
            try:
                connection.rollback()
            except BaseException:
                try:
                    connection.invalidate()
                except BaseException:
                    pass
        raise CensusIncomplete("census incomplete") from None
    finally:
        if connection is not None:
            cleanup_failed = False
            try:
                if original_query_only is not None and not connection.invalidated:
                    connection.rollback()
                    connection.exec_driver_sql("PRAGMA query_only=" + ("ON" if original_query_only else "OFF"))
                    connection.commit()
            except BaseException:
                cleanup_failed = True
                try:
                    connection.invalidate()
                except BaseException:
                    pass
            try:
                connection.close()
            except BaseException:
                cleanup_failed = True
                try:
                    connection.invalidate()
                except BaseException:
                    pass
            if cleanup_failed:
                raise CensusIncomplete("census incomplete") from None
