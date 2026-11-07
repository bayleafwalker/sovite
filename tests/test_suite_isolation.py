"""Regression guard for the suite-wide heap growth behind the timing flakes.

See ``tests/conftest.py``. Each disposed per-test engine must not stay
reachable through module-level ORM mappers. Otherwise full garbage collections
grow long enough to exceed the production 1 s authority budget during
in-process tests.
"""
import gc

import pytest
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
    assert len(_cached_dialects()) >= 3
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


def test_suite_wiring_releases_caches_between_tests_and_freezes_import_graph(request):
    # Guards the conftest wiring itself, not only the helper: the fixture must be
    # autouse for ordinary tests and the collection hook must have frozen objects.
    assert "_isolate_orm_statement_caches" in request.fixturenames
    assert gc.get_freeze_count() > 0


@pytest.fixture(scope="module")
def _engine_left_by_previous_test():
    return {}


def test_order_a_leaves_a_disposed_engine_in_mapper_caches(tmp_path, _engine_left_by_previous_test):
    _use_disposable_engine(tmp_path, "left")
    _engine_left_by_previous_test["cached"] = len(_cached_dialects())
    assert _engine_left_by_previous_test["cached"] >= 1


def test_order_b_previous_test_cache_was_released_by_the_autouse_fixture(_engine_left_by_previous_test):
    # No helper call here: only the autouse teardown of the previous test can
    # have emptied the caches.
    if "cached" not in _engine_left_by_previous_test:
        pytest.skip("test_order_a did not run in this session (deselected or run alone); nothing to release")
    assert _engine_left_by_previous_test["cached"] >= 1
    assert _cached_dialects() == set()
