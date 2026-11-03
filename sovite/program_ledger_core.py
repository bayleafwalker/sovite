"""Private storage algorithms for isolated owner-transaction tests.

These functions neither authenticate inputs nor authorize dispatch. No product
route or production verifier imports this module. Callers must hold the
collection owner transaction, including its audit-head lock, throughout.
"""
import hashlib
import math

from sqlalchemy import insert, select

from .collection_transaction import _owner_transaction_is_active
from .db import canonical, digest
from .lease_service import _FENCE_RE, _GENERATION_RE, _INSTANCE_RE, _WORKLOAD_RE, execution_owner_key

from .program_accounting import SUPPORTED_ALLOWANCE_UNIT
from .program_ledger import (_AggregateOverflow, _MAX_I64, _is_lower_hex, _obligation_totals,
                             _validate_locked_legacy_binding)


def _locked(session, table, *where):
    return session.execute(select(table).where(*where).with_for_update()).mappings().one_or_none()


def _bounded_id(value):
    return (type(value) is str and 1 <= len(value) <= 96
            and all("!" <= char <= "~" for char in value))


def _locked_scope_rows(session, table, scope_id, key_names):
    """Lock discovered scope rows by UTF-8 key bytes and recheck the scan.

    This checks participating owners only; it does not fence external writers
    or turn a PostgreSQL absent-row lookup into a predicate lock.
    """
    def scan():
        rows = session.execute(select(table).where(table.c.scope_id == scope_id)).mappings().all()
        if any(not _bounded_id(row[name]) for row in rows for name in key_names):
            raise ValueError("scope row identity unavailable")
        return sorted(rows, key=lambda row: tuple(row[name].encode("utf-8") for name in key_names))

    discovered = scan()
    locked = [_locked(session, table, table.c.scope_id == scope_id,
                      *(table.c[name] == row[name] for name in key_names)) for row in discovered]
    if locked != discovered or scan() != discovered:
        raise ValueError("scope rows changed")
    return locked


_BASELINE_FIELDS = ("scope_id", "batch_id", "link_id", "job_id", "original_attempt_id", "original_call_id", "original_slot_id",
                    "subject_id", "call_id", "slot_id", "baseline_state", "baseline_slot_state",
                    "baseline_reserved_usd_micro", "baseline_reserved_allowance_micro",
                    "baseline_actual_usd_micro", "baseline_actual_allowance_micro")


def _baseline_payload(link):
    return {name: link[name] for name in _BASELINE_FIELDS}


def _source_identity_digest(source_code, issuer_code):
    # Synthetic fixture-domain identity binding; not external authentication.
    return digest({"source_code": source_code, "issuer_code": issuer_code})


