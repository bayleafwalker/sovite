"""Dormant collection-owner persistence primitives; no effect authority."""
from __future__ import annotations

import hashlib
import json
import re
import time

from sqlalchemy import select, update

from .collection_identity import (CAPABILITIES, IDENTIFIER, MAX_INTEGER,
                                  InvalidCollectionInput, create_request_identity)
from .collection_transaction import owner_transaction
from .db import Base


class CollectionOwnerError(ValueError):
    """A deliberately static, non-sensitive owner refusal."""

    def __init__(self, code: str, status: int):
        self.code, self.status = code, status
        super().__init__("Collection owner request refused")


def _deny(code="collection_conflict", status=409):
    raise CollectionOwnerError(code, status)


def _table(name):
    return Base.metadata.tables[name]


def _canonical_payload(payload):
    """Canonicalize the closed ASCII JSON domain of the frozen binding."""
    def check(value):
        if type(value) is str:
            return value.isascii() and all(ord(c) >= 32 for c in value)
        if type(value) is int:
            return 0 <= value <= MAX_INTEGER
        if type(value) is list:
            return all(check(item) for item in value)
        if type(value) is dict:
            return all(type(k) is str and check(k) and check(v) for k, v in value.items())
        return False
    if not check(payload):
        _deny()
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def immutable_payload_digest(payload):
    canonical = _canonical_payload(payload)
    return hashlib.sha256(b"sovite.collection.immutable/v1\0" + canonical.encode()).hexdigest()


def _binding(row):
    """Validate every persisted immutable column before relying on its roles."""
    try:
        payload = row["immutable_payload"]
        if type(payload) is not dict or set(payload) != {
            "schema_version", "request_canonical", "request_digest", "policy"
        } or payload["schema_version"] != "sovite.collection-binding/v1":
            _deny()
        request, canonical, digest = create_request_identity(payload["request_canonical"].encode("utf-8"))
        if canonical != payload["request_canonical"] or digest != payload["request_digest"]:
            _deny()
        policy = payload["policy"]
        if type(policy) is not dict or set(policy) != {
            "schema_version", "policy_revision", "policy_digest", "scope_id", "scope_policy_digest",
            "principals", "seed_digest", "base_sha", "paths_digest", "minimum_retention_ms",
            "maximum_retain_until_ms", "resource_deadline_ms", "commissioning_deadline_ms",
            "permit_deadline_ms", "epoch_authorization_digest"
        } or policy["schema_version"] != "sovite.collection-owner-policy/v1":
            _deny()
        principals = policy["principals"]
        if (type(principals) is not dict or set(principals) != CAPABILITIES
                or any(type(v) is not str or not IDENTIFIER.fullmatch(v) for v in principals.values())
                or len(set(principals.values())) != len(CAPABILITIES)):
            _deny()
        if principals["collection-operator"] != row["principal_id"]:
            _deny()
        if immutable_payload_digest(payload) != row["immutable_digest"]:
            _deny()
        for name in ("workspace_id", "repository_id", "generation", "suite_version", "suite_digest",
                     "deployment_digest", "permit_digest", "deadline_ms", "schema_version"):
            if request[name] != row[name]:
                _deny()
        for name in ("seed_digest", "base_sha", "paths_digest"):
            if policy[name] != row[name]:
                _deny()
        if policy["scope_id"] != row["scope_id"] or request["retain_until_ms"] > row["retain_until_ms"]:
            _deny()
        if row["purpose"] != "operator_c0_collection":
            _deny()
        return payload
    except (KeyError, TypeError, UnicodeError, InvalidCollectionInput, AttributeError):
        _deny()


def _locked_session(session, session_id):
    """Resolve under audit lock, then lock scope, subject and session in order."""
    sessions, scope, subjects = (_table("collection_sessions"), _table("program_budget_scopes"),
                                _table("program_budget_subjects"))
    candidate = session.execute(select(sessions.c.scope_id).where(sessions.c.id == session_id)).scalar_one_or_none()
    if candidate is None:
        _deny("collection_forbidden", 403)
    session.execute(select(scope).where(scope.c.id == candidate).with_for_update()).first()
    session.execute(select(subjects).where(subjects.c.scope_id == candidate,
                    subjects.c.subject_id == session_id).with_for_update()).first()
    row = session.execute(select(sessions).where(sessions.c.id == session_id).with_for_update()).mappings().one_or_none()
    if row is None or row["scope_id"] != candidate:
        _deny()
    return row


def _actor(row, credential, allowed):
    payload = _binding(row)
    if (credential.capability not in allowed or
            payload["policy"]["principals"].get(credential.capability) != credential.principal_id):
        _deny("collection_forbidden", 403)
    return payload


def cancel(database, *, session_id, credential, expected_revision, immutable_digest, reason_code):
    """Commit the cancellation inhibit; never report guest stop or completion."""
    if (type(session_id) is not str or not IDENTIFIER.fullmatch(session_id)
            or type(expected_revision) is not int or expected_revision < 0
            or type(immutable_digest) is not str or not re.fullmatch(r"[0-9a-f]{64}", immutable_digest)
            or type(reason_code) is not str or not IDENTIFIER.fullmatch(reason_code)):
        _deny("collection_invalid_request", 422)
    with owner_transaction(database) as session:
        row = _locked_session(session, session_id)
        _actor(row, credential, {"collection-operator"})
        if (row["immutable_digest"] != immutable_digest or row["revision"] != expected_revision
                or row["state"] not in {"created", "admitted", "active", "stopped_unknown"}
                or row["cancel_revision"] is not None or row["cancel_at_ms"] is not None):
            _deny()
        timestamp = time.time_ns() // 1_000_000
        if not 0 < timestamp <= MAX_INTEGER:
            _deny("collection_unavailable", 503)
        sessions = _table("collection_sessions")
        updated = session.execute(update(sessions).where(
            sessions.c.id == session_id, sessions.c.revision == expected_revision,
            sessions.c.state == row["state"], sessions.c.immutable_digest == immutable_digest,
            sessions.c.cancel_revision.is_(None), sessions.c.cancel_at_ms.is_(None),
        ).values(state="cancel_pending", revision=expected_revision + 1,
                 cancel_revision=expected_revision + 1, cancel_at_ms=timestamp))
        if updated.rowcount != 1:
            _deny()
        database.audit(session, credential.principal_id, "collection.cancel_requested", session_id,
                       {"capability": credential.capability, "old_state": row["state"],
                        "new_state": "cancel_pending", "old_revision": expected_revision,
                        "new_revision": expected_revision + 1, "scope_id": row["scope_id"],
                        "immutable_digest": immutable_digest, "reason_code": reason_code})
        return {"session_id": session_id, "state": "cancel_pending", "revision": expected_revision + 1}
