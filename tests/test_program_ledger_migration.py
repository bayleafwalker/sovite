"""Disposable offline migration tests; no production database is used."""
import hashlib
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, insert, inspect, select

from sovite.db import Base, Database, DatabaseSchemaError, Job, MetadataRow, _PROGRAM_LEDGER_V9


_FIXTURE_SHA256 = {
    "program_ledger_v8_sqlite.sql": "5ad46615b2b78dd20c4c6d27be546c45afd6372382a0ee44e76d880206bf8c24",
    "program_ledger_v8_postgresql.sql": "f83c2a3b4e1e6b056369780aabde2a96309e47b48875d5bf9c0f794838c61d01",
}


def _frozen_fixture(path):
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == _FIXTURE_SHA256[path.name]
    return raw.decode("utf-8")


_EXPECTED_V9 = {
    "program_policy_revisions", "program_census_batches", "program_gate_state", "program_subject_registry",
    "program_call_intents", "program_overflow_evidence", "program_call_receipts", "program_legacy_links",
}


@pytest.fixture(params=["sqlite", "postgresql"])
def v8_database(request, tmp_path):
    admin = None
    namespace = None
    fixtures = Path(__file__).parent / "fixtures"
    if request.param == "sqlite":
        db = Database(f"sqlite:///{tmp_path}/upgrade.db")
        sql = _frozen_fixture(fixtures / "program_ledger_v8_sqlite.sql")
        with db.engine.connect() as connection:
            connection.connection.driver_connection.executescript(sql)
    else:
        url = os.environ.get("SOVITE_TEST_POSTGRES_URL")
        if not url:
            if os.environ.get("SOVITE_LEDGER_SCHEMA_REQUIRE_PG") == "1":
                pytest.fail("SOVITE_TEST_POSTGRES_URL is required for ledger schema tests")
            pytest.skip("isolated PostgreSQL fixture not supplied")
        admin = create_engine(url)
        namespace = "ledger_migration_" + uuid.uuid4().hex
        sql = _frozen_fixture(fixtures / "program_ledger_v8_postgresql.sql").replace("__LEDGER_V8_SCHEMA__", namespace)
        with admin.connect() as connection:
            raw = connection.connection.driver_connection
            raw.execute(sql, prepare=False)
            raw.commit()
        db = Database(url, schema=namespace)
    try:
        yield db
    finally:
        db.engine.dispose()
        if admin is not None:
            with admin.begin() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA "{namespace}" CASCADE')
            admin.dispose()


def test_v8_upgrade_preserves_history_and_starts_without_gate(v8_database):
    db = v8_database
    assert {table.name for table in _PROGRAM_LEDGER_V9} == _EXPECTED_V9
    historical_tables = ("audit_events", "jobs", "program_budget_scopes", "program_budget_subjects",
                         "program_reservations", "program_slots")
    with db.sessions() as session:
        before = {name: [dict(row) for row in session.execute(select(Base.metadata.tables[name])).mappings()]
                  for name in historical_tables}
    db.migrate()
    db.check()
    from sovite.collection_transaction import owner_transaction
    with owner_transaction(db):
        pass  # Verify the frozen audit chain after migration.
    with db.sessions() as session:
        after = {name: [dict(row) for row in session.execute(select(Base.metadata.tables[name])).mappings()]
                 for name in historical_tables}
        assert after == before
        assert session.get(MetadataRow, "schema_version").value == {"version": 9}
        job = session.get(Job, "historical")
        assert (job.task, job.resolved) == ("preserve", {"marker": ["a", 1]})
        subject = session.execute(select(Base.metadata.tables["program_budget_subjects"])).mappings().one()
        assert (subject.scope_id, subject.subject_id, subject.purpose, subject.binding_digest) == (
            "scope", "historical", "work", "b" * 64)
        assert all(session.execute(select(table)).first() is None for table in _PROGRAM_LEDGER_V9)
    old_version = db.schema_version
    db.schema_version = 8
    try:
        with pytest.raises(DatabaseSchemaError, match="newer"):
            db.check()
    finally:
        db.schema_version = old_version
    db.migrate()


def test_partial_v9_is_refused_without_version_advance(v8_database):
    db = v8_database
    Base.metadata.tables["program_policy_revisions"].create(db.engine)
    with pytest.raises(DatabaseSchemaError, match="partial ledger"):
        db.migrate()
    with db.sessions() as session:
        assert session.get(MetadataRow, "schema_version").value == {"version": 8}
    assert inspect(db.engine).has_table("program_policy_revisions", schema=db.schema)
    assert not inspect(db.engine).has_table("program_gate_state", schema=db.schema)


