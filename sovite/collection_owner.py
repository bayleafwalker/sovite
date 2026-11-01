"""Dormant collection-owner persistence primitives; no effect authority."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
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


def _validate_policy(policy):
    keys = {"schema_version", "policy_revision", "policy_digest", "scope_id", "scope_policy_revision", "scope_policy_digest",
            "principals", "seed_digest", "base_sha", "paths_digest", "minimum_retention_ms",
            "maximum_retain_until_ms", "resource_deadline_ms", "commissioning_deadline_ms",
            "permit_deadline_ms", "epoch_authorization_digest"}
    if type(policy) is not dict or set(policy) != keys or policy["schema_version"] != "sovite.collection-owner-policy/v1":
        _deny()
    if type(policy["scope_id"]) is not str or not IDENTIFIER.fullmatch(policy["scope_id"]):
        _deny()
    for name in ("seed_digest", "base_sha", "paths_digest", "policy_digest", "scope_policy_digest", "epoch_authorization_digest"):
        if type(policy[name]) is not str or not re.fullmatch(r"[0-9a-f]{64}", policy[name]):
            _deny()
    for name in ("policy_revision", "scope_policy_revision", "minimum_retention_ms", "maximum_retain_until_ms", "resource_deadline_ms",
                 "commissioning_deadline_ms", "permit_deadline_ms"):
        if type(policy[name]) is not int or not 0 < policy[name] <= MAX_INTEGER:
            _deny()


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
        _validate_policy(policy)
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
        _retention(policy, row["deadline_ms"], row["retain_until_ms"])
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


def _trusted_snapshot():
    """No production authority is installed. A future concrete source needs review."""
    _deny("collection_unavailable", 503)


def _current_snapshot():
    """Validate the fixed authority's closed snapshot; it is not caller selectable."""
    snapshot = _trusted_snapshot()
    if type(snapshot) is not dict or set(snapshot) != {
        "policy", "allowed_request", "now_ms", "observed_at_ms", "uncertainty_ms", "drift_bounded",
        "anti_rollback_verified", "evidence_verified"
    }:
        _deny("collection_unavailable", 503)
    for key in ("now_ms", "observed_at_ms", "uncertainty_ms"):
        if type(snapshot[key]) is not int or not 0 <= snapshot[key] <= MAX_INTEGER:
            _deny("collection_unavailable", 503)
    if (snapshot["now_ms"] < snapshot["observed_at_ms"]
            or snapshot["now_ms"] - snapshot["observed_at_ms"] > 30000
            or snapshot["uncertainty_ms"] > 2000
            or snapshot["drift_bounded"] is not True
            or snapshot["anti_rollback_verified"] is not True
            or snapshot["evidence_verified"] is not True):
        _deny("collection_unavailable", 503)
    return snapshot


def _scope(session, policy):
    scope = _table("program_budget_scopes")
    row = session.execute(select(scope).where(scope.c.id == policy["scope_id"]).with_for_update()).mappings().one_or_none()
    if (row is None or row["policy_digest"] != policy["scope_policy_digest"]
            or row["policy_revision"] != policy["scope_policy_revision"]):
        _deny()
    return row


def _check_snapshot(snapshot, policy, request):
    if snapshot["policy"] != policy or snapshot["allowed_request"] != request:
        _deny()
    now = snapshot["now_ms"] + snapshot["uncertainty_ms"]
    if now > MAX_INTEGER or any(type(policy.get(key)) is not int or now >= policy[key] for key in (
            "permit_deadline_ms", "commissioning_deadline_ms", "resource_deadline_ms")):
        _deny()
    return now


def _retention(policy, deadline, retain):
    minimum = policy.get("minimum_retention_ms")
    maximum = policy.get("maximum_retain_until_ms")
    if (type(minimum) is not int or minimum <= 0 or type(maximum) is not int
            or type(deadline) is not int or type(retain) is not int
            or deadline + minimum > MAX_INTEGER or retain < deadline + minimum or retain > maximum):
        _deny()


def _scope_current(session, scope_id, now):
    scope = _table("program_budget_scopes")
    row = session.execute(select(scope).where(scope.c.id == scope_id)).mappings().one_or_none()
    return bool(row and row["window_start_ms"] <= now < row["window_end_ms"])


def _integrity(session, row, payload):
    policy = payload["policy"]
    scope = _table("program_budget_scopes")
    subject = _table("program_budget_subjects")
    scope_row = session.execute(select(scope).where(scope.c.id == row["scope_id"])).mappings().one_or_none()
    subject_row = session.execute(select(subject).where(subject.c.scope_id == row["scope_id"],
        subject.c.subject_id == row["id"])).mappings().one_or_none()
    return bool(scope_row and subject_row and scope_row["policy_digest"] == policy["scope_policy_digest"]
                and scope_row["policy_revision"] == policy["scope_policy_revision"]
                and subject_row["purpose"] == "collection" and subject_row["binding_digest"] == row["immutable_digest"])


