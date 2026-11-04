"""Durable recovery-cursor scope, strictness, and transaction tests."""
from __future__ import annotations

import pytest

from sovite.collection_transaction import owner_transaction
from sovite.db import Database, MetadataRow
from sovite.recovery_cursor import (
    CURSOR_PREFIX, CURSOR_SCHEMA, RecoveryCursorUnavailable,
    advance_cursor, cursor_key, read_cursor,
)


def test_cursor_key_has_schema_bound_and_isolated_scope():
    key = cursor_key("compute-a", False)
    assert len(key) == 79 and key.startswith(CURSOR_PREFIX)
    assert cursor_key("compute-a", True) != key
    assert cursor_key("compute-b", False) != key
    for compute_id, repository_only in (("", False), ("compute", 1), ("x" * 161, False)):
        with pytest.raises(RecoveryCursorUnavailable):
            cursor_key(compute_id, repository_only)


def test_cursor_initialization_cas_wrap_and_rollback(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'recovery-cursor.db'}")
    try:
        db.migrate()
        with db.sessions.begin() as session:
            with pytest.raises(RecoveryCursorUnavailable):
                read_cursor(session, "compute", False)

        with owner_transaction(db) as session:
            first = read_cursor(session, "compute", False)
            assert first.last_job_id is None
            assert first.value == {
                "schema_version": CURSOR_SCHEMA,
                "compute_id": "compute",
                "repository_worker_only": False,
                "last_job_id": None,
            }
            assert advance_cursor(session, first, "job-1")
            assert not advance_cursor(session, first, "job-2")

        with owner_transaction(db) as session:
            current = read_cursor(session, "compute", False)
            assert current.last_job_id == "job-1"
            assert advance_cursor(session, current, None)

        with pytest.raises(RuntimeError, match="test rollback"):
            with owner_transaction(db) as session:
                current = read_cursor(session, "compute", False)
                assert current.last_job_id is None
                assert advance_cursor(session, current, "job-rollback")
                raise RuntimeError("test rollback")

        with owner_transaction(db) as session:
            assert read_cursor(session, "compute", False).last_job_id is None
            assert read_cursor(session, "compute", True).last_job_id is None
    finally:
        db.engine.dispose()


@pytest.mark.parametrize("bad_value", [
    None,
    {},
    {"schema_version": CURSOR_SCHEMA, "compute_id": "other",
     "repository_worker_only": False, "last_job_id": None},
    {"schema_version": CURSOR_SCHEMA, "compute_id": "compute",
     "repository_worker_only": 0, "last_job_id": None},
    {"schema_version": CURSOR_SCHEMA, "compute_id": "compute",
     "repository_worker_only": False, "last_job_id": ""},
    {"schema_version": CURSOR_SCHEMA, "compute_id": "compute",
     "repository_worker_only": False, "last_job_id": "x" * 65},
    {"schema_version": CURSOR_SCHEMA, "compute_id": "compute",
     "repository_worker_only": False, "last_job_id": None, "extra": "no"},
])
def test_malformed_cursor_fails_closed_without_reset(tmp_path, bad_value):
    db = Database(f"sqlite:///{tmp_path / 'malformed-cursor.db'}")
    try:
        db.migrate()
        key = cursor_key("compute", False)
        with db.sessions.begin() as session:
            session.add(MetadataRow(key=key, value=bad_value))
        with pytest.raises(RecoveryCursorUnavailable):
            with owner_transaction(db) as session:
                read_cursor(session, "compute", False)
        with db.sessions() as session:
            assert session.get(MetadataRow, key).value == bad_value
    finally:
        db.engine.dispose()