def _prepare_in_transaction(database, session, tables, *, scope_id, subject_id, purpose,
                            call_id, slot_id, effect_key, idempotency_key, request_digest,
                            usd_micro, allowance_micro, observed_ms, valid_until_ms,
                            price_digest, meter_digest, permit_digest, source_code, issuer_code,
                            source_identity_digest, context, aggregate_custody=None):
    """Atomically create synthetic accounting rows, without dispatch authority."""
    if not _owner_transaction_is_active(session):
        raise ValueError("owner transaction unavailable")
    identifiers = (scope_id, subject_id, call_id, slot_id, effect_key, idempotency_key, source_code, issuer_code)
    if not all(_bounded_id(value) for value in identifiers) or purpose not in {"work", "collection"}:
        raise ValueError("invalid storage identity")
    if (not all(_is_lower_hex(value, 64) for value in (request_digest, price_digest, meter_digest, permit_digest, source_identity_digest))
            or source_identity_digest != _source_identity_digest(source_code, issuer_code)):
        raise ValueError("invalid storage digest or source identity")
    if any(type(value) is not int or not 0 <= value <= _MAX_I64
           for value in (usd_micro, allowance_micro, observed_ms, valid_until_ms)) or valid_until_ms <= observed_ms:
        raise ValueError("invalid storage amount or time")
    scope_t, gate_t = tables["program_budget_scopes"], tables["program_gate_state"]
    batch_t, policy_t = tables["program_census_batches"], tables["program_policy_revisions"]
    registry_t, subject_t = tables["program_subject_registry"], tables["program_budget_subjects"]
    reservation_t, slot_t, intent_t = tables["program_reservations"], tables["program_slots"], tables["program_call_intents"]
    receipt_t, evidence_t = tables["program_call_receipts"], tables["program_overflow_evidence"]
    scope = _locked(session, scope_t, scope_t.c.id == scope_id)
    gate = _locked(session, gate_t, gate_t.c.scope_id == scope_id)
    if scope is None or gate is None or gate.mode != "active" or gate.cutover_batch_id is None:
        raise ValueError("gate unavailable")
    policy = _locked(session, policy_t, policy_t.c.scope_id == scope_id, policy_t.c.revision == gate.policy_revision)
    batch = _locked(session, batch_t, batch_t.c.scope_id == scope_id, batch_t.c.batch_id == gate.cutover_batch_id)
    if (batch is None or policy is None or batch.state != "complete" or batch.unmatched_count != 0
            or batch.source_count != batch.mapped_count or batch.policy_revision != gate.policy_revision
            or batch.policy_digest != gate.policy_digest or policy.policy_digest != gate.policy_digest
            or batch.snapshot_digest != gate.cutover_digest or batch.census_generation != gate.cutover_generation
            or batch.fence_digest != gate.cutover_fence_digest or batch.owner_epoch != gate.cutover_owner_epoch
            or not _is_lower_hex(gate.cutover_generation, 32) or not _is_lower_hex(gate.cutover_digest, 64)
            or not _is_lower_hex(gate.cutover_fence_digest, 64)):
        raise ValueError("invalid cutover binding")
    if (any(type(value) is not int or not 0 <= value <= _MAX_I64 for value in (
            gate.revision, gate.policy_revision, gate.cutover_owner_epoch, batch.policy_revision, batch.owner_epoch,
            policy.revision, scope.policy_revision)) or gate.policy_revision == 0 or policy.revision == 0
            or scope.policy_revision == 0):
        raise ValueError("invalid policy or gate revision")
    if (any(type(batch[name]) is not int or not 0 <= batch[name] <= _MAX_I64 for name in (
            "source_count", "mapped_count", "unmatched_count", "reserved_usd_micro", "settled_usd_micro",
            "unknown_usd_micro", "reserved_allowance_micro", "settled_allowance_micro", "unknown_allowance_micro",
            "held_slots", "unknown_slots"))):
        raise ValueError("invalid census facts")
    if (any(type(value) is not int or not 0 <= value <= _MAX_I64 for value in (
            scope.usd_limit_micro, scope.allowance_limit_micro, scope.slot_limit,
            policy.usd_limit_micro, policy.allowance_limit_micro, policy.slot_limit,
            scope.window_start_ms, scope.window_end_ms, policy.window_start_ms, policy.window_end_ms))
            or (scope.window_start_ms, scope.window_end_ms) != (policy.window_start_ms, policy.window_end_ms)
            or scope.policy_revision != policy.revision or scope.policy_digest != policy.policy_digest
            or scope.allowance_unit != SUPPORTED_ALLOWANCE_UNIT or policy.allowance_unit != SUPPORTED_ALLOWANCE_UNIT
            or policy.currency != "USD" or price_digest != policy.price_policy_digest
            or meter_digest != policy.meter_policy_digest or permit_digest != policy.permit_policy_digest
            or source_identity_digest != policy.source_identity_digest
            or not policy.window_start_ms <= observed_ms < policy.window_end_ms
            or valid_until_ms > policy.window_end_ms):
        raise ValueError("policy unavailable")
    link_t = tables["program_legacy_links"]
    # Discovery supplies candidate keys only; locked rows are rechecked below.
    links = session.execute(select(link_t).where(link_t.c.scope_id == scope_id,
                            link_t.c.batch_id == batch.batch_id)).mappings().all()
    if any(not _bounded_id(link.link_id) for link in links):
        raise ValueError("census source identity unavailable")
    links.sort(key=lambda link: link.link_id.encode("utf-8"))
    if len(links) != batch.source_count or any(digest(_baseline_payload(link)) != link.original_digest for link in links):
        raise ValueError("census source binding mismatch")
    if digest([_baseline_payload(link) for link in links]) != batch.snapshot_digest:
        raise ValueError("census snapshot mismatch")
    subject_ids = sorted({subject_id, *(link.subject_id for link in links)})
    registry_rows, subject_rows = {}, {}
    discovered_registry = {}
    for candidate in subject_ids:
        row = session.execute(select(registry_t).where(registry_t.c.scope_id == scope_id,
                              registry_t.c.subject_id == candidate)).mappings().one_or_none()
        if row is None or not _bounded_id(row.purpose):
            raise ValueError("subject registry unavailable")
        discovered_registry[candidate] = row
    registry_order = sorted(subject_ids, key=lambda candidate:
                            (discovered_registry[candidate].purpose.encode("utf-8"), candidate.encode("utf-8")))
    for candidate in registry_order:
        discovered = discovered_registry[candidate]
        registry_rows[candidate] = _locked(session, registry_t, registry_t.c.scope_id == scope_id,
            registry_t.c.purpose == discovered.purpose, registry_t.c.subject_id == candidate)
        if registry_rows[candidate] != discovered:
            raise ValueError("subject registry changed")
    for candidate in subject_ids:
        subject_rows[candidate] = _locked(session, subject_t, subject_t.c.scope_id == scope_id,
                                          subject_t.c.subject_id == candidate)
    registry, subject = registry_rows[subject_id], subject_rows[subject_id]
    if registry is None or subject is None or registry.purpose != purpose or subject.purpose != purpose or registry.binding_digest != subject.binding_digest:
        raise ValueError("subject unavailable")
    job_t = tables["jobs"]
    job_ids = sorted({*(link.job_id for link in links), *([subject_id] if purpose == "work" else [])})
    jobs = {job_id: _locked(session, job_t, job_t.c.id == job_id) for job_id in job_ids}
    if type(context) is not dict:
        raise ValueError("context unavailable")
    if purpose == "work":
        lease_t = tables["worker_execution_leases"]
        job = jobs[subject_id]
        lease = _locked(session, lease_t, lease_t.c.job_id == subject_id)
        attempt, context_digest = context.get("attempt_id"), context.get("lease_digest")
        if (job is None or lease is None or lease.phase != "started" or job.status != "running"
                or job.cancel_requested or lease.generation != job.generation or not _bounded_id(attempt)
                or type(lease.workload_id) is not str or _WORKLOAD_RE.fullmatch(lease.workload_id) is None
                or type(lease.instance_id) is not str or _INSTANCE_RE.fullmatch(lease.instance_id) is None
                or type(lease.generation) is not str or _GENERATION_RE.fullmatch(lease.generation) is None
                or type(lease.fence) is not str or _FENCE_RE.fullmatch(lease.fence) is None
                or job.lease_owner != execution_owner_key(workload_id=lease.workload_id, instance_id=lease.instance_id)
                or type(job.lease_until) not in (int, float) or not math.isfinite(job.lease_until)
                or job.lease_until != lease.lease_until or not _is_lower_hex(context_digest, 64)
                or type(lease.lease_until) not in (int, float) or not math.isfinite(lease.lease_until)
                or type(lease.execution_deadline) not in (int, float) or not math.isfinite(lease.execution_deadline)
                or min(lease.lease_until, lease.execution_deadline) * 1000 < valid_until_ms):
            raise ValueError("work context unavailable")
        fence_digest = hashlib.sha256(lease.fence.encode("utf-8")).hexdigest()
        run = job.resolved.get("repository_run") if type(job.resolved) is dict else None
        attempts = run.get("attempts") if type(run) is dict else None
        matching = [entry for entry in attempts if type(entry) is dict and entry.get("id") == attempt] if type(attempts) is list else []
        expected_context = digest({"job_id": job.id, "attempt_id": attempt, "workload_id": lease.workload_id,
                                   "instance_id": lease.instance_id, "generation": lease.generation,
                                   "fence_sha256": fence_digest})
        if (len(matching) != 1 or attempts[-1] is not matching[0] or matching[0].get("state") not in {"preparing", "running", "awaiting_activation"}
                or matching[0].get("fence_sha256") != fence_digest or context_digest != expected_context):
            raise ValueError("work context mismatch")
        fields = dict(work_job_id=subject_id, work_attempt_id=attempt, work_lease_digest=context_digest)
    else:
        session_t, epoch_t = tables["collection_sessions"], tables["collection_epochs"]
        guard_t = tables["collection_epoch_guards"]
        collection = _locked(session, session_t, session_t.c.id == subject_id)
        guard = _locked(session, guard_t, guard_t.c.session_id == subject_id)
        epoch_value, context_digest = context.get("epoch"), context.get("context_digest")
        if type(epoch_value) is not int or epoch_value <= 0 or not _is_lower_hex(context_digest, 64):
            raise ValueError("collection context unavailable")
        epoch = _locked(session, epoch_t, epoch_t.c.session_id == subject_id, epoch_t.c.epoch == epoch_value)
        if (collection is None or guard is None or epoch is None or collection.scope_id != scope_id
                or collection.current_epoch != epoch_value or guard.active_epoch != epoch_value
                or collection.state not in {"admitted", "active"} or epoch.epoch_purpose != "execution"
                or epoch.state != "active" or min(collection.deadline_ms, epoch.deadline_ms) < valid_until_ms):
            raise ValueError("collection context unavailable")
        expected_context = digest({"scope_id": scope_id, "session_id": subject_id, "epoch": epoch_value,
                                   "session_digest": collection.immutable_digest, "authorization_digest": epoch.authorization_digest})
        if context_digest != expected_context:
            raise ValueError("collection context mismatch")
        fields = dict(collection_session_id=subject_id, collection_epoch=epoch_value, collection_context_digest=context_digest)
    reservations = _locked_scope_rows(session, reservation_t, scope_id, ("call_id",))
    slots = _locked_scope_rows(session, slot_t, scope_id, ("slot_id",))
    intents = _locked_scope_rows(session, intent_t, scope_id, ("call_id",))
    receipts = _locked_scope_rows(session, receipt_t, scope_id, ("call_id", "receipt_id"))
    policies = {row.revision: row for row in session.execute(select(policy_t).where(
        policy_t.c.scope_id == scope_id).order_by(policy_t.c.revision)).mappings()}
    for historical in intents:
        historical_policy = policies.get(historical.policy_revision)
        if (historical_policy is None or historical.policy_digest != historical_policy.policy_digest
                or not _bounded_id(historical.source_code) or not _bounded_id(historical.issuer_code)
                or not _is_lower_hex(historical.source_identity_digest, 64)
                or historical.source_identity_digest != historical_policy.source_identity_digest
                or historical.source_identity_digest != _source_identity_digest(historical.source_code, historical.issuer_code)
                or historical.price_digest != historical_policy.price_policy_digest
                or historical.meter_digest != historical_policy.meter_policy_digest
                or historical.permit_digest != historical_policy.permit_policy_digest):
            raise ValueError("historical intent policy binding mismatch")
    evidence = session.execute(select(evidence_t.c.evidence_id).where(evidence_t.c.scope_id == scope_id)
                               .with_for_update()).first()
    if evidence is not None:
        raise ValueError("overflow evidence requires reconciliation")
    locked_links = [_locked(session, link_t, link_t.c.scope_id == scope_id,
                            link_t.c.batch_id == batch.batch_id, link_t.c.link_id == link.link_id)
                    for link in links]
    current_links = session.execute(select(link_t).where(link_t.c.scope_id == scope_id,
                                    link_t.c.batch_id == batch.batch_id)).mappings().all()
    if any(not _bounded_id(link.link_id) for link in current_links):
        raise ValueError("census source identity unavailable")
    current_links.sort(key=lambda link: link.link_id.encode("utf-8"))
    if locked_links != links or current_links != links:
        raise ValueError("census source binding changed")
    links = locked_links
    # Compare immutable baseline values with its retained mappings, then count
    # current obligations below exactly once. The fixture digest validates
    # binding only and is not authenticated source evidence.
    totals = {name: 0 for name in ("reserved_usd_micro", "settled_usd_micro", "unknown_usd_micro",
        "reserved_allowance_micro", "settled_allowance_micro", "unknown_allowance_micro", "held_slots", "unknown_slots")}
    reservations_by_call = {row.call_id: row for row in reservations}
    slots_by_id = {row.slot_id: row for row in slots}
    for link in links:
        linked_reservation = reservations_by_call.get(link.call_id)
        linked_slot = slots_by_id.get(link.slot_id)
        _validate_locked_legacy_binding(link, batch, registry_rows.get(link.subject_id), jobs.get(link.job_id),
                                        linked_reservation, linked_slot)
        values = (link.baseline_reserved_usd_micro, link.baseline_reserved_allowance_micro,
                  link.baseline_actual_usd_micro, link.baseline_actual_allowance_micro)
        if any(value is not None and (type(value) is not int or not 0 <= value <= _MAX_I64) for value in values):
            raise ValueError("invalid census baseline amount")
        if values[0] is None or values[1] is None or ((values[2] is None) != (values[3] is None)):
            raise ValueError("incomplete census baseline amount")
        if link.baseline_state == "reserved" and link.baseline_slot_state == "held" and values[2] is None:
            kind, pair = "reserved", values[:2]
            totals["held_slots"] += 1
        elif link.baseline_state == "settled" and link.baseline_slot_state == "released" and values[2] is not None:
            kind, pair = "settled", values[2:]
        elif link.baseline_state == "unknown" and link.baseline_slot_state == "unknown":
            kind = "unknown"
            pair = values[:2] if values[2] is None else (max(values[0], values[2]), max(values[1], values[3]))
            totals["unknown_slots"] += 1
        else:
            raise ValueError("invalid census baseline state")
        totals[kind + "_usd_micro"] += pair[0]
        totals[kind + "_allowance_micro"] += pair[1]
    if any(total > _MAX_I64 or batch[name] != total for name, total in totals.items()):
        raise ValueError("census total mismatch")
    legacy_calls = {link.call_id for link in links}
    reservation_ids = {row.call_id for row in reservations}
    if any(row.call_id not in reservation_ids for row in receipts):
        raise ValueError("orphan receipt")
    by_call = {}
    intents_by_call = {row.call_id: row for row in intents}
    receipts_by_call = {}
    unknown_receipts_present = False
    for row in receipts:
        receipts_by_call.setdefault(row.call_id, []).append(row)
        receipt_intent = intents_by_call.get(row.call_id)
        if (receipt_intent is None or row.source_code != receipt_intent.source_code
                or row.issuer_code != receipt_intent.issuer_code
                or row.source_identity_digest != receipt_intent.source_identity_digest
                or row.source_identity_digest != _source_identity_digest(row.source_code, row.issuer_code)):
            raise ValueError("receipt source binding mismatch")
        if (row.currency != "USD" or row.allowance_unit != SUPPORTED_ALLOWANCE_UNIT
                or not _bounded_id(row.receipt_id) or not _bounded_id(row.source_code) or not _bounded_id(row.issuer_code)
                or not _is_lower_hex(row.source_digest, 64) or not _is_lower_hex(row.receipt_digest, 64)
                or type(row.custody_ref) is not str or not 1 <= len(row.custody_ref.encode("utf-8")) <= 512
                or type(row.custody_bytes) is not int or not 1 <= row.custody_bytes <= 1048576
                or type(row.observed_ms) is not int or not 0 <= row.observed_ms <= _MAX_I64):
            raise ValueError("invalid stored receipt facts")
        if row.outcome == "unknown":
            expected = digest(dict(scope_id=row.scope_id, call_id=row.call_id, receipt_id=row.receipt_id,
                actual_usd=None, actual_allowance=None, currency=row.currency, allowance_unit=row.allowance_unit,
                source_code=row.source_code, issuer_code=row.issuer_code, source_digest=row.source_digest,
                source_identity_digest=row.source_identity_digest, custody_ref=row.custody_ref, custody_bytes=row.custody_bytes, observed_ms=row.observed_ms))
            if (row.actual_usd_micro is not None or row.actual_allowance_micro is not None
                    or row.evidence_id is not None or row.receipt_digest != expected):
                raise ValueError("invalid stored unknown receipt")
            unknown_receipts_present = True
            continue
        if row.outcome != "known":
            raise ValueError("invalid stored receipt outcome")
        if row.outcome == "known":
            if row.actual_usd_micro is None or row.actual_allowance_micro is None:
                raise ValueError("unrepresentable receipt requires reconciliation")
            expected = digest(dict(scope_id=row.scope_id, call_id=row.call_id, receipt_id=row.receipt_id,
                actual_usd=str(row.actual_usd_micro), actual_allowance=str(row.actual_allowance_micro),
                currency=row.currency, allowance_unit=row.allowance_unit, source_code=row.source_code,
                issuer_code=row.issuer_code, source_digest=row.source_digest, source_identity_digest=row.source_identity_digest, custody_ref=row.custody_ref,
                custody_bytes=row.custody_bytes, observed_ms=row.observed_ms))
            if row.receipt_digest != expected:
                raise ValueError("stored receipt binding mismatch")
            pair = (row.actual_usd_micro, row.actual_allowance_micro)
            if row.call_id in by_call and by_call[row.call_id] != pair:
                raise ValueError("conflicting receipt")
            by_call[row.call_id] = pair
    if len(slots) != len(reservations):
        raise ValueError("incomplete scope obligations")
    obligations = []
    for row in reservations:
        matching_slots = [item for item in slots if item.call_id == row.call_id]
        matching_intents = [item for item in intents if item.call_id == row.call_id]
        if len(matching_slots) != 1 or len(matching_intents) > 1:
            raise ValueError("incomplete scope obligations")
        held = matching_slots[0]
        matching_links = [item for item in links if item.call_id == row.call_id]
        if matching_links:
            baseline = matching_links[0]
            if ((row.reserved_usd_micro, row.reserved_allowance_micro) !=
                    (baseline.baseline_reserved_usd_micro, baseline.baseline_reserved_allowance_micro)):
                raise ValueError("legacy reserve changed")
            if baseline.baseline_state == "unknown":
                if row.state != "unknown":
                    raise ValueError("unknown baseline requires reconciliation")
                if (baseline.baseline_actual_usd_micro is not None and
                        (row.actual_usd_micro is None or row.actual_allowance_micro is None
                         or row.actual_usd_micro < baseline.baseline_actual_usd_micro
                         or row.actual_allowance_micro < baseline.baseline_actual_allowance_micro)):
                    raise ValueError("unknown baseline actual regressed")
            if baseline.baseline_state == "settled" and (
                    row.state != "settled" or (row.actual_usd_micro, row.actual_allowance_micro) !=
                    (baseline.baseline_actual_usd_micro, baseline.baseline_actual_allowance_micro)):
                raise ValueError("settled baseline changed")
        if not matching_intents:
            if len(matching_links) != 1:
                raise ValueError("incomplete scope obligations")
            baseline = matching_links[0]
            if ((row.state, held.state, row.reserved_usd_micro, row.reserved_allowance_micro,
                 row.actual_usd_micro, row.actual_allowance_micro) !=
                    (baseline.baseline_state, baseline.baseline_slot_state, baseline.baseline_reserved_usd_micro,
                     baseline.baseline_reserved_allowance_micro, baseline.baseline_actual_usd_micro,
                     baseline.baseline_actual_allowance_micro)):
                raise ValueError("unowned legacy obligation changed")
            intent = None
        else:
            intent = matching_intents[0]
        call_receipts = receipts_by_call.get(row.call_id, [])
        if (len(call_receipts) > 2 or sum(item.outcome == "known" for item in call_receipts) > 1
                or sum(item.outcome == "unknown" for item in call_receipts) > 1
                or (row.state == "reserved" and call_receipts)
                or (row.state != "unknown" and any(item.outcome == "unknown" for item in call_receipts))):
            raise ValueError("incoherent receipt set")
        if row.state == "settled" and intent is not None and (len(call_receipts) != 1 or call_receipts[0].outcome != "known"
                or (call_receipts[0].actual_usd_micro, call_receipts[0].actual_allowance_micro)
                   != (row.actual_usd_micro, row.actual_allowance_micro)):
            raise ValueError("settled receipt mismatch")
        if (held.subject_id != row.subject_id or (intent is not None and
                (intent.subject_id != row.subject_id or intent.purpose != row.purpose
                 or (row.state, held.state, intent.state) not in {
                    ("reserved", "held", "committed"), ("unknown", "unknown", "unknown"),
                    ("settled", "released", "known")}))):
            raise ValueError("incoherent scope obligation")
        amounts = (row.reserved_usd_micro, row.reserved_allowance_micro)
        if any(type(value) is not int or not 0 <= value <= _MAX_I64 for value in amounts):
            raise ValueError("invalid stored reserve")
        actual = (row.actual_usd_micro, row.actual_allowance_micro)
        if (actual[0] is None) != (actual[1] is None):
            raise ValueError("invalid stored actual pair")
        if row.state == "unknown" and row.call_id in by_call:
            observed = by_call[row.call_id]
            if actual[0] is not None and actual != observed:
                raise ValueError("conflicting stored and receipt actuals")
            actual = observed
        if any(value is not None and (type(value) is not int or not 0 <= value <= _MAX_I64) for value in actual):
            raise ValueError("invalid stored actual")
        obligations.append((row.call_id, row.state, str(amounts[0]), str(amounts[1]),
                            None if actual[0] is None else str(actual[0]), None if actual[1] is None else str(actual[1])))
    try:
        total_usd, total_allowance = _obligation_totals(obligations)
    except _AggregateOverflow as overflow:
        # This is an observation of existing liabilities, never a prospective
        # amount. Confirmation below is a synthetic fixture input only.
        snapshot_body = canonical(obligations)
        snapshot_digest = hashlib.sha256(snapshot_body).hexdigest()
        if (type(aggregate_custody) is not dict
                or set(aggregate_custody) != {"evidence_id", "snapshot_digest", "source_digest", "custody_ref", "custody_bytes"}
                or not _bounded_id(aggregate_custody["evidence_id"])
                or aggregate_custody["snapshot_digest"] != snapshot_digest
                or aggregate_custody["source_digest"] != snapshot_digest
                or type(aggregate_custody["custody_ref"]) is not str
                or not 1 <= len(aggregate_custody["custody_ref"].encode("utf-8")) <= 512
                or type(aggregate_custody["custody_bytes"]) is not int
                or not 1 <= aggregate_custody["custody_bytes"] <= 1048576
                or aggregate_custody["custody_bytes"] != len(snapshot_body)
                or type(gate.revision) is not int or not 0 <= gate.revision < _MAX_I64):
            raise ValueError("overflow custody unavailable") from overflow
        event = database.audit(session, "program-ledger-fixture", "aggregate-overflow", scope_id,
                               {"snapshot_digest": snapshot_digest})
        session.flush()
        session.execute(insert(evidence_t), dict(scope_id=scope_id, evidence_id=aggregate_custody["evidence_id"],
            kind="aggregate", policy_revision=policy.revision, policy_digest=policy.policy_digest,
            snapshot_digest=snapshot_digest, usd_decimal=str(overflow.usd), allowance_decimal=str(overflow.allowance),
            currency="USD", allowance_unit=SUPPORTED_ALLOWANCE_UNIT, source_digest=snapshot_digest,
            custody_ref=aggregate_custody["custody_ref"], custody_bytes=aggregate_custody["custody_bytes"],
            observed_ms=observed_ms, audit_sequence=event.sequence))
        from sqlalchemy import update
        session.execute(update(gate_t).where(gate_t.c.scope_id == scope_id)
                        .values(mode="inhibited", revision=gate.revision + 1, audit_sequence=event.sequence))
        return "aggregate_overflow_unverified", None
    if unknown_receipts_present:
        raise ValueError("unknown receipt requires reconciliation")
    existing = [row for row in intents if row.purpose == purpose and row.subject_id == subject_id
                and row.idempotency_key == idempotency_key]
    if existing:
        row = existing[0]
        original = next((item for item in reservations if item.call_id == row.call_id), None)
        original_slots = [item for item in slots if item.call_id == row.call_id]
        if (original is None or len(original_slots) != 1 or row.request_digest != request_digest
                or row.effect_key != effect_key or row.policy_revision != policy.revision
                or row.policy_digest != policy.policy_digest or row.price_digest != price_digest
                or row.meter_digest != meter_digest or row.permit_digest != permit_digest
                or row.source_code != source_code or row.issuer_code != issuer_code
                or row.source_identity_digest != source_identity_digest
                or row.observed_ms != observed_ms or row.valid_until_ms != valid_until_ms
                or original.reserved_usd_micro != usd_micro or original.reserved_allowance_micro != allowance_micro
                or any(row[key] != value for key, value in fields.items())):
            raise ValueError("idempotency conflict")
        return "existing", row.call_id
    if any(row.call_id == call_id or (row.subject_id == subject_id and row.effect_key == effect_key) for row in reservations):
        raise ValueError("call or effect conflict")
    if any(row.slot_id == slot_id for row in slots):
        raise ValueError("slot conflict")
    if total_usd + usd_micro > _MAX_I64 or total_allowance + allowance_micro > _MAX_I64:
        raise ValueError("post-reservation total overflow")
    if (total_usd + usd_micro > min(scope.usd_limit_micro, policy.usd_limit_micro)
            or total_allowance + allowance_micro > min(scope.allowance_limit_micro, policy.allowance_limit_micro)
            or sum(row.state in {"held", "unknown"} for row in slots) + 1 > min(scope.slot_limit, policy.slot_limit)):
        raise ValueError("capacity unavailable")
    event = database.audit(session, "program-ledger-fixture", "prepare", call_id, {"scope_id": scope_id})
    session.flush()
    session.execute(insert(reservation_t), dict(scope_id=scope_id, call_id=call_id, subject_id=subject_id,
        purpose=purpose, effect_key=effect_key, reserved_usd_micro=usd_micro, reserved_allowance_micro=allowance_micro,
        permit_digest=permit_digest, meter_digest=meter_digest, price_digest=price_digest,
        permit_expires_ms=valid_until_ms, state="reserved", audit_sequence=event.sequence))
    session.execute(insert(slot_t), dict(scope_id=scope_id, slot_id=slot_id, subject_id=subject_id,
        call_id=call_id, state="held", audit_sequence=event.sequence))
    session.execute(insert(intent_t), dict(scope_id=scope_id, call_id=call_id, subject_id=subject_id, purpose=purpose,
        effect_key=effect_key, idempotency_key=idempotency_key, request_digest=request_digest,
        policy_revision=policy.revision, policy_digest=policy.policy_digest, price_digest=price_digest,
        meter_digest=meter_digest, permit_digest=permit_digest, source_code=source_code, issuer_code=issuer_code,
        source_identity_digest=source_identity_digest, observed_ms=observed_ms, valid_until_ms=valid_until_ms,
        state="committed", audit_sequence=event.sequence, **fields))
    return "created", call_id


