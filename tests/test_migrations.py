"""Upgrade the released schema without changing durable authority or job snapshots."""
import os
import uuid

import pytest
from sqlalchemy import create_engine, event, inspect
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
            assert json.loads(result.out) == {"status": "compatible", "schema_version": 3}
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
        assert session.get(MetadataRow, "schema_version").value == {"version": 3}
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
        assert session.get(MetadataRow, "schema_version").value == {"version": 3}
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
        assert session.get(MetadataRow, "schema_version").value == {"version": 3}
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
        database.schema_version = 3
