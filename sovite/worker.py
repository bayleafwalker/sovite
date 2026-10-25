from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import socket
import time
import uuid

from sqlalchemy import select

from .adapters import AdapterRunner, RuntimeFailure
from .db import Job, WorkerHeartbeat
from .lease_service import CLAIM_LOCK, LeaseService, replay_safe
from .service import Platform

log = logging.getLogger("sovite.worker")

# CLAIM_LOCK and replay_safe are re-exported from sovite.lease_service above for
# existing importers such as `from sovite.worker import replay_safe`.


def worker_scope(platform: Platform, hostname: str | None = None) -> str:
    pool, host = platform.settings.compute_id, hostname or socket.gethostname()
    pool_part = pool[:12] + "-" + hashlib.sha256(pool.encode()).hexdigest()[:12]
    host_part = host[:20] + "-" + hashlib.sha256(host.encode()).hexdigest()[:12]
    return f"{pool_part}:{host_part}:"


def healthy_worker_count(platform: Platform, hostname: str | None = None) -> int:
    scope = worker_scope(platform, hostname)
    with platform.db.sessions() as session:
        return len(session.scalars(select(WorkerHeartbeat).where(WorkerHeartbeat.updated_at > time.time() - 120, WorkerHeartbeat.id.startswith(scope, autoescape=True))).all())


