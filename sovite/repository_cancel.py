"""Owner-transaction cancellation for repository runs with Work projections."""
from __future__ import annotations

import hashlib
import math
import time

from sqlalchemy import Text, cast, select

from .collection_transaction import owner_transaction
from .db import Base, Job, MetadataRow, WorkerExecutionLease
from .service import PlatformError, TERMINAL


class _ProjectionStateUnavailable(Exception):
    pass


def cancel_repository_job(platform, job_id: str, *, actor: str,
                          require_owner: bool) -> Job:
    """Request cancellation and inhibit every canonical current Work call.

    Projection failures roll back the attempted Work update, then a second
    owner transaction persists only the Job-level cancellation request and
    audit. The caller receives an explicit error that inhibition did not
    succeed. No verifier, custody, or provider service is called.
    """
    try:
        return _cancel_in_owner_transaction(
            platform, job_id, actor=actor, require_owner=require_owner,
        )
    except _ProjectionStateUnavailable:
        try:
            job, changed = _persist_cancel_request_only(
                platform, job_id, actor=actor, require_owner=require_owner,
            )
        except PlatformError:
            raise
        except Exception:
            raise PlatformError(
                "cancel_owner_state_unavailable",
                "Cancellation owner state is unavailable; retry the request",
                503,
            ) from None
        if not changed and job.status in TERMINAL:
            return job
        raise PlatformError(
            "cancel_projection_unavailable",
            "Cancellation was requested, but Work projection inhibition was unavailable",
            503,
        ) from None
    except PlatformError:
        raise
    except Exception:
        # Audit-head or owner database failure means the request itself did
        # not safely commit. Do not turn it into a claimed cancellation.
        raise PlatformError(
            "cancel_owner_state_unavailable",
            "Cancellation owner state is unavailable; retry the request",
            503,
        ) from None


def _cancel_fields(platform, session, job, actor: str) -> bool:
    if job.status in TERMINAL:
        return False
    now = time.time()
    was_requested = job.cancel_requested is True
    job.cancel_requested = True
    changed = not was_requested
    if job.status != "running":
        job.status = "cancelled"
        job.finished_at = now
        changed = True
    if changed:
        job.updated_at = now
    if not was_requested:
        platform.db.audit(session, actor, "task.cancel_requested", job.id)
    return changed


