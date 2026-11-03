"""Synthetic SQLite and isolated PostgreSQL tests for schema-10 migration."""

import hashlib
import os
from pathlib import Path
import uuid

import pytest
from sqlalchemy import MetaData, Table, create_engine, event, inspect, make_url, select
from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import postgresql, sqlite

from sovite.db import Database, DatabaseSchemaError, _PROGRAM_RELATIONAL_V10
from program_ledger_v9_oracle import (
    ORACLE_TABLES,
    canonical_json,
    data_snapshot,
    schema_snapshot,
)
from test_program_ledger_v9_oracle import METADATA as V9_ORACLE


FIXTURES = Path(__file__).parent / "fixtures"
V10_NAMES = {
    "program_work_projection_bindings",
    "program_census_manifests",
    "program_census_perimeter_members",
    "program_census_manifest_states",
    "program_census_streams",
    "program_census_stream_states",
    "program_census_pages",
    "program_census_confirmations",
    "program_census_gate_bindings",
}


def _verified_fixture(name):
    raw = (FIXTURES / name).read_bytes()
    expected = V9_ORACLE["fixture_sha256"][name]
    assert hashlib.sha256(raw).hexdigest() == expected
    return raw.decode("utf-8")


def _local_test_url(value):
    url = make_url(value)
    if url.get_backend_name() != "postgresql":
        pytest.fail("SOVITE_TEST_POSTGRES_URL must select PostgreSQL")
    if any(key.startswith("PG") for key in os.environ):
        pytest.fail("PostgreSQL migration tests refuse inherited libpq PG environment variables")
    if set(url.query) - {"host", "port"}:
        pytest.fail("PostgreSQL migration URL permits only local host and port options")
    query_hosts = url.query.get("host", ())
    if isinstance(query_hosts, str):
        query_hosts = (query_hosts,)
    if url.host or len(query_hosts) != 1 or not query_hosts[0].startswith("/tmp/"):
        pytest.fail("PostgreSQL migration tests require an explicit Unix socket under /tmp")
    database_name = (url.database or "").lower()
    if not any(token in database_name for token in ("test", "oracle", "fixture")):
        pytest.fail("PostgreSQL migration tests require a dedicated test/oracle/fixture database")
    return url


def _reflected_table(connection, name, schema):
    return Table(name, MetaData(), schema=schema, autoload_with=connection)


def _schema_version(connection, schema):
    metadata = _reflected_table(connection, "platform_metadata", schema)
    return connection.execute(
        select(metadata.c.value).where(metadata.c.key == "schema_version")
    ).scalar_one()


def _selected_schema_snapshot(connection, names, schema):
    inspector = inspect(connection)
    payload = []
    for name in names:
        foreign_keys = inspector.get_foreign_keys(
            name, schema=schema,
            **({"postgresql_ignore_search_path": True}
               if connection.dialect.name == "postgresql" else {}),
        )
        payload.append({
            "table": name,
            "columns": [
                {"name": col["name"], "type": str(col["type"]),
                 "nullable": bool(col["nullable"]), "default": col.get("default")}
                for col in inspector.get_columns(name, schema=schema)
            ],
            "primary_key": {
                "name": inspector.get_pk_constraint(name, schema=schema).get("name"),
                "columns": inspector.get_pk_constraint(name, schema=schema).get("constrained_columns") or [],
            },
            "unique_constraints": sorted(
                ({"name": row.get("name"), "columns": row.get("column_names") or []}
                 for row in inspector.get_unique_constraints(name, schema=schema)),
                key=lambda row: (row["name"] or "", row["columns"]),
            ),
            "indexes": sorted(
                ({"name": row.get("name"), "columns": row.get("column_names") or [],
                  "unique": bool(row.get("unique"))} for row in inspector.get_indexes(name, schema=schema)),
                key=lambda row: (row["name"] or "", row["columns"]),
            ),
            "foreign_keys": sorted(
                ({"name": row.get("name"), "columns": row.get("constrained_columns") or [],
                  "schema": ("__ORACLE_SCHEMA__" if schema is not None and row.get("referred_schema") == schema
                             else row.get("referred_schema")), "table": row.get("referred_table"),
                  "referred_columns": row.get("referred_columns") or [], "options": row.get("options") or {}}
                 for row in foreign_keys),
                key=lambda row: (row["name"] or "", row["columns"], row["table"] or ""),
            ),
            "checks": sorted(
                ({"name": row.get("name"), "sqltext": row.get("sqltext"),
                  "dialect_options": row.get("dialect_options") or {}}
                  for row in inspector.get_check_constraints(name, schema=schema)),
                key=lambda row: row["name"] or "",
            ),
        })
    encoded = canonical_json(payload)
    return hashlib.sha256(encoded).hexdigest(), payload


