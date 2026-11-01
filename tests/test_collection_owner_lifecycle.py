import json

import pytest
from sqlalchemy import select

from sovite import collection_owner as owner
from sovite.collection_identity import CollectionCredential
from sovite.db import Base, Database
from test_collection_owner_cancel import fixture, operator


def agent():
    return CollectionCredential("collection-agent", "collection-agent", "a" * 24)


def snapshot(db):
    sessions = Base.metadata.tables["collection_sessions"]
    with db.sessions() as session:
        row = session.execute(select(sessions)).mappings().one()
        payload = row["immutable_payload"]
    return {"policy": payload["policy"], "allowed_request": json.loads(payload["request_canonical"]),
            "now_ms": 10, "observed_at_ms": 10, "uncertainty_ms": 1, "drift_bounded": True,
            "anti_rollback_verified": True, "evidence_verified": True}


def change(db, digest, action, revision, credential=None, **values):
    return owner.transition(db, session_id="session", credential=credential or operator(),
                            expected_revision=revision, immutable_digest=digest, action=action, **values)


def test_unavailable_authority_refuses_admit(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/owner.db")
    db.migrate()
    try:
        digest = fixture(db)
        with pytest.raises(owner.CollectionOwnerError) as error:
            change(db, digest, "admit", 0)
        assert error.value.status == 503
        assert owner.read(db, session_id="session", credential=operator())["state"] == "created"
    finally:
        db.engine.dispose()


def test_one_epoch_currentness_and_cancel(tmp_path, monkeypatch):
    db = Database(f"sqlite:///{tmp_path}/owner.db")
    db.migrate()
    try:
        digest = fixture(db)
        evidence = snapshot(db)
        monkeypatch.setattr(owner, "_trusted_snapshot", lambda: evidence)
        assert change(db, digest, "admit", 0)["state"] == "admitted"
        with pytest.raises(owner.CollectionOwnerError):
            change(db, digest, "start", 1, credential=operator(), epoch_deadline_ms=80,
                   retain_until_ms=200, authorization_digest="a" * 64)
        assert change(db, digest, "start", 1, credential=agent(), epoch_deadline_ms=80,
                      retain_until_ms=200, authorization_digest="a" * 64)["state"] == "active"
        assert owner.currentness(db, session_id="session", credential=agent())["current"] is True
        assert change(db, digest, "close", 2, credential=agent())["state"] == "admitted"
        with pytest.raises(owner.CollectionOwnerError):
            change(db, digest, "start", 3, credential=agent(), epoch_deadline_ms=80,
                   retain_until_ms=200, authorization_digest="a" * 64)
        assert owner.cancel(db, session_id="session", credential=operator(), expected_revision=3,
                            immutable_digest=digest, reason_code="stop")["state"] == "cancel_pending"
        assert owner.currentness(db, session_id="session", credential=agent())["current"] is False
        assert db.verify_audit()["valid"]
    finally:
        db.engine.dispose()


def test_untrusted_clock_and_stale_revision_refused(tmp_path, monkeypatch):
    db = Database(f"sqlite:///{tmp_path}/owner.db")
    db.migrate()
    try:
        digest = fixture(db)
        evidence = snapshot(db)
        monkeypatch.setattr(owner, "_trusted_snapshot", lambda: evidence)
        for key in ("drift_bounded", "anti_rollback_verified", "evidence_verified"):
            evidence[key] = False
            with pytest.raises(owner.CollectionOwnerError) as error:
                change(db, digest, "admit", 0)
            assert error.value.status == 503
            evidence[key] = True
        with pytest.raises(owner.CollectionOwnerError):
            change(db, digest, "admit", 1)
        assert owner.read(db, session_id="session", credential=operator())["revision"] == 0
    finally:
        db.engine.dispose()


def test_create_idempotency_replays_without_current_policy(tmp_path, monkeypatch):
    db = Database(f"sqlite:///{tmp_path}/owner.db")
    db.migrate()
    try:
        fixture(db)
        evidence = snapshot(db)
        raw = json.dumps(evidence["allowed_request"]).encode()
        monkeypatch.setattr(owner, "_trusted_snapshot", lambda: evidence)
        created = owner.create(db, raw=raw, credential=operator(), idempotency_key="another-key")
        monkeypatch.setattr(owner, "_trusted_snapshot", lambda: owner._deny("collection_unavailable", 503))
        replay = owner.create(db, raw=raw, credential=operator(), idempotency_key="another-key")
        assert replay == created
        altered = dict(evidence["allowed_request"], deadline_ms=99)
        with pytest.raises(owner.CollectionOwnerError) as error:
            owner.create(db, raw=json.dumps(altered).encode(), credential=operator(), idempotency_key="another-key")
        assert error.value.status == 409
    finally:
        db.engine.dispose()


def test_inconsistent_guard_commits_inhibit(tmp_path, monkeypatch):
    db = Database(f"sqlite:///{tmp_path}/owner.db")
    db.migrate()
    try:
        digest = fixture(db)
        evidence = snapshot(db)
        monkeypatch.setattr(owner, "_trusted_snapshot", lambda: evidence)
        guard = Base.metadata.tables["collection_epoch_guards"]
        with db.engine.begin() as conn:
            conn.execute(guard.delete())
        with pytest.raises(owner.CollectionOwnerError):
            change(db, digest, "admit", 0)
        row = owner.read(db, session_id="session", credential=operator())
        assert (row["state"], row["revision"]) == ("stopped_unknown", 1)
        assert db.verify_audit()["valid"]
        with pytest.raises(owner.CollectionOwnerError):
            change(db, digest, "admit", 1)
    finally:
        db.engine.dispose()


def test_retention_and_clock_boundaries(tmp_path, monkeypatch):
    db = Database(f"sqlite:///{tmp_path}/owner.db")
    db.migrate()
    try:
        digest = fixture(db)
        evidence = snapshot(db)
        monkeypatch.setattr(owner, "_trusted_snapshot", lambda: evidence)
        change(db, digest, "admit", 0)
        for deadline, retention in ((80, 179), (80, 501), (101, 201)):
            with pytest.raises(owner.CollectionOwnerError):
                change(db, digest, "start", 1, credential=agent(), epoch_deadline_ms=deadline,
                       retain_until_ms=retention, authorization_digest="a" * 64)
        evidence["uncertainty_ms"] = 2001
        with pytest.raises(owner.CollectionOwnerError) as error:
            change(db, digest, "start", 1, credential=agent(), epoch_deadline_ms=80,
                   retain_until_ms=200, authorization_digest="a" * 64)
        assert error.value.status == 503
        assert owner.read(db, session_id="session", credential=operator())["revision"] == 1
    finally:
        db.engine.dispose()


def test_unknown_evidence_blocks_admission_and_guard_removal_control(tmp_path, monkeypatch):
    from sovite.collection_transaction import owner_transaction
    db = Database(f"sqlite:///{tmp_path}/owner.db")
    db.migrate()
    try:
        digest = fixture(db)
        evidence = snapshot(db)
        monkeypatch.setattr(owner, "_trusted_snapshot", lambda: evidence)
        with owner_transaction(db) as session:
            audit = db.audit(session, "fixture", "collection.test", "session")
            session.flush()
            session.execute(Base.metadata.tables["collection_checkpoints"].insert().values(
                session_id="session", sequence=1, artifact_digest="b" * 64, custody_ref="retained",
                retain_until_ms=200, audit_sequence=audit.sequence))
        with pytest.raises(owner.CollectionOwnerError):
            change(db, digest, "admit", 0)
        assert owner.read(db, session_id="session", credential=operator())["state"] == "stopped_unknown"
        with db.sessions() as session:
            assert session.execute(select(Base.metadata.tables["collection_checkpoints"])).first() is not None
    finally:
        db.engine.dispose()

    # The same negative assertion detects a deliberately removed clean guard.
    unguarded = Database(f"sqlite:///{tmp_path}/unguarded.db")
    unguarded.migrate()
    try:
        digest = fixture(unguarded)
        evidence = snapshot(unguarded)
        monkeypatch.setattr(owner, "_trusted_snapshot", lambda: evidence)
        with owner_transaction(unguarded) as session:
            audit = unguarded.audit(session, "fixture", "collection.test", "session")
            session.flush()
            session.execute(Base.metadata.tables["collection_checkpoints"].insert().values(
                session_id="session", sequence=1, artifact_digest="b" * 64, custody_ref="retained",
                retain_until_ms=200, audit_sequence=audit.sequence))
        monkeypatch.setattr(owner, "_clean", lambda *_: True)
        result = change(unguarded, digest, "admit", 0)
        with pytest.raises(AssertionError):
            assert result["state"] != "admitted"
    finally:
        unguarded.engine.dispose()


def test_cancel_currentness_snapshot_and_policy_change(tmp_path, monkeypatch):
    db = Database(f"sqlite:///{tmp_path}/owner.db")
    db.migrate()
    try:
        digest = fixture(db)
        evidence = snapshot(db)
        monkeypatch.setattr(owner, "_trusted_snapshot", lambda: evidence)
        change(db, digest, "admit", 0)
        change(db, digest, "start", 1, credential=agent(), epoch_deadline_ms=80,
               retain_until_ms=200, authorization_digest="a" * 64)
        earlier = owner.currentness(db, session_id="session", credential=agent())
        assert earlier["current"] is True
        owner.cancel(db, session_id="session", credential=operator(), expected_revision=2,
                     immutable_digest=digest, reason_code="stop")
        assert not owner.currentness(db, session_id="session", credential=agent())["current"]
        assert earlier["current"] is True  # a past snapshot is not revocable or an effect permit
        evidence["anti_rollback_verified"] = False
        with pytest.raises(owner.CollectionOwnerError) as error:
            owner.currentness(db, session_id="session", credential=agent())
        assert error.value.status == 503
    finally:
        db.engine.dispose()


def test_create_idempotency_race_one_subject(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    db = Database(f"sqlite:///{tmp_path}/owner.db")
    db.migrate()
    try:
        fixture(db)
        evidence = snapshot(db)
        monkeypatch.setattr(owner, "_trusted_snapshot", lambda: evidence)
        raw = json.dumps(evidence["allowed_request"]).encode()
        barrier = Barrier(2)
        def create():
            barrier.wait(timeout=5)
            return owner.create(db, raw=raw, credential=operator(), idempotency_key="race-key")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: create(), range(2)))
        assert results[0] == results[1]
        with db.sessions() as session:
            sessions = Base.metadata.tables["collection_sessions"]
            rows = session.execute(select(sessions.c.id).where(sessions.c.idempotency_key == "race-key")).all()
            assert len(rows) == 1
        assert db.verify_audit()["valid"]
    finally:
        db.engine.dispose()


