"""Independent frozen v9 schema/data oracle; always uses synthetic local fixtures."""
import hashlib
import json
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import MetaData, Table, create_engine, make_url, select

from sovite.db import Database, DatabaseSchemaError
from program_ledger_v9_oracle import ORACLE_TABLES, data_snapshot, schema_snapshot


FIXTURES = Path(__file__).parent / "fixtures"
METADATA = json.loads((FIXTURES / "program_ledger_v9_oracle.json").read_text())


def _fixture_sql(name):
    path = FIXTURES / name
    raw = path.read_bytes()
    expected = METADATA["fixture_sha256"][name]
    assert hashlib.sha256(raw).hexdigest() == expected, f"frozen fixture changed: {name}"
    return raw.decode("utf-8")


def _local_test_url(value):
    url = make_url(value)
    if url.get_backend_name() != "postgresql":
        pytest.fail("SOVITE_TEST_POSTGRES_URL must select PostgreSQL")
    if any(key.startswith("PG") for key in os.environ):
        pytest.fail("PostgreSQL oracle refuses inherited libpq PG environment variables")
    if set(url.query) - {"host", "port"}:
        pytest.fail("PostgreSQL oracle URL permits only local host and port options")
    query_hosts = url.query.get("host", ())
    if isinstance(query_hosts, str):
        query_hosts = (query_hosts,)
    if url.host or len(query_hosts) != 1 or not query_hosts[0].startswith("/tmp/"):
        pytest.fail("PostgreSQL oracle requires an explicit Unix socket under /tmp")
    database_name = (url.database or "").lower()
    if not any(token in database_name for token in ("test", "oracle", "fixture")):
        pytest.fail("PostgreSQL oracle requires a dedicated test/oracle/fixture database name")
    return url


@pytest.fixture(params=(("sqlite", "frozen"), ("sqlite", "migrated"), ("postgresql", "frozen"), ("postgresql", "migrated")), ids=lambda p: "-".join(p))
def oracle_database(request, tmp_path):
    backend, origin = request.param
    admin = None
    database = None
    namespace = None
    if backend == "sqlite":
        database = Database(f"sqlite:///{tmp_path / 'ledger-v9-oracle.sqlite3'}")
        try:
            with database.engine.connect() as connection:
                raw = connection.connection.driver_connection
                raw.executescript(_fixture_sql("program_ledger_v8_sqlite.sql"))
            if origin == "migrated":
                database.migrate()
            v9 = _fixture_sql("program_ledger_v9_oracle_sqlite.sql")
            if origin == "migrated":
                v9 = v9[v9.index("-- Synthetic historical-only oracle rows."): ]
            with database.engine.connect() as connection:
                connection.connection.driver_connection.executescript(v9)
            database.check()
            yield database
        finally:
            database.engine.dispose()
        return

    value = os.environ.get("SOVITE_TEST_POSTGRES_URL")
    if not value:
        if os.environ.get("SOVITE_LEDGER_SCHEMA_REQUIRE_PG") == "1":
            pytest.fail("SOVITE_TEST_POSTGRES_URL is required for PostgreSQL oracle tests")
        pytest.skip("local isolated PostgreSQL oracle database not supplied")
    url = _local_test_url(value)
    admin = create_engine(url)
    namespace = "ledger_v9_oracle_" + uuid.uuid4().hex
    marker = "sovite-oracle-" + uuid.uuid4().hex
    owned_oid = None
    try:
        with admin.connect() as connection:
            is_superuser = connection.exec_driver_sql(
                "SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user"
            ).scalar_one()
            if is_superuser:
                pytest.fail("PostgreSQL oracle refuses superuser roles")
            if connection.exec_driver_sql("SELECT inet_server_addr() IS NULL").scalar_one() is not True:
                pytest.fail("PostgreSQL oracle requires a Unix socket connection")
        with admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{namespace}"')
            connection.exec_driver_sql(f'COMMENT ON SCHEMA "{namespace}" IS \'{marker}\'')
            owned_oid = connection.exec_driver_sql("SELECT oid FROM pg_catalog.pg_namespace WHERE nspname = %s", (namespace,)).scalar_one()
        v8_sql = _fixture_sql("program_ledger_v8_postgresql.sql").replace(
            "__LEDGER_V8_SCHEMA__", namespace
        )
        create_statement = f"CREATE SCHEMA {namespace};"
        assert v8_sql.count(create_statement) == 1
        v8_sql = v8_sql.replace(create_statement, "", 1)
        with admin.connect() as connection:
            raw = connection.connection.driver_connection
            raw.execute(v8_sql, prepare=False)
            raw.commit()
        if origin == "migrated":
            migration_database = Database(url.render_as_string(hide_password=False), schema=namespace)
            try:
                migration_database.migrate()
            finally:
                migration_database.engine.dispose()
        with admin.begin() as connection:
            connection.exec_driver_sql(f'SET LOCAL search_path TO "{namespace}"')
            raw = connection.connection.driver_connection
            v9 = _fixture_sql("program_ledger_v9_oracle_postgresql.sql")
            if origin == "migrated":
                v9 = v9[v9.index("-- Synthetic historical-only oracle rows."): ]
            raw.execute(v9, prepare=False)
            owner = connection.exec_driver_sql(
                "SELECT n.nspowner = r.oid FROM pg_catalog.pg_namespace n "
                "JOIN pg_catalog.pg_roles r ON r.rolname = current_user WHERE n.nspname = %s",
                (namespace,),
            ).scalar_one()
            assert owner, "the UUID namespace must be owned by the non-superuser test role"
        database = Database(url.render_as_string(hide_password=False), schema=namespace)
        database.check()
        yield database
    finally:
        if database is not None:
            database.engine.dispose()
        if admin is not None:
            if owned_oid is not None:
                with admin.begin() as connection:
                    matches = connection.exec_driver_sql(
                        "SELECT n.oid = %s AND n.nspowner = (SELECT oid FROM pg_roles WHERE rolname = current_user) AND obj_description(n.oid, 'pg_namespace') = %s FROM pg_namespace n WHERE n.nspname = %s",
                        (owned_oid, marker, namespace),
                    ).scalar_one_or_none()
                    if matches is True:
                        connection.exec_driver_sql(f'DROP SCHEMA "{namespace}" CASCADE')
            admin.dispose()


