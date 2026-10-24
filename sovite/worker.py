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
    def __init__(self, platform: Platform, runner: AdapterRunner | None = None):
        self.platform, self.runner = platform, runner or AdapterRunner()
        self.id = f"{worker_scope(platform)}{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.stop = asyncio.Event()
        self.execution_fences = {}
        self.leases = LeaseService(platform, self.id, self.execution_fences)

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
                if not self.renew(job.id, fence=captured_fence):
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)
                    self.finish(job.id, cancelled=True, fence=captured_fence)
                    return
                if time.monotonic() >= deadline:
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)
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
                 *, poll_seconds: float = 2.0, lease_seconds: float = 90.0):
        from .execution_client import _identifier

        self.client = client
        self.instance_id = _identifier(instance_id, "instance_id")
        self.runner = runner or AdapterRunner()
        self.poll_seconds = max(0.1, float(poll_seconds))
        self.lease_seconds = max(1.0, float(lease_seconds))
        self.stop = asyncio.Event()

    @staticmethod
    def _context(instance_id: str, job: dict, operation_id: str):
        from .execution_client import ExecutionContext

        return ExecutionContext(instance_id, operation_id, job["job_id"],
                                job["generation"], job["fence"])

    @staticmethod
    def _operation_id() -> str:
        return uuid.uuid4().hex

    def _reconcile_unknown(self, context, expected_kind: str):
        try:
            result = self.client.lookup(context.instance_id, context.operation_id)
            if (result.get("state") != "found"
                    or result.get("operation_id") != context.operation_id
                    or result.get("operation_kind") != expected_kind):
                raise RuntimeError("Execution operation lookup did not resolve the exact key")
            return result
        except Exception:
            # Keep the original operation key available to an operator. Never
            # invent a new key and repeat a mutation whose commit is unknown.
            self.stop.set()
            raise

    async def run_once(self) -> bool:
        from .execution_client import ExecutionClientError, ExecutionOutcomeUnknown

        def complete_or_fail(context, *, outcome, result=None, error_code=None):
            try:
                return self.client.complete(context, outcome=outcome, result=result,
                                            error_code=error_code)
            except ExecutionOutcomeUnknown:
                self._reconcile_unknown(context, "complete")
                return None
            except ExecutionClientError:
                # Client-side bounds and typed owner refusals are known
                # non-commits. Close the still-current attempt with a small,
                # finite failure under a fresh operation key.
                fallback = self._context(self.instance_id, job, self._operation_id())
                try:
                    return self.client.complete(
                        fallback, outcome="failed", error_code="internal_execution_error"
                    )
                except ExecutionOutcomeUnknown:
                    self._reconcile_unknown(fallback, "complete")
                    return None

        pull_id = self._operation_id()
        try:
            pulled = self.client.pull(self.instance_id, pull_id)
        except ExecutionOutcomeUnknown:
            # Pull can reserve work even if its response is lost. The operation
            # journal does not expose private task data through lookup, so stop
            # this iteration rather than claim again under a new key.
            self.stop.set()
            raise
        if pulled["state"] == "idle":
            return False
        job = pulled["job"]
        start_context = self._context(self.instance_id, job, self._operation_id())
        try:
            started = self.client.start(start_context)
        except ExecutionOutcomeUnknown:
            # Start may have committed and the response may contain the only
            # gateway capability. Status cannot recover it, so do not dispatch.
            self.stop.set()
            raise
        if started["state"] != "started":
            return False
        context = self._context(self.instance_id, job, self._operation_id())
        # Start's bearer is never retained or returned by this worker runtime;
        # model-gateway execution still requires a separately qualified adapter.
        execution = started.get("execution")
        if execution is not None:
            # This composition does not yet bind the gateway capability to a
            # qualified remote gateway adapter. Close the exact started fence
            # with a finite failure instead of leaving it running to expire.
            complete = self._context(self.instance_id, job, self._operation_id())
            complete_or_fail(complete, outcome="failed",
                             error_code="model_gateway_adapter_unavailable")
            return True

        lease_until = float(started["lease_until"])

        def renew_exact_fence():
            nonlocal lease_until
            renew_context = self._context(self.instance_id, job, self._operation_id())
            try:
                response = self.client.renew(renew_context)
                if response["state"] != "renewed":
                    return False
                lease_until = float(response["lease_until"])
                return True
            except ExecutionOutcomeUnknown:
                observed = self.client.status(context)
                if observed["state"] != "current" or observed["lease_until"] is None:
                    return False
                lease_until = float(observed["lease_until"])
                return True

        self.runner.authorization_check = renew_exact_fence
        operation = asyncio.create_task(self.runner.run(job["resolved"], job["task"], job["job_id"]))
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
                    try:
                        self.client.handback(handback)
                    except ExecutionOutcomeUnknown:
                        self._reconcile_unknown(handback, "handback")
                    return True
                state = self.client.status(context)
                if state["state"] != "current" or state["lease_until"] is None:
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)
                    complete = self._context(self.instance_id, job, self._operation_id())
                    complete_or_fail(complete, outcome="cancelled")
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
                # In particular, an unknown pull/start is not retried here.
                log.error("Remote execution operation failed; no further work dispatched")
                worked = False
            if not worked:
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=self.poll_seconds)
                except asyncio.TimeoutError:
                    pass
