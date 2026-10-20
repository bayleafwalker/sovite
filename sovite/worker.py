from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import socket
import time
import uuid
from collections import Counter

from sqlalchemy import or_, select, text, update

from .adapters import AdapterRunner, RuntimeFailure
from .authority import issue_execution, refresh_execution, revoke_execution, sweep_terminal_authority
from .db import Job, WorkerHeartbeat
from .service import Platform, PlatformError, TERMINAL

log = logging.getLogger("sovite.worker")

# Arbitrary constant naming the PostgreSQL advisory lock that serializes claims.
CLAIM_LOCK = 0x50617669746531


def replay_safe(resolved: dict) -> bool:
    harness = resolved["harness"]
    return harness["kind"] in {"demo", "tool-agent"} and all(t.get("read_only", t["kind"] == "kubernetes-read") for t in resolved.get("tools", [])) or harness.get("idempotent", False)


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

    def heartbeat(self, job_id: str | None = None):
        with self.platform.db.sessions.begin() as session:
            row = session.get(WorkerHeartbeat, self.id)
            if not row:
                session.add(WorkerHeartbeat(id=self.id, updated_at=time.time(), job_id=job_id))
            else:
                row.updated_at, row.job_id = time.time(), job_id

    def recover_expired(self):
        now = time.time()
        with self.platform.db.sessions.begin() as session:
            self.platform.db.authority_lock(session)
            sweep_terminal_authority(self.platform, session)
            rows = session.scalars(select(Job).where(Job.status == "running", Job.lease_until < now).with_for_update(skip_locked=True)).all()
            for row in rows:
                if row.resolved.get("compute", {}).get("id") != self.platform.settings.compute_id:
                    continue
                if row.cancel_requested:
                    row.status, row.error_code = "cancelled", "cancelled"
                elif not replay_safe(row.resolved):
                    row.status, row.error_code = "blocked", "execution_uncertain"
                elif row.attempts >= int(row.resolved["grant"].get("max_attempts", self.platform.settings.max_attempts)):
                    row.status, row.error_code = "failed", "lease_recovery_exhausted"
                else:
                    row.status, row.error_code = "retry_wait", "lease_expired"
                    row.next_attempt_at = now
                row.lease_owner, row.lease_until, row.updated_at = None, None, now
                revoke_execution(session, row.id)
                self.execution_fences.pop(row.id, None)
                if row.status in TERMINAL:
                    row.finished_at = now
                self.platform.db.audit(session, self.id, "task.lease_recovered", row.id, {"status": row.status, "error_code": row.error_code})

    def claim(self) -> Job | None:
        self.recover_expired()
        now = time.time()
        with self.platform.db.sessions.begin() as session:
            self.platform.db.authority_lock(session)
            if self.platform.db.engine.dialect.name == "postgresql":
                # Per-model limits count running jobs, so concurrent claims must not both see a free slot.
                session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": CLAIM_LOCK})
            rows = session.scalars(select(Job).where(Job.status.in_(["queued", "retry_wait"]), Job.next_attempt_at <= now, Job.cancel_requested == False).order_by(Job.created_at).limit(100).with_for_update(skip_locked=True)).all()
            running = Counter(job.resolved.get("model", {}).get("id") for job in session.scalars(select(Job).where(Job.status == "running")))
            for row in rows:
                if row.resolved.get("compute", {}).get("id") != self.platform.settings.compute_id:
                    continue
                model = row.resolved.get("model", {})
                if model.get("max_concurrency") and running[model.get("id")] >= model["max_concurrency"]:
                    continue  # Leave it queued; a slow model must not hold workers other models could use.
                try:
                    self.platform.authorize(session, row.grant_id, row.caller, generation=row.generation, is_operator=row.resolved.get("submission_role") == "operator")
                except PlatformError as exc:
                    row.status, row.error_code, row.updated_at, row.finished_at = "blocked", exc.code, now, now
                    self.platform.db.audit(session, self.id, "task.dispatch_denied", row.id, {"code": exc.code})
                    continue
                claimed = session.execute(update(Job).where(Job.id == row.id, Job.status.in_(["queued", "retry_wait"])).values(status="running", lease_owner=self.id, lease_until=now + self.platform.settings.lease_seconds, attempts=Job.attempts + 1, started_at=row.started_at or now, updated_at=now))
                if claimed.rowcount != 1:
                    continue
                session.refresh(row)
                issue_execution(self.platform, session, row)
                if hasattr(row, "execution_fence"):
                    self.execution_fences[row.id] = row.execution_fence
                self.platform.db.audit(session, self.id, "task.started", row.id, {"attempt": row.attempts, "compute_id": self.platform.settings.compute_id})
                return row
        return None

    def renew(self, job_id: str) -> bool:
        with self.platform.db.sessions.begin() as session:
            self.platform.db.authority_lock(session)
            row = session.get(Job, job_id)
            if not row or row.status != "running" or row.lease_owner != self.id or row.cancel_requested or row.lease_until < time.time():
                return False
            try:
                self.platform.authorize(session, row.grant_id, row.caller, generation=row.generation, is_operator=row.resolved.get("submission_role") == "operator")
            except PlatformError:
                return False
            now = time.time()
            row.lease_until, row.updated_at = now + self.platform.settings.lease_seconds, now
            refresh_execution(self.platform, session, row, self.execution_fences.get(job_id), now)
            return True

    def hand_back(self, job_id: str):
        """Return a running job on worker shutdown, as lease recovery would but at once."""
        with self.platform.db.sessions.begin() as session:
            self.platform.db.authority_lock(session)
            row = session.execute(select(Job).where(Job.id == job_id).with_for_update()).scalar_one()
            if row.status != "running" or row.lease_owner != self.id:
                return
            now = time.time()
            if row.cancel_requested:
                row.status, row.error_code = "cancelled", "cancelled"
            elif not replay_safe(row.resolved):
                row.status, row.error_code = "blocked", "execution_uncertain"
            else:
                # The interrupted attempt was the worker's, not the task's; do not charge it.
                row.status, row.error_code, row.next_attempt_at = "retry_wait", "worker_shutdown", now
                row.attempts = max(0, row.attempts - 1)
            row.lease_owner, row.lease_until, row.updated_at = None, None, now
            revoke_execution(session, row.id)
            self.execution_fences.pop(row.id, None)
            if row.status in TERMINAL:
                row.finished_at = now
            self.platform.db.audit(session, self.id, "task.handed_back", row.id, {"status": row.status, "error_code": row.error_code})

    def finish(self, job_id: str, artifact: tuple[str, str] | None = None, error: RuntimeFailure | None = None, cancelled: bool = False):
        with self.platform.db.sessions.begin() as session:
            self.platform.db.authority_lock(session)
            row = session.execute(select(Job).where(Job.id == job_id).with_for_update()).scalar_one()
            if row.status != "running" or row.lease_owner != self.id or row.lease_until < time.time():
                # A late process cannot publish after its lease has been reclaimed.
                return
            now = time.time()
            denied = None
            try:
                self.platform.authorize(session, row.grant_id, row.caller, generation=row.generation, is_operator=row.resolved.get("submission_role") == "operator")
            except PlatformError as exc:
                denied = exc.code
            if artifact:
                row.artifact_key, row.artifact_sha256 = artifact
            if denied:
                row.status, row.error_code = "blocked", denied
            elif cancelled or row.cancel_requested:
                row.status, row.error_code = "cancelled", "cancelled"
            elif artifact:
                row.status, row.error_code = "completed", None
                # Persistence happened before this transaction. The projection is built
                # here from constants; model/tool strings cannot enter this surface.
                row.public_projection = {"receipt": row.receipt, "status": "available"}
                row.released_at = now
                self.platform.db.audit(session, self.id, "result.release_authorized", row.id, {"grant_id": row.grant_id, "generation": row.generation, "artifact_sha256": row.artifact_sha256})
            elif error and (error.code == "external_execution_uncertain" or error.retryable and any(not tool.get("read_only", tool["kind"] == "kubernetes-read") for tool in row.resolved.get("tools", []))):
                # Whatever path discovered it, an execution whose effect is unknown waits for an operator.
                row.status, row.error_code = "blocked", "execution_uncertain"
            elif error and error.retryable and row.resolved["grant"].get("on_outage", "queue") == "queue" and row.attempts < int(row.resolved["grant"].get("max_attempts", self.platform.settings.max_attempts)):
                row.status, row.error_code = "retry_wait", error.code
                row.next_attempt_at = now + min(300, 2 ** row.attempts * 5)
            else:
                row.status, row.error_code = "failed", error.code if error else "execution_failed"
            row.lease_owner, row.lease_until, row.updated_at = None, None, now
            revoke_execution(session, row.id)
            self.execution_fences.pop(row.id, None)
            if row.status in TERMINAL:
                row.finished_at = now
            self.platform.db.audit(session, self.id, "task.state_changed", row.id, {"status": row.status, "error_code": row.error_code})

    async def execute(self, job: Job):
        self.heartbeat(job.id)
        def check_authorization():
            if not self.renew(job.id):
                raise RuntimeFailure("authorization_revoked")
        self.runner.authorization_check = check_authorization
        operation = asyncio.create_task(self.runner.run(job.resolved, job.task, job.id))
        timeout = min(int(job.resolved["grant"].get("timeout_seconds", self.platform.settings.job_timeout_seconds)), self.platform.settings.job_timeout_seconds)
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
                    self.hand_back(job.id)
                    return
                self.heartbeat(job.id)
                if not self.renew(job.id):
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)
                    self.finish(job.id, cancelled=True)
                    return
                if time.monotonic() >= deadline:
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)
                    self.finish(job.id, error=RuntimeFailure("execution_timeout", False))
                    return
            result = await operation
            # Artifact commit can be retried safely, while re-executing external effects
            # after an uncertain failure is a separate decision.
            artifact = await asyncio.to_thread(self.platform.store.put, job.id, result)
            self.finish(job.id, artifact=artifact)
        except RuntimeFailure as exc:
            self.finish(job.id, error=exc)
        except asyncio.CancelledError:
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            # Keep the lease for recovery; shutdown is not a user cancellation.
            raise
        except Exception:
            # Exception messages may contain URLs, response bodies, or sensitive data.
            log.error("Task %s failed with an internal adapter/storage error", job.id)
            self.finish(job.id, error=RuntimeFailure("internal_execution_error"))
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