def test_unknown_accounting_is_retained_and_inhibits(tmp_path, monkeypatch):
    from sovite.collection_transaction import owner_transaction
    db = Database(f"sqlite:///{tmp_path}/owner.db")
    db.migrate()
    try:
        digest = fixture(db)
        evidence = snapshot(db)
        monkeypatch.setattr(owner, "_trusted_snapshot", lambda: evidence)
        with owner_transaction(db) as session:
            audit = db.audit(session, "fixture", "collection.test", "session")
            session.flush()
            reservation = Base.metadata.tables["program_reservations"]
            slots = Base.metadata.tables["program_slots"]
            session.execute(reservation.insert().values(scope_id="scope", call_id="call", subject_id="session",
                purpose="collection", effect_key="effect", reserved_usd_micro=12, reserved_allowance_micro=8,
                actual_usd_micro=None, actual_allowance_micro=None, permit_digest="a" * 64,
                meter_digest="b" * 64, price_digest="c" * 64, permit_expires_ms=100,
                state="unknown", audit_sequence=audit.sequence))
            session.execute(slots.insert().values(scope_id="scope", slot_id="slot", subject_id="session",
                call_id="call", state="unknown", audit_sequence=audit.sequence))
        with pytest.raises(owner.CollectionOwnerError):
            change(db, digest, "admit", 0)
        assert owner.read(db, session_id="session", credential=operator())["state"] == "stopped_unknown"
        with db.sessions() as session:
            retained_reservation = session.execute(select(reservation)).mappings().one()
            retained_slot = session.execute(select(slots)).mappings().one()
            assert (retained_reservation["state"], retained_reservation["reserved_usd_micro"], retained_slot["state"]) == ("unknown", 12, "unknown")
        db.engine.dispose()
        db = Database(f"sqlite:///{tmp_path}/owner.db")
        assert owner.read(db, session_id="session", credential=operator())["state"] == "stopped_unknown"
        assert db.verify_audit()["valid"]
    finally:
        db.engine.dispose()


