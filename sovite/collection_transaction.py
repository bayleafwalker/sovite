"""Serialized owner transaction boundary for dormant collection state."""
from __future__ import annotations

from contextlib import contextmanager


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
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
