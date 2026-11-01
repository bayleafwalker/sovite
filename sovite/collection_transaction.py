"""Serialized owner transaction boundary for dormant collection state."""
from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import select

from .db import AuditEvent, MetadataRow, digest


def _verify_audit(session):
    previous, sequence = "0" * 64, 0
    rows = session.scalars(select(AuditEvent).order_by(AuditEvent.sequence)).all()
    for row in rows:
        body = {name: getattr(row, name) for name in
                ("sequence", "created_at", "actor", "action", "target", "detail", "previous_hash")}
        if row.sequence != sequence + 1 or row.previous_hash != previous or row.hash != digest(body):
            raise ValueError("collection audit unavailable")
        previous, sequence = row.hash, row.sequence
    head = session.get(MetadataRow, "audit_head")
    if head is None or head.value != {"sequence": sequence, "hash": previous}:
        raise ValueError("collection audit unavailable")


@contextmanager
def owner_transaction(database):
    """Acquire the global audit authority lock before reading owner state.

    SQLite requires a write transaction before any read. PostgreSQL uses the
    existing audit-head row update as the first lock. Callers must take any
    scope, subject, session and obligation locks in that order after this one.
    """
    with database.sessions() as session:
        try:
            if database.engine.dialect.name == "sqlite":
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            elif database.engine.dialect.name != "postgresql":
                raise ValueError("unsupported collection database")
            database.authority_lock(session)
            _verify_audit(session)
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
