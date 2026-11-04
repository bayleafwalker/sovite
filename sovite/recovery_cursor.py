"""Durable scan coordination for expired-owner recovery.

The cursor records only scan progress. It does not classify an owner, prove
settlement, authorize a retry, or imply that an external executor stopped.
All operations are transaction-scoped and require ``owner_transaction``.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping


CURSOR_SCHEMA = "sovite-recovery-cursor/v1"
CURSOR_PREFIX = "recover_cursor:"


class RecoveryCursorUnavailable(RuntimeError):
    """Cursor storage or its scope/value contract is unavailable."""


@dataclass(frozen=True, slots=True)
class RecoveryCursor:
    key: str
    compute_id: str
    repository_worker_only: bool
    last_job_id: str | None
    value: Mapping[str, Any]


def _validate_scope(compute_id: Any, repository_worker_only: Any) -> tuple[str, bool]:
    if type(compute_id) is not str or not compute_id or "\x00" in compute_id \
            or type(repository_worker_only) is not bool:
        raise RecoveryCursorUnavailable()
    try:
        if len(compute_id.encode("utf-8")) > 160:
            raise RecoveryCursorUnavailable()
    except UnicodeError:
        raise RecoveryCursorUnavailable() from None
    return compute_id, repository_worker_only


def _validate_job_id(job_id: Any) -> str | None:
    if job_id is None:
        return None
    if type(job_id) is not str or not job_id or "\x00" in job_id:
        raise RecoveryCursorUnavailable()
    try:
        if len(job_id.encode("utf-8")) > 64:
            raise RecoveryCursorUnavailable()
    except UnicodeError:
        raise RecoveryCursorUnavailable() from None
    return job_id


def cursor_key(compute_id: str, repository_worker_only: bool) -> str:
    """Return the 79-character metadata key for one exact sweep scope."""
    compute_id, repository_worker_only = _validate_scope(compute_id, repository_worker_only)
    identity = json.dumps(
        {"compute_id": compute_id, "repository_worker_only": repository_worker_only},
        ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode("ascii")
    key = CURSOR_PREFIX + hashlib.sha256(identity).hexdigest()
    if len(key) != 79:
        raise RecoveryCursorUnavailable()
    return key


def _cursor_value(compute_id: str, repository_worker_only: bool,
                  last_job_id: str | None) -> dict[str, Any]:
    return {
        "schema_version": CURSOR_SCHEMA,
        "compute_id": compute_id,
        "repository_worker_only": repository_worker_only,
        "last_job_id": _validate_job_id(last_job_id),
    }


def _unavailable() -> RecoveryCursorUnavailable:
    return RecoveryCursorUnavailable("expired-owner cursor is unavailable")


def _require_owner_transaction(session) -> None:
    from .collection_transaction import _owner_transaction_is_active

    if not _owner_transaction_is_active(session):
        raise _unavailable()


def _decode(key: str, compute_id: str, repository_worker_only: bool,
            value: Any) -> RecoveryCursor:
    expected = {"schema_version", "compute_id", "repository_worker_only", "last_job_id"}
    if type(value) is not dict or set(value) != expected:
        raise _unavailable()
    if (value.get("schema_version") != CURSOR_SCHEMA
            or value.get("compute_id") != compute_id
            or type(value.get("repository_worker_only")) is not bool
            or value.get("repository_worker_only") is not repository_worker_only):
        raise _unavailable()
    try:
        last_job_id = _validate_job_id(value.get("last_job_id"))
    except (UnicodeError, TypeError):
        raise _unavailable() from None
    canonical = _cursor_value(compute_id, repository_worker_only, last_job_id)
    if value != canonical:
        raise _unavailable()
    return RecoveryCursor(
        key, compute_id, repository_worker_only, last_job_id,
        MappingProxyType(canonical),
    )


def read_cursor(session, compute_id: str, repository_worker_only: bool) -> RecoveryCursor:
    """Read and lock a cursor, initializing only an actually absent row.

    Caller must already hold the owner transaction and PostgreSQL claim
    advisory lock. Malformed or mis-scoped values fail closed; they are never
    silently reset to a new scan.
    """
    _require_owner_transaction(session)
    compute_id, repository_worker_only = _validate_scope(compute_id, repository_worker_only)
    from .db import MetadataRow
    from sqlalchemy import select

    key = cursor_key(compute_id, repository_worker_only)
    row = session.execute(select(MetadataRow).where(MetadataRow.key == key).with_for_update()).scalar_one_or_none()
    if row is None:
        value = _cursor_value(compute_id, repository_worker_only, None)
        row = MetadataRow(key=key, value=value)
        session.add(row)
        session.flush()
    return _decode(key, compute_id, repository_worker_only, row.value)


def advance_cursor(session, cursor: RecoveryCursor, last_job_id: str | None) -> bool:
    """Compare-and-advance one locked cursor value; return False on CAS miss.

    A successful update remains part of the caller's transaction. Unexpected
    row counts or database failures propagate so the caller cannot mistake a
    global control failure for a candidate-local refusal.
    """
    _require_owner_transaction(session)
    if not isinstance(cursor, RecoveryCursor):
        raise _unavailable()
    compute_id, repository_worker_only = _validate_scope(
        cursor.compute_id, cursor.repository_worker_only,
    )
    key = cursor_key(compute_id, repository_worker_only)
    if cursor.key != key:
        raise _unavailable()
    validated_cursor = _decode(key, compute_id, repository_worker_only, dict(cursor.value))
    if validated_cursor.last_job_id != _validate_job_id(cursor.last_job_id):
        raise _unavailable()
    from .db import MetadataRow
    from sqlalchemy import select, update

    row = session.execute(select(MetadataRow).where(MetadataRow.key == key).with_for_update()).scalar_one_or_none()
    if row is None:
        raise _unavailable()
    actual = _decode(key, compute_id, repository_worker_only, row.value)
    if actual.value != validated_cursor.value:
        return False
    target = _cursor_value(compute_id, repository_worker_only, last_job_id)
    result = session.execute(update(MetadataRow).where(MetadataRow.key == key).values(value=target))
    if result.rowcount != 1:
        raise _unavailable()
    return True
