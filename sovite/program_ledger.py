"""Dormant program ledger boundary.

The trusted census and admission authority have not been implemented.  This
module has no provider, transport, or configuration-dependent fallback.
"""


class ProgramLedgerUnavailable(RuntimeError):
    """Raised when a ledger operation requires unavailable authority."""


def prepare():
    """Refuse admission until a reviewed census and authority are available."""
    raise ProgramLedgerUnavailable("program ledger admission is unavailable")


_MAX_AMOUNT_DIGITS = 128
_MAX_CANONICAL_AMOUNT = 10 ** _MAX_AMOUNT_DIGITS - 1
_MAX_I64 = (1 << 63) - 1


class _AggregateOverflow(ValueError):
    """Exact bounded totals cannot be represented by the i64 ledger columns."""

    def __init__(self, usd, allowance):
        super().__init__("aggregate exceeds signed 64-bit representation")
        self.usd = usd
        self.allowance = allowance


def _is_lower_hex(value, length):
    return type(value) is str and len(value) == length and all(ch in "0123456789abcdef" for ch in value)


def _parse_micro_units(value: str) -> int:
    """Parse a complete bounded canonical integer; this authenticates nothing."""
    if (type(value) is not str or not 1 <= len(value) <= _MAX_AMOUNT_DIGITS
            or any(ch < "0" or ch > "9" for ch in value)
            or (len(value) > 1 and value[0] == "0")):
        raise ValueError("invalid canonical micro-unit amount")
    return int(value)


def _obligation_totals(obligations):
    """Count identified held obligations once; caller must verify all evidence.

    Each item is (identity, state, reserved USD, reserved allowance, actual USD
    or None, actual allowance or None). Inputs are complete canonical strings.
    Values outside the bounded exact representation raise and cannot grant
    capacity. This calculation does not provide admission or persistence.
    """
    seen = set()
    total_usd = total_allowance = 0
    for identity, state, reserved_usd, reserved_allowance, actual_usd, actual_allowance in obligations:
        if type(identity) is not str or not identity or identity in seen:
            raise ValueError("duplicate or invalid obligation identity")
        seen.add(identity)
        usd = _parse_micro_units(reserved_usd)
        allowance = _parse_micro_units(reserved_allowance)
        if (actual_usd is None) != (actual_allowance is None):
            raise ValueError("incomplete actual amount pair")
        if state not in {"reserved", "unknown", "settled"}:
            raise ValueError("invalid obligation state")
        if state == "reserved" and actual_usd is not None:
            raise ValueError("reserved obligation cannot assert actuals")
        if state == "settled" and actual_usd is None:
            raise ValueError("settled obligation requires actuals")
        if actual_usd is not None:
            parsed_usd = _parse_micro_units(actual_usd)
            parsed_allowance = _parse_micro_units(actual_allowance)
            if state == "settled":
                usd, allowance = parsed_usd, parsed_allowance
            else:
                usd, allowance = max(usd, parsed_usd), max(allowance, parsed_allowance)
        total_usd += usd
        total_allowance += allowance
        if total_usd > _MAX_CANONICAL_AMOUNT or total_allowance > _MAX_CANONICAL_AMOUNT:
            raise ValueError("aggregate exceeds canonical bound")
    if total_usd > _MAX_I64 or total_allowance > _MAX_I64:
        raise _AggregateOverflow(total_usd, total_allowance)
    return total_usd, total_allowance


