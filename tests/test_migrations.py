"""Upgrade the released schema without changing durable authority or job snapshots."""
import os
import uuid

import pytest
from sqlalchemy import create_engine, event, inspect

from sovite.db import (AuthSession, Base, BindingState, BundleRevision, Database,
                       GrantState, Job, MetadataRow, Qualification)
from sovite.policy import load_bundle, resolve_recipe


@pytest.fixture(params=["sqlite", "postgresql"])
def database(request, tmp_path):
    if request.param == "sqlite":
        db = Database(f"sqlite:///{tmp_path}/upgrade.db")
        yield db
        db.engine.dispose()
        return
    url = os.environ.get("SOVITE_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("SOVITE_TEST_POSTGRES_URL is unset")
    if url.startswith(("postgresql://", "postgres://")):
        url = "postgresql+psycopg://" + url.split("://", 1)[1]
    admin = create_engine(url)
    schema = "sovite_upgrade_" + uuid.uuid4().hex
    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    db = Database(url)
    @event.listens_for(db.engine, "connect")
    def search_path(dbapi_connection, connection_record):
        with dbapi_connection.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{schema}"')
        dbapi_connection.commit()
    try:
        yield db
    finally:
        db.engine.dispose()
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()


def released_schema(db):
    Base.metadata.create_all(db.engine, tables=[table for table in Base.metadata.sorted_tables
                                               if table is not BindingState.__table__])
    with db.sessions.begin() as session:
        session.add(MetadataRow(key="schema_version", value={"version": 1}))
        session.add(MetadataRow(key="audit_head", value={"sequence": 0, "hash": "0" * 64}))


def test_upgrade_v1_preserves_jobs_bundle_and_operational_state(database):
    db = database
    released_schema(db)
    bundle, generation = load_bundle("config/platform.local.yaml")
    recipe = resolve_recipe(bundle, "incident-forensics")
    with db.sessions.begin() as session:
        session.add(BundleRevision(generation=generation, bundle=bundle, created_at=123))
        session.add(MetadataRow(key="active_generation", value={"generation": generation}))
        session.add(GrantState(id="incident-forensics", inhibited=True, reason="Existing inhibit", updated_at=123))
        session.add(Job(id="old-job", receipt="old-receipt", idempotency_key="old-key", request_digest="old-digest",
                        caller="operations", grant_id="incident-forensics", generation=generation,
                        resolved=recipe, task="Private queued task", status="retry_wait", attempts=2,
                        lease_owner="old-worker", lease_until=123, next_attempt_at=456))
        session.add(AuthSession(digest="old-session", identity="operator", role="operator", expires_at=789))
        session.add(Qualification(id="old-qualification", generation=generation, profile="local", created_at=123,
                                  expires_at=789, passed=True, issuer="operator", payload={"legacy": True},
                                  signature="old-signature", public_key="old-key"))
    with db.sessions.begin() as session:
        db.audit(session, "operator", "old-event", "incident-forensics")
    audit = db.verify_audit()
    assert audit["valid"]
    assert "binding_runtime_state" not in inspect(db.engine).get_table_names()
    db.initialize()
    assert "binding_runtime_state" in inspect(db.engine).get_table_names()
    with db.sessions.begin() as session:
        assert session.get(MetadataRow, "schema_version").value == {"version": 2}
        assert session.get(MetadataRow, "active_generation").value == {"generation": generation}
        assert session.get(BundleRevision, generation).bundle == bundle
        job = session.get(Job, "old-job")
        assert (job.generation, job.resolved, job.status, job.attempts) == (generation, recipe, "retry_wait", 2)
        assert (job.lease_owner, job.lease_until, job.next_attempt_at) == ("old-worker", 123, 456)
        assert job.task == "Private queued task"
        assert session.get(GrantState, "incident-forensics").inhibited
        assert session.get(GrantState, "incident-forensics").reason == "Existing inhibit"
        assert session.get(AuthSession, "old-session").expires_at == 789
        assert session.get(Qualification, "old-qualification").signature == "old-signature"
        assert session.get(BindingState, "assessment-model") is None
        session.add(BindingState(id="assessment-model", inhibited=True, reason="Operator stop", updated_at=500))
    db.initialize()
    with db.sessions() as session:
        binding = session.get(BindingState, "assessment-model")
        assert (binding.inhibited, binding.reason, binding.updated_at) == (True, "Operator stop", 500)
        assert session.get(BundleRevision, generation).bundle["models"][0].get("enabled", True)
    assert db.verify_audit() == audit


def test_fresh_schema_stores_and_reads_binding_state(database):
    db = database
    db.initialize()
    with db.sessions.begin() as session:
        assert session.get(MetadataRow, "schema_version").value == {"version": 2}
        session.add(BindingState(id="model-a"))
    with db.sessions.begin() as session:
        state = session.get(BindingState, "model-a")
        assert not state.inhibited and state.reason == "" and state.updated_at > 0
        state.inhibited, state.reason, state.updated_at = True, "Maintenance", 123
    db.initialize()
    with db.sessions.begin() as session:
        state = session.get(BindingState, "model-a")
        assert (state.inhibited, state.reason, state.updated_at) == (True, "Maintenance", 123)
        state.inhibited, state.reason, state.updated_at = False, "Resumed", 456
    with db.sessions() as session:
        assert not session.get(BindingState, "model-a").inhibited
    assert db.verify_audit()["valid"]


def test_upgrade_is_idempotent_after_table_creation(database):
    db = database
    released_schema(db)
    BindingState.__table__.create(db.engine)
    with db.sessions.begin() as session:
        session.add(BindingState(id="model-a", inhibited=True, reason="Preserve during upgrade"))
    db.initialize()
    db.initialize()
    with db.sessions() as session:
        assert session.get(MetadataRow, "schema_version").value == {"version": 2}
        assert session.get(BindingState, "model-a").inhibited


def test_unknown_schema_version_rejected_before_migration(database):
    db = database
    released_schema(db)
    with db.sessions.begin() as session:
        session.get(MetadataRow, "schema_version").value = {"version": 99}
    with pytest.raises(ValueError, match="Unsupported database schema_version"):
        db.initialize()
    assert "binding_runtime_state" not in inspect(db.engine).get_table_names()
    with db.sessions() as session:
        assert session.get(MetadataRow, "schema_version").value == {"version": 99}