def test_altered_epoch_binding_never_current(tmp_path, monkeypatch):
    db = Database(f"sqlite:///{tmp_path}/owner.db")
    db.migrate()
    try:
        digest = fixture(db)
        evidence = snapshot(db)
        monkeypatch.setattr(owner, "_trusted_snapshot", lambda: evidence)
        change(db, digest, "admit", 0)
        change(db, digest, "start", 1, credential=agent(), epoch_deadline_ms=80,
               retain_until_ms=200, authorization_digest="a" * 64)
        epochs = Base.metadata.tables["collection_epochs"]
        with db.engine.begin() as connection:
            connection.execute(epochs.update().values(authorization_digest="f" * 64))
        with pytest.raises(owner.CollectionOwnerError) as error:
            owner.currentness(db, session_id="session", credential=agent())
        assert error.value.status == 503
        with pytest.raises(owner.CollectionOwnerError):
            change(db, digest, "close", 2, credential=agent())
        assert owner.read(db, session_id="session", credential=operator())["state"] == "stopped_unknown"
    finally:
        db.engine.dispose()


def test_scope_policy_revision_change_refuses_without_rebinding(tmp_path, monkeypatch):
    db = Database(f"sqlite:///{tmp_path}/owner.db")
    db.migrate()
    try:
        digest = fixture(db)
        evidence = snapshot(db)
        monkeypatch.setattr(owner, "_trusted_snapshot", lambda: evidence)
        scope = Base.metadata.tables["program_budget_scopes"]
        with db.engine.begin() as connection:
            connection.execute(scope.update().values(policy_revision=2))
        with pytest.raises(owner.CollectionOwnerError):
            change(db, digest, "admit", 0)
        assert owner.read(db, session_id="session", credential=operator())["state"] == "created"
        assert owner.currentness(db, session_id="session", credential=operator())["current"] is False
    finally:
        db.engine.dispose()