def _mark_unknown_in_transaction(database, session, tables, *, scope_id, call_id,
                                 audit_detail=None):
    """Conservatively retain an existing obligation inside the owner transaction.

    The caller must already hold owner_transaction's audit-head lock. This
    internal storage operation grants no admission, clearance, or replay.
    Its return describes rows within this transaction only: it does not prove
    commit, authority, validated cutover, or durable inhibition.
    """
    from sqlalchemy import select, update
    from .collection_transaction import _owner_transaction_is_active

    if not _owner_transaction_is_active(session):
        raise ValueError("owner transaction unavailable")

    scope = tables["program_budget_scopes"]
    gate = tables["program_gate_state"]
    subject = tables["program_budget_subjects"]
    reservation = tables["program_reservations"]
    slot = tables["program_slots"]
    intent = tables["program_call_intents"]
    locked_scope = session.execute(select(scope).where(scope.c.id == scope_id).with_for_update()).mappings().one_or_none()
    if locked_scope is None:
        raise ValueError("scope unavailable")
    locked_gate = session.execute(select(gate).where(gate.c.scope_id == scope_id).with_for_update()).mappings().one_or_none()
    # This non-authoritative lookup only locates the subject to lock. Recheck
    # every binding after acquiring the subject and obligation row locks.
    candidate = session.execute(select(reservation.c.subject_id).where(
        reservation.c.scope_id == scope_id, reservation.c.call_id == call_id)).scalar_one_or_none()
    if candidate is None:
        raise ValueError("reservation unavailable")
    locked_subject = session.execute(select(subject).where(subject.c.scope_id == scope_id,
        subject.c.subject_id == candidate).with_for_update()).mappings().one_or_none()
    locked_reservation = session.execute(select(reservation).where(reservation.c.scope_id == scope_id,
        reservation.c.call_id == call_id).with_for_update()).mappings().one_or_none()
    locked_slot = session.execute(select(slot).where(slot.c.scope_id == scope_id,
        slot.c.call_id == call_id).with_for_update()).mappings().one_or_none()
    locked_intent = session.execute(select(intent).where(intent.c.scope_id == scope_id,
        intent.c.call_id == call_id).with_for_update()).mappings().one_or_none()
    if (locked_subject is None or locked_reservation is None or locked_slot is None or locked_intent is None
            or locked_reservation.subject_id != candidate or locked_slot.subject_id != candidate
            or locked_intent.subject_id != candidate or locked_intent.purpose != locked_subject.purpose
            or locked_reservation.purpose != locked_subject.purpose):
        raise ValueError("obligation binding unavailable")
    state = (locked_reservation.state, locked_slot.state, locked_intent.state)
    if state not in {("reserved", "held", "committed"), ("unknown", "unknown", "unknown")}:
        raise ValueError("incoherent obligation state")
    for name in ("reserved_usd_micro", "reserved_allowance_micro"):
        value = locked_reservation[name]
        if type(value) is not int or not 0 <= value <= _MAX_I64:
            raise ValueError("invalid persisted reservation amount")
    actuals = (locked_reservation.actual_usd_micro, locked_reservation.actual_allowance_micro)
    if (actuals[0] is None) != (actuals[1] is None):
        raise ValueError("incomplete persisted actual pair")
    if any(value is not None and (type(value) is not int or not 0 <= value <= _MAX_I64) for value in actuals):
        raise ValueError("invalid persisted actual amount")
    if state[0] == "reserved" and actuals[0] is not None:
        raise ValueError("reserved obligation has actuals")
    audit_table = tables["audit_events"]
    audit_ids = {locked_reservation.audit_sequence, locked_slot.audit_sequence, locked_intent.audit_sequence}
    if locked_gate is not None:
        audit_ids.add(locked_gate.audit_sequence)
    if (any(type(value) is not int or value <= 0 for value in audit_ids)
            or set(session.execute(select(audit_table.c.sequence).where(audit_table.c.sequence.in_(audit_ids))).scalars()) != audit_ids):
        raise ValueError("obligation audit unavailable")
    if locked_gate is not None and (locked_gate.mode not in {"disabled", "census", "active", "inhibited"}
            or type(locked_gate.revision) is not int or not 0 <= locked_gate.revision <= _MAX_I64):
        raise ValueError("invalid persisted gate")
    if locked_gate is not None:
        cutover = (locked_gate.cutover_batch_id, locked_gate.cutover_digest, locked_gate.cutover_generation,
                   locked_gate.cutover_fence_digest, locked_gate.cutover_owner_epoch)
        if any(value is None for value in cutover) and any(value is not None for value in cutover):
            raise ValueError("incomplete persisted gate cutover")
        if not _is_lower_hex(locked_gate.policy_digest, 64):
            raise ValueError("invalid persisted gate policy digest")
        if cutover[0] is not None and (not _is_lower_hex(cutover[1], 64)
                or not _is_lower_hex(cutover[2], 32) or not _is_lower_hex(cutover[3], 64)):
            raise ValueError("invalid persisted gate binding")
        if cutover[4] is not None and (type(cutover[4]) is not int or not 0 <= cutover[4] <= _MAX_I64):
            raise ValueError("invalid persisted gate epoch")
        if locked_gate.mode == "active" and cutover[0] is None:
            raise ValueError("active gate has no cutover")
    if state[0] == "unknown" and (locked_gate is None or locked_gate.mode == "inhibited"):
        return "unknown_gate_missing" if locked_gate is None else "unknown_gate_row_inhibited_unverified"
    if locked_gate is not None and locked_gate.mode != "inhibited" and locked_gate.revision >= _MAX_I64:
        raise ValueError("gate revision exhausted")
    detail = {"scope_id": scope_id}
    if audit_detail is not None:
        if (type(audit_detail) is not dict
                or any(type(key) is not str or type(value) is not str
                       or len(key) > 64 or len(value) > 128
                       for key, value in audit_detail.items())):
            raise ValueError("invalid unknown audit context")
        detail.update(audit_detail)
    event = database.audit(session, "program-ledger", "mark-unknown", call_id, detail)
    session.flush()
    session.execute(update(reservation).where(reservation.c.scope_id == scope_id, reservation.c.call_id == call_id)
                    .values(state="unknown", audit_sequence=event.sequence))
    session.execute(update(slot).where(slot.c.scope_id == scope_id, slot.c.call_id == call_id)
                    .values(state="unknown", audit_sequence=event.sequence))
    session.execute(update(intent).where(intent.c.scope_id == scope_id, intent.c.call_id == call_id)
                    .values(state="unknown", audit_sequence=event.sequence))
    if locked_gate is not None and locked_gate.mode != "inhibited":
        session.execute(update(gate).where(gate.c.scope_id == scope_id)
                        .values(mode="inhibited", revision=locked_gate.revision + 1, audit_sequence=event.sequence))
    return "unknown_gate_missing" if locked_gate is None else "unknown_gate_row_inhibited_unverified"