def _selected_data_snapshot(connection, names, schema):
    rows = []
    counts = {}
    for name in names:
        table = _reflected_table(connection, name, schema)
        values = [list(row) for row in connection.execute(select(table))]
        if name == "platform_metadata":
            columns = [column.name for column in table.columns]
            key_index, value_index = columns.index("key"), columns.index("value")
            for row in values:
                if row[key_index] == "schema_version":
                    # Version advancement is the one intended metadata mutation.
                    row[value_index] = "<schema-version-advanced-by-migration>"
        values.sort(key=canonical_json)
        rows.append({"table": name, "rows": values})
        counts[name] = len(values)
    return hashlib.sha256(canonical_json(rows)).hexdigest(), counts


@pytest.fixture(params=("sqlite", "postgresql"), ids=("sqlite", "postgresql"))
def v9_oracle_database(request, tmp_path):
    backend = request.param
    admin = None
    database = None
    namespace = None
    owned_oid = None
    marker = None
    try:
        if backend == "sqlite":
            database = Database(f"sqlite:///{tmp_path / 'unit4c-v9.sqlite3'}")
            with database.engine.connect() as connection:
                raw = connection.connection.driver_connection
                raw.executescript(_verified_fixture("program_ledger_v8_sqlite.sql"))
                raw.executescript(_verified_fixture("program_ledger_v9_oracle_sqlite.sql"))
        else:
            value = os.environ.get("SOVITE_TEST_POSTGRES_URL")
            if not value:
                if os.environ.get("SOVITE_LEDGER_SCHEMA_REQUIRE_PG") == "1":
                    pytest.fail("SOVITE_TEST_POSTGRES_URL is required for PostgreSQL migration tests")
                pytest.skip("local isolated PostgreSQL migration database not supplied")
            url = _local_test_url(value)
            admin = create_engine(url)
            namespace = "relational_v10_test_" + uuid.uuid4().hex
            marker = "sovite-relational-v10-" + uuid.uuid4().hex
            with admin.connect() as connection:
                is_superuser = connection.exec_driver_sql(
                    "SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user"
                ).scalar_one()
                if is_superuser:
                    pytest.fail("PostgreSQL migration tests refuse superuser roles")
                if connection.exec_driver_sql("SELECT inet_server_addr() IS NULL").scalar_one() is not True:
                    pytest.fail("PostgreSQL migration tests require a Unix socket connection")
            with admin.begin() as connection:
                connection.exec_driver_sql(f'CREATE SCHEMA "{namespace}"')
                connection.exec_driver_sql(f"COMMENT ON SCHEMA \"{namespace}\" IS '{marker}'")
                owned_oid = connection.exec_driver_sql(
                    "SELECT oid FROM pg_catalog.pg_namespace WHERE nspname = %s", (namespace,)
                ).scalar_one()

            v8_sql = _verified_fixture("program_ledger_v8_postgresql.sql").replace(
                "__LEDGER_V8_SCHEMA__", namespace
            )
            create_statement = f"CREATE SCHEMA {namespace};"
            assert v8_sql.count(create_statement) == 1
            v8_sql = v8_sql.replace(create_statement, "", 1)
            with admin.connect() as connection:
                raw = connection.connection.driver_connection
                raw.execute(v8_sql, prepare=False)
                raw.commit()
            with admin.begin() as connection:
                connection.exec_driver_sql(f'SET LOCAL search_path TO "{namespace}"')
                raw = connection.connection.driver_connection
                raw.execute(_verified_fixture("program_ledger_v9_oracle_postgresql.sql"), prepare=False)
                owner = connection.exec_driver_sql(
                    "SELECT n.nspowner = r.oid FROM pg_catalog.pg_namespace n "
                    "JOIN pg_catalog.pg_roles r ON r.rolname = current_user WHERE n.nspname = %s",
                    (namespace,),
                ).scalar_one()
                assert owner, "the UUID namespace must be owned by the non-superuser test role"
            database = Database(url.render_as_string(hide_password=False), schema=namespace)
        yield database
    finally:
        if database is not None:
            database.engine.dispose()
        if admin is not None:
            if owned_oid is not None:
                with admin.begin() as connection:
                    matches = connection.exec_driver_sql(
                        "SELECT n.oid = %s AND n.nspowner = (SELECT oid FROM pg_roles WHERE rolname = current_user) "
                        "AND obj_description(n.oid, 'pg_namespace') = %s FROM pg_namespace n WHERE n.nspname = %s",
                        (owned_oid, marker, namespace),
                    ).scalar_one_or_none()
                    if matches is True:
                        connection.exec_driver_sql(f'DROP SCHEMA "{namespace}" CASCADE')
            admin.dispose()