def test_full_v8_v9_schema_and_data_match_frozen_baseline(oracle_database):
    database = oracle_database
    backend = database.engine.dialect.name
    expected = METADATA["snapshots"][backend]
    with database.engine.connect() as connection:
        schema_result = schema_snapshot(connection, database.schema)
        data_result = data_snapshot(connection, database.schema)
        assert schema_snapshot(connection, database.schema) == schema_result
        assert data_snapshot(connection, database.schema) == data_result
        schema_hash, _ = schema_result
        data_hash, counts = data_result
        metadata = Table("platform_metadata", MetaData(), schema=database.schema,
                         autoload_with=connection)
        metadata_rows = {row.key: row.value for row in connection.execute(select(metadata))}
        audit_events = Table("audit_events", MetaData(), schema=database.schema,
                             autoload_with=connection)
        audit_row = connection.execute(select(audit_events)).mappings().one()
    assert tuple(ORACLE_TABLES) == tuple(METADATA["tables"])
    assert metadata_rows == METADATA["historical_metadata"][backend]
    assert metadata_rows["audit_head"]["hash"] == audit_row["hash"]
    assert audit_row["sequence"] == 1
    assert schema_hash == expected["schema_sha256"]
    assert data_hash == expected["data_sha256"]
    assert counts == expected["row_counts"]