def _record_known_in_transaction(database, session, tables, *, scope_id, call_id, receipt_id,
                                 receipt_digest, actual_usd, actual_allowance, currency,
                                 allowance_unit, source_code, issuer_code, source_digest, source_identity_digest,
                                 custody_ref, custody_bytes, observed_ms):
    """Record a synthetic known outcome against an existing immutable intent.

    The caller supplies test-only already-validated fixture values. This does
    not authenticate a receipt, confirm custody, dispatch, or enable a gate.
    """
    if not _owner_transaction_is_active(session):
        raise ValueError("owner transaction unavailable")
    from sqlalchemy import update
    from .program_ledger import _parse_micro_units
    if (not all(_bounded_id(value) for value in (scope_id, call_id, receipt_id, source_code, issuer_code))
            or not _is_lower_hex(receipt_digest, 64) or not _is_lower_hex(source_digest, 64)
            or not _is_lower_hex(source_identity_digest, 64)
            or source_identity_digest != _source_identity_digest(source_code, issuer_code)
            or currency != "USD" or allowance_unit != SUPPORTED_ALLOWANCE_UNIT
            or type(custody_ref) is not str or not 1 <= len(custody_ref.encode("utf-8")) <= 512
            or type(custody_bytes) is not int or not 1 <= custody_bytes <= 1048576
            or type(observed_ms) is not int or not 0 <= observed_ms <= _MAX_I64):
        raise ValueError("invalid synthetic receipt")
    actual_u, actual_a = _parse_micro_units(actual_usd), _parse_micro_units(actual_allowance)
    expected_receipt = digest(dict(scope_id=scope_id, call_id=call_id, receipt_id=receipt_id,
        actual_usd=actual_usd, actual_allowance=actual_allowance, currency=currency,
        allowance_unit=allowance_unit, source_code=source_code, issuer_code=issuer_code,
        source_digest=source_digest, source_identity_digest=source_identity_digest,
        custody_ref=custody_ref, custody_bytes=custody_bytes, observed_ms=observed_ms))
    if receipt_digest != expected_receipt:
        raise ValueError("receipt binding mismatch")
    scope_t, gate_t = tables["program_budget_scopes"], tables["program_gate_state"]
    subject_t, reservation_t = tables["program_budget_subjects"], tables["program_reservations"]
    slot_t, intent_t = tables["program_slots"], tables["program_call_intents"]
    receipt_t, evidence_t = tables["program_call_receipts"], tables["program_overflow_evidence"]
    scope = _locked(session, scope_t, scope_t.c.id == scope_id)
    gate = _locked(session, gate_t, gate_t.c.scope_id == scope_id)
    if scope is None:
        raise ValueError("scope unavailable")
    # Resolve immutable policy identity before taking later lock classes; the
    # locked intent must still match this discovery before it is used.
    policy_t = tables["program_policy_revisions"]
    policy_key = session.execute(select(intent_t.c.policy_revision, intent_t.c.policy_digest).where(
        intent_t.c.scope_id == scope_id, intent_t.c.call_id == call_id)).one_or_none()
    if policy_key is None:
        raise ValueError("receipt policy unavailable")
    policy = _locked(session, policy_t, policy_t.c.scope_id == scope_id,
                     policy_t.c.revision == policy_key.policy_revision,
                     policy_t.c.policy_digest == policy_key.policy_digest)
    candidate = session.execute(select(reservation_t.c.subject_id).where(
        reservation_t.c.scope_id == scope_id, reservation_t.c.call_id == call_id)).scalar_one_or_none()
    if candidate is None:
        raise ValueError("reservation unavailable")
    subject = _locked(session, subject_t, subject_t.c.scope_id == scope_id, subject_t.c.subject_id == candidate)
    reservation = _locked(session, reservation_t, reservation_t.c.scope_id == scope_id, reservation_t.c.call_id == call_id)
    slot = _locked(session, slot_t, slot_t.c.scope_id == scope_id, slot_t.c.call_id == call_id)
    intent = _locked(session, intent_t, intent_t.c.scope_id == scope_id, intent_t.c.call_id == call_id)
    if (subject is None or reservation is None or slot is None or intent is None
            or reservation.subject_id != candidate or slot.subject_id != candidate or intent.subject_id != candidate
            or reservation.purpose != subject.purpose or intent.purpose != subject.purpose):
        raise ValueError("obligation binding unavailable")
    if (policy is None or (intent.policy_revision, intent.policy_digest) != tuple(policy_key)
            or policy.currency != currency or policy.allowance_unit != allowance_unit
            or intent.source_code != source_code or intent.issuer_code != issuer_code
            or intent.source_identity_digest != source_identity_digest or policy.source_identity_digest != source_identity_digest
            or intent.price_digest != policy.price_policy_digest or intent.meter_digest != policy.meter_policy_digest
            or intent.permit_digest != policy.permit_policy_digest):
        raise ValueError("receipt source or policy unavailable")
    state = (reservation.state, slot.state, intent.state)
    if state not in {("reserved", "held", "committed"), ("unknown", "unknown", "unknown"),
                     ("settled", "released", "known")}:
        raise ValueError("incoherent obligation state")
    if any(type(value) is not int or not 0 <= value <= _MAX_I64
           for value in (reservation.reserved_usd_micro, reservation.reserved_allowance_micro)):
        raise ValueError("invalid stored reserve")
    prior_actual = (reservation.actual_usd_micro, reservation.actual_allowance_micro)
    if (prior_actual[0] is None) != (prior_actual[1] is None):
        raise ValueError("invalid stored actual pair")
    if any(value is not None and (type(value) is not int or not 0 <= value <= _MAX_I64) for value in prior_actual):
        raise ValueError("invalid stored actual")
    if state[0] == "reserved" and prior_actual[0] is not None:
        raise ValueError("reserved obligation has actuals")
    if state[0] == "settled" and prior_actual[0] is None:
        raise ValueError("settled obligation lacks actuals")
    if prior_actual[0] is not None and prior_actual != (actual_u, actual_a):
        raise ValueError("conflicting prior actual")
    audit_table = tables["audit_events"]
    audit_ids = {reservation.audit_sequence, slot.audit_sequence, intent.audit_sequence}
    if gate is not None:
        audit_ids.add(gate.audit_sequence)
        gate_cutover = (gate.cutover_batch_id, gate.cutover_digest, gate.cutover_generation,
                        gate.cutover_fence_digest, gate.cutover_owner_epoch)
        if (gate.mode not in {"disabled", "census", "active", "inhibited"} or type(gate.revision) is not int
                or not 0 <= gate.revision <= _MAX_I64 or not _is_lower_hex(gate.policy_digest, 64)
                or (any(value is None for value in gate_cutover) and any(value is not None for value in gate_cutover))
                or (gate.mode == "active" and gate_cutover[0] is None)):
            raise ValueError("invalid persisted gate")
        if gate_cutover[0] is not None and (not _is_lower_hex(gate_cutover[1], 64)
                or not _is_lower_hex(gate_cutover[2], 32) or not _is_lower_hex(gate_cutover[3], 64)
                or type(gate_cutover[4]) is not int or not 0 <= gate_cutover[4] <= _MAX_I64):
            raise ValueError("invalid persisted gate")
    if (any(type(value) is not int or value <= 0 for value in audit_ids)
            or set(session.execute(select(audit_table.c.sequence).where(audit_table.c.sequence.in_(audit_ids))).scalars()) != audit_ids):
        raise ValueError("obligation audit unavailable")
    receipts = session.execute(select(receipt_t).where(receipt_t.c.scope_id == scope_id,
        receipt_t.c.call_id == call_id).order_by(receipt_t.c.receipt_id).with_for_update()).mappings().all()
    if len(receipts) > 2 or sum(row.outcome == "known" for row in receipts) > 1 or sum(row.outcome == "unknown" for row in receipts) > 1:
        raise ValueError("conflicting receipt set")
    matched_receipt = None
    for prior in receipts:
        receipt_audit = prior.audit_sequence
        if (type(receipt_audit) is not int or receipt_audit <= 0
                or session.execute(select(audit_table.c.sequence).where(audit_table.c.sequence == receipt_audit)).scalar_one_or_none() is None):
            raise ValueError("receipt audit unavailable")
        if prior.outcome == "unknown":
            expected_unknown = digest(dict(scope_id=prior.scope_id, call_id=prior.call_id, receipt_id=prior.receipt_id,
                actual_usd=None, actual_allowance=None, currency=prior.currency, allowance_unit=prior.allowance_unit,
                source_code=prior.source_code, issuer_code=prior.issuer_code, source_digest=prior.source_digest,
                source_identity_digest=prior.source_identity_digest, custody_ref=prior.custody_ref, custody_bytes=prior.custody_bytes, observed_ms=prior.observed_ms))
            if (state[0] != "unknown" or prior.actual_usd_micro is not None or prior.actual_allowance_micro is not None
                    or prior.evidence_id is not None or prior.receipt_digest != expected_unknown
                    or prior.currency != currency or prior.allowance_unit != allowance_unit
                    or prior.source_code != intent.source_code or prior.issuer_code != intent.issuer_code
                    or prior.source_identity_digest != intent.source_identity_digest
                    or not all(_bounded_id(value) for value in (prior.receipt_id, prior.source_code, prior.issuer_code))
                    or not _is_lower_hex(prior.source_digest, 64) or type(prior.observed_ms) is not int
                    or not 0 <= prior.observed_ms <= _MAX_I64 or type(prior.custody_ref) is not str
                    or not 1 <= len(prior.custody_ref.encode("utf-8")) <= 512
                    or type(prior.custody_bytes) is not int or not 1 <= prior.custody_bytes <= 1048576):
                raise ValueError("invalid unknown receipt")
            continue
        if prior.receipt_digest == receipt_digest:
            if (prior.outcome != "known" or prior.receipt_id != receipt_id or prior.currency != currency
                    or prior.allowance_unit != allowance_unit or prior.source_code != source_code
                    or prior.issuer_code != issuer_code or prior.observed_ms != observed_ms or prior.custody_ref != custody_ref
                    or prior.source_digest != source_digest or prior.source_identity_digest != source_identity_digest
                    or prior.custody_bytes != custody_bytes or state[0] == "reserved"):
                raise ValueError("receipt conflict")
            if prior.evidence_id is None:
                if (prior.actual_usd_micro, prior.actual_allowance_micro) != (actual_u, actual_a):
                    raise ValueError("receipt conflict")
            else:
                evidence = _locked(session, evidence_t, evidence_t.c.scope_id == scope_id,
                                   evidence_t.c.evidence_id == prior.evidence_id)
                if (evidence is None or evidence.kind != "call" or evidence.call_id != call_id
                        or evidence.receipt_digest != receipt_digest or evidence.currency != currency
                        or evidence.allowance_unit != allowance_unit or evidence.observed_ms != observed_ms
                        or evidence.audit_sequence != prior.audit_sequence or evidence.source_digest != source_digest
                        or evidence.custody_ref != custody_ref or evidence.custody_bytes != custody_bytes
                        or (evidence.usd_decimal, evidence.allowance_decimal) != (actual_usd, actual_allowance)):
                    raise ValueError("receipt conflict")
            matched_receipt = prior.receipt_id
        elif prior.outcome != "known":
            raise ValueError("invalid receipt outcome")
    if matched_receipt is not None:
        return "existing", matched_receipt
    if any(prior.outcome == "known" for prior in receipts):
        raise ValueError("receipt conflict")
    if state not in {("reserved", "held", "committed"), ("unknown", "unknown", "unknown")}:
        raise ValueError("incoherent obligation state")
    if any(type(value) is not int or not 0 <= value <= _MAX_I64
           for value in (reservation.reserved_usd_micro, reservation.reserved_allowance_micro)):
        raise ValueError("invalid stored reserve")
    if (reservation.actual_usd_micro is None) != (reservation.actual_allowance_micro is None):
        raise ValueError("invalid stored actual pair")
    overrun = actual_u > reservation.reserved_usd_micro or actual_a > reservation.reserved_allowance_micro
    overflow = actual_u > _MAX_I64 or actual_a > _MAX_I64
    unresolved = overrun or reservation.state == "unknown"
    if unresolved and gate is not None and gate.mode != "inhibited" and gate.revision >= _MAX_I64:
        raise ValueError("gate revision exhausted")
    event = database.audit(session, "program-ledger-fixture", "record-known", call_id, {"scope_id": scope_id})
    session.flush()
    evidence_id = None
    if overflow:
        evidence_id = receipt_id
        session.execute(insert(evidence_t), dict(scope_id=scope_id, evidence_id=evidence_id, kind="call",
            call_id=call_id, receipt_digest=receipt_digest, usd_decimal=actual_usd, allowance_decimal=actual_allowance,
            currency=currency, allowance_unit=allowance_unit, source_digest=source_digest, custody_ref=custody_ref,
            custody_bytes=custody_bytes, observed_ms=observed_ms, audit_sequence=event.sequence))
        session.flush()
    session.execute(insert(receipt_t), dict(scope_id=scope_id, call_id=call_id, receipt_id=receipt_id,
        receipt_digest=receipt_digest, outcome="known", actual_usd_micro=None if overflow else actual_u,
        actual_allowance_micro=None if overflow else actual_a, evidence_id=evidence_id, currency=currency,
        allowance_unit=allowance_unit, source_code=source_code, issuer_code=issuer_code,
        observed_ms=observed_ms, custody_ref=custody_ref, source_digest=source_digest,
        source_identity_digest=source_identity_digest, custody_bytes=custody_bytes, audit_sequence=event.sequence))
    next_state = "unknown" if unresolved else "settled"
    session.execute(update(reservation_t).where(reservation_t.c.scope_id == scope_id, reservation_t.c.call_id == call_id)
        .values(state=next_state, actual_usd_micro=None if overflow else actual_u,
                actual_allowance_micro=None if overflow else actual_a, audit_sequence=event.sequence))
    session.execute(update(slot_t).where(slot_t.c.scope_id == scope_id, slot_t.c.call_id == call_id)
        .values(state="unknown" if unresolved else "released", audit_sequence=event.sequence))
    session.execute(update(intent_t).where(intent_t.c.scope_id == scope_id, intent_t.c.call_id == call_id)
        .values(state="unknown" if unresolved else "known", audit_sequence=event.sequence))
    if unresolved and gate is not None and gate.mode != "inhibited":
        session.execute(update(gate_t).where(gate_t.c.scope_id == scope_id)
            .values(mode="inhibited", revision=gate.revision + 1, audit_sequence=event.sequence))
    return ("known_unresolved_unverified" if unresolved else "known_settled_unverified"), receipt_id