def test_v10_is_additive_empty_and_preserves_frozen_v9_oracle(v9_oracle_database):
    database = v9_oracle_database
    backend = database.engine.dialect.name
    assert len(ORACLE_TABLES) == 42
    with database.engine.connect() as connection:
        before_schema, _ = schema_snapshot(connection, database.schema)
        before_data, before_counts = data_snapshot(connection, database.schema)
    assert before_schema == V9_ORACLE["snapshots"][backend]["schema_sha256"]
    assert before_data == V9_ORACLE["snapshots"][backend]["data_sha256"]
    assert before_counts == V9_ORACLE["snapshots"][backend]["row_counts"]
    with database.engine.connect() as connection:
        before_stable_data, before_stable_counts = _selected_data_snapshot(
            connection, ORACLE_TABLES, database.schema
        )

    database.migrate()
    database.check()

    with database.engine.connect() as connection:
        after_schema, _ = _selected_schema_snapshot(connection, ORACLE_TABLES, database.schema)
        after_data, after_counts = _selected_data_snapshot(connection, ORACLE_TABLES, database.schema)
        assert after_schema == before_schema
        assert after_data == before_stable_data
        assert after_counts == before_stable_counts == before_counts
        assert set(inspect(connection).get_table_names(schema=database.schema)) == set(ORACLE_TABLES) | V10_NAMES
        assert all(
            connection.execute(select(_reflected_table(connection, name, database.schema))).first() is None
            for name in V10_NAMES
        )
        assert _schema_version(connection, database.schema) == {"version": 10}


def test_partial_v10_schema_is_refused_without_advancing_v9(v9_oracle_database):
    database = v9_oracle_database
    _PROGRAM_RELATIONAL_V10[0].create(database.engine)
    with pytest.raises(DatabaseSchemaError, match="partial relational"):
        database.migrate()
    with database.engine.connect() as connection:
        assert _schema_version(connection, database.schema) == {"version": 9}
        inspector = inspect(connection)
        assert inspector.has_table("program_work_projection_bindings", schema=database.schema)
        assert not any(
            inspector.has_table(name, schema=database.schema)
            for name in V10_NAMES - {"program_work_projection_bindings"}
        )


def test_v10_metadata_with_partial_schema_fails_closed(v9_oracle_database):
    database = v9_oracle_database
    _PROGRAM_RELATIONAL_V10[0].create(database.engine)
    with database.engine.begin() as connection:
        metadata = _reflected_table(connection, "platform_metadata", database.schema)
        connection.execute(
            metadata.update().where(metadata.c.key == "schema_version").values(value={"version": 10})
        )

    with pytest.raises(DatabaseSchemaError, match="Missing table"):
        database.check()
    with pytest.raises(DatabaseSchemaError, match="Missing table"):
        database.migrate()
    with database.engine.connect() as connection:
        inspector = inspect(connection)
        assert not any(
            inspector.has_table(name, schema=database.schema)
            for name in V10_NAMES - {"program_work_projection_bindings"}
        )
        assert _schema_version(connection, database.schema) == {"version": 10}