def test_v9_ddl_failure_rolls_back_tables_and_version(v8_database):
    from sqlalchemy import event
    db = v8_database
    def fail_during_ddl(conn, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("CREATE TABLE") and "PROGRAM_CENSUS_BATCHES" in statement.upper():
            raise RuntimeError("injected migration failure")
    event.listen(db.engine, "before_cursor_execute", fail_during_ddl)
    try:
        with pytest.raises(RuntimeError, match="migration failure"):
            db.migrate()
    finally:
        event.remove(db.engine, "before_cursor_execute", fail_during_ddl)
    with db.sessions() as session:
        assert session.get(MetadataRow, "schema_version").value == {"version": 8}
        assert session.get(Job, "historical").task == "preserve"
    assert not any(inspect(db.engine).has_table(name, schema=db.schema) for name in _EXPECTED_V9)


def test_runtime_rejects_altered_v9_check(v8_database):
    db = v8_database
    db.migrate()
    with db.engine.begin() as connection:
        if db.engine.dialect.name == "postgresql":
            connection.exec_driver_sql(f'ALTER TABLE "{db.schema}".program_call_receipts DROP CONSTRAINT ledger_receipt_bounds')
        else:
            connection.exec_driver_sql("PRAGMA writable_schema=ON")
            connection.exec_driver_sql(
                "UPDATE sqlite_master SET sql=replace(sql, 'custody_bytes <= 1048576', 'custody_bytes <= 1048575') "
                "WHERE type='table' AND name='program_call_receipts'")
            connection.exec_driver_sql("PRAGMA writable_schema=OFF")
            version = connection.exec_driver_sql("PRAGMA schema_version").scalar_one()
            connection.exec_driver_sql(f"PRAGMA schema_version={version + 1}")
    with pytest.raises(DatabaseSchemaError, match="check"):
        db.check()


def test_sqlite_disabled_foreign_keys_are_rejected(v8_database):
    db = v8_database
    if db.engine.dialect.name != "sqlite":
        pytest.skip("SQLite-specific foreign-key switch")
    db.migrate()
    with db.engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        with pytest.raises(DatabaseSchemaError, match="foreign key enforcement"):
            db._structure(connection, Base.metadata.sorted_tables)
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")


def test_runtime_check_after_upgrade_performs_no_ddl(v8_database):
    from sqlalchemy import event
    db = v8_database
    db.migrate()
    statements = []
    def capture(conn, cursor, statement, parameters, context, many):
        statements.append(statement)
    event.listen(db.engine, "before_cursor_execute", capture)
    try:
        db.initialize("never")
    finally:
        event.remove(db.engine, "before_cursor_execute", capture)
    assert statements
    assert not any(sql.lstrip().upper().startswith(("CREATE", "ALTER", "DROP", "INSERT", "UPDATE", "DELETE"))
                   for sql in statements)


def test_runtime_rejects_altered_v9_fk_action(v8_database):
    db = v8_database
    db.migrate()
    with db.engine.begin() as connection:
        if db.engine.dialect.name == "postgresql":
            connection.exec_driver_sql(f'ALTER TABLE "{db.schema}".program_call_receipts DROP CONSTRAINT ledger_receipt_intent_fk')
            connection.exec_driver_sql(f'ALTER TABLE "{db.schema}".program_call_receipts ADD CONSTRAINT ledger_receipt_intent_fk '
                f'FOREIGN KEY (scope_id, call_id) REFERENCES "{db.schema}".program_call_intents (scope_id, call_id) '
                'ON DELETE CASCADE ON UPDATE RESTRICT')
        else:
            connection.exec_driver_sql("PRAGMA writable_schema=ON")
            connection.exec_driver_sql(
                "UPDATE sqlite_master SET sql=replace(sql, "
                "'REFERENCES program_call_intents (scope_id, call_id) ON DELETE RESTRICT', "
                "'REFERENCES program_call_intents (scope_id, call_id) ON DELETE CASCADE') "
                "WHERE type='table' AND name='program_call_receipts'")
            connection.exec_driver_sql("PRAGMA writable_schema=OFF")
            version = connection.exec_driver_sql("PRAGMA schema_version").scalar_one()
            connection.exec_driver_sql(f"PRAGMA schema_version={version + 1}")
    with pytest.raises(DatabaseSchemaError, match="foreign key"):
        db.check()


def test_restored_postgres_check_normalization_preserves_literal_values():
    from sovite.db import _check_expression_tree
    expected = _check_expression_tree("purpose IN ('work', 'collection')", text_columns={"purpose"})
    restored = "purpose::text = ANY (ARRAY['work'::character varying::text, 'collection'::character varying::text])"
    assert _check_expression_tree(restored, text_columns={"purpose"}) == expected
    altered = restored.replace("'work'", "'different'")
    assert _check_expression_tree(altered, text_columns={"purpose"}) != expected


def test_concurrent_offline_upgrades_leave_one_valid_schema(v8_database):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    db = v8_database
    barrier = Barrier(2)
    def upgrade():
        barrier.wait(timeout=10)
        try:
            db.migrate()
            return "ok"
        except Exception:
            # A concurrent offline migrator may be rejected; it must not leave
            # an accepted partial schema or lose the historical obligation.
            return "rejected"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: upgrade(), range(2)))
    assert "ok" in results
    db.check()
    with db.sessions() as session:
        assert session.get(MetadataRow, "schema_version").value == {"version": 9}
        reservation = session.execute(select(Base.metadata.tables["program_reservations"])).mappings().one()
        assert (reservation.state, reservation.reserved_usd_micro, reservation.reserved_allowance_micro) == ("unknown", 3, 4)
