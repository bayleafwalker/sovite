"""Unexpected recovery helper failures must abort claim globally."""
from __future__ import annotations

import traceback
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from sovite.db import Base, Job, MetadataRow, WorkerExecutionLease
from sovite.lease_service import LeaseService, execution_owner_key
from sovite.recovery_cursor import cursor_key
from sovite.service import PlatformError
from test_work_projection_rows import _finish_snapshot, work_rows


@pytest.mark.parametrize("failure_site", (
    "lock_executor", "comparator", "receipt_query", "binding_reflection",
))
def test_unexpected_recovery_helper_failure_rolls_back_and_blocks_claim(
    work_rows, monkeypatch, failure_site,
):
    db, _, _, _ = work_rows
    with db.sessions.begin() as session:
        job = session.get(Job, "job")
        job.resolved = {**job.resolved, "compute": {"id": "fixture"}}
        expiry = session.get(WorkerExecutionLease, "job").lease_until
    import sovite.lease_service as lease_module
    import sovite.work_projection as projection_module

    monkeypatch.setattr(lease_module.time, "time", lambda: expiry + 1)
    # Keep the failure injection downstream of the unrelated terminal sweep;
    # this service fixture intentionally does not configure sweep retention.
    monkeypatch.setattr(lease_module, "sweep_terminal_authority", lambda *_: None)
    service = LeaseService(
        SimpleNamespace(db=db, settings=SimpleNamespace(compute_id="fixture", max_attempts=3)),
        execution_owner_key(workload_id="workload", instance_id="instance"), {},
    )
    reserve_calls = []
    monkeypatch.setattr(service, "reserve", lambda **_: reserve_calls.append(True))
    before = _finish_snapshot(db)
    injected = {"reached": False}

    if failure_site == "lock_executor":
        original = projection_module.lock_work_projection_set
        scope = Base.metadata.tables["program_budget_scopes"]

        def fail_after_candidate_write(session, *args, **kwargs):
            original(session, *args, **kwargs)
            session.execute(update(scope).where(scope.c.id == "scope").values(usd_limit_micro=99))
            session.flush()
            injected["reached"] = True
            raise RuntimeError("synthetic-private-lock-marker")

        monkeypatch.setattr(projection_module, "lock_work_projection_set", fail_after_candidate_write)
    elif failure_site == "comparator":
        def fail_comparator(*_args, **_kwargs):
            injected["reached"] = True
            raise RuntimeError("synthetic-private-comparator-marker")

        monkeypatch.setattr(projection_module, "_compare_actual_rows", fail_comparator)
    elif failure_site == "receipt_query":
        from sqlalchemy.orm import Session

        original_execute = Session.execute

        def fail_receipt_query(session, statement, *args, **kwargs):
            if not injected["reached"] and "program_call_receipts" in str(statement):
                injected["reached"] = True
                raise RuntimeError("synthetic-private-receipt-query-marker")
            return original_execute(session, statement, *args, **kwargs)

        monkeypatch.setattr(Session, "execute", fail_receipt_query)
    else:
        import sqlalchemy

        original_table = sqlalchemy.Table

        def fail_binding_reflection(name, *args, **kwargs):
            if not injected["reached"] and name == projection_module._BINDING_TABLE:
                injected["reached"] = True
                raise RuntimeError("synthetic-private-binding-reflection-marker")
            return original_table(name, *args, **kwargs)

        monkeypatch.setattr(sqlalchemy, "Table", fail_binding_reflection)

    with pytest.raises(PlatformError) as error:
        service.claim()
    assert error.value.code == "owner_unavailable"
    assert injected["reached"] is True
    assert "synthetic-private" not in "".join(traceback.format_exception(error.value))
    assert error.value.__suppress_context__ is True
    assert reserve_calls == []
    assert _finish_snapshot(db) == before
    key = cursor_key("fixture", False)
    with db.sessions() as session:
        assert session.get(MetadataRow, key) is None
        assert session.get(Job, "job").status == "running"