class Worker:
    def __init__(self, platform: Platform, runner: AdapterRunner | None = None,
                 repository_run_executor=None):
        self.platform, self.runner = platform, runner or AdapterRunner()
        self.repository_run_executor = repository_run_executor
        self.id = f"{worker_scope(platform)}{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.stop = asyncio.Event()
        self.execution_fences = {}
        self.leases = LeaseService(platform, self.id, self.execution_fences,
                                   repository_run_profile=repository_run_executor is not None)

    def heartbeat(self, job_id: str | None = None):
        return self.leases.heartbeat(job_id)

    def recover_expired(self):
        return self.leases.recover_expired()

    def claim(self) -> Job | None:
        return self.leases.claim()

    def renew(self, job_id: str, fence: str | None = None) -> bool:
        return self.leases.renew(job_id, fence=fence)

    def hand_back(self, job_id: str, fence: str | None = None):
        return self.leases.hand_back(job_id, fence=fence)

    def finish(self, job_id: str, artifact: tuple[str, str] | None = None, error: RuntimeFailure | None = None, cancelled: bool = False, fence: str | None = None):
        return self.leases.finish(job_id, artifact=artifact, error=error, cancelled=cancelled, fence=fence)

    async def execute(self, job: Job):
        # The running task owns this immutable start fence for its entire life.
        # A later same-instance reclaim must not be borrowed from the mutable map.
        captured_fence = getattr(job, "execution_fence", None)
        self.heartbeat(job.id)
        def check_authorization():
            if not self.renew(job.id, fence=captured_fence):
                raise RuntimeFailure("authorization_revoked")
            if repository_run is not None:
                self.repository_run_executor.check_authorization(job)
            return True
        repository_run = job.resolved.get("repository_run")
        if repository_run is not None:
            if self.repository_run_executor is None:
                self.finish(job.id, error=RuntimeFailure("repository_profile_unavailable"), fence=captured_fence)
                return
            operation = asyncio.create_task(self.repository_run_executor.run(
                job, fence=captured_fence, authorization_check=check_authorization))
        else:
            self.runner.authorization_check = check_authorization
            operation = asyncio.create_task(self.runner.run(job.resolved, job.task, job.id))
        timeout = min(int(job.resolved["grant"].get("timeout_seconds", self.platform.settings.job_timeout_seconds)), self.platform.settings.job_timeout_seconds)
        fixed_deadline = getattr(job, "execution_deadline", None)
        if isinstance(fixed_deadline, (int, float)) and not isinstance(fixed_deadline, bool):
            timeout = min(timeout, max(0, fixed_deadline - time.time()))
        deadline = time.monotonic() + timeout
        stopping = asyncio.ensure_future(self.stop.wait())
        try:
            while not operation.done():
                await asyncio.wait({operation, stopping}, timeout=min(max(0.2, self.platform.settings.lease_seconds / 3), max(0.01, deadline - time.monotonic())), return_when=asyncio.FIRST_COMPLETED)
                if operation.done():
                    break
                if self.stop.is_set():
                    # Shutdown (rollout, drain, eviction) is not a cancellation: hand the job back.
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)
                    self.hand_back(job.id, fence=captured_fence)
                    return
                self.heartbeat(job.id)
                try:
                    check_authorization()
                except RuntimeFailure as exc:
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)
                    if repository_run is not None and self.repository_run_executor.effect_started(job.id):
                        self.finish(job.id, error=RuntimeFailure("external_execution_uncertain"), fence=captured_fence)
                    elif repository_run is not None and exc.code != "authorization_revoked":
                        self.finish(job.id, error=exc, fence=captured_fence)
                    else:
                        self.finish(job.id, cancelled=True, fence=captured_fence)
                    return
                if time.monotonic() >= deadline:
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)
                    if repository_run is not None and self.repository_run_executor.effect_started(job.id):
                        self.finish(job.id, error=RuntimeFailure("external_execution_uncertain"), fence=captured_fence)
                    else:
                        self.finish(job.id, error=RuntimeFailure("execution_timeout", False), fence=captured_fence)
                    return
            result = await operation
            # Artifact commit can be retried safely, while re-executing external effects
            # after an uncertain failure is a separate decision.
            artifact = await asyncio.to_thread(self.platform.store.put, job.id, result)
            self.finish(job.id, artifact=artifact, fence=captured_fence)
        except RuntimeFailure as exc:
            self.finish(job.id, error=exc, fence=captured_fence)
        except asyncio.CancelledError:
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            # Keep the lease for recovery; shutdown is not a user cancellation.
            raise
        except Exception:
            # Exception messages may contain URLs, response bodies, or sensitive data.
            log.error("Task %s failed with an internal adapter/storage error", job.id)
            self.finish(job.id, error=RuntimeFailure("internal_execution_error"), fence=captured_fence)
        finally:
            stopping.cancel()
            self.runner.authorization_check = None
            self.heartbeat()

    async def run_once(self) -> bool:
        self.heartbeat()
        job = self.claim()
        if not job:
            return False
        await self.execute(job)
        return True

    async def run(self):
        self.platform.initialize()
        while not self.stop.is_set():
            try:
                worked = await self.run_once()
            except Exception:
                log.error("Worker control-store operation failed; no job dispatched")
                worked = False
            if not worked:
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=self.platform.settings.poll_seconds)
                except asyncio.TimeoutError:
                    pass


