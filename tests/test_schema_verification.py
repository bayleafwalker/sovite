"""Structural checks must detect missing or altered database invariants."""
import os
import uuid

import pytest
from sqlalchemy import CheckConstraint, Column, ForeignKeyConstraint, Index, Integer, MetaData, String, Table, UniqueConstraint, create_engine

from sovite.db import Database, DatabaseSchemaError, _check_expression_tree


def tables(*, target="parent", delete="RESTRICT", update="RESTRICT", check="amount >= 0", unique=True, index=True, foreign=True, deferrable=False):
    metadata = MetaData()
    Table("parent", metadata, Column("id", Integer, primary_key=True))
    Table("other", metadata, Column("id", Integer, primary_key=True))
    child = Table("child", metadata, Column("id", Integer, primary_key=True),
                  Column("parent_id", Integer, nullable=False), Column("amount", Integer, nullable=False),
                  Column("state", String(16), nullable=False),
                  CheckConstraint(check, name="child_amount_check"),
                  CheckConstraint("state IN ('ready', 'done')", name="child_state_check"))
    if foreign:
        child.append_constraint(ForeignKeyConstraint(["parent_id"], [f"{target}.id"], ondelete=delete,
                                                     onupdate=update, deferrable=deferrable, name="child_parent_fk"))
    if unique:
        child.append_constraint(UniqueConstraint("parent_id", "state", name="child_parent_state_unique"))
    if index:
        Index("child_amount_idx", child.c.amount)
    return metadata


@pytest.fixture(params=["sqlite", "postgresql"])
def db(request, tmp_path):
    if request.param == "sqlite":
        database = Database(f"sqlite:///{tmp_path}/schema.db")
        yield database
        database.engine.dispose()
        return
    url = os.environ.get("SOVITE_TEST_POSTGRES_URL")
    if not url:
        if os.environ.get("SOVITE_SCHEMA_REQUIRE_PG") == "1":
            pytest.fail("SOVITE_TEST_POSTGRES_URL is required for schema verification")
        pytest.skip("isolated PostgreSQL fixture not supplied")
    schema = "schema_verify_" + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    database = Database(url, schema=schema)
    try:
        yield database
    finally:
        database.engine.dispose()
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()


def verify(db, actual, expected):
    with db.engine.begin() as connection:
        actual.create_all(connection)
        db._structure(connection, expected.sorted_tables)


def test_valid_structure(db):
    verify(db, tables(), tables())


@pytest.mark.parametrize("damage", ["target", "delete", "update", "check", "unique", "index", "missing_fk", "deferred"])
def test_changed_constraint_is_rejected(db, damage):
    changes = {"target": {"target": "other"}, "delete": {"delete": "CASCADE"},
               "update": {"update": "CASCADE"}, "check": {"check": "amount > 0"},
               "unique": {"unique": False}, "index": {"index": False},
               "missing_fk": {"foreign": False}, "deferred": {"deferrable": True}}
    with pytest.raises(DatabaseSchemaError, match="foreign key|check constraint|unique constraint|index"):
        verify(db, tables(**changes[damage]), tables())


def test_sqlite_disabled_foreign_keys_rejected(tmp_path):
    database = Database(f"sqlite:///{tmp_path}/disabled.db")
    try:
        with database.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            with pytest.raises(DatabaseSchemaError, match="foreign key enforcement"):
                database._structure(connection, [])
    finally:
        database.engine.dispose()


@pytest.mark.parametrize("bad", ["amount + 1 > 0", "f(amount) = 1", "amount >= 0; SELECT 1", "state IN ('a') OR 1=1", "amount >= 1.5"])
def test_unsupported_check_syntax_is_rejected(bad):
    with pytest.raises(ValueError):
        _check_expression_tree(bad)


def test_postgres_unvalidated_foreign_key_rejected(db):
    if db.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL-specific constraint state")
    metadata = tables()
    with db.engine.begin() as connection:
        metadata.create_all(connection)
        connection.exec_driver_sql(f'ALTER TABLE "{db.schema}".child DROP CONSTRAINT child_parent_fk')
        connection.exec_driver_sql(f'ALTER TABLE "{db.schema}".child ADD CONSTRAINT child_parent_fk FOREIGN KEY (parent_id) REFERENCES "{db.schema}".parent(id) ON DELETE RESTRICT ON UPDATE RESTRICT NOT VALID')
        with pytest.raises(DatabaseSchemaError, match="Unvalidated foreign key"):
            db._structure(connection, metadata.sorted_tables)


def test_postgres_unvalidated_check_rejected(db):
    if db.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL-specific constraint state")
    metadata = tables()
    with db.engine.begin() as connection:
        metadata.create_all(connection)
        connection.exec_driver_sql(f'ALTER TABLE "{db.schema}".child DROP CONSTRAINT child_amount_check')
        connection.exec_driver_sql(f'ALTER TABLE "{db.schema}".child ADD CONSTRAINT child_amount_check CHECK (amount >= 0) NOT VALID')
        with pytest.raises(DatabaseSchemaError, match="Unvalidated check constraint"):
            db._structure(connection, metadata.sorted_tables)


def test_postgres_fk_to_search_path_decoy_is_rejected(db):
    if db.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL-specific search path")
    metadata = tables()
    decoy = db.schema + "_decoy"
    with db.engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{decoy}"')
        connection.exec_driver_sql(f'CREATE TABLE "{decoy}".parent (id integer PRIMARY KEY)')
        metadata.create_all(connection)
        connection.exec_driver_sql(f'ALTER TABLE "{db.schema}".child DROP CONSTRAINT child_parent_fk')
        connection.exec_driver_sql(f'ALTER TABLE "{db.schema}".child ADD CONSTRAINT child_parent_fk FOREIGN KEY (parent_id) REFERENCES "{decoy}".parent(id) ON DELETE RESTRICT ON UPDATE RESTRICT')
        connection.exec_driver_sql(f'SET LOCAL search_path TO "{decoy}"')
        with pytest.raises(DatabaseSchemaError, match="foreign key"):
            db._structure(connection, metadata.sorted_tables)
        connection.exec_driver_sql(f'DROP SCHEMA "{decoy}" CASCADE')
