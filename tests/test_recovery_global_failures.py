"""Unexpected recovery helper failures must abort claim globally."""
from __future__ import annotations

import traceback
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from sovite.db import Base, Job, MetadataRow, WorkerExecutionLease
from sovite.lease_service import ExecutionPrincipal, LeaseService, execution_owner_key
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
        SimpleNamespace(db=db, active=lambda _session: None, settings=SimpleNamespace(
            compute_id="fixture", execution_workload_identity="workload", max_attempts=3)),
        execution_owner_key(workload_id="workload", instance_id="instance"), {},
    )
    before = _finish_snapshot(db)
    injected = {
        "reached": False,
        "recovery_comparator_entered": False,
        "comparator_wrapper_called": False,
    }
    recovery_comparator_active = {"value": False}

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

        original_comparator = projection_module._compare_actual_rows
        original_execute = Session.execute

        def track_recovery_comparator(session, *args, **kwargs):
            injected["comparator_wrapper_called"] = True
            recovery = kwargs.get("recovery_attempt_states") is not None
            previous = recovery_comparator_active["value"]
            recovery_comparator_active["value"] = recovery
            if recovery:
                injected["recovery_comparator_entered"] = True
            try:
                return original_comparator(session, *args, **kwargs)
            finally:
                recovery_comparator_active["value"] = previous

        def fail_receipt_query(session, statement, *args, **kwargs):
            if (not injected["reached"] and recovery_comparator_active["value"]
                    and hasattr(statement, "get_final_froms")):
                receipt_select = any(
                    getattr(table, "name", None) == "program_call_receipts"
                    for table in statement.get_final_froms()
                )
                locked = getattr(statement, "_for_update_arg", None) is not None
                if receipt_select and locked:
                    injected["reached"] = True
                    raise RuntimeError("synthetic-private-receipt-query-marker")
            return original_execute(session, statement, *args, **kwargs)

        monkeypatch.setattr(projection_module, "_compare_actual_rows", track_recovery_comparator)
        monkeypatch.setattr(Session, "execute", fail_receipt_query)
    else:
        import sqlalchemy

        original_comparator = projection_module._compare_actual_rows
        original_table = sqlalchemy.Table

        def track_recovery_comparator(session, *args, **kwargs):
            injected["comparator_wrapper_called"] = True
            recovery = kwargs.get("recovery_attempt_states") is not None
            previous = recovery_comparator_active["value"]
            recovery_comparator_active["value"] = recovery
            if recovery:
                injected["recovery_comparator_entered"] = True
            try:
                return original_comparator(session, *args, **kwargs)
            finally:
                recovery_comparator_active["value"] = previous

        def fail_binding_reflection(name, *args, **kwargs):
            if (not injected["reached"] and recovery_comparator_active["value"]
                    and name == projection_module._BINDING_TABLE and "autoload_with" in kwargs):
                injected["reached"] = True
                raise RuntimeError("synthetic-private-binding-reflection-marker")
            return original_table(name, *args, **kwargs)

        monkeypatch.setattr(projection_module, "_compare_actual_rows", track_recovery_comparator)
        monkeypatch.setattr(sqlalchemy, "Table", fail_binding_reflection)

    with pytest.raises(PlatformError) as error:
        service.reserve(principal=ExecutionPrincipal("workload", "fixture"),
                        instance_id="instance", operation_id="pull-global-failure")
    assert error.value.code == "owner_unavailable"
    assert injected["reached"] is True, injected
    if failure_site in {"receipt_query", "binding_reflection"}:
        assert injected["recovery_comparator_entered"] is True
    assert "synthetic-private" not in "".join(traceback.format_exception(error.value))
    assert error.value.__suppress_context__ is True
    assert _finish_snapshot(db) == before
    key = cursor_key("fixture", False)
    with db.sessions() as session:
        assert session.get(MetadataRow, key) is None
        assert session.get(Job, "job").status == "running"
