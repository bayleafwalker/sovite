from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import select

from sovite.collection_transaction import owner_transaction
from sovite.db import AuditEvent, Database


def test_transaction_rolls_back_audit_on_failure(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/owner.db")
    db.migrate()
    try:
        with pytest.raises(RuntimeError):
            with owner_transaction(db) as session:
                db.audit(session, "actor", "collection.test", "session")
                session.flush()
                raise RuntimeError("commit must not happen")
        with db.sessions() as session:
            assert session.execute(select(AuditEvent)).scalars().all() == []
        assert db.verify_audit()["valid"]
    finally:
        db.engine.dispose()


def test_two_writers_serialize_and_keep_chain(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/owner.db")
    db.migrate()
    first_locked, release_first, second_started, second_locked = (Event() for _ in range(4))

    def first():
        with owner_transaction(db) as session:
            first_locked.set()
            assert release_first.wait(5)
            db.audit(session, "one", "collection.test", "one")

    def second():
        assert first_locked.wait(5)
        second_started.set()
        with owner_transaction(db) as session:
            second_locked.set()
            db.audit(session, "two", "collection.test", "two")

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(first)
            two = pool.submit(second)
            assert second_started.wait(5)
            assert not second_locked.wait(0.1)
            release_first.set()
            one.result(timeout=5)
            two.result(timeout=5)
        assert second_locked.is_set()
        assert db.verify_audit()["valid"]
        with db.sessions() as session:
            rows = session.execute(select(AuditEvent).order_by(AuditEvent.sequence)).scalars().all()
            assert [row.actor for row in rows] == ["one", "two"]
    finally:
        release_first.set()
        db.engine.dispose()