def test_v10_ddl_failure_rolls_back_all_new_tables_and_version(v9_oracle_database):
    database = v9_oracle_database

    def fail_after_some_ddl(conn, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("CREATE TABLE") and "PROGRAM_CENSUS_PAGES" in statement.upper():
            raise RuntimeError("injected v10 migration failure")

    event.listen(database.engine, "before_cursor_execute", fail_after_some_ddl)
    try:
        with pytest.raises(RuntimeError, match="v10 migration failure"):
            database.migrate()
    finally:
        event.remove(database.engine, "before_cursor_execute", fail_after_some_ddl)

    with database.engine.connect() as connection:
        inspector = inspect(connection)
        assert _schema_version(connection, database.schema) == {"version": 9}
        assert not any(inspector.has_table(name, schema=database.schema) for name in V10_NAMES)


def test_v9_reader_rejects_v10_and_runtime_check_is_read_only(v9_oracle_database):
    database = v9_oracle_database
    database.migrate()
    supported = database.schema_version
    database.schema_version = 9
    try:
        with pytest.raises(DatabaseSchemaError, match="newer"):
            database.check()
    finally:
        database.schema_version = supported

    statements = []

    def capture(conn, cursor, statement, parameters, context, many):
        statements.append(statement)

    event.listen(database.engine, "before_cursor_execute", capture)
    try:
        database.initialize("never")
        database.check()
    finally:
        event.remove(database.engine, "before_cursor_execute", capture)
    assert statements
    assert not any(
        sql.lstrip().upper().startswith(("CREATE", "ALTER", "DROP", "INSERT", "UPDATE", "DELETE"))
        for sql in statements
    )


def test_v10_constraint_ddl_compiles_for_both_supported_dialects():
    assert {table.name for table in _PROGRAM_RELATIONAL_V10} == V10_NAMES
    sqlite_ddl = "\n".join(str(CreateTable(table).compile(dialect=sqlite.dialect()))
                            for table in _PROGRAM_RELATIONAL_V10)
    postgres_ddl = "\n".join(str(CreateTable(table).compile(dialect=postgresql.dialect()))
                              for table in _PROGRAM_RELATIONAL_V10)
    assert "NOT GLOB '*[^0-9a-f]*'" in sqlite_ddl
    assert "~ '^[0-9a-f]{64}$'" in postgres_ddl
    for compiled in (sqlite_ddl, postgres_ddl):
        assert "ON DELETE RESTRICT ON UPDATE RESTRICT" in compiled
        assert "program_census_batches" in compiled
        assert "program_census_manifest_states" in compiled
    assert "NOT DEFERRABLE" in sqlite_ddl
    assert "NOT DEFERRABLE" in postgres_ddl
    assert "DEFERRABLE INITIALLY" not in postgres_ddl
    assert "BLOB" in sqlite_ddl
    assert "BYTEA" in postgres_ddl


def test_v10_runtime_schema_verifier_detects_constraint_drift(v9_oracle_database):
    database = v9_oracle_database
    database.migrate()
    if database.engine.dialect.name == "postgresql":
        with database.engine.begin() as connection:
            connection.exec_driver_sql(
                f'ALTER TABLE "{database.schema}".program_census_pages '
                "DROP CONSTRAINT v10_page_values_ck"
            )
            connection.exec_driver_sql(
                f'ALTER TABLE "{database.schema}".program_census_pages '
                "ADD CONSTRAINT v10_page_values_ck CHECK (page_number >= 0 AND row_count > 0 AND "
                "byte_count > 0 AND length(first_key) >= 1 AND length(first_key) <= 512 AND "
                "length(last_key) >= 1 AND length(last_key) <= 512 AND length(cursor_after) >= 1 AND "
                "length(cursor_after) <= 512 AND (cursor_before IS NULL OR "
                "(length(cursor_before) >= 1 AND length(cursor_before) <= 512)) AND "
                "audit_sequence >= 1 AND audit_sequence <= 2147483647 AND "
                "((page_number = 1 AND cursor_before IS NULL AND previous_page_digest IS NULL) OR "
                "(page_number > 1 AND cursor_before IS NOT NULL AND previous_page_digest IS NOT NULL)))"
            )
    else:
        with database.engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA writable_schema=ON")
            connection.exec_driver_sql(
                "UPDATE sqlite_master SET sql=replace(sql, 'page_number > 0', 'page_number >= 0') "
                "WHERE type='table' AND name='program_census_pages'"
            )
            connection.exec_driver_sql("PRAGMA writable_schema=OFF")
            version = connection.exec_driver_sql("PRAGMA schema_version").scalar_one()
            connection.exec_driver_sql(f"PRAGMA schema_version={version + 1}")
    with pytest.raises(DatabaseSchemaError, match="check constraint"):
        database.check()


def test_lowercase_digest_constraint_drift_does_not_normalize_as_equivalent():
    from sovite.db import _check_expression_tree
    accepted = "length(projection_digest) = 64 AND projection_digest NOT GLOB '*[^0-9a-f]*'"
    drifted = "length(projection_digest) = 64 AND projection_digest NOT GLOB '*[^0-9A-F]*'"
    _check_expression_tree(accepted, text_columns={"projection_digest"})
    with pytest.raises(ValueError):
        _check_expression_tree(drifted, text_columns={"projection_digest"})


def test_postgresql_lowercase_digest_constraint_rejects_uppercase_drift():
    from sovite.db import _check_expression_tree
    expected = "length(projection_digest) = 64 AND projection_digest ~ '^[0-9a-f]{64}$'"
    drifted = "length(projection_digest) = 64 AND projection_digest ~ '^[0-9A-F]{64}$'"
    _check_expression_tree(expected, text_columns={"projection_digest"})
    with pytest.raises(ValueError):
        _check_expression_tree(drifted, text_columns={"projection_digest"})


def test_reflected_pg_digest_check_normalization_is_narrow():
    from sovite.db import _check_expression_tree
    expected = "length(projection_digest) = 64 AND projection_digest NOT GLOB '*[^0-9a-f]*'"
    reflected = "length(projection_digest::text) = 64 AND projection_digest::text ~ '^[0-9a-f]{64}$'::text"
    assert _check_expression_tree(reflected, text_columns={"projection_digest"}) == _check_expression_tree(expected, text_columns={"projection_digest"})
    for drift in (reflected.replace("[0-9a-f]", "[0-9A-F]"), reflected.replace(" ~ ", " ~* "), reflected.replace("{64}", "{63}"), reflected + " OR projection_digest IS NULL"):
        try:
            result = _check_expression_tree(drift, text_columns={"projection_digest"})
        except ValueError:
            continue
        assert result != _check_expression_tree(expected, text_columns={"projection_digest"})


def test_v10_runtime_rejects_digest_case_semantic_drift(v9_oracle_database):
    database = v9_oracle_database
    database.migrate()
    with database.engine.begin() as connection:
        if database.engine.dialect.name == "postgresql":
            table = f'"{database.schema}".program_work_projection_bindings'
            connection.exec_driver_sql(f"ALTER TABLE {table} DROP CONSTRAINT v10_projection_digest_ck")
            connection.exec_driver_sql(f"ALTER TABLE {table} ADD CONSTRAINT v10_projection_digest_ck CHECK (length(policy_digest) = 64 AND policy_digest ~ '^[0-9a-f]{{64}}$' AND length(projection_digest) = 64 AND projection_digest ~ '^[0-9A-F]{{64}}$')")
        else:
            connection.exec_driver_sql("PRAGMA writable_schema=ON")
            result = connection.exec_driver_sql("UPDATE sqlite_master SET sql=replace(sql, :old, :new) WHERE type='table' AND name='program_work_projection_bindings'", {"old": "projection_digest NOT GLOB '*[^0-9a-f]*'", "new": "projection_digest NOT GLOB '*[^0-9A-F]*'"})
            assert result.rowcount == 1
            connection.exec_driver_sql("PRAGMA writable_schema=OFF")
            version = connection.exec_driver_sql("PRAGMA schema_version").scalar_one()
            connection.exec_driver_sql(f"PRAGMA schema_version={version + 1}")
    with pytest.raises(DatabaseSchemaError, match="check constraint"):
        database.check()
