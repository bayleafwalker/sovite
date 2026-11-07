"""Suite-wide test isolation for process-global state.

SQLAlchemy keeps a per-mapper compiled-statement LRU cache
(``Mapper._compiled_cache``, 100 entries) keyed by dialect instance. A
production process uses a few long-lived engines. The suite creates a new
engine, and therefore a new dialect, for almost every test. Without this
fixture, the module-level ORM mappers keep up to 100 dead dialects each, along
with their type caches and compiled statements. In a local full run on
2026-11-07, the live heap grew from about 0.45 M to 1.6 M objects. That run
had 105 generation-2 collections: median 333 ms, max 1028 ms. Hosted runners
pause longer (see below). Those pauses exceeded the
production 1 s authority and lifecycle budgets that in-process tests exercise,
and they showed up as the "unexplained" ``authority_unavailable``,
handshake-timeout and interrupted-fixture CI failures.

A diagnostic hosted run (slowcall probe, run 42347878439) confirmed it. The
failing ``Authority.hello`` took 1037 ms, and 1032 ms of that overlapped a
generation-2 collection. That run recorded 98 generation-2 pauses over 100 ms,
the longest 1.4 s.

The same probe found a second, smaller source: SQLite fsync stalls of up to
about 1 s on the runner disk. A commit (``do_commit``) inside an authority call
accounted for a 389 ms owner call. Test databases are disposable, so the suite
turns off SQLite's fsync. Durability against a process crash is unchanged, and
production engines keep SQLite's default synchronous mode.

These fixtures change no production timeout or authority semantics. They keep
each test's ORM statement cache local to that test, as it would be in a
long-lived production process.
"""
import gc

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

from sovite.db import Base


@event.listens_for(Engine, "connect")
def _disposable_sqlite_without_fsync(dbapi_connection, connection_record):
    if type(dbapi_connection).__module__.startswith("sqlite3"):
        dbapi_connection.execute("PRAGMA synchronous=OFF")


def release_orm_statement_caches():
    """Drop per-mapper compiled statements that pin the dialects of disposed engines."""
    for mapper in Base.registry.mappers:
        cache = mapper.__dict__.get("_compiled_cache")
        if cache is not None:
            cache.clear()


@pytest.fixture(autouse=True)
def _isolate_orm_statement_caches():
    yield
    release_orm_statement_caches()


def pytest_collection_finish(session):
    # Imported modules, classes and collected items live for the whole session.
    # Freezing them keeps later full collections proportional to test-created
    # objects instead of rescanning the whole import graph every time.
    gc.collect()
    gc.freeze()
