"""Attempt lineage is metadata; approved artifact transfer has a separate owner."""
from sqlalchemy import select

from .db import Job, RequestLedger, digest
from .service import PlatformError, TERMINAL


def validate_metadata(previous_attempt, selection_reason):
    for value, limit, code in ((previous_attempt, 256, "invalid_previous_attempt"),
                               (selection_reason, 500, "invalid_selection_reason")):
        if value is None:
            continue
        try:
            valid = isinstance(value, str) and 1 <= len(value) <= limit and bool(value.strip()) and bool(value.encode("utf-8"))
        except UnicodeError:
            valid = False
        if not valid:
            raise PlatformError(code, "Attempt metadata must be bounded nonempty UTF-8 text", 422)


def previous(platform, session, receipt: str | None, caller: str, *, role="caller", recorded=None):
    if receipt is None:
        return None
    # Caller-owned provenance is not content access. Never read an artifact here.
    row = session.scalar(select(Job).where(Job.receipt == receipt, Job.caller == caller))
    if row is None or row.resolved.get("submission_role", "caller") != role:
        raise PlatformError("previous_attempt_unavailable", "Previous attempt is unavailable", 404)
    identity = {"receipt": row.receipt, "job_id": row.id, "submission_role": role}
    if recorded is not None and any(recorded.get(key) != value for key, value in identity.items()):
        raise PlatformError("previous_attempt_unavailable", "Previous attempt is unavailable", 404)
    if row.status not in TERMINAL:
        raise PlatformError("previous_attempt_unfinished", "Previous attempt has not reached a terminal state", 409)
    from .lease_service import replay_safe
    started = row.started_at is not None or row.attempts > 0
    gateway = row.resolved.get("model", {}).get("connection", {}).get("kind") == "model-gateway"
    ledger = session.scalars(select(RequestLedger).where(RequestLedger.job_id == row.id)).all() if gateway and started else []
    # Preserve the owner's positive admission evidence through normal ledger
    # retention. Later absence is not contrary evidence; live uncertain rows are.
    captured = recorded.get("gateway_stop") if recorded else None
    captured_stop = (isinstance(captured, dict) and captured.get("kind") == "gateway-stop/v1"
                     and type(captured.get("request_count")) is int and captured["request_count"] > 0)
    gateway_uncertain = gateway and started and ((not ledger and not captured_stop) or any(
        item.status != "finished" or item.outcome == "model_cancel_unconfirmed" or
        item.evidence.get("stop_confirmed") is not True for item in ledger))
    if gateway_uncertain or row.error_code in {"execution_uncertain", "external_execution_uncertain"} or (
            row.status != "completed" and started and not replay_safe(row.resolved)):
        raise PlatformError("previous_effect_uncertain", "The previous effect remains uncertain; a new binding cannot resolve it", 409)
    if recorded is not None:
        return recorded
    if gateway and started:
        identity["gateway_stop"] = {"kind": "gateway-stop/v1", "request_count": len(ledger),
            "request_identity_digest": "sha256:" + digest(sorted((item.id, item.fence) for item in ledger))}
    return identity