def _epoch_binding_valid(row, epoch, payload):
    policy = payload["policy"]
    try:
        if (epoch["epoch"] != 1 or epoch["epoch_purpose"] != "execution"
                or epoch["principal_id"] != policy["principals"]["collection-agent"]
                or epoch["authorization_digest"] != policy["epoch_authorization_digest"]
                or type(epoch["deadline_ms"]) is not int
                or epoch["deadline_ms"] > min(row["deadline_ms"], policy["permit_deadline_ms"],
                    policy["commissioning_deadline_ms"], policy["resource_deadline_ms"])
                or epoch["retain_until_ms"] > row["retain_until_ms"]):
            return False
        _retention(policy, epoch["deadline_ms"], epoch["retain_until_ms"])
        return True
    except (CollectionOwnerError, KeyError, TypeError):
        return False


def _guard_consistent(row, guard, epochs, payload):
    if guard is None:
        return False
    if row["state"] in {"created", "admitted"}:
        history = not epochs or (row["state"] == "admitted" and len(epochs) == 1
                    and epochs[0]["state"] == "closed" and _epoch_binding_valid(row, epochs[0], payload))
        return guard["active_epoch"] is None and row["current_epoch"] is None and history
    if row["state"] == "active":
        return (guard["active_epoch"] == 1 and row["current_epoch"] == 1 and len(epochs) == 1
                and epochs[0]["state"] == "active" and _epoch_binding_valid(row, epochs[0], payload))
    return True


def _clean(session, session_id, scope_id):
    for name in ("collection_enrollments", "collection_operations", "collection_acknowledgements",
                 "collection_checkpoints", "collection_observations", "collection_retention_refs"):
        table = _table(name)
        if session.execute(select(table.c.session_id).where(table.c.session_id == session_id).limit(1)).first():
            return False
    for name in ("program_reservations", "program_slots"):
        table = _table(name)
        if session.execute(select(table.c.subject_id).where(table.c.scope_id == scope_id,
                           table.c.subject_id == session_id).limit(1)).first():
            return False
    return True


def _audit_state(database, session, row, actor, action, new_state, revision):
    database.audit(session, actor.principal_id, action, row["id"], {
        "capability": actor.capability, "scope_id": row["scope_id"], "old_state": row["state"],
        "new_state": new_state, "old_revision": row["revision"], "new_revision": revision,
        "epoch": 1 if action in {"collection.start", "collection.close"} else row["current_epoch"],
        "epoch_purpose": "execution" if (action in {"collection.start", "collection.close"} or row["current_epoch"] == 1) else None,
        "reason_code": "unsafe_state" if action == "collection.inhibited" else "requested",
        "immutable_digest": row["immutable_digest"]})


def _change_state(session, row, new_state, **values):
    sessions = _table("collection_sessions")
    result = session.execute(update(sessions).where(
        sessions.c.id == row["id"], sessions.c.revision == row["revision"],
        sessions.c.state == row["state"], sessions.c.immutable_digest == row["immutable_digest"],
        sessions.c.cancel_revision.is_(None), sessions.c.cancel_at_ms.is_(None)
    ).values(state=new_state, revision=row["revision"] + 1, **values))
    if result.rowcount != 1:
        _deny()


def _guard(session, session_id):
    table = _table("collection_epoch_guards")
    return session.execute(select(table).where(table.c.session_id == session_id).with_for_update()).mappings().one_or_none()


def _epoch_rows(session, session_id):
    table = _table("collection_epochs")
    return session.execute(select(table).where(table.c.session_id == session_id).order_by(table.c.epoch).with_for_update()).mappings().all()


