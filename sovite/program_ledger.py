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


def _mark_unknown_in_transaction(database, session, tables, *, scope_id, call_id):
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
    event = database.audit(session, "program-ledger", "mark-unknown", call_id,
                           {"scope_id": scope_id})
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