def test_synthetic_settled_work_and_collection_history_are_present(oracle_database):
    database = oracle_database
    with database.engine.connect() as connection:
        metadata = MetaData()
        tables = {
            name: Table(name, metadata, schema=database.schema, autoload_with=connection)
            for name in (
                "program_reservations", "program_slots", "program_call_intents",
                "program_call_receipts", "collection_sessions", "collection_epochs", "collection_epoch_guards",
            )
        }
        reservations = tables["program_reservations"]
        slots = tables["program_slots"]
        intents = tables["program_call_intents"]
        receipts = tables["program_call_receipts"]

        work = connection.execute(select(reservations).where(
            reservations.c.call_id == "oracle-settled-work-call"
        )).mappings().one()
        work_slot = connection.execute(select(slots).where(
            slots.c.slot_id == "oracle-settled-work-slot"
        )).mappings().one()
        work_intent = connection.execute(select(intents).where(
            intents.c.call_id == "oracle-settled-work-call"
        )).mappings().one()
        work_receipt = connection.execute(select(receipts).where(
            receipts.c.call_id == "oracle-settled-work-call"
        )).mappings().one()

        collection = connection.execute(select(tables["collection_sessions"]).where(
            tables["collection_sessions"].c.id == "oracle-collection"
        )).mappings().one()
        epoch = connection.execute(select(tables["collection_epochs"]).where(
            tables["collection_epochs"].c.session_id == "oracle-collection"
        )).mappings().one()
        guard = connection.execute(select(tables["collection_epoch_guards"]).where(
            tables["collection_epoch_guards"].c.session_id == "oracle-collection"
        )).mappings().one()
        collection_reservation = connection.execute(select(reservations).where(
            reservations.c.call_id == "oracle-collection-call"
        )).mappings().one()
        collection_slot = connection.execute(select(slots).where(
            slots.c.slot_id == "oracle-collection-slot"
        )).mappings().one()
        collection_intent = connection.execute(select(intents).where(
            intents.c.call_id == "oracle-collection-call"
        )).mappings().one()
        collection_receipt = connection.execute(select(receipts).where(
            receipts.c.call_id == "oracle-collection-call"
        )).mappings().one()

    assert (work["purpose"], work["state"], work["actual_usd_micro"],
            work["actual_allowance_micro"]) == ("work", "settled", 2, 3)
    assert work_slot["state"] == "released"
    assert work_intent["state"] == "known"
    assert (work_receipt["outcome"], work_receipt["actual_usd_micro"],
            work_receipt["actual_allowance_micro"]) == ("known", 2, 3)
    assert (collection["state"], collection["current_epoch"]) == ("complete", 1)
    assert (epoch["epoch_purpose"], epoch["state"]) == ("execution", "closed")
    assert (guard["active_epoch"], guard["revision"]) == (None, 2)
    assert (collection_reservation["purpose"], collection_reservation["state"]) == (
        "collection", "settled"
    )
    assert collection_slot["state"] == "released"
    assert (collection_intent["purpose"], collection_intent["state"],
            collection_intent["collection_session_id"], collection_intent["collection_epoch"]) == (
        "collection", "known", "oracle-collection", 1
    )
    assert collection_receipt["outcome"] == "known"


def test_same_count_historical_mutation_changes_frozen_data_snapshot(oracle_database):
    database = oracle_database
    backend = database.engine.dialect.name
    with database.engine.connect() as connection:
        before_hash, before_counts = data_snapshot(connection, database.schema)
    with database.engine.begin() as connection:
        table = f'"{database.schema}".' if database.schema else ""
        result = connection.exec_driver_sql(
            f"UPDATE {table}audit_events SET target = 'historical-value-mutated' "
            "WHERE sequence = 1"
        )
        assert result.rowcount == 1
    with database.engine.connect() as connection:
        after_hash, after_counts = data_snapshot(connection, database.schema)
    assert before_counts == after_counts == METADATA["snapshots"][backend]["row_counts"]
    assert after_hash != before_hash


@pytest.mark.parametrize("drift", ("extra", "missing"))
def test_table_inventory_rejects_extra_or_missing_application_table(oracle_database, drift):
    database = oracle_database
    if drift == "extra":
        table_prefix = f'"{database.schema}".' if database.schema else ""
        statement = f"CREATE TABLE {table_prefix}oracle_unexpected_table (id INTEGER PRIMARY KEY)"
    elif database.engine.dialect.name == "postgresql":
        statement = f' DROP TABLE "{database.schema}".worker_heartbeats'
    else:
        statement = "DROP TABLE worker_heartbeats"
    with database.engine.begin() as connection:
        connection.exec_driver_sql(statement)
    with database.engine.connect() as connection:
        expected_word = "unexpected" if drift == "extra" else "missing"
        with pytest.raises(AssertionError, match=expected_word):
            schema_snapshot(connection, database.schema)