def create(database, *, raw, credential, idempotency_key):
    if credential.capability != "collection-operator":
        _deny("collection_forbidden", 403)
    if type(idempotency_key) is not str or not IDENTIFIER.fullmatch(idempotency_key):
        _deny("collection_invalid_request", 422)
    try:
        request, canonical, request_digest = create_request_identity(raw)
    except InvalidCollectionInput:
        _deny("collection_invalid_request", 422)
    sessions = _table("collection_sessions")
    with owner_transaction(database) as session:
        previous = session.execute(select(sessions).where(sessions.c.principal_id == credential.principal_id,
                    sessions.c.idempotency_key == idempotency_key)).mappings().one_or_none()
        if previous is not None:
            payload = _binding(previous)
            if payload["request_canonical"] != canonical or payload["request_digest"] != request_digest:
                _deny()
            return {"session_id": previous["id"], "revision": previous["revision"],
                    "state": previous["state"], "immutable_digest": previous["immutable_digest"]}
        snapshot = _current_snapshot()
        policy = snapshot["policy"]
        try:
            _validate_policy(policy)
        except CollectionOwnerError:
            _deny("collection_unavailable", 503)
        now = _check_snapshot(snapshot, policy, request)
        if request["deadline_ms"] <= now:
            _deny()
        _retention(policy, request["deadline_ms"], request["retain_until_ms"])
        scope = _scope(session, policy)
        if not scope["window_start_ms"] <= now < scope["window_end_ms"]:
            _deny()
        if policy.get("principals", {}).get("collection-operator") != credential.principal_id:
            _deny("collection_forbidden", 403)
        payload = {"schema_version": "sovite.collection-binding/v1", "request_canonical": canonical,
                   "request_digest": request_digest, "policy": policy}
        digest = immutable_payload_digest(payload)
        session_id = "collection_" + secrets.token_hex(24)
        row = dict(request, id=session_id, principal_id=credential.principal_id, idempotency_key=idempotency_key,
                   scope_id=policy["scope_id"], purpose="operator_c0_collection", immutable_digest=digest,
                   immutable_payload=payload, seed_digest=policy["seed_digest"], base_sha=policy["base_sha"],
                   paths_digest=policy["paths_digest"], state="created", revision=0, current_epoch=None,
                   cancel_revision=None, cancel_at_ms=None)
        _binding(row)
        session.execute(_table("program_budget_subjects").insert().values(scope_id=policy["scope_id"],
                        subject_id=session_id, purpose="collection", binding_digest=digest))
        session.execute(sessions.insert().values(**row))
        session.execute(_table("collection_epoch_guards").insert().values(session_id=session_id,
                        active_epoch=None, revision=0))
        database.audit(session, credential.principal_id, "collection.created", session_id,
                       {"capability": credential.capability, "scope_id": policy["scope_id"],
                        "new_state": "created", "new_revision": 0, "immutable_digest": digest})
        return {"session_id": session_id, "revision": 0, "state": "created", "immutable_digest": digest}