def _mark_work_projection_unknown_in_transaction(database, session, projection, *, job_id, attempt_id):
    """Record one canonical Work projection as held UNKNOWN after possible dispatch.

    The caller must have joined and locked the stored projection and relational
    rows in the global owner lock order. Immutable projection identity is
    rechecked here; only the v10 current projection pointer advances.
    """
    from sqlalchemy import select, update
    from .collection_transaction import _owner_transaction_is_active
    from .db import Base
    from .work_projection import (
        MAX_I64 as PROJECTION_MAX_I64,
        projection_digest,
        validate_payload,
    )

    if not _owner_transaction_is_active(session):
        raise ValueError("owner transaction unavailable")
    value = projection.to_payload() if hasattr(projection, "to_payload") else projection
    checked = validate_payload(value)
    if (checked.work_job_id != job_id or checked.subject_id != job_id
            or checked.attempt_id != attempt_id or checked.purpose != "work"):
        raise ValueError("Work projection owner binding unavailable")
    state = (checked.intent_state, checked.reservation_state, checked.slot_state)
    if state == ("known", "settled", "released"):
        _inhibit_work_gate_for_uncertain_in_transaction(
            database, session, checked.scope_id, checked.call_id,
            job_id=job_id, attempt_id=attempt_id,
        )
        return None
    if state not in {("committed", "reserved", "held"), ("unknown", "unknown", "unknown")}:
        raise ValueError("Work projection state unavailable")

    tables = Base.metadata.tables
    _mark_unknown_in_transaction(
        database, session, tables, scope_id=checked.scope_id, call_id=checked.call_id,
        audit_detail={"work_job_id": job_id, "work_attempt_id": attempt_id},
    )
    intent_t = tables["program_call_intents"]
    sequence = session.execute(select(intent_t.c.audit_sequence).where(
        intent_t.c.scope_id == checked.scope_id,
        intent_t.c.call_id == checked.call_id,
    )).scalar_one_or_none()
    if type(sequence) is not int:
        raise ValueError("Work projection audit sequence unavailable")
    if state == ("unknown", "unknown", "unknown") and sequence == int(checked.audit_sequence):
        # An already unknown, inhibited row is an idempotent no-op. If its gate
        # was still open, _mark_unknown_in_transaction appended an audit event
        # and changed the sequence, so the projection pointer must advance.
        return None
    if int(checked.projection_revision) >= PROJECTION_MAX_I64:
        raise ValueError("Work projection revision exhausted")

    payload = checked.to_payload()
    revision = int(checked.projection_revision) + 1
    payload.update(
        intent_state="unknown", reservation_state="unknown", slot_state="unknown",
        projection_revision=str(revision), audit_sequence=str(sequence),
    )
    next_projection = validate_payload(payload)
    digest_value = projection_digest(next_projection)
    binding_t = tables["program_work_projection_bindings"]
    updated = session.execute(update(binding_t).where(
        binding_t.c.scope_id == checked.scope_id,
        binding_t.c.call_id == checked.call_id,
        binding_t.c.subject_id == checked.subject_id,
        binding_t.c.purpose == checked.purpose,
        binding_t.c.work_job_id == checked.work_job_id,
        binding_t.c.attempt_id == checked.attempt_id,
        binding_t.c.slot_id == checked.slot_id,
        binding_t.c.policy_revision == int(checked.policy_revision),
        binding_t.c.policy_digest == checked.policy_digest,
        binding_t.c.projection_revision == int(checked.projection_revision),
        binding_t.c.projection_digest == projection_digest(checked),
        binding_t.c.audit_sequence == int(checked.audit_sequence),
    ).values(
        projection_revision=revision,
        projection_digest=digest_value,
        audit_sequence=sequence,
    ))
    if updated.rowcount != 1:
        raise ValueError("Work projection pointer changed")
    return payload


