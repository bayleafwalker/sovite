"""These tests require a separately created, isolated PostgreSQL fixture."""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import create_engine, text

from sovite import collection_owner as owner
from sovite.db import Database
from test_collection_owner_cancel import fixture, invoke, operator
from test_collection_owner_lifecycle import agent, change, snapshot


@pytest.fixture
def pg():
    url = os.environ.get("SOVITE_UNIT3_TEST_POSTGRES")
    if not url:
        pytest.skip("isolated PostgreSQL fixture not configured")
    schema = "unit3_" + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    db = Database(url, schema=schema)
    try:
        db.migrate()
        yield db
    finally:
        db.engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_postgres_cancel_race_and_audit(pg):
    digest, barrier = fixture(pg), Barrier(2)
    def race():
        barrier.wait(timeout=5)
        try:
            invoke(pg, digest)
            return 200
        except owner.CollectionOwnerError as error:
            return error.status
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: race(), range(2))) == [200, 409]
    assert pg.verify_audit()["valid"]


def test_postgres_epoch_cas_and_retention(pg, monkeypatch):
    digest = fixture(pg)
    evidence = snapshot(pg)
    monkeypatch.setattr(owner, "_trusted_snapshot", lambda: evidence)
    change(pg, digest, "admit", 0)
    barrier = Barrier(2)
    def race():
        barrier.wait(timeout=5)
        try:
            change(pg, digest, "start", 1, credential=agent(), epoch_deadline_ms=80,
                   retain_until_ms=200, authorization_digest="a" * 64)
            return 200
        except owner.CollectionOwnerError as error:
            return error.status
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: race(), range(2))) == [200, 409]
    assert owner.currentness(pg, session_id="session", credential=operator())["current"]
    change(pg, digest, "close", 2, credential=agent())
    assert pg.verify_audit()["valid"]


def test_postgres_duplicate_create_and_post_cancel_currentness(pg, monkeypatch):
    import json
    fixture(pg)
    evidence = snapshot(pg)
    monkeypatch.setattr(owner, "_trusted_snapshot", lambda: evidence)
    raw = json.dumps(evidence["allowed_request"]).encode()
    barrier = Barrier(2)
    def create():
        barrier.wait(timeout=5)
        return owner.create(pg, raw=raw, credential=operator(), idempotency_key="parallel-create")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(), range(2)))
    assert results[0] == results[1]
    session_id, digest = results[0]["session_id"], results[0]["immutable_digest"]
    owner.transition(pg, session_id=session_id, credential=operator(), expected_revision=0,
                     immutable_digest=digest, action="admit")
    owner.transition(pg, session_id=session_id, credential=agent(), expected_revision=1,
                     immutable_digest=digest, action="start", epoch_deadline_ms=80,
                     retain_until_ms=200, authorization_digest="a" * 64)
    assert owner.currentness(pg, session_id=session_id, credential=agent())["current"]
    owner.cancel(pg, session_id=session_id, credential=operator(), expected_revision=2,
                 immutable_digest=digest, reason_code="stop")
    assert not owner.currentness(pg, session_id=session_id, credential=agent())["current"]
    assert pg.verify_audit()["valid"]


def test_postgres_cross_scope_subject_inhibits(pg, monkeypatch):
    from sovite.collection_transaction import owner_transaction
    from sovite.db import Base
    digest = fixture(pg)
    evidence = snapshot(pg)
    monkeypatch.setattr(owner, "_trusted_snapshot", lambda: evidence)
    with owner_transaction(pg) as session:
        tables = Base.metadata.tables
        session.execute(tables["program_budget_scopes"].insert().values(id="other-scope", policy_revision=1,
            window_start_ms=0, window_end_ms=1000, usd_limit_micro=0, allowance_unit="unit",
            allowance_limit_micro=0, slot_limit=0, policy_digest="6" * 64))
        session.execute(tables["program_budget_subjects"].insert().values(scope_id="other-scope",
            subject_id="session", purpose="collection", binding_digest=digest))
    with pytest.raises(owner.CollectionOwnerError):
        change(pg, digest, "admit", 0)
    assert owner.read(pg, session_id="session", credential=operator())["state"] == "stopped_unknown"


def test_postgres_malformed_active_epoch_never_current(pg, monkeypatch):
    from sovite.db import Base
    digest = fixture(pg)
    evidence = snapshot(pg)
    monkeypatch.setattr(owner, "_trusted_snapshot", lambda: evidence)
    change(pg, digest, "admit", 0)
    change(pg, digest, "start", 1, credential=agent(), epoch_deadline_ms=80,
           retain_until_ms=200, authorization_digest="a" * 64)
    epochs = Base.metadata.tables["collection_epochs"]
    with pg.engine.begin() as connection:
        connection.execute(epochs.update().values(started_at_ms=None))
    with pytest.raises(owner.CollectionOwnerError) as error:
        owner.currentness(pg, session_id="session", credential=agent())
    assert error.value.status == 503
