"""Upgrade the released schema without changing durable authority or job snapshots."""
import hashlib
import json
import os
import sqlite3
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import (Boolean, Column, Float, Integer, JSON, MetaData, String, Table, Text,
                        UniqueConstraint, cast, create_engine, event, inspect, select, text)
from sqlalchemy.engine import Engine

from sovite.db import (AuthSession, Base, BindingState, BundleRevision, Database,
                       DatabaseSchemaError, GrantState, Job, MetadataRow, Qualification, MIGRATIONS)
from sovite.policy import load_bundle, resolve_recipe


@pytest.fixture
def database_cli(database, monkeypatch, capsys):
    from sovite.cli import main
    monkeypatch.setenv("SOVITE_DATABASE_URL", database.engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv("SOVITE_DATABASE_SCHEMA", database.schema or "")
    monkeypatch.setenv("SOVITE_DATABASE_MIGRATE", "never")
    monkeypatch.delenv("SOVITE_OPERATOR_TOKEN", raising=False)
    def run(action, code=None):
        import json
        monkeypatch.setattr("sys.argv", ["sovite", "db", action])
        if code:
            with pytest.raises(SystemExit) as failure:
                main()
            assert failure.value.code == 1
            result = capsys.readouterr()
            assert not result.out
            assert json.loads(result.err)["code"] == code
        else:
            main()
            result = capsys.readouterr()
            assert not result.err
            assert json.loads(result.out) == {"status": "compatible", "schema_version": 5}
    return run


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
    db = Database(url, schema=schema)
    try:
        yield db
    finally:
        db.engine.dispose()
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()


def released_schema(db):
    Base.metadata.create_all(db.engine, tables=list(MIGRATIONS[0][1]))
    with db.sessions.begin() as session:
        session.add(MetadataRow(key="schema_version", value={"version": 1}))
        session.add(MetadataRow(key="audit_head", value={"sequence": 0, "hash": "0" * 64}))


def test_upgrade_v1_preserves_jobs_bundle_and_operational_state(database, database_cli):
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
    assert "binding_runtime_state" not in inspect(db.engine).get_table_names(schema=db.schema)
    database_cli("migrate")
    assert "binding_runtime_state" in inspect(db.engine).get_table_names(schema=db.schema)
    with db.sessions.begin() as session:
        assert session.get(MetadataRow, "schema_version").value == {"version": 5}
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
        assert session.get(MetadataRow, "schema_version").value == {"version": 5}
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
        assert session.get(MetadataRow, "schema_version").value == {"version": 5}
        assert session.get(BindingState, "model-a").inhibited


def test_unknown_schema_version_rejected_before_migration(database):
    db = database
    released_schema(db)
    with db.sessions.begin() as session:
        session.get(MetadataRow, "schema_version").value = {"version": 99}
    with pytest.raises(ValueError, match="Unsupported database schema_version"):
        db.initialize()
    assert "binding_runtime_state" not in inspect(db.engine).get_table_names(schema=db.schema)
    with db.sessions() as session:
        assert session.get(MetadataRow, "schema_version").value == {"version": 99}


def test_db_migrate_v2_preserves_state_and_is_idempotent(database, database_cli):
    released_schema(database)
    BindingState.__table__.create(database.engine)
    with database.sessions.begin() as session:
        session.get(MetadataRow, "schema_version").value = {"version": 2}
    with database.sessions.begin() as session:
        session.add(BindingState(id="model-v2", inhibited=True, reason="Preserve v2"))
        database.audit(session, "operator", "v2-event", "model-v2")
    before = database.verify_audit()
    database_cli("migrate")
    database_cli("migrate")
    with database.sessions() as session:
        assert session.get(BindingState, "model-v2").reason == "Preserve v2"
    assert database.verify_audit() == before


@pytest.mark.parametrize("state,code", [
    ("missing", "database_schema_missing"),
    ("older", "database_schema_older"),
    ("newer", "database_schema_newer"),
    ("unknown", "database_schema_unknown"),
    ("unversioned", "database_schema_incomplete"),
    ("partial", "database_schema_incomplete"),
    ("column", "database_schema_incomplete"),
    ("index", "database_schema_incomplete"),
    ("audit", "database_schema_incomplete"),
    ("compatible", None),
])
def test_db_check_outcomes_are_read_only(database, database_cli, state, code):
    if state == "older":
        released_schema(database)
    elif state != "missing":
        database.migrate()
        with database.sessions.begin() as session:
            if state in {"newer", "unknown"}:
                session.get(MetadataRow, "schema_version").value = {"version": 99 if state == "newer" else True}
            if state in {"unversioned", "audit"}:
                session.delete(session.get(MetadataRow, "schema_version" if state == "unversioned" else "audit_head"))
        with database.engine.begin() as connection:
            if state == "partial":
                BindingState.__table__.drop(connection)
            elif state == "column":
                table = f'"{database.schema}".jobs' if database.schema else 'jobs'
                connection.exec_driver_sql(f'ALTER TABLE {table} DROP COLUMN task')
            elif state == "index":
                index = f'"{database.schema}".ix_jobs_status' if database.schema else 'ix_jobs_status'
                connection.exec_driver_sql(f'DROP INDEX {index}')
    def read_only(connection, cursor, statement, parameters, context, executemany):
        assert not context.isddl
        assert statement.lstrip().split()[0].upper() in {"SELECT", "PRAGMA"}
    event.listen(Engine, "before_cursor_execute", read_only)
    try:
        database_cli("check", code)
    finally:
        event.remove(Engine, "before_cursor_execute", read_only)
    if state == "missing" and database.path:
        assert not database.path.exists()


@pytest.mark.parametrize("version,code", [
    ({"version": 99}, "database_schema_newer"),
    ({"version": 0}, "database_schema_unknown"),
    ({"version": "2"}, "database_schema_unknown"),
    ({"version": True}, "database_schema_unknown"),
    ({"version": 2, "extra": True}, "database_schema_unknown"),
])
def test_db_migrate_refuses_unsupported_versions_without_ddl(database, database_cli, version, code):
    released_schema(database)
    with database.sessions.begin() as session:
        session.execute(MetadataRow.__table__.update().where(MetadataRow.key == "schema_version").values(value=version))
    def no_ddl(connection, cursor, statement, parameters, context, executemany):
        assert not context.isddl
        assert statement.lstrip().split()[0].upper() in {"SELECT", "PRAGMA"}
    event.listen(Engine, "before_cursor_execute", no_ddl)
    try:
        database_cli("migrate", code)
    finally:
        event.remove(Engine, "before_cursor_execute", no_ddl)
    with database.sessions() as session:
        assert session.get(MetadataRow, "schema_version").value == version


@pytest.mark.parametrize("state", ["unversioned", "missing_metadata", "partial_v1", "partial_v2", "audit"])
def test_db_migrate_refuses_incomplete_schema_without_ddl(database, database_cli, state):
    if state == "partial_v1":
        released_schema(database)
    else:
        database.migrate()
    with database.sessions.begin() as session:
        if state in {"unversioned", "audit"}:
            session.delete(session.get(MetadataRow, "schema_version" if state == "unversioned" else "audit_head"))
    with database.engine.begin() as connection:
        if state == "missing_metadata":
            MetadataRow.__table__.drop(connection)
        if state.startswith("partial"):
            Job.__table__.drop(connection)
    def no_ddl(connection, cursor, statement, parameters, context, executemany):
        assert not context.isddl
        assert statement.lstrip().split()[0].upper() in {"SELECT", "PRAGMA"}
    event.listen(Engine, "before_cursor_execute", no_ddl)
    try:
        database_cli("migrate", "database_schema_incomplete")
    finally:
        event.remove(Engine, "before_cursor_execute", no_ddl)


@pytest.mark.parametrize("entrypoint", ["platform", "api", "worker"])
@pytest.mark.parametrize("state,code", [("missing", "database_schema_missing"), ("older", "database_schema_older"),
                                       ("newer", "database_schema_newer"), ("partial", "database_schema_incomplete"),
                                       ("compatible", None)])
def test_runtime_never_issues_ddl(database, tmp_path, entrypoint, state, code):
    import asyncio
    from fastapi.testclient import TestClient
    from sovite.api import create_app
    from sovite.service import Platform
    from sovite.settings import Settings
    from sovite.worker import Worker
    if state == "older":
        released_schema(database)
    elif state != "missing":
        database.migrate()
        if state == "partial":
            BindingState.__table__.drop(database.engine)
        if state == "newer":
            with database.sessions.begin() as session:
                session.get(MetadataRow, "schema_version").value = {"version": 99}
    settings = Settings(database_url=database.engine.url.render_as_string(hide_password=False),
                        database_schema=database.schema, database_migrate="never", require_qualification=False,
                        artifact_path=str(tmp_path / "artifacts"))
    platform = Platform(settings)
    def start():
        if entrypoint == "api":
            with TestClient(create_app(settings, platform)) as client:
                assert client.get("/health/live").status_code == 200
        elif entrypoint == "worker":
            worker = Worker(platform)
            worker.stop.set()
            asyncio.run(worker.run())
        else:
            platform.initialize()
    def no_ddl(connection, cursor, statement, parameters, context, executemany):
        assert not context.isddl
        assert statement.lstrip().split()[0].upper() not in {"CREATE", "ALTER", "DROP", "TRUNCATE"}
        if code:
            assert statement.lstrip().split()[0].upper() in {"SELECT", "PRAGMA"}
    event.listen(Engine, "before_cursor_execute", no_ddl)
    try:
        if code:
            with pytest.raises(DatabaseSchemaError) as failure:
                start()
            assert failure.value.code == code
        else:
            start()
            assert platform.active()[0]["mode"] == "development"
    finally:
        event.remove(Engine, "before_cursor_execute", no_ddl)
        platform.db.engine.dispose()


@pytest.mark.parametrize("schema", ["public", "pg_temp", "information_schema", "Upper", "has.dot", "has space", 'bad";--', "a" * 64, "_prefix", ""])
def test_database_schema_names_are_strict(schema):
    from sovite.settings import Settings
    with pytest.raises(ValueError, match="SOVITE_DATABASE_SCHEMA"):
        Settings(database_schema=schema)
    with pytest.raises(ValueError, match="SOVITE_DATABASE_SCHEMA"):
        Database("postgresql+psycopg://unused/db", schema=schema)


def test_sqlite_refuses_configured_schema(tmp_path):
    with pytest.raises(ValueError, match="only on PostgreSQL"):
        Database(f"sqlite:///{tmp_path}/must-not-exist.db", schema="sovite")
    assert not (tmp_path / "must-not-exist.db").exists()


def test_database_settings_defaults_and_environment(monkeypatch):
    from sovite.settings import Settings
    for key in ("SOVITE_DATABASE_URL", "SOVITE_DATABASE_SCHEMA", "SOVITE_DATABASE_MIGRATE"):
        monkeypatch.delenv(key, raising=False)
    assert Settings.from_database_env() == Settings()
    monkeypatch.setenv("SOVITE_DATABASE_URL", "postgresql+psycopg://unused/db")
    monkeypatch.setenv("SOVITE_DATABASE_SCHEMA", "workspace_sovite")
    monkeypatch.setenv("SOVITE_DATABASE_MIGRATE", "never")
    monkeypatch.setenv("SOVITE_OPERATOR_TOKEN", "x" * 24)
    settings = Settings.from_env()
    assert settings.database_schema == "workspace_sovite" and settings.database_migrate == "never"
    monkeypatch.setenv("SOVITE_DATABASE_MIGRATE", "sometimes")
    with pytest.raises(ValueError, match="SOVITE_DATABASE_MIGRATE"):
        Settings.from_env()


@pytest.fixture
def postgres_roles():
    url = os.environ.get("SOVITE_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("SOVITE_TEST_POSTGRES_URL is unset")
    if url.startswith(("postgresql://", "postgres://")):
        url = "postgresql+psycopg://" + url.split("://", 1)[1]
    admin = create_engine(url)
    suffix = uuid.uuid4().hex
    schema, other = "sovite_a_" + suffix, "sovite_b_" + suffix
    migration_role, runtime_role = "sovite_m_" + suffix, "sovite_r_" + suffix
    databases = []
    def scoped(role, target):
        db = Database(url, schema=target)
        databases.append(db)
        @event.listens_for(db.engine, "connect")
        def role_connection(dbapi_connection, connection_record):
            with dbapi_connection.cursor() as cursor:
                cursor.execute(f'SET ROLE "{role}"')
                cursor.execute(f'SET search_path TO "{other}", public')
            dbapi_connection.commit()
        return db
    try:
        with admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE ROLE "{migration_role}" NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT')
            connection.exec_driver_sql(f'CREATE ROLE "{runtime_role}" NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT')
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}" AUTHORIZATION "{migration_role}"')
            connection.exec_driver_sql(f'CREATE SCHEMA "{other}"')
            connection.exec_driver_sql(f'REVOKE ALL ON SCHEMA "{schema}", "{other}" FROM PUBLIC')
            connection.exec_driver_sql(f'GRANT USAGE ON SCHEMA "{schema}" TO "{runtime_role}"')
            connection.exec_driver_sql(f'ALTER DEFAULT PRIVILEGES FOR ROLE "{migration_role}" IN SCHEMA "{schema}" GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{runtime_role}"')
        foreign = Database(url, schema=other)
        databases.append(foreign)
        foreign.migrate()
        with foreign.sessions.begin() as session:
            session.add(BindingState(id="private-other", inhibited=True))
        yield admin, scoped, schema, other, migration_role, runtime_role
    finally:
        for db in databases:
            db.engine.dispose()
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{other}" CASCADE')
            connection.exec_driver_sql(f'DROP ROLE IF EXISTS "{runtime_role}"')
            connection.exec_driver_sql(f'DROP ROLE IF EXISTS "{migration_role}"')
        admin.dispose()


def test_postgres_migration_and_runtime_roles_are_isolated(postgres_roles, tmp_path):
    import asyncio
    from sqlalchemy.exc import DBAPIError
    from sovite.service import Platform
    from sovite.settings import Settings
    from sovite.worker import Worker
    admin, scoped, schema, other, migration_role, runtime_role = postgres_roles
    migration = scoped(migration_role, schema)
    runtime = scoped(runtime_role, schema)
    with runtime.engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT current_user").scalar_one() == runtime_role
    with pytest.raises(DatabaseSchemaError) as failure:
        runtime.check()
    assert failure.value.code == "database_schema_missing"
    migration.migrate()
    assert set(inspect(admin).get_table_names(schema=schema)) == set(Base.metadata.tables)
    assert set(inspect(admin).get_table_names(schema=other)) == set(Base.metadata.tables)
    settings = Settings(database_url=runtime.engine.url.render_as_string(hide_password=False),
                        database_schema=schema, database_migrate="never", require_qualification=False,
                        artifact_path=str(tmp_path / "artifacts"))
    platform = Platform(settings)
    platform.db.engine.dispose()
    platform.db = runtime
    def no_ddl(connection, cursor, statement, parameters, context, executemany):
        assert not context.isddl
        assert statement.lstrip().split()[0].upper() not in {"CREATE", "ALTER", "DROP", "TRUNCATE"}
    event.listen(runtime.engine, "before_cursor_execute", no_ddl)
    try:
        runtime.check()
        platform.initialize()
        job = platform.submit("incident-forensics", "Private task", "cloud-codex", "runtime-role-task")
        assert asyncio.run(Worker(platform).run_once())
        assert platform.get_job(job.id).status == "completed"
        platform.inhibit("incident-forensics", "Runtime DML", True)
        with runtime.sessions.begin() as session:
            state = BindingState(id="runtime-dml", inhibited=True)
            session.add(state)
        with runtime.sessions.begin() as session:
            session.delete(session.get(BindingState, "runtime-dml"))
        assert runtime.verify_audit()["valid"]
        with runtime.engine.begin() as connection:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            runtime._structure(connection, Base.metadata.sorted_tables)
            runtime._audit_head(connection)
    finally:
        event.remove(runtime.engine, "before_cursor_execute", no_ddl)
    with pytest.raises(DBAPIError) as failure:
        with runtime.engine.begin() as connection:
            connection.exec_driver_sql(f'CREATE TABLE "{schema}".forbidden (id integer)')
    assert failure.value.orig.sqlstate == "42501"
    for db in (runtime, migration):
        with pytest.raises(DBAPIError) as failure:
            with db.engine.connect() as connection:
                connection.exec_driver_sql(f'SELECT * FROM "{other}".binding_runtime_state')
        assert failure.value.orig.sqlstate == "42501"
    with runtime.sessions() as session:
        assert session.get(BindingState, "private-other") is None


def test_postgres_missing_schema_never_falls_back(postgres_roles):
    admin, scoped, schema, other, migration_role, runtime_role = postgres_roles
    missing = "sovite_missing_" + uuid.uuid4().hex
    db = Database(admin.url.render_as_string(hide_password=False), schema=missing)
    @event.listens_for(db.engine, "connect")
    def decoy_search_path(dbapi_connection, connection_record):
        with dbapi_connection.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{other}", public')
        dbapi_connection.commit()
    try:
        for operation in (db.check, db.migrate):
            with pytest.raises(DatabaseSchemaError) as failure:
                operation()
            assert failure.value.code == "database_schema_missing"
        assert not inspect(admin).has_schema(missing)
        with db.engine.connect() as connection:
            assert connection.exec_driver_sql('SELECT count(*) FROM binding_runtime_state').scalar_one() == 1
    finally:
        db.engine.dispose()


def test_v3_execution_tables_require_upgrade_and_old_runtime_refuses(database):
    released_schema(database)
    BindingState.__table__.create(database.engine)
    with database.sessions.begin() as session:
        session.get(MetadataRow, 'schema_version').value = {'version': 2}
    assert not {'execution_authority', 'helper_enrollments', 'helper_sessions', 'inference_requests'}.intersection(
        inspect(database.engine).get_table_names(schema=database.schema))
    with pytest.raises(DatabaseSchemaError) as failure:
        database.check()
    assert failure.value.code == 'database_schema_older'
    database.migrate()
    database.check()
    assert {'execution_authority', 'helper_enrollments', 'helper_sessions', 'inference_requests'}.issubset(
        inspect(database.engine).get_table_names(schema=database.schema))
    database.schema_version = 2
    try:
        with pytest.raises(DatabaseSchemaError) as failure:
            database.check()
        assert failure.value.code == 'database_schema_newer'
    finally:
        database.schema_version = Database.schema_version


def test_v4_setup_tables_need_explicit_upgrade(database):
    released_schema(database)
    for _, tables in MIGRATIONS[1:3]:
        Base.metadata.create_all(database.engine, tables=list(tables))
    with database.sessions.begin() as session:
        session.get(MetadataRow, "schema_version").value = {"version": 3}
    assert "setup_principals" not in inspect(database.engine).get_table_names(schema=database.schema)
    with pytest.raises(DatabaseSchemaError) as failure:
        database.initialize("never")
    assert failure.value.code == "database_schema_older"
    database.migrate()
    database.initialize("never")
    assert {"setup_principals", "setup_drafts"}.issubset(inspect(database.engine).get_table_names(schema=database.schema))
    database.schema_version = 3
    try:
        with pytest.raises(DatabaseSchemaError) as failure:
            database.check()
        assert failure.value.code == "database_schema_newer"
    finally:
        database.schema_version = Database.schema_version


# O1 independent historical layout: do not derive these sets from current Base.
_HISTORICAL_TABLES = {
    1: frozenset({"platform_metadata", "bundle_revisions", "grant_runtime_state", "jobs",
                  "audit_events", "auth_sessions", "qualifications", "worker_heartbeats"}),
    2: frozenset({"binding_runtime_state"}),
    3: frozenset({"execution_authority", "helper_enrollments", "helper_sessions", "inference_requests"}),
    4: frozenset({"setup_principals", "setup_drafts"}),
}
_SCHEMA5_FIELDS = {
    "worker_execution_leases": {
        "job_id": (String, 64, False), "workload_id": (String, 160, False),
        "instance_id": (String, 64, False), "fence": (String, 64, False),
        "generation": (String, 80, False), "phase": (String, 16, False),
        "lease_until": (Float, None, False), "execution_deadline": (Float, None, True),
        "created_at": (Float, None, False), "updated_at": (Float, None, False),
    },
    "worker_execution_operations": {
        "workload_id": (String, 160, False), "instance_id": (String, 64, False),
        "operation_id": (String, 64, False), "operation_kind": (String, 32, False),
        "request_digest": (String, 64, False), "job_id": (String, 64, True),
        "fence": (String, 64, True), "result": (JSON, None, False),
        "created_at": (Float, None, False), "updated_at": (Float, None, False),
    },
}
_SCHEMA5_PKS = {
    "worker_execution_leases": ["job_id"],
    "worker_execution_operations": ["workload_id", "instance_id", "operation_id"],
}


def _historical_layout(version):
    return set().union(*(_HISTORICAL_TABLES[number] for number in range(1, version + 1)))


def _assert_historical_migrations():
    import sovite.db as owner
    for version, names in _HISTORICAL_TABLES.items():
        assert {table.name for number, tables in owner.MIGRATIONS if number == version
                for table in tables} == names, f"migration {version} changed its historical table set"


def _create_historical_schema(db, version):
    _assert_historical_migrations()
    names = _historical_layout(version)
    # This is fixture construction, using the owner's ORM and migration table sets.
    Base.metadata.create_all(db.engine, tables=[Base.metadata.tables[name] for name in sorted(names)])
    with db.sessions.begin() as session:
        session.add(MetadataRow(key="schema_version", value={"version": version}))
        session.add(MetadataRow(key="audit_head", value={"sequence": 0, "hash": "0" * 64}))
    assert set(inspect(db.engine).get_table_names(schema=db.schema)) == names


def _seed_historical_rows(db, version):
    from sovite.db import AuditEvent, digest
    with db.engine.begin() as connection:
        for name in sorted(_historical_layout(version) - {"platform_metadata", "audit_events"}):
            table = Base.metadata.tables[name]
            row = {}
            for column in table.columns:
                affinity = column.type._type_affinity
                if affinity is JSON:
                    row[column.name] = {"authored": "Säilytä 私密", "order": [3, 1, 2], "nullable": None}
                elif affinity is Boolean:
                    row[column.name] = True
                elif affinity is Integer:
                    row[column.name] = 7
                elif affinity is Float:
                    row[column.name] = 1234.25
                else:
                    row[column.name] = "old-" + column.name
            if name == "jobs":
                for index, status in enumerate(("queued", "running", "retry_wait", "completed", "blocked")):
                    job = dict(row, id=f"old-job-{index}", receipt=f"old-receipt-{index}",
                               idempotency_key=f"old-key-{index}", status=status,
                               task="Authored task: Säilytä 私密\nline two", attempts=index)
                    connection.execute(table.insert().values(**job))
            else:
                connection.execute(table.insert().values(**row))
        detail = {"z": "Authored Säilytä 私密", "a": [None, 3, 1], "nested": {"b": True}}
        body = {"sequence": 1, "created_at": 1234.125, "actor": "original-author",
                "action": "original-event", "target": "original-target", "detail": detail,
                "previous_hash": "0" * 64}
        authored_hash = digest(body)
        connection.execute(AuditEvent.__table__.insert().values(**body, hash=authored_hash))
        connection.execute(MetadataRow.__table__.update().where(MetadataRow.key == "audit_head")
                           .values(value={"sequence": 1, "hash": authored_hash}))
        connection.execute(MetadataRow.__table__.insert().values(key="active_generation", value={"generation": "old-generation"}))
        # JSON text is deliberately noncanonical; semantic equality would miss a rewrite.
        authored = '{  "z" : "Authored Säilytä 私密", "a": [null, 3, 1], "nested" : {"b": true} }'
        table_name = f'"{db.schema}".audit_events' if db.schema else 'audit_events'
        expression = "CAST(:detail AS JSON)" if db.schema else ":detail"
        connection.execute(text(f'UPDATE {table_name} SET detail = {expression} WHERE sequence = 1'), {"detail": authored})
    assert db.verify_audit()["valid"]


def _historical_snapshot(db, version):
    # Cast JSON to text so whitespace, key order, Unicode and signatures are frozen too.
    rows = {}
    with db.engine.connect() as connection:
        for name in sorted(_historical_layout(version)):
            table = Base.metadata.tables[name]
            columns = [cast(column, Text) if column.type._type_affinity is JSON else column
                       for column in table.columns]
            query = select(*columns).order_by(*table.primary_key.columns)
            if name == "platform_metadata":
                query = query.where(table.c.key != "schema_version")
            rows[name] = [tuple(row) for row in connection.execute(query)]
    encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return {"rows": rows, "counts": {name: len(value) for name, value in rows.items()},
            "sha256": hashlib.sha256(encoded).hexdigest(), "audit": db.verify_audit()}


def _require_schema5_tables(db):
    actual = set(inspect(db.engine).get_table_names(schema=db.schema))
    assert set(_SCHEMA5_FIELDS) <= actual, "schema-5 owner tables are missing after explicit migration"


def _read_only_sql(statements):
    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
        assert not context.isddl
        assert statement.lstrip().split()[0].upper() in {"SELECT", "PRAGMA"}
    return capture


def test_schema5_migration_ledger_preserves_historical_meanings():
    import sovite.db as owner
    _assert_historical_migrations()
    assert len(owner.MIGRATIONS) == 5, "schema-5 append migration is missing"
    assert [number for number, _ in owner.MIGRATIONS] == [1, 2, 3, 4, 5]
    assert {table.name for table in owner.MIGRATIONS[4][1]} == set(_SCHEMA5_FIELDS)
    assert set(Base.metadata.tables) == _historical_layout(4) | set(_SCHEMA5_FIELDS)
    for model_name, table_name in (("WorkerExecutionLease", "worker_execution_leases"),
                                   ("WorkerExecutionOperation", "worker_execution_operations")):
        model = getattr(owner, model_name, None)
        assert model is not None, f"missing owner ORM model {model_name}"
        assert model.__table__ is Base.metadata.tables[table_name]


def test_schema5_historical_oracle_detects_migration1_leak(monkeypatch):
    import sovite.db as owner
    missing = set(_SCHEMA5_FIELDS) - set(Base.metadata.tables)
    assert not missing, f"schema-5 owner table definitions missing: {sorted(missing)}"
    original = owner.MIGRATIONS
    leaked = tuple(Base.metadata.tables[name] for name in sorted(_SCHEMA5_FIELDS))
    monkeypatch.setattr(owner, "MIGRATIONS", ((1, original[0][1] + leaked),) + original[1:])
    with pytest.raises(AssertionError, match="migration 1 changed its historical table set"):
        _assert_historical_migrations()


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_schema5_upgrade_preserves_every_historical_row_and_authored_bytes(database, version):
    _create_historical_schema(database, version)
    _seed_historical_rows(database, version)
    before = _historical_snapshot(database, version)
    assert all(before["counts"].values())
    assert before["counts"]["jobs"] == 5
    database.migrate()
    _require_schema5_tables(database)
    for _ in range(2):
        database.initialize("never")
        assert _historical_snapshot(database, version) == before
        with database.engine.connect() as connection:
            for name in _SCHEMA5_FIELDS:
                assert connection.execute(select(Base.metadata.tables[name])).all() == [], "legacy jobs must not get backfilled reservations or operations"
        with database.sessions() as session:
            assert session.get(MetadataRow, "schema_version").value == {"version": 5}
        database.migrate()


def test_schema5_v4_startup_never_refuses_without_ddl_or_writes(database):
    _create_historical_schema(database, 4)
    _seed_historical_rows(database, 4)
    before = _historical_snapshot(database, 4)
    statements = []
    capture = _read_only_sql(statements)
    event.listen(database.engine, "before_cursor_execute", capture)
    try:
        with pytest.raises(DatabaseSchemaError) as failure:
            database.initialize("never")
        assert failure.value.code == "database_schema_older"
    finally:
        event.remove(database.engine, "before_cursor_execute", capture)
    assert statements
    assert set(inspect(database.engine).get_table_names(schema=database.schema)) == _historical_layout(4)
    assert _historical_snapshot(database, 4) == before
    with database.sessions() as session:
        assert session.get(MetadataRow, "schema_version").value == {"version": 4}


def test_schema5_fresh_reopen_structure_and_never_startup_are_read_only(database):
    created = []
    def at_creation(table, connection, **kwargs):
        version = connection.execute(select(MetadataRow.value).where(MetadataRow.key == "schema_version")).scalar_one()
        created.append((table.name, version))
    targets = [Base.metadata.tables[name] for name in _SCHEMA5_FIELDS if name in Base.metadata.tables]
    for table in targets:
        event.listen(table, "before_create", at_creation)
    try:
        database.migrate()
    finally:
        for table in targets:
            event.remove(table, "before_create", at_creation)
    _require_schema5_tables(database)
    # Each CREATE happens after migration 4 and before the migration-5 marker.
    assert sorted(created) == [(name, {"version": 4}) for name in sorted(_SCHEMA5_FIELDS)]
    inspector = inspect(database.engine)
    for name, fields in _SCHEMA5_FIELDS.items():
        table = Base.metadata.tables[name]
        assert set(table.columns.keys()) == set(fields)
        columns = {column["name"]: column for column in inspector.get_columns(name, schema=database.schema)}
        assert set(columns) == set(fields)
        assert inspector.get_pk_constraint(name, schema=database.schema)["constrained_columns"] == _SCHEMA5_PKS[name]
        for field, (kind, length, nullable) in fields.items():
            assert columns[field]["type"]._type_affinity is kind
            assert getattr(columns[field]["type"], "length", None) == length
            assert columns[field]["nullable"] is nullable
            assert table.c[field].default is None and table.c[field].server_default is None
    reopened = Database(database.engine.url.render_as_string(hide_password=False), schema=database.schema)
    statements = []
    capture = _read_only_sql(statements)
    event.listen(reopened.engine, "before_cursor_execute", capture)
    try:
        reopened.initialize("never")
        reopened.check()
    finally:
        event.remove(reopened.engine, "before_cursor_execute", capture)
        reopened.engine.dispose()
    assert statements


@pytest.mark.parametrize("table_name", sorted(_SCHEMA5_FIELDS))
@pytest.mark.parametrize("damage", ["missing_table", "missing_column", "wrong_type", "nullable", "wrong_pk"])
def test_schema5_check_refuses_corrupt_new_table_structure(database, table_name, damage):
    database.migrate()
    _require_schema5_tables(database)
    Base.metadata.tables[table_name].drop(database.engine)
    if damage != "missing_table":
        fields = _SCHEMA5_FIELDS[table_name]
        victim = "fence" if table_name.endswith("leases") else "result"
        columns = []
        for field, (kind, length, nullable) in fields.items():
            if damage == "missing_column" and field == victim:
                continue
            column_type = kind(length) if length else kind()
            if damage == "wrong_type" and field == victim:
                column_type = Integer()
            if damage == "nullable" and field == victim:
                nullable = True
            primary = field in _SCHEMA5_PKS[table_name]
            if damage == "wrong_pk":
                primary = field == "created_at"
            columns.append(Column(field, column_type, primary_key=primary, nullable=nullable))
        Table(table_name, MetaData(), *columns, schema=database.schema).create(database.engine)
    statements = []
    capture = _read_only_sql(statements)
    event.listen(database.engine, "before_cursor_execute", capture)
    try:
        with pytest.raises(DatabaseSchemaError) as failure:
            database.check()
        assert failure.value.code == "database_schema_incomplete"
        assert table_name in str(failure.value)
    finally:
        event.remove(database.engine, "before_cursor_execute", capture)
    assert statements


def test_schema5_operation_identity_is_exact_composite_key(database):
    from sqlalchemy.exc import IntegrityError
    database.migrate()
    _require_schema5_tables(database)
    table = Base.metadata.tables["worker_execution_operations"]
    row = dict(workload_id="workload", instance_id="instance", operation_id="operation",
               operation_kind="reserve", request_digest="d" * 64, job_id=None, fence=None,
               result={"state": "empty"}, created_at=123.25, updated_at=124.25)
    with database.engine.begin() as connection:
        connection.execute(table.insert().values(**row))
    with pytest.raises(IntegrityError):
        with database.engine.begin() as connection:
            connection.execute(table.insert().values(**row))
    for field in _SCHEMA5_PKS[table.name]:
        with database.engine.begin() as connection:
            connection.execute(table.insert().values(**dict(row, **{field: "different-" + field})))
    with database.engine.connect() as connection:
        assert len(connection.execute(select(table)).all()) == 4
    database.check()


# Exact checker-only source frozen from accepted v4 db.py, SHA-256 61a442de92f05cc5e7e610e5f0537068c72e0b5e9b6f8642e7b6db699d107038.
# No historical migrator is copied; the checker has its own historical Base view.
_V4_CHECKER_SOURCE = r'''
class FrozenV4Checker(Database):
    schema_version = 4

    def _structure(self, connection, tables):
        inspector = inspect(connection)
        names = set(inspector.get_table_names(schema=self.schema))
        for table in tables:
            if table.name not in names:
                raise DatabaseSchemaError("database_schema_incomplete", f"Missing table {table.name}; restore or migrate a supported schema")
            columns = {column["name"]: column for column in inspector.get_columns(table.name, schema=self.schema)}
            for column in table.columns:
                actual = columns.get(column.name)
                if actual is None or actual["type"]._type_affinity is not column.type._type_affinity or actual["nullable"] != column.nullable or getattr(actual["type"], "length", None) != getattr(column.type, "length", None):
                    raise DatabaseSchemaError("database_schema_incomplete", f"Incompatible column {table.name}.{column.name}")
            primary = inspector.get_pk_constraint(table.name, schema=self.schema)["constrained_columns"]
            if primary != [column.name for column in table.primary_key]:
                raise DatabaseSchemaError("database_schema_incomplete", f"Incompatible primary key on {table.name}")
            indexes = inspector.get_indexes(table.name, schema=self.schema)
            for index in table.indexes:
                if not any(row["column_names"] == [column.name for column in index.columns] and bool(row["unique"]) == bool(index.unique) for row in indexes):
                    raise DatabaseSchemaError("database_schema_incomplete", f"Missing index on {table.name}")
            unique = inspector.get_unique_constraints(table.name, schema=self.schema)
            for constraint in table.constraints:
                if isinstance(constraint, UniqueConstraint) and not any(row["column_names"] == [column.name for column in constraint.columns] for row in unique):
                    raise DatabaseSchemaError("database_schema_incomplete", f"Missing unique constraint on {table.name}")

    def _version(self, connection) -> int:
        names = set(inspect(connection).get_table_names(schema=self.schema))
        if MetadataRow.__tablename__ not in names:
            if names.intersection(Base.metadata.tables):
                raise DatabaseSchemaError("database_schema_incomplete", "Sovite tables exist without platform_metadata")
            return 0
        self._structure(connection, [MetadataRow.__table__])
        value = connection.execute(select(MetadataRow.value).where(MetadataRow.key == "schema_version")).scalar_one_or_none()
        if value is None:
            raise DatabaseSchemaError("database_schema_incomplete", "Missing schema_version metadata")
        if not isinstance(value, dict) or set(value) != {"version"} or type(value["version"]) is not int or value["version"] < 1:
            raise DatabaseSchemaError("database_schema_unknown", "Unsupported database schema_version")
        if value["version"] > self.schema_version:
            raise DatabaseSchemaError("database_schema_newer", "Unsupported database schema_version; use a compatible release")
        return value["version"]

    def _audit_head(self, connection):
        value = connection.execute(select(MetadataRow.value).where(MetadataRow.key == "audit_head")).scalar_one_or_none()
        if not isinstance(value, dict) or type(value.get("sequence")) is not int or value["sequence"] < 0 or not isinstance(value.get("hash"), str) or len(value["hash"]) != 64 or any(char not in "0123456789abcdef" for char in value["hash"]):
            raise DatabaseSchemaError("database_schema_incomplete", "Missing or invalid audit_head metadata")

    def check(self):
        if self.path is not None and not self.path.is_file():
            raise DatabaseSchemaError("database_schema_missing", "Run sovite db migrate before startup")
        with self.engine.connect() as connection:
            version = self._version(connection)
            if version == 0:
                raise DatabaseSchemaError("database_schema_missing", "Run sovite db migrate before startup")
            if version < self.schema_version:
                raise DatabaseSchemaError("database_schema_older", "Run sovite db migrate with the migration role before startup")
            self._structure(connection, Base.metadata.sorted_tables)
            self._audit_head(connection)
'''


def _frozen_v4_checker(url):
    tables = {name: Base.metadata.tables[name] for name in _historical_layout(4)}
    namespace = {"Database": Database, "DatabaseSchemaError": DatabaseSchemaError,
                 "MetadataRow": MetadataRow, "inspect": inspect, "select": select,
                 "UniqueConstraint": UniqueConstraint,
                 "Base": SimpleNamespace(metadata=SimpleNamespace(tables=tables,
                      sorted_tables=[tables[name] for name in sorted(tables)]))}
    exec(_V4_CHECKER_SOURCE, namespace)
    return namespace["FrozenV4Checker"](url)


def _sqlite_backup(source, destination):
    with sqlite3.connect(source) as original, sqlite3.connect(destination) as saved:
        original.backup(saved)


def test_schema5_old_checker_refuses_then_saved_v4_restore_reads(tmp_path):
    live_path, saved_path, restored_path = (tmp_path / name for name in ("live.db", "saved-v4.db", "restored-v4.db"))
    db = Database(f"sqlite:///{live_path}")
    _create_historical_schema(db, 4)
    _seed_historical_rows(db, 4)
    before = _historical_snapshot(db, 4)
    old = _frozen_v4_checker(f"sqlite:///{live_path}")
    old.check()  # Positive control: the actual v4 checker accepts its original schema.
    old.engine.dispose()
    _sqlite_backup(live_path, saved_path)
    saved_sha256 = hashlib.sha256(saved_path.read_bytes()).hexdigest()
    db.migrate()
    _require_schema5_tables(db)
    db.engine.dispose()
    old = _frozen_v4_checker(f"sqlite:///{live_path}")
    statements = []
    capture = _read_only_sql(statements)
    event.listen(old.engine, "before_cursor_execute", capture)
    try:
        with pytest.raises(DatabaseSchemaError) as failure:
            old.check()
        assert failure.value.code == "database_schema_newer"
    finally:
        event.remove(old.engine, "before_cursor_execute", capture)
        old.engine.dispose()
    assert statements
    # Restore the separately saved pre-upgrade file; never edit schema metadata.
    assert hashlib.sha256(saved_path.read_bytes()).hexdigest() == saved_sha256
    _sqlite_backup(saved_path, restored_path)
    restored = _frozen_v4_checker(f"sqlite:///{restored_path}")
    try:
        restored.check()
        assert _historical_snapshot(restored, 4) == before
        assert set(inspect(restored.engine).get_table_names()) == _historical_layout(4)
        with restored.sessions() as session:
            assert session.get(MetadataRow, "schema_version").value == {"version": 4}
    finally:
        restored.engine.dispose()


@pytest.mark.parametrize("damage", ["json_bytes", "row_value", "row_count", "audit_head"])
def test_schema5_preservation_oracle_detects_historical_damage(database, damage):
    _create_historical_schema(database, 4)
    _seed_historical_rows(database, 4)
    before = _historical_snapshot(database, 4)
    with database.engine.begin() as connection:
        if damage == "json_bytes":
            from sovite.db import AuditEvent
            detail = connection.execute(select(AuditEvent.detail)).scalar_one()
            # Same JSON value and same valid audit hash, different authored bytes.
            connection.execute(AuditEvent.__table__.update().values(detail=detail))
        elif damage == "row_value":
            connection.execute(Job.__table__.update().where(Job.id == "old-job-0").values(status="completed"))
        elif damage == "row_count":
            connection.execute(Qualification.__table__.delete())
        else:
            connection.execute(MetadataRow.__table__.update().where(MetadataRow.key == "audit_head")
                               .values(value={"sequence": 0, "hash": "0" * 64}))
    after = _historical_snapshot(database, 4)
    if damage == "json_bytes":
        assert after["audit"] == before["audit"]  # Semantic chain checks cannot catch this damage.
        assert after["counts"] == before["counts"]
    with pytest.raises(AssertionError):
        assert after == before
