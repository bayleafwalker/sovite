"""Regression guard for the suite-wide heap growth behind the timing flakes.

See ``tests/conftest.py``. Each disposed per-test engine must not stay
reachable through module-level ORM mappers. Otherwise full garbage collections
grow long enough to exceed the production 1 s authority budget during
in-process tests.
"""
import gc

from sqlalchemy.engine import Dialect

from conftest import release_orm_statement_caches
from sovite.db import Base, Database, MetadataRow


def _cached_dialects():
    found = set()
    for mapper in Base.registry.mappers:
        cache = mapper.__dict__.get("_compiled_cache")
        for key in list(cache or ()):
            if isinstance(key, tuple) and key and isinstance(key[0], Dialect):
                found.add(id(key[0]))
    return found


def _use_disposable_engine(tmp_path, index):
    database = Database(f"sqlite:///{tmp_path}/isolation-{index}.db")
    try:
        database.migrate()
        with database.sessions.begin() as session:
            session.add(MetadataRow(key=f"isolation-{index}", value={"index": index}))
    finally:
        database.engine.dispose()


def test_disposed_engines_do_not_stay_reachable_through_mapper_caches(tmp_path):
    release_orm_statement_caches()
    for index in range(3):
        _use_disposable_engine(tmp_path, index)
    # ORM persistence caches compiled INSERTs per dialect on the mapper.
    assert len(_cached_dialects()) == 3
    release_orm_statement_caches()
    assert _cached_dialects() == set()


def test_repeated_per_test_engines_keep_the_live_heap_bounded(tmp_path):
    def live_objects():
        gc.collect()
        return len(gc.get_objects())

    release_orm_statement_caches()
    _use_disposable_engine(tmp_path, "warm")
    release_orm_statement_caches()
    baseline = live_objects()
    for index in range(25):
        _use_disposable_engine(tmp_path, index)
        release_orm_statement_caches()
    growth = live_objects() - baseline
    # Measured: about 0 objects with the release, and about 220 objects per
    # iteration (about 5.5k here) without it. Real tests touch many more mappers
    # per engine, so they retain far more per test.
    assert growth < 1000, growth