def transition(database, *, session_id, credential, expected_revision, immutable_digest,
               action, epoch_deadline_ms=None, retain_until_ms=None, authorization_digest=None):
    allowed = {"admit": "collection-operator", "start": "collection-agent", "close": "collection-agent"}
    if action not in allowed or credential.capability != allowed[action]:
        _deny("collection_forbidden", 403)
    if (type(expected_revision) is not int or expected_revision < 0 or type(session_id) is not str
            or not IDENTIFIER.fullmatch(session_id) or type(immutable_digest) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", immutable_digest)):
        _deny("collection_invalid_request", 422)
    inhibited = False
    with owner_transaction(database) as session:
        row = _locked_session(session, session_id)
        payload = _actor(row, credential, {allowed[action]})
        if (row["revision"] != expected_revision or row["immutable_digest"] != immutable_digest
                or row["cancel_revision"] is not None or row["cancel_at_ms"] is not None):
            _deny()
        request, _, _ = create_request_identity(payload["request_canonical"].encode())
        snapshot = _current_snapshot()
        now = _check_snapshot(snapshot, payload["policy"], request)
        if now >= row["deadline_ms"] or not _scope_current(session, row["scope_id"], now):
            _deny()
        guard = _guard(session, session_id)
        epochs = _epoch_rows(session, session_id)
        clean = _clean(session, session_id, row["scope_id"])
        if not clean or not _integrity(session, row, payload) or not _guard_consistent(row, guard, epochs, payload):
            if row["state"] in {"created", "admitted", "active"}:
                _change_state(session, row, "stopped_unknown")
                _audit_state(database, session, row, credential, "collection.inhibited", "stopped_unknown", row["revision"] + 1)
                inhibited = True
            else:
                _deny()
        else:
            guards = _table("collection_epoch_guards")
            epochs_table = _table("collection_epochs")
            if action == "admit":
                if row["state"] != "created" or guard["active_epoch"] is not None or epochs or row["current_epoch"] is not None:
                    _deny()
                _change_state(session, row, "admitted")
                new_state = "admitted"
            elif action == "start":
                if (row["state"] != "admitted" or guard["active_epoch"] is not None or epochs
                        or row["current_epoch"] is not None or type(epoch_deadline_ms) is not int
                        or epoch_deadline_ms <= now or epoch_deadline_ms > min(row["deadline_ms"],
                            payload["policy"]["permit_deadline_ms"], payload["policy"]["commissioning_deadline_ms"],
                            payload["policy"]["resource_deadline_ms"])
                        or authorization_digest != payload["policy"]["epoch_authorization_digest"]):
                    _deny()
                _retention(payload["policy"], epoch_deadline_ms, retain_until_ms)
                if retain_until_ms < row["retain_until_ms"]:
                    _deny()
                session.execute(epochs_table.insert().values(session_id=session_id, epoch=1, epoch_purpose="execution",
                    principal_id=credential.principal_id, authorization_digest=authorization_digest,
                    deadline_ms=epoch_deadline_ms, retain_until_ms=retain_until_ms, state="active",
                    started_at_ms=now, ended_at_ms=None))
                guard_update = session.execute(update(guards).where(guards.c.session_id == session_id,
                    guards.c.active_epoch.is_(None), guards.c.revision == guard["revision"]).values(
                    active_epoch=1, revision=guard["revision"] + 1))
                if guard_update.rowcount != 1:
                    _deny()
                _change_state(session, row, "active", current_epoch=1, retain_until_ms=retain_until_ms)
                new_state = "active"
            else:
                if (row["state"] != "active" or row["current_epoch"] != 1 or guard["active_epoch"] != 1
                        or len(epochs) != 1 or epochs[0]["epoch"] != 1 or epochs[0]["state"] != "active"
                        or epochs[0]["epoch_purpose"] != "execution" or epochs[0]["principal_id"] != credential.principal_id):
                    _deny()
                epoch_update = session.execute(update(epochs_table).where(epochs_table.c.session_id == session_id,
                    epochs_table.c.epoch == 1, epochs_table.c.state == "active").values(state="closed", ended_at_ms=now))
                guard_update = session.execute(update(guards).where(guards.c.session_id == session_id,
                    guards.c.active_epoch == 1, guards.c.revision == guard["revision"]).values(
                    active_epoch=None, revision=guard["revision"] + 1))
                if epoch_update.rowcount != 1 or guard_update.rowcount != 1:
                    _deny()
                _change_state(session, row, "admitted", current_epoch=None)
                new_state = "admitted"
            _audit_state(database, session, row, credential, "collection." + action, new_state, row["revision"] + 1)
    if inhibited:
        _deny()
    return {"session_id": session_id, "state": new_state, "revision": expected_revision + 1}


def read(database, *, session_id, credential):
    with owner_transaction(database) as session:
        row = _locked_session(session, session_id)
        _actor(row, credential, CAPABILITIES)
        result = {key: row[key] for key in ("state", "revision", "immutable_digest", "deadline_ms",
                                            "retain_until_ms", "current_epoch", "cancel_revision", "cancel_at_ms")}
        result["session_id"] = row["id"]
        return result


def currentness(database, *, session_id, credential):
    with owner_transaction(database) as session:
        row = _locked_session(session, session_id)
        payload = _actor(row, credential, CAPABILITIES)
        snapshot = _current_snapshot()
        request, _, _ = create_request_identity(payload["request_canonical"].encode())
        try:
            now = _check_snapshot(snapshot, payload["policy"], request)
        except CollectionOwnerError as exc:
            if exc.status != 409:
                raise
            now = snapshot["now_ms"] + snapshot["uncertainty_ms"]
            matches = False
        else:
            matches = True
        guard = _guard(session, session_id)
        epochs = _epoch_rows(session, session_id)
        if not _guard_consistent(row, guard, epochs, payload) or not _integrity(session, row, payload):
            _deny("collection_unavailable", 503)
        clean = _clean(session, session_id, row["scope_id"])
        valid_epoch = (len(epochs) == 1 and epochs[0]["state"] == "active"
                       and _epoch_binding_valid(row, epochs[0], payload))
        state_matches = row["state"] == "active" and row["current_epoch"] == 1 and guard["active_epoch"] == 1
        if row["state"] == "active" and (not state_matches or not valid_epoch):
            _deny("collection_unavailable", 503)
        current = bool(matches and _scope_current(session, row["scope_id"], now) and state_matches and valid_epoch and clean and row["cancel_revision"] is None
                       and row["cancel_at_ms"] is None and now < row["deadline_ms"]
                       and now < epochs[0]["deadline_ms"])
        return {"schema_version": "sovite.collection-currentness/v1", "session_id": session_id,
                "immutable_digest": row["immutable_digest"], "revision": row["revision"], "state": row["state"],
                "epoch": row["current_epoch"], "epoch_purpose": "execution" if valid_epoch else None,
                "deadline_ms": row["deadline_ms"], "cancel_revision": row["cancel_revision"],
                "observed_at_ms": snapshot["observed_at_ms"], "uncertainty_ms": snapshot["uncertainty_ms"],
                "current": current}