class RemoteWorker:
    """Opt-in runtime composition for the authenticated execution API.

    This class is deliberately not selected by ``Worker`` or application
    startup. Callers must construct it with a configured ExecutionClient and
    an instance identifier. Every operation closes over the fence returned by
    pull; uncertain mutations are reconciled or abandoned without replay.
    """

    def __init__(self, client, instance_id: str, runner: AdapterRunner | None = None,
                 *, journal, poll_seconds: float = 2.0, lease_seconds: float = 90.0):
        from .execution_client import _identifier

        self.client = client
        self.instance_id = _identifier(instance_id, "instance_id")
        if journal is None or not all(hasattr(journal, name) for name in ("load", "write", "clear")):
            raise ValueError("An exclusive execution recovery journal is required")
        self.journal = journal
        self.runner = runner or AdapterRunner()
        self.poll_seconds = max(0.1, float(poll_seconds))
        self.lease_seconds = max(1.0, float(lease_seconds))
        self.stop = asyncio.Event()
        self._startup_reconciled = False

    def _journal_write(self, phase, operation_id, operation_kind, *, job=None,
                       expected=(), lease_until=None, deadline=None):
        job = job or {}
        self.journal.write({
            "schema_version": 1,
            "instance_id": self.instance_id,
            "phase": phase,
            "operation_id": operation_id,
            "operation_kind": operation_kind,
            "job_id": job.get("job_id"),
            "generation": job.get("generation"),
            "fence": job.get("fence"),
            "expected_result_states": sorted(expected),
            "lease_until": lease_until,
            "deadline": deadline,
        })

    def _startup_reconcile(self):
        from .execution_client import ExecutionLookupNotFound

        record = None
        try:
            record = self.journal.load()
            if record is None:
                self._startup_reconciled = True
                return True
            if record["instance_id"] != self.instance_id:
                raise RuntimeError("Execution journal belongs to another instance")
            self._diagnostic_ids(self.instance_id, record["operation_id"],
                                 record["operation_kind"], "startup_reconcile")
            result = self.client.lookup(self.instance_id, record["operation_id"])
            if (result.get("state") != "found"
                    or result.get("operation_id") != record["operation_id"]
                    or result.get("operation_kind") != record["operation_kind"]):
                raise RuntimeError("Execution journal lookup did not match its exact key")
            if (record["job_id"] is not None
                    and (result.get("job_id") != record["job_id"]
                         or result.get("fence") != record["fence"])):
                raise RuntimeError("Execution journal lookup did not match its captured fence")
            if (record["expected_result_states"]
                    and result.get("result_state") not in set(record["expected_result_states"])):
                raise RuntimeError("Execution journal result is outside its recorded expectation")
            if record["operation_kind"] == "pull":
                if result.get("result_state") == "idle":
                    self.journal.clear()
                    self._startup_reconciled = True
                    return True
                if result.get("result_state") == "reserved":
                    record.update(phase="manual", job_id=result.get("job_id"),
                                  fence=result.get("fence"), expected_result_states=["reserved"])
            elif (record["operation_kind"] in {"handback", "complete"}
                  and record["expected_result_states"]):
                if (result.get("job_id") != record["job_id"]
                        or result.get("fence") != record["fence"]
                        or result.get("result_state") not in set(record["expected_result_states"])):
                    raise RuntimeError("Execution journal mutation has not reached its expected state")
                # A journaled handback/complete response is the terminal result
                # for this worker attempt, even when job scheduling retries it.
                self.journal.clear()
                self._startup_reconciled = True
                return True
            else:
                if record["job_id"] is not None and record["generation"] is not None and record["fence"] is not None:
                    context = self._context(self.instance_id, record, record["operation_id"])
                    observed = self.client.status(context)
                    self._diagnostic(context, "status", observed.get("state", "unavailable"))
                # Active, reserved, or otherwise unresolved work is never
                # resumed across processes. Keep an actionable record.
            record["phase"] = "manual"
            self.journal.write(record)
            self.stop.set()
            self._startup_reconciled = True
            return False
        except ExecutionLookupNotFound:
            if record is not None:
                if (record["job_id"] is not None and record["generation"] is not None
                        and record["fence"] is not None):
                    try:
                        context = self._context(self.instance_id, record, record["operation_id"])
                        observed = self.client.status(context)
                        self._diagnostic(context, "status", observed.get("state", "unavailable"))
                    except Exception:
                        self._diagnostic_ids(self.instance_id, record["operation_id"],
                                             record["operation_kind"], "status_unavailable")
                record["phase"] = "manual"
                self.journal.write(record)
            self.stop.set()
            self._diagnostic_ids(self.instance_id, record["operation_id"],
                                 record["operation_kind"], "manual_reconciliation_required")
            self._startup_reconciled = True
            return False
        except Exception:
            # Journal and owner disagreement is not repaired by a fresh pull.
            log.error("Remote execution journal reconciliation failed; manual reconciliation is required")
            self.stop.set()
            self._startup_reconciled = True
            return False

    @staticmethod
    def _context(instance_id: str, job: dict, operation_id: str):
        from .execution_client import ExecutionContext

        return ExecutionContext(instance_id, operation_id, job["job_id"],
                                job["generation"], job["fence"])

    @staticmethod
    def _operation_id() -> str:
        return uuid.uuid4().hex

    @staticmethod
    def _diagnostic_ids(instance_id: str, operation_id: str,
                        operation_kind: str, state: str) -> None:
        # Identifiers are validated at construction and by ExecutionClient;
        # never include transport or owner exception text here.
        log.error(
            "Remote execution instance_id=%s operation_id=%s operation=%s state=%s; "
            "lookup/reconciliation is required before any retry",
            instance_id, operation_id, operation_kind, state,
        )

    @classmethod
    def _diagnostic(cls, context, operation_kind: str, state: str) -> None:
        cls._diagnostic_ids(context.instance_id, context.operation_id,
                            operation_kind, state)

    def _reconcile_unknown(self, context, expected_kind: str, *, result_states=None):
        from .execution_client import ExecutionLookupNotFound

        try:
            result = self.client.lookup(context.instance_id, context.operation_id)
            if (result.get("state") != "found"
                    or result.get("operation_id") != context.operation_id
                    or result.get("operation_kind") != expected_kind
                    or result.get("job_id") != context.job_id
                    or result.get("fence") != context.fence
                    or (result_states is not None and result.get("result_state") not in result_states)):
                raise RuntimeError("Execution operation lookup did not resolve the exact key")
            return result
        except ExecutionLookupNotFound:
            self._diagnostic(context, expected_kind, "not_found")
            self.stop.set()
            raise
        except Exception:
            # Keep the original operation key available to an operator. Never
            # invent a new key and repeat a mutation whose commit is unknown.
            self._diagnostic(context, expected_kind, "unavailable")
            self.stop.set()
            raise

    async def run_once(self) -> bool:
        from .execution_client import ExecutionClientError, ExecutionOutcomeUnknown

        if not self._startup_reconciled and not self._startup_reconcile():
            return False

        def complete_or_fail(context, *, outcome, result=None, error_code=None):
            expected_states = {
                "completed": {"completed", "cancelled", "blocked"},
                "failed": {"failed", "cancelled", "blocked"},
                "retryable_failure": {"retry_wait", "failed", "cancelled", "blocked"},
                "cancelled": {"cancelled", "blocked"},
            }[outcome]
            self._journal_write("pending_complete", context.operation_id, "complete",
                                job={"job_id": context.job_id, "generation": context.generation,
                                     "fence": context.fence}, expected=expected_states,
                                lease_until=lease_until, deadline=deadline)
            try:
                response = self.client.complete(context, outcome=outcome, result=result,
                                                error_code=error_code)
                if (response.get("state") not in expected_states
                        or response.get("job_id") != context.job_id
                        or response.get("generation") != context.generation
                        or response.get("fence") != context.fence):
                    self.stop.set()
                    raise RuntimeError("Execution completion did not confirm the requested transition")
                self.journal.clear()
                return response
            except ExecutionOutcomeUnknown:
                self._diagnostic(context, "complete", "unknown")
                self._reconcile_unknown(context, "complete", result_states=expected_states)
                self._diagnostic(context, "complete", "confirmed")
                self.journal.clear()
                return {"state": "confirmed"}
            except ExecutionClientError:
                self._diagnostic(context, "complete", "refused")
                self.stop.set()
                raise

        pull_id = self._operation_id()
        self._journal_write("pending_pull", pull_id, "pull", expected={"idle", "reserved"})
        try:
            pulled = self.client.pull(self.instance_id, pull_id)
        except ExecutionOutcomeUnknown:
            self._diagnostic_ids(self.instance_id, pull_id, "pull", "unknown")
            try:
                observed = self.client.lookup(self.instance_id, pull_id)
                if (observed.get("state") != "found" or observed.get("operation_id") != pull_id
                        or observed.get("operation_kind") != "pull"
                        or observed.get("result_state") not in {"idle", "reserved"}):
                    raise RuntimeError("Execution pull lookup did not resolve the exact operation")
                if observed.get("result_state") == "idle":
                    self._diagnostic_ids(self.instance_id, pull_id, "pull", "idle_confirmed")
                    self.journal.clear()
                    return False
                # Lookup intentionally omits task/resolved; never dispatch a
                # reservation recovered without its original pull response.
                self._diagnostic_ids(self.instance_id, pull_id, "pull", "reservation_not_recovered")
                self.stop.set()
                return False
            except Exception:
                self._diagnostic_ids(self.instance_id, pull_id, "pull", "unresolved")
                self.stop.set()
                raise
        except ExecutionClientError:
            self._diagnostic_ids(self.instance_id, pull_id, "pull", "refused")
            self.stop.set()
            raise
        if pulled["state"] == "idle":
            self.journal.clear()
            return False
        if pulled["state"] != "reserved" or not isinstance(pulled.get("job"), dict):
            self._diagnostic_ids(self.instance_id, pull_id, "pull", "unexpected")
            self.stop.set()
            return False
        job = pulled["job"]
        self._journal_write("reserved", pull_id, "pull", job=job,
                            expected={"reserved"}, lease_until=job.get("lease_until"))
        start_context = self._context(self.instance_id, job, self._operation_id())
        self._journal_write("pending_start", start_context.operation_id, "start", job=job,
                            expected={"started", "already_started"},
                            lease_until=job.get("lease_until"))
        try:
            started = self.client.start(start_context)
        except ExecutionOutcomeUnknown:
            self.stop.set()
            self._diagnostic(start_context, "start", "unknown")
            self._reconcile_unknown(start_context, "start",
                                    result_states={"started", "already_started"})
            raise
        except ExecutionClientError:
            self.stop.set()
            self._diagnostic(start_context, "start", "refused")
            raise
        if (started.get("state") != "started"
                or started.get("job_id") != job.get("job_id")
                or started.get("generation") != job.get("generation")
                or started.get("fence") != job.get("fence")):
            self.stop.set()
            self._diagnostic(start_context, "start", "unexpected")
            return False
        lease_until = float(started["lease_until"])
        deadline = float(started["deadline"])
        self._journal_write("started", start_context.operation_id, "start", job=job,
                            expected={"started"}, lease_until=lease_until, deadline=deadline)
        context = self._context(self.instance_id, job, self._operation_id())
        # Keep the one-time execution capability only in this process and pass
        # it to the exact adapter invocation. It never enters the journal,
        # diagnostics, subprocess environment, or a later start response.
        execution = started.get("execution")

        def renew_exact_fence():
            nonlocal lease_until
            renew_context = self._context(self.instance_id, job, self._operation_id())
            self._journal_write("pending_renew", renew_context.operation_id, "renew", job=job,
                                expected={"renewed"}, lease_until=lease_until, deadline=deadline)
            try:
                response = self.client.renew(renew_context)
                if response["state"] != "renewed":
                    self.stop.set()
                    return False
                lease_until = float(response["lease_until"])
                self._journal_write("started", renew_context.operation_id, "renew", job=job,
                                    expected={"renewed"}, lease_until=lease_until, deadline=deadline)
                return True
            except ExecutionOutcomeUnknown:
                self._diagnostic(renew_context, "renew", "unknown")
                self._reconcile_unknown(renew_context, "renew", result_states={"renewed"})
                try:
                    observed = self.client.status(context)
                except ExecutionClientError:
                    self._diagnostic(context, "status", "unavailable")
                    self.stop.set()
                    return False
                if observed["state"] != "current" or observed["lease_until"] is None:
                    state = observed["state"] if observed["lease_until"] is not None else "unavailable"
                    self._diagnostic(context, "status", state)
                    self.stop.set()
                    return False
                lease_until = float(observed["lease_until"])
                self._journal_write("started", renew_context.operation_id, "renew", job=job,
                                    expected={"renewed"}, lease_until=lease_until, deadline=deadline)
                return True
            except ExecutionClientError:
                self._diagnostic(renew_context, "renew", "refused")
                self.stop.set()
                return False

        self.runner.authorization_check = renew_exact_fence
        public_recipe = job["recipe"]
        runtime_recipe = {
            "generation": job["generation"],
            "harness": {
                "kind": "agent-http",
                "endpoint": public_recipe["endpoint"],
                "cancel_endpoint": public_recipe.get("cancel_endpoint"),
                "api_key_env": public_recipe.get("credential_env"),
                "idempotent": public_recipe["idempotent"],
            },
            "agent": {"id": public_recipe["agent_id"]},
            "model": {},
            "grant": {"timeout_seconds": public_recipe["timeout_seconds"]},
        }
        operation = asyncio.create_task(self.runner.run(
            runtime_recipe, job["task"], job["job_id"],
            execution=execution, deadline=deadline, fence=job["fence"],
        ))
        deadline = float(started["deadline"])
        try:
            while not operation.done():
                now = time.time()
                remaining = min(lease_until - now, deadline - now)
                if remaining <= 0:
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)
                    complete = self._context(self.instance_id, job, self._operation_id())
                    complete_or_fail(complete, outcome="failed", error_code="execution_timeout")
                    return True
                await asyncio.wait({operation}, timeout=min(self.poll_seconds, max(0.05, remaining / 3)))
                if operation.done():
                    break
                if self.stop.is_set():
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)
                    handback = self._context(self.instance_id, job, self._operation_id())
                    self._journal_write("pending_handback", handback.operation_id, "handback",
                                        job=job, expected={"handed_back"},
                                        lease_until=lease_until, deadline=deadline)
                    try:
                        response = self.client.handback(handback)
                        if (response.get("state") != "handed_back"
                                or response.get("job_id") != handback.job_id
                                or response.get("generation") != handback.generation
                                or response.get("fence") != handback.fence):
                            self._diagnostic(handback, "handback", "unexpected")
                            self.stop.set()
                            raise RuntimeError("Execution handback did not confirm the captured fence")
                        self.journal.clear()
                    except ExecutionOutcomeUnknown:
                        self._diagnostic(handback, "handback", "unknown")
                        self._reconcile_unknown(handback, "handback",
                                                result_states={"handed_back"})
                        self.journal.clear()
                    except ExecutionClientError:
                        self._diagnostic(handback, "handback", "refused")
                        self.stop.set()
                        raise
                    return True
                try:
                    state = self.client.status(context)
                except Exception:
                    self._diagnostic(context, "status", "unavailable")
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)
                    self.stop.set()
                    raise
                if state["state"] != "current" or state["lease_until"] is None:
                    status_state = state["state"] if state["lease_until"] is not None else "unavailable"
                    self._diagnostic(context, "status", status_state)
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)
                    self.stop.set()
                    return True
                lease_until = float(state["lease_until"])
                if time.time() >= deadline:
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)
                    complete = self._context(self.instance_id, job, self._operation_id())
                    complete_or_fail(complete, outcome="failed", error_code="execution_timeout")
                    return True
                if not renew_exact_fence():
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)
                    return True
            result = await operation
            complete = self._context(self.instance_id, job, self._operation_id())
            complete_or_fail(complete, outcome="completed", result=result)
        except RuntimeFailure as exc:
            if self.stop.is_set():
                # A failed renewal/status or control shutdown already closed
                # this process to new work. Keep its active journal for owner
                # reconciliation instead of issuing another mutation.
                return True
            complete = self._context(self.instance_id, job, self._operation_id())
            outcome = "retryable_failure" if exc.retryable else "failed"
            complete_or_fail(complete, outcome=outcome, error_code=exc.code)
        finally:
            self.runner.authorization_check = None
        return True

    async def run(self):
        while not self.stop.is_set():
            try:
                worked = await self.run_once()
            except Exception:
                # A prior pull may have reserved work or left a started fence.
                self.stop.set()
                log.error("Remote execution operation failed; no further work dispatched")
                worked = False
            if not worked:
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=self.poll_seconds)
                except asyncio.TimeoutError:
                    pass