def _cancel_in_owner_transaction(platform, job_id: str, *, actor: str,
                                 require_owner: bool) -> Job:
    from .program_ledger import _mark_work_projection_unknown_in_transaction
    from .work_projection import (
        WorkProjectionError,
        _compare_actual_work_projection,
        _raw_job_object,
        lock_work_projection_set,
        work_projections_from_job,
    )

    with owner_transaction(platform.db) as session:
        schema = session.get(MetadataRow, "schema_version")
        value = schema.value if schema is not None else None
        version = value.get("version") if type(value) is dict else None
        if type(version) is not int:
            raise _ProjectionStateUnavailable()

        job_table = Base.metadata.tables["jobs"]
        raw = session.execute(select(cast(job_table.c.resolved, Text)).where(
            job_table.c.id == job_id)).scalar_one_or_none()
        if type(raw) is not str:
            raise PlatformError("not_found", "Task not found", 404)
        try:
            root = _raw_job_object(raw)
            run = root.get("repository_run")
            if run is None:
                if "program_ledger_projections_v1" in root:
                    raise WorkProjectionError("orphan Work projection marker")
                projections_all = ()
            elif (type(run) is dict
                  and run.get("schema_version") == "sovite-repository-run/v1"):
                projections_all = work_projections_from_job(raw, allow_absent=True)
            else:
                raise WorkProjectionError("repository-run JSON unavailable")
        except (WorkProjectionError, TypeError, ValueError):
            raise _ProjectionStateUnavailable() from None
        if any(item.work_job_id != job_id for item in projections_all):
            raise _ProjectionStateUnavailable()

        attempts = run.get("attempts") if type(run) is dict else []
        if type(attempts) is not list:
            raise _ProjectionStateUnavailable()
        last_attempt = attempts[-1] if attempts else None
        attempt_id = last_attempt.get("id") if type(last_attempt) is dict else None
        attempt_state = last_attempt.get("state") if type(last_attempt) is dict else None
        job_probe = session.execute(select(
            job_table.c.status, job_table.c.caller, job_table.c.attempts,
        ).where(job_table.c.id == job_id)).mappings().one_or_none()
        if job_probe is None:
            raise PlatformError("not_found", "Task not found", 404)
        if require_owner and job_probe["caller"] != actor:
            raise PlatformError("not_found", "Task not found", 404)

        active_attempt = attempt_state in {"preparing", "awaiting_activation", "running"}
        if active_attempt and (
            type(job_probe["attempts"]) is not int
            or attempt_id != f"{job_id}-{job_probe['attempts']}"
        ):
            raise _ProjectionStateUnavailable()
        current = tuple(item for item in projections_all
                        if active_attempt and item.attempt_id == attempt_id)
        current_pairs = {(item.scope_id, item.call_id) for item in current}

        binding_rows = []
        intent_rows = []
        if version >= 10:
            binding_t = Base.metadata.tables.get("program_work_projection_bindings")
            intent_t = Base.metadata.tables.get("program_call_intents")
            if binding_t is None or intent_t is None:
                raise _ProjectionStateUnavailable()
            if run is None:
                binding_rows = session.execute(select(
                    binding_t.c.scope_id, binding_t.c.call_id,
                ).where(binding_t.c.work_job_id == job_id).order_by(
                    binding_t.c.scope_id, binding_t.c.call_id)).mappings().all()
                intent_rows = session.execute(select(
                    intent_t.c.scope_id, intent_t.c.call_id,
                ).where(intent_t.c.work_job_id == job_id).order_by(
                    intent_t.c.scope_id, intent_t.c.call_id)).mappings().all()
            elif active_attempt or job_probe["status"] == "running":
                binding_query = select(binding_t.c.scope_id, binding_t.c.call_id).where(
                    binding_t.c.work_job_id == job_id)
                intent_query = select(intent_t.c.scope_id, intent_t.c.call_id).where(
                    intent_t.c.work_job_id == job_id)
                if attempt_id is not None:
                    binding_query = binding_query.where(binding_t.c.attempt_id == attempt_id)
                    intent_query = intent_query.where(intent_t.c.work_attempt_id == attempt_id)
                binding_rows = session.execute(binding_query.order_by(
                    binding_t.c.scope_id, binding_t.c.call_id)).mappings().all()
                intent_rows = session.execute(intent_query.order_by(
                    intent_t.c.scope_id, intent_t.c.call_id)).mappings().all()
            binding_pairs = {(row["scope_id"], row["call_id"]) for row in binding_rows}
            intent_pairs = {(row["scope_id"], row["call_id"]) for row in intent_rows}
            if (len(binding_pairs) != len(binding_rows)
                    or len(intent_pairs) != len(intent_rows)
                    or binding_pairs != current_pairs or intent_pairs != current_pairs):
                raise _ProjectionStateUnavailable()
        elif current:
            raise _ProjectionStateUnavailable()

        if job_probe["status"] == "running" and not active_attempt and (
            any(item.attempt_id == attempt_id for item in projections_all)
            or current_pairs or binding_rows or intent_rows
        ):
            raise _ProjectionStateUnavailable()
        if job_probe["status"] != "running" and active_attempt:
            # A queued or retrying Job cannot own a live Work attempt.
            raise _ProjectionStateUnavailable()

        if current:
            try:
                lock_work_projection_set(session, current)
            except Exception:
                raise _ProjectionStateUnavailable() from None

        job = session.execute(select(Job).where(Job.id == job_id).with_for_update()).scalar_one_or_none()
        if job is None:
            raise PlatformError("not_found", "Task not found", 404)
        if require_owner and job.caller != actor:
            raise PlatformError("not_found", "Task not found", 404)
        lease = session.get(WorkerExecutionLease, job_id, with_for_update=True)
        locked_raw = session.execute(select(cast(job_table.c.resolved, Text)).where(
            job_table.c.id == job_id).with_for_update()).scalar_one_or_none()
        if type(locked_raw) is not str:
            raise _ProjectionStateUnavailable()
        try:
            locked_root = _raw_job_object(locked_raw)
            locked_run = locked_root.get("repository_run")
            locked_all = work_projections_from_job(locked_raw, allow_absent=True)
        except (WorkProjectionError, TypeError, ValueError):
            raise _ProjectionStateUnavailable() from None
        if ((locked_run is None and "program_ledger_projections_v1" in locked_root)
                or locked_run is not None and (type(locked_run) is not dict
                    or locked_run.get("schema_version") != "sovite-repository-run/v1")
                or locked_all != projections_all):
            raise _ProjectionStateUnavailable()

        locked_attempts = locked_run.get("attempts", []) if type(locked_run) is dict else []
        if (locked_attempts != attempts or job.status != job_probe["status"]
                or job.attempts != job_probe["attempts"]):
            raise _ProjectionStateUnavailable()
        if active_attempt and job.status != "running":
            raise _ProjectionStateUnavailable()

        if job.status == "running" and active_attempt:
            if (type(locked_attempts) is not list or not locked_attempts
                    or type(locked_attempts[-1]) is not dict
                    or locked_attempts[-1].get("id") != attempt_id
                    or locked_attempts[-1].get("state") != attempt_state
                    or type(job.attempts) is not int
                    or attempt_id != f"{job.id}-{job.attempts}"):
                raise _ProjectionStateUnavailable()
            if (lease is None or lease.phase != "started"
                    or job.lease_until != lease.lease_until
                    or lease.generation != job.generation
                    or type(lease.fence) is not str or not lease.fence
                    or type(lease.lease_until) not in {int, float}
                    or isinstance(lease.lease_until, bool)
                    or type(lease.execution_deadline) not in {int, float}
                    or isinstance(lease.execution_deadline, bool)
                    or not math.isfinite(lease.lease_until)
                    or not math.isfinite(lease.execution_deadline)
                    or min(lease.lease_until, lease.execution_deadline) <= time.time()):
                raise _ProjectionStateUnavailable()
            if locked_attempts[-1].get("fence_sha256") != hashlib.sha256(
                    lease.fence.encode("utf-8")).hexdigest():
                raise _ProjectionStateUnavailable()
            if current:
                try:
                    for projection in current:
                        _compare_actual_work_projection(
                            session, scope_id=projection.scope_id,
                            call_id=projection.call_id, allow_cancel_requested=True,
                        )
                except Exception:
                    raise _ProjectionStateUnavailable() from None
                updated = {}
                for projection in current:
                    try:
                        payload = _mark_work_projection_unknown_in_transaction(
                            platform.db, session, projection,
                            job_id=job_id, attempt_id=attempt_id,
                        )
                    except Exception:
                        raise _ProjectionStateUnavailable() from None
                    if payload is not None:
                        updated[(projection.scope_id, projection.call_id)] = payload
                if updated:
                    entries = locked_run.get("program_ledger_projections_v1")
                    if type(entries) is not list:
                        raise _ProjectionStateUnavailable()
                    locked_run["program_ledger_projections_v1"] = [
                        updated.get((entry.get("scope_id"), entry.get("call_id")), entry)
                        if type(entry) is dict else entry for entry in entries
                    ]
                    job.resolved = locked_root

        _cancel_fields(platform, session, job, actor)
        return job


def _persist_cancel_request_only(platform, job_id: str, *, actor: str,
                                 require_owner: bool) -> tuple[Job, bool]:
    with owner_transaction(platform.db) as session:
        job = session.execute(select(Job).where(Job.id == job_id).with_for_update()).scalar_one_or_none()
        if job is None:
            raise PlatformError("not_found", "Task not found", 404)
        if require_owner and job.caller != actor:
            raise PlatformError("not_found", "Task not found", 404)
        if job.status in TERMINAL:
            return job, False
        changed = job.cancel_requested is not True
        if changed:
            job.cancel_requested = True
            job.updated_at = time.time()
            platform.db.audit(session, actor, "task.cancel_requested", job.id)
        return job, changed