def _inhibit_work_gate_for_uncertain_in_transaction(database, session, scope_id, call_id,
                                                     *, job_id, attempt_id):
    """Inhibit a scope for an uncertain attempt without rewriting known rows."""
    from sqlalchemy import select, update
    from .collection_transaction import _owner_transaction_is_active
    from .db import Base

    if not _owner_transaction_is_active(session):
        raise ValueError("owner transaction unavailable")
    tables = Base.metadata.tables
    scope_t = tables["program_budget_scopes"]
    gate_t = tables["program_gate_state"]
    scope = session.execute(select(scope_t.c.id).where(
        scope_t.c.id == scope_id).with_for_update()).scalar_one_or_none()
    gate = session.execute(select(gate_t).where(
        gate_t.c.scope_id == scope_id).with_for_update()).mappings().one_or_none()
    if scope is None or gate is None:
        raise ValueError("Work gate unavailable")
    if gate["mode"] == "inhibited":
        return
    if (gate["mode"] not in {"disabled", "census", "active"}
            or type(gate["revision"]) is not int or not 0 <= gate["revision"] < _MAX_I64):
        raise ValueError("Work gate unavailable")
    event = database.audit(session, "program-ledger", "inhibit-uncertain-work", call_id, {
        "scope_id": scope_id, "work_job_id": job_id, "work_attempt_id": attempt_id,
    })
    session.flush()
    result = session.execute(update(gate_t).where(
        gate_t.c.scope_id == scope_id,
        gate_t.c.revision == gate["revision"],
        gate_t.c.audit_sequence == gate["audit_sequence"],
        gate_t.c.mode == gate["mode"],
    ).values(mode="inhibited", revision=gate["revision"] + 1,
             audit_sequence=event.sequence))
    if result.rowcount != 1:
        raise ValueError("Work gate changed")


def _validate_locked_legacy_binding(link, batch, registry, job, reservation, slot):
    """Compare already locked rows; this does not authenticate a census.

    The owner must acquire the audit, scope, gate, batch, registry, Job,
    reservation and slot locks in the reviewed order before calling. A valid
    link alone does not authorize activation or prove external evidence.
    """
    if not all((link, batch, registry, job, reservation, slot)):
        raise ValueError("missing legacy binding")
    for name in ("scope_id", "batch_id", "link_id", "job_id", "original_attempt_id",
                 "original_call_id", "original_slot_id", "subject_id", "call_id", "slot_id"):
        value = link[name]
        limit = 64 if name == "job_id" else 96
        if (type(value) is not str or not 1 <= len(value) <= limit
                or any(not "!" <= char <= "~" for char in value)):
            raise ValueError("invalid legacy identity")
    if not _is_lower_hex(link["original_digest"], 64) or not _is_lower_hex(registry["binding_digest"], 64):
        raise ValueError("invalid legacy digest")
    scope = link["scope_id"]
    subject = link["subject_id"]
    call = link["call_id"]
    if (batch["scope_id"] != scope or batch["batch_id"] != link["batch_id"]
            or registry["scope_id"] != scope or registry["purpose"] != "work"
            or registry["subject_id"] != subject or job["id"] != subject or link["job_id"] != subject
            or reservation["scope_id"] != scope or reservation["subject_id"] != subject
            or reservation["purpose"] != "work" or reservation["call_id"] != call
            or slot["scope_id"] != scope or slot["subject_id"] != subject
            or slot["call_id"] != call or slot["slot_id"] != link["slot_id"]):
        raise ValueError("inconsistent legacy binding")
