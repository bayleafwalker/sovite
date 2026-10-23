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

    def renew(self, job_id: str) -> bool:
        return self.leases.renew(job_id)

    def hand_back(self, job_id: str):
        return self.leases.hand_back(job_id)

    def finish(self, job_id: str, artifact: tuple[str, str] | None = None, error: RuntimeFailure | None = None, cancelled: bool = False):
        return self.leases.finish(job_id, artifact=artifact, error=error, cancelled=cancelled)

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
