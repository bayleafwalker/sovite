import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import select

from sovite.collection_identity import CollectionCredential, create_request_identity
from sovite.collection_owner import CollectionOwnerError, cancel, immutable_payload_digest
from sovite.db import Base, Database


def fixture(db):
    table = Base.metadata.tables
    principals = {role: role for role in ("collection-operator", "collection-agent",
                                           "collection-reconciler", "collection-recovery")}
    request = {"schema_version": "sovite.collection-session/v1", "workspace_id": "workspace",
               "repository_id": "repo", "generation": "sha256:" + "1" * 64,
               "suite_version": "suite-v1", "suite_digest": "2" * 64,
               "deployment_digest": "3" * 64, "permit_digest": "4" * 64,
               "deadline_ms": 100, "retain_until_ms": 200}
    _, canonical, request_digest = create_request_identity(json.dumps(request).encode())
    policy = {"schema_version": "sovite.collection-owner-policy/v1", "policy_revision": 1,
              "policy_digest": "5" * 64, "scope_id": "scope", "scope_policy_digest": "6" * 64,
              "principals": principals, "seed_digest": "7" * 64, "base_sha": "8" * 64,
              "paths_digest": "9" * 64, "minimum_retention_ms": 100,
              "maximum_retain_until_ms": 500, "resource_deadline_ms": 100,
              "commissioning_deadline_ms": 100, "permit_deadline_ms": 100,
              "epoch_authorization_digest": "a" * 64}
    payload = {"schema_version": "sovite.collection-binding/v1", "request_canonical": canonical,
               "request_digest": request_digest, "policy": policy}
    digest = immutable_payload_digest(payload)
    with db.engine.begin() as conn:
        conn.execute(table["program_budget_scopes"].insert().values(id="scope", policy_revision=1,
            window_start_ms=0, window_end_ms=1000, usd_limit_micro=0, allowance_unit="unit",
            allowance_limit_micro=0, slot_limit=0, policy_digest="6" * 64))
        conn.execute(table["program_budget_subjects"].insert().values(scope_id="scope", subject_id="session",
            purpose="collection", binding_digest=digest))
        conn.execute(table["collection_sessions"].insert().values(id="session", principal_id=principals["collection-operator"],
            idempotency_key="key", scope_id="scope", purpose="operator_c0_collection", revision=0,
            current_epoch=None, cancel_revision=None, cancel_at_ms=None, state="created",
            immutable_digest=digest, immutable_payload=payload, seed_digest=policy["seed_digest"],
            base_sha=policy["base_sha"], paths_digest=policy["paths_digest"], **request))
        conn.execute(table["collection_epoch_guards"].insert().values(session_id="session", active_epoch=None, revision=0))
    return digest


def operator():
    return CollectionCredential("collection-operator", "collection-operator", "x" * 24)


def invoke(db, digest, credential=None):
    return cancel(db, session_id="session", credential=credential or operator(), expected_revision=0,
                  immutable_digest=digest, reason_code="operator_requested")


def test_cancel_cas_retry_actor_and_audit(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/owner.db")
    db.migrate()
    try:
        digest = fixture(db)
        with pytest.raises(CollectionOwnerError) as exc:
            invoke(db, digest, CollectionCredential("collection-agent", "collection-agent", "a" * 24))
        assert exc.value.status == 403
        assert invoke(db, digest)["state"] == "cancel_pending"
        with pytest.raises(CollectionOwnerError) as exc:
            invoke(db, digest)
        assert exc.value.status == 409
        with db.sessions() as session:
            row = session.execute(select(Base.metadata.tables["collection_sessions"])).mappings().one()
            guard = session.execute(select(Base.metadata.tables["collection_epoch_guards"])).mappings().one()
            assert (row["state"], row["revision"], row["cancel_revision"]) == ("cancel_pending", 1, 1)
            assert row["cancel_at_ms"] > 0 and guard["active_epoch"] is None
        assert db.verify_audit()["valid"]
    finally:
        db.engine.dispose()


def test_cancel_audit_failure_rolls_back(tmp_path, monkeypatch):
    db = Database(f"sqlite:///{tmp_path}/owner.db")
    db.migrate()
    try:
        digest = fixture(db)
        def fail(*args, **kwargs):
            raise RuntimeError("audit unavailable")
        monkeypatch.setattr(db, "audit", fail)
        with pytest.raises(RuntimeError):
            invoke(db, digest)
        with db.sessions() as session:
            row = session.execute(select(Base.metadata.tables["collection_sessions"])).mappings().one()
            assert (row["state"], row["revision"], row["cancel_revision"]) == ("created", 0, None)
    finally:
        db.engine.dispose()


def test_competing_cancel_has_one_winner(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/owner.db")
    db.migrate()
    try:
        digest, barrier = fixture(db), Barrier(2)
        def race():
            barrier.wait(timeout=5)
            try:
                invoke(db, digest)
                return 200
            except CollectionOwnerError as exc:
                return exc.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(lambda _: race(), range(2))) == [200, 409]
        assert db.verify_audit()["valid"]
    finally:
        db.engine.dispose()


def test_cancel_refuses_tampered_binding_and_broken_audit(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/owner.db")
    db.migrate()
    try:
        digest = fixture(db)
        sessions = Base.metadata.tables["collection_sessions"]
        with db.engine.begin() as conn:
            conn.execute(sessions.update().values(workspace_id="changed"))
        with pytest.raises(CollectionOwnerError):
            invoke(db, digest)
        with db.engine.begin() as conn:
            conn.execute(sessions.update().values(workspace_id="workspace"))
            metadata = Base.metadata.tables["platform_metadata"]
            conn.execute(metadata.update().where(metadata.c.key == "audit_head").values(value={"sequence": 1, "hash": "0" * 64}))
        with pytest.raises(ValueError, match="audit unavailable"):
            invoke(db, digest)
        with db.sessions() as session:
            assert session.execute(select(sessions.c.state)).scalar_one() == "created"
    finally:
        db.engine.dispose()


def test_auth_guard_removal_control_detects_forbidden_cancel(tmp_path, monkeypatch):
    from sovite import collection_owner as owner
    db = Database(f"sqlite:///{tmp_path}/unguarded.db")
    db.migrate()
    try:
        digest = fixture(db)
        agent = CollectionCredential("collection-agent", "collection-agent", "a" * 24)
        monkeypatch.setattr(owner, "_actor", lambda row, credential, allowed: row["immutable_payload"])
        result = invoke(db, digest, agent)
        with pytest.raises(AssertionError):
            assert result["state"] != "cancel_pending"
    finally:
        db.engine.dispose()


def test_audit_guard_removal_control_detects_broken_chain(tmp_path, monkeypatch):
    from sovite import collection_transaction as tx
    db = Database(f"sqlite:///{tmp_path}/unguarded.db")
    db.migrate()
    try:
        digest = fixture(db)
        metadata = Base.metadata.tables["platform_metadata"]
        with db.engine.begin() as conn:
            conn.execute(metadata.update().where(metadata.c.key == "audit_head").values(
                value={"sequence": 1, "hash": "0" * 64}))
        monkeypatch.setattr(tx, "_verify_audit", lambda session: None)
        invoke(db, digest)
        with pytest.raises(AssertionError):
            assert db.verify_audit()["valid"]
    finally:
        db.engine.dispose()
