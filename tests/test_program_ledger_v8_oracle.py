"""Frozen SQLite collection schema oracle from landed schema-8 tree 26867e10.

This deliberately does not derive the table list or expected digest from the
current metadata definitions. A digest failure requires reviewing the actual
historical DDL; it must not be regenerated to make a new migration pass.
"""
import hashlib
import json

from sovite.db import Database


_V8_NAMES = (
    "program_budget_scopes", "program_budget_subjects", "collection_sessions",
    "collection_epochs", "collection_epoch_guards", "collection_enrollments",
    "collection_operations", "collection_acknowledgements", "collection_checkpoints",
    "collection_observations", "program_reservations", "program_slots",
    "collection_retention_refs",
)
_V8_SQLITE_SCHEMA_SHA256 = "a68acda4dd4b22fa8a5e4002469d3b2b7d7333157b4ff77ba30c5b1eea79ba93"


def test_collection_v8_structure_remains_frozen(tmp_path):
    database = Database(f"sqlite:///{tmp_path}/oracle.db")
    try:
        database.migrate()
        placeholders = ",".join("?" for _ in _V8_NAMES)
        with database.engine.connect() as connection:
            rows = connection.exec_driver_sql(
                "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE tbl_name IN ("
                + placeholders + ") ORDER BY type,name", _V8_NAMES).all()
        payload = json.dumps([list(row) for row in rows], separators=(",", ":"), ensure_ascii=True).encode()
        assert len(rows) == 50
        assert hashlib.sha256(payload).hexdigest() == _V8_SQLITE_SCHEMA_SHA256
    finally:
        database.engine.dispose()