def test_named_constraint_rename_changes_schema_snapshot_and_fails_runtime_check(oracle_database):
    database = oracle_database
    with database.engine.connect() as connection:
        before_hash, before_payload = schema_snapshot(connection, database.schema)
    if database.engine.dialect.name == "postgresql":
        with database.engine.begin() as connection:
            connection.exec_driver_sql(
                f'ALTER TABLE "{database.schema}".program_call_receipts '
                "RENAME CONSTRAINT ledger_receipt_bounds TO ledger_receipt_bounds_oracle_drift"
            )
    else:
        with database.engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA writable_schema=ON")
            connection.exec_driver_sql(
                "UPDATE sqlite_master SET sql=replace(sql, "
                "'CONSTRAINT ledger_receipt_bounds', "
                "'CONSTRAINT ledger_receipt_bounds_oracle_drift') "
                "WHERE type='table' AND name='program_call_receipts'"
            )
            connection.exec_driver_sql("PRAGMA writable_schema=OFF")
            version = connection.exec_driver_sql("PRAGMA schema_version").scalar_one()
            connection.exec_driver_sql(f"PRAGMA schema_version={version + 1}")
    with database.engine.connect() as connection:
        after_hash, after_payload = schema_snapshot(connection, database.schema)
    assert after_hash != before_hash
    before_names = {row["name"] for row in before_payload[-2]["checks"]}
    after_names = {row["name"] for row in after_payload[-2]["checks"]}
    assert "ledger_receipt_bounds" in before_names
    assert "ledger_receipt_bounds_oracle_drift" in after_names
    with pytest.raises(DatabaseSchemaError, match="check constraint"):
        database.check()


def test_postgresql_url_guard_rejects_implicit_or_remote_and_libpq_environment(monkeypatch):
    for key in list(os.environ):
        if key.startswith("PG"):
            monkeypatch.delenv(key)
    for value in (
        "postgresql+psycopg://oracle@/fixture",
        "postgresql+psycopg://oracle@localhost/fixture",
        "postgresql+psycopg://oracle@/fixture?host=/var/run/postgresql",
        "postgresql+psycopg://oracle@/fixture?host=/tmp/oracle&hostaddr=192.0.2.1",
    ):
        with pytest.raises(pytest.fail.Exception):
            _local_test_url(value)
    monkeypatch.setenv("PGHOST", "example.invalid")
    with pytest.raises(pytest.fail.Exception):
        _local_test_url("postgresql+psycopg://oracle@/fixture?host=/tmp/oracle")


def test_postgresql_namespace_collision_preserves_existing_schema(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from sqlalchemy.exc import ProgrammingError

    value = os.environ.get("SOVITE_TEST_POSTGRES_URL")
    if not value:
        if os.environ.get("SOVITE_LEDGER_SCHEMA_REQUIRE_PG") == "1":
            pytest.fail("isolated PostgreSQL fixture required")
        pytest.skip("isolated PostgreSQL fixture not supplied")
    url = _local_test_url(value)
    engine = create_engine(url)
    token = uuid.uuid4().hex
    name = "ledger_v9_oracle_" + token
    created = False
    try:
        with engine.begin() as connection:
            assert connection.exec_driver_sql("SELECT inet_server_addr() IS NULL AND NOT rolsuper FROM pg_roles WHERE rolname = current_user").scalar_one()
            connection.exec_driver_sql(f'CREATE SCHEMA "{name}"')
            created = True
        monkeypatch.setattr(uuid, "uuid4", lambda: SimpleNamespace(hex=token))
        generator = oracle_database.__wrapped__(SimpleNamespace(param=("postgresql", "frozen")), tmp_path)
        with pytest.raises(ProgrammingError):
            next(generator)
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT count(*) FROM pg_namespace WHERE nspname = %s", (name,)).scalar_one() == 1
    finally:
        if created:
            with engine.begin() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA "{name}"')
        engine.dispose()
