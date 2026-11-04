"""Durable recovery-cursor scope, strictness, and transaction tests."""
from __future__ import annotations

import pytest

from sovite.collection_transaction import owner_transaction
from sovite.db import Database, MetadataRow
from sovite.recovery_cursor import (
    CURSOR_PREFIX, CURSOR_SCHEMA, RecoveryCursorUnavailable,
    advance_cursor, cursor_key, peek_cursor, read_cursor,
)


def _advance(session, cursor, **changes):
    values = {
        "phase": cursor.phase,
        "last_job_id": cursor.last_job_id,
        "fair_pointer_job_id": cursor.fair_pointer_job_id,
        "forward_upper_id": cursor.forward_upper_id,
        "scan_started_at": cursor.scan_started_at,
        "covered_through": cursor.covered_through,
    }
    values.update(changes)
    return advance_cursor(session, cursor, **values)


def test_cursor_key_scope_and_read_only_missing_cursor(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'recovery-cursor.db'}")
    try:
        db.migrate()
        key = cursor_key("compute-a", False)
        assert len(key) == 79 and key.startswith(CURSOR_PREFIX)
        assert cursor_key("compute-a", True) != key
        assert cursor_key("compute-b", False) != key
        for compute_id, repository_only in (("", False), ("compute", 1), ("x" * 161, False)):
            with pytest.raises(RecoveryCursorUnavailable):
                cursor_key(compute_id, repository_only)
        with db.sessions() as session:
            assert peek_cursor(session, "compute-a", False) is None
            assert session.get(MetadataRow, key) is None
        with owner_transaction(db) as session:
            current = read_cursor(session, "compute-a", False)
            assert current.phase == "idle"
            assert current.last_job_id is None
            assert current.value == {
                "schema_version": CURSOR_SCHEMA,
                "compute_id": "compute-a",
                "repository_worker_only": False,
                "phase": "idle",
                "last_job_id": None,
                "fair_pointer_job_id": None,
                "forward_upper_id": None,
                "scan_started_at": None,
                "covered_through": None,
            }
    finally:
        db.engine.dispose()


def test_cursor_cas_cycle_progress_and_rollback(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'recovery-cursor-cas.db'}")
    try:
        db.migrate()
        with owner_transaction(db) as session:
            first = read_cursor(session, "compute", False)
            assert _advance(session, first, phase="forward", scan_started_at=1.0,
                            forward_upper_id="job-9")
            assert not _advance(session, first, phase="forward", scan_started_at=1.0,
                                forward_upper_id="job-9")
        with owner_transaction(db) as session:
            current = read_cursor(session, "compute", False)
            assert current.phase == "forward" and current.scan_started_at == 1.0
            assert _advance(session, current, last_job_id="job-1")
        with pytest.raises(RuntimeError, match="test rollback"):
            with owner_transaction(db) as session:
                current = read_cursor(session, "compute", False)
                assert _advance(session, current, phase="wrap", last_job_id=None,
                                fair_pointer_job_id="job-1")
                raise RuntimeError("test rollback")
        with owner_transaction(db) as session:
            current = read_cursor(session, "compute", False)
            assert current.phase == "forward" and current.last_job_id == "job-1"
            assert _advance(session, current, phase="idle", last_job_id=None,
                            fair_pointer_job_id="job-9", forward_upper_id=None,
                            scan_started_at=None, covered_through=1.0)
        with db.sessions() as session:
            completed = peek_cursor(session, "compute", False)
            assert completed.phase == "idle" and completed.fair_pointer_job_id == "job-9"
            assert completed.covered_through == 1.0
    finally:
        db.engine.dispose()


@pytest.mark.parametrize("bad_value", [
    None,
    {},
    {"schema_version": CURSOR_SCHEMA, "compute_id": "other",
     "repository_worker_only": False, "phase": "idle", "last_job_id": None,
     "fair_pointer_job_id": None, "forward_upper_id": None,
     "scan_started_at": None, "covered_through": None},
    {"schema_version": CURSOR_SCHEMA, "compute_id": "compute",
     "repository_worker_only": 0, "phase": "idle", "last_job_id": None,
     "fair_pointer_job_id": None, "forward_upper_id": None,
     "scan_started_at": None, "covered_through": None},
    {"schema_version": CURSOR_SCHEMA, "compute_id": "compute",
     "repository_worker_only": False, "phase": "idle", "last_job_id": "",
     "fair_pointer_job_id": None, "forward_upper_id": None,
     "scan_started_at": None, "covered_through": None},
    {"schema_version": CURSOR_SCHEMA, "compute_id": "compute",
     "repository_worker_only": False, "phase": "idle", "last_job_id": None,
     "fair_pointer_job_id": None, "forward_upper_id": None,
     "scan_started_at": None, "covered_through": None, "extra": "no"},
    {"schema_version": CURSOR_SCHEMA, "compute_id": "compute",
     "repository_worker_only": False, "phase": "forward", "last_job_id": "job-9",
     "fair_pointer_job_id": None, "forward_upper_id": "job-8",
     "scan_started_at": 1.0, "covered_through": None},
    {"schema_version": CURSOR_SCHEMA, "compute_id": "compute",
     "repository_worker_only": False, "phase": "wrap", "last_job_id": "job-9",
     "fair_pointer_job_id": "job-8", "forward_upper_id": "job-z",
     "scan_started_at": 1.0, "covered_through": None},
    {"schema_version": CURSOR_SCHEMA, "compute_id": "compute",
     "repository_worker_only": False, "phase": "forward", "last_job_id": None,
     "fair_pointer_job_id": None, "forward_upper_id": "job-z",
     "scan_started_at": True, "covered_through": None},
    {"schema_version": CURSOR_SCHEMA, "compute_id": "compute",
     "repository_worker_only": False, "phase": "idle", "last_job_id": None,
     "fair_pointer_job_id": None, "forward_upper_id": None,
     "scan_started_at": None, "covered_through": float("inf")},
    {"schema_version": CURSOR_SCHEMA, "compute_id": "compute",
     "repository_worker_only": False, "phase": "forward", "last_job_id": None,
     "fair_pointer_job_id": None, "forward_upper_id": "job-z",
     "scan_started_at": 1.0, "covered_through": 2.0},
])
def test_malformed_cursor_fails_closed_without_reset(tmp_path, bad_value):
    db = Database(f"sqlite:///{tmp_path / 'malformed-cursor.db'}")
    try:
        db.migrate()
        key = cursor_key("compute", False)
        with db.sessions.begin() as session:
            session.add(MetadataRow(key=key, value=bad_value))
        with db.sessions() as session:
            with pytest.raises(RecoveryCursorUnavailable):
                peek_cursor(session, "compute", False)
        with pytest.raises(RecoveryCursorUnavailable):
            with owner_transaction(db) as session:
                read_cursor(session, "compute", False)
        with db.sessions() as session:
            assert session.get(MetadataRow, key).value == bad_value
    finally:
        db.engine.dispose()
