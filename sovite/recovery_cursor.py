"""Durable, scoped progress for bounded expired-owner scans.

A completed horizon is a scan cutoff, never evidence of external settlement.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

CURSOR_SCHEMA = "sovite-recovery-cursor/v2"
CURSOR_PREFIX = "recover_cursor:"


class RecoveryCursorUnavailable(RuntimeError):
    """Cursor state cannot be trusted."""


@dataclass(frozen=True, slots=True)
class RecoveryCursor:
    key: str
    compute_id: str
    repository_worker_only: bool
    phase: str
    last_job_id: str | None
    fair_pointer_job_id: str | None
    forward_upper_id: str | None
    scan_started_at: float | None
    covered_through: float | None
    value: Mapping[str, Any]


def _unavailable():
    return RecoveryCursorUnavailable("expired-owner cursor is unavailable")


def _scope(compute_id, repository_worker_only):
    if type(compute_id) is not str or not compute_id or "\x00" in compute_id or type(repository_worker_only) is not bool:
        raise _unavailable()
    try:
        if len(compute_id.encode("utf-8")) > 160:
            raise _unavailable()
    except UnicodeError:
        raise _unavailable() from None
    return compute_id, repository_worker_only


def _job_id(value):
    if value is None:
        return None
    if type(value) is not str or not value or "\x00" in value:
        raise _unavailable()
    try:
        if len(value.encode("utf-8")) > 64:
            raise _unavailable()
    except UnicodeError:
        raise _unavailable() from None
    return value


def _timestamp(value):
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value):
        raise _unavailable()
    return float(value)


def cursor_key(compute_id, repository_worker_only):
    compute_id, repository_worker_only = _scope(compute_id, repository_worker_only)
    identity = json.dumps({"compute_id": compute_id, "repository_worker_only": repository_worker_only},
                          ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    return CURSOR_PREFIX + hashlib.sha256(identity).hexdigest()


def _value(compute_id, repository_worker_only, *, phase="idle", last_job_id=None,
           fair_pointer_job_id=None, forward_upper_id=None, scan_started_at=None, covered_through=None):
    _scope(compute_id, repository_worker_only)
    last_job_id, fair_pointer_job_id, forward_upper_id = map(_job_id, (last_job_id, fair_pointer_job_id, forward_upper_id))
    scan_started_at, covered_through = _timestamp(scan_started_at), _timestamp(covered_through)
    if phase not in {"idle", "forward", "wrap"}:
        raise _unavailable()
    if phase == "idle":
        if last_job_id is not None or forward_upper_id is not None or scan_started_at is not None:
            raise _unavailable()
    elif scan_started_at is None or (phase == "wrap" and fair_pointer_job_id is None):
        raise _unavailable()
    if scan_started_at is not None and covered_through is not None and covered_through > scan_started_at:
        raise _unavailable()
    if phase == "forward" and last_job_id is not None and (forward_upper_id is None or last_job_id > forward_upper_id):
        raise _unavailable()
    if phase == "wrap" and last_job_id is not None and last_job_id > fair_pointer_job_id:
        raise _unavailable()
    return {"schema_version": CURSOR_SCHEMA, "compute_id": compute_id,
            "repository_worker_only": repository_worker_only, "phase": phase,
            "last_job_id": last_job_id, "fair_pointer_job_id": fair_pointer_job_id,
            "forward_upper_id": forward_upper_id, "scan_started_at": scan_started_at,
            "covered_through": covered_through}


def _decode(key, compute_id, repository_worker_only, value):
    if type(value) is not dict or set(value) != {"schema_version", "compute_id", "repository_worker_only", "phase", "last_job_id", "fair_pointer_job_id", "forward_upper_id", "scan_started_at", "covered_through"}:
        raise _unavailable()
    if value["schema_version"] != CURSOR_SCHEMA or value["compute_id"] != compute_id or type(value["repository_worker_only"]) is not bool or value["repository_worker_only"] is not repository_worker_only:
        raise _unavailable()
    canonical = _value(compute_id, repository_worker_only, **{k: value[k] for k in ("phase", "last_job_id", "fair_pointer_job_id", "forward_upper_id", "scan_started_at", "covered_through")})
    if value != canonical:
        raise _unavailable()
    return RecoveryCursor(key, compute_id, repository_worker_only, *(canonical[k] for k in ("phase", "last_job_id", "fair_pointer_job_id", "forward_upper_id", "scan_started_at", "covered_through")), MappingProxyType(canonical))


def _strict_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise _unavailable()
            result[key] = value
        return result
    try:
        if type(raw) is not str:
            raise _unavailable()
        return json.loads(raw, object_pairs_hook=unique)
    except (ValueError, TypeError, RecursionError):
        raise _unavailable() from None


def _raw_cursor(session, key, compute_id, repository_worker_only, *, lock):
    from .db import MetadataRow
    from sqlalchemy import Text, cast, select
    statement = select(cast(MetadataRow.value, Text)).where(MetadataRow.key == key)
    if lock:
        statement = statement.with_for_update()
    row = session.execute(statement).one_or_none()
    return None if row is None else _decode(key, compute_id, repository_worker_only, _strict_json(row[0]))


def peek_cursor(session, compute_id, repository_worker_only):
    """Read raw JSON without mutation; an absent cursor has no covered horizon."""
    key = cursor_key(compute_id, repository_worker_only)
    return _raw_cursor(session, key, compute_id, repository_worker_only, lock=False)


def _require_owner_transaction(session):
    from .collection_transaction import _owner_transaction_is_active
    if not _owner_transaction_is_active(session):
        raise _unavailable()


def read_cursor(session, compute_id, repository_worker_only):
    from .db import MetadataRow
    _require_owner_transaction(session)
    key = cursor_key(compute_id, repository_worker_only)
    current = _raw_cursor(session, key, compute_id, repository_worker_only, lock=True)
    if current is None:
        session.add(MetadataRow(key=key, value=_value(compute_id, repository_worker_only)))
        session.flush()
        current = _raw_cursor(session, key, compute_id, repository_worker_only, lock=True)
        if current is None:
            raise _unavailable()
    return current


def advance_cursor(session, cursor, *, phase, last_job_id, fair_pointer_job_id,
                   forward_upper_id, scan_started_at, covered_through):
    from .db import MetadataRow
    from sqlalchemy import update
    _require_owner_transaction(session)
    if not isinstance(cursor, RecoveryCursor) or cursor.key != cursor_key(cursor.compute_id, cursor.repository_worker_only):
        raise _unavailable()
    expected = _decode(cursor.key, cursor.compute_id, cursor.repository_worker_only, dict(cursor.value))
    if expected != cursor:
        raise _unavailable()
    actual = _raw_cursor(session, cursor.key, cursor.compute_id, cursor.repository_worker_only, lock=True)
    if actual is None:
        raise _unavailable()
    if actual.value != cursor.value:
        return False
    target = _value(cursor.compute_id, cursor.repository_worker_only, phase=phase, last_job_id=last_job_id,
                    fair_pointer_job_id=fair_pointer_job_id, forward_upper_id=forward_upper_id,
                    scan_started_at=scan_started_at, covered_through=covered_through)
    result = session.execute(update(MetadataRow).where(MetadataRow.key == cursor.key).values(value=target))
    if result.rowcount != 1:
        raise _unavailable()
    return True
