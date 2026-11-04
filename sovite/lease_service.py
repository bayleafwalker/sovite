from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
import uuid
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Text, and_, cast, inspect, or_, select, text

from .adapters import RuntimeFailure
from .authority import check_execution_binding, issue_execution, refresh_execution, revoke_execution, sweep_terminal_authority
from .db import ExecutionAuthority, Job, MetadataRow, WorkerExecutionLease, WorkerExecutionOperation, WorkerHeartbeat
from .service import Platform, PlatformError, TERMINAL


class _HandBackOwnerUnavailable(RuntimeError):
    """A complete Job/Work owner snapshot could not be established."""


# Arbitrary constant naming the PostgreSQL advisory lock that serializes claims.
CLAIM_LOCK = 0x50617669746531


def replay_safe(resolved: dict) -> bool:
    # A repository edit may have reached an external provider or tool before a
    # worker disappeared. Preserve the workspace/evidence and require an
    # explicit owner reconciliation; never replay it as an ordinary task.
    if resolved.get("repository_run") is not None:
        return False
    harness = resolved["harness"]
    return harness["kind"] in {"demo", "tool-agent"} and all(t.get("read_only", t["kind"] == "kubernetes-read") for t in resolved.get("tools", [])) or harness.get("idempotent", False)


@dataclass(frozen=True)
class ExecutionPrincipal:
    workload_id: str
    compute_id: str


@dataclass(frozen=True)
class ExecutionContext:
    instance_id: str
    operation_id: str
    job_id: str
    generation: str
    fence: str


_OP_DIGEST_SCHEMA = "sovite-worker-execution-operation/v1"
_OWNER_KEY_SCHEMA = "sovite-worker-execution-owner/v1"
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_INSTANCE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_OPERATION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_WORKLOAD_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_COMPUTE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_FENCE_RE = re.compile(r"^[0-9a-f]{32}$")
_GENERATION_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _fail(code: str, status: int, message: str):
    raise PlatformError(code, message, status)


def _request_error():
    _fail("invalid_request", 422, "Execution owner request is invalid")


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, OverflowError):
        raise ValueError("noncanonical value") from None


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _validate_principal(principal: ExecutionPrincipal) -> None:
    if (not isinstance(principal, ExecutionPrincipal)
        or not isinstance(principal.workload_id, str) or _WORKLOAD_RE.fullmatch(principal.workload_id) is None
        or not isinstance(principal.compute_id, str) or _COMPUTE_RE.fullmatch(principal.compute_id) is None):
        _request_error()


def _validate_instance(value: Any) -> None:
    if not isinstance(value, str) or _INSTANCE_RE.fullmatch(value) is None:
        _request_error()


def _validate_operation(value: Any) -> None:
    if not isinstance(value, str) or _OPERATION_RE.fullmatch(value) is None:
        _request_error()


def operation_request_digest(*, operation_kind: str, principal: ExecutionPrincipal, request: Mapping) -> str:
    """New internal owner digest; never redefines existing authored/audit bytes."""
    _validate_principal(principal)
    if operation_kind not in {"pull", "start"} or not isinstance(request, Mapping):
        _request_error()
    value = dict(request)
    fields = {"instance_id", "operation_id"} if operation_kind == "pull" else {"instance_id", "operation_id", "job_id", "generation", "fence"}
    profile = value.pop("profile", None)
    if set(value) != fields or (profile is not None and profile != "agent-http-v1"):
        _request_error()
    _validate_instance(value["instance_id"])
    _validate_operation(value["operation_id"])
    if operation_kind == "start":
        if (not isinstance(value["job_id"], str) or not value["job_id"] or len(value["job_id"]) > 64
            or not isinstance(value["generation"], str) or _GENERATION_RE.fullmatch(value["generation"]) is None
            or not isinstance(value["fence"], str) or _FENCE_RE.fullmatch(value["fence"]) is None):
            _request_error()
    try:
        return _digest({"schema_version": _OP_DIGEST_SCHEMA, "operation_kind": operation_kind,
                        "workload_id": principal.workload_id, "compute_id": principal.compute_id,
                        "request": {**value, **({"profile": profile} if profile is not None else {})}})
    except ValueError:
        _request_error()


def execution_owner_key(*, workload_id: str, instance_id: str) -> str:
    """Bounded owner key; full scope remains separately stored in lease rows."""
    if not isinstance(workload_id, str) or _WORKLOAD_RE.fullmatch(workload_id) is None:
        _request_error()
    _validate_instance(instance_id)
    return "execution:" + _digest({"schema_version": _OWNER_KEY_SCHEMA, "workload_id": workload_id, "instance_id": instance_id})


class LeaseService:
    """Owner-side lease transition service for the Worker execution loop.

    This is an internal extraction, not an independent authority: every transition
    runs inside the owning Platform's database sessions and authority checks. It
    cannot run without database authority.
    """

    def __init__(self, platform: Platform, worker_id: str, execution_fences: dict,
                 *, repository_run_profile: bool = False, repository_worker_only: bool = False):
        # execution_fences is the Worker's own dict, shared by reference so the
        # execution loop and lease transitions see the same fence state.
        self.platform, self.id, self.execution_fences = platform, worker_id, execution_fences
        self.repository_run_profile = repository_run_profile is True
        self.repository_worker_only = repository_worker_only is True
        self._trusted_worker_binding: tuple[ExecutionPrincipal, str] | None = None

    def _eligible_job(self, job):
        if not self.repository_worker_only:
            return True
        from .repository_runs import RUN_SCHEMA
        run=job.resolved.get("repository_run")
        return type(run)is dict and run.get("schema_version")==RUN_SCHEMA

    def _eligible_query(self, query):
        if self.repository_worker_only:
            from .repository_runs import RUN_SCHEMA
            query=query.where(Job.resolved["repository_run"]["schema_version"].as_string()==RUN_SCHEMA)
        return query

    def bind_trusted_worker(self, *, principal: ExecutionPrincipal, instance_id: str) -> None:
        """Internal adapter only: instance must hash this service's complete Worker.id."""
        _validate_principal(principal)
        _validate_instance(instance_id)
        expected = hashlib.sha256(self.id.encode("utf-8")).hexdigest()
        if instance_id != expected or principal.compute_id != self.platform.settings.compute_id:
            _fail("execution_scope_denied", 403, "Execution owner scope is not permitted")
        self._trusted_worker_binding = (principal, instance_id)

    def _owner_for(self, principal: ExecutionPrincipal, instance_id: str) -> str:
        binding = self._trusted_worker_binding
        if binding is not None:
            if binding != (principal, instance_id):
                _fail("execution_scope_denied", 403, "Execution owner scope is not permitted")
            return self.id
        return execution_owner_key(workload_id=principal.workload_id, instance_id=instance_id)

    def _authorize_current(self, session, job: Job, now: float) -> dict:
        if not self._eligible_job(job):
            _fail("execution_scope_denied", 403, "Execution owner scope is not permitted")
        if job.cancel_requested:
            _fail("cancelled", 409, "Execution was cancelled")
        repository_run = job.resolved.get("repository_run")
        if repository_run is not None:
            try:
                raw_deadline = repository_run["deadline"]
                deadline = datetime.fromisoformat(raw_deadline.replace("Z", "+00:00")).timestamp()
            except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
                _fail("owner_unavailable", 503, "Repository-run owner state is unavailable")
            if not math.isfinite(deadline) or deadline <= now:
                _fail("repository_run_deadline", 409, "Repository-run deadline has expired")
        recipe = self.platform.authorize_existing_job(session, job)
        check_execution_binding(self.platform, session, job)
        return recipe

    def _journal(self, session, principal, instance_id, operation_id, *, kind, digest, now):
        key = (principal.workload_id, instance_id, operation_id)
        row = session.scalar(select(WorkerExecutionOperation).where(
            WorkerExecutionOperation.workload_id == key[0],
            WorkerExecutionOperation.instance_id == key[1],
            WorkerExecutionOperation.operation_id == key[2],
        ).with_for_update())
        if row is not None and (row.operation_kind != kind or row.request_digest != digest):
            _fail("idempotency_conflict", 409, "Execution operation key was already used")
        return row

    @staticmethod
    def _private_hashes(job: Job) -> tuple[str, str]:
        try:
            return hashlib.sha256(job.task.encode("utf-8")).hexdigest(), _digest(job.resolved)
        except (AttributeError, UnicodeEncodeError, ValueError):
            _fail("owner_unavailable", 503, "Execution owner state is unavailable")

    @staticmethod
    def _check_lease_finite(job: Job, lease: WorkerExecutionLease) -> None:
        if (lease.phase not in {"reserved", "started", "finished"}
            or not isinstance(lease.fence, str) or _FENCE_RE.fullmatch(lease.fence) is None
            or not isinstance(lease.generation, str) or not _GENERATION_RE.fullmatch(lease.generation)
            or not isinstance(lease.workload_id, str) or _WORKLOAD_RE.fullmatch(lease.workload_id) is None
            or not isinstance(lease.instance_id, str) or _INSTANCE_RE.fullmatch(lease.instance_id) is None
            or isinstance(lease.created_at, bool) or not isinstance(lease.created_at, (int, float)) or not math.isfinite(lease.created_at)
            or isinstance(lease.updated_at, bool) or not isinstance(lease.updated_at, (int, float)) or not math.isfinite(lease.updated_at)
            or isinstance(lease.lease_until, bool) or not isinstance(lease.lease_until, (int, float))
            or not math.isfinite(lease.lease_until)
            or (lease.execution_deadline is not None and
                (isinstance(lease.execution_deadline, bool) or not isinstance(lease.execution_deadline, (int, float)) or not math.isfinite(lease.execution_deadline)))
            or lease.generation != job.generation):
            _fail("owner_unavailable", 503, "Execution owner state is unavailable")

    def _same_owner(self, job: Job, lease: WorkerExecutionLease, principal: ExecutionPrincipal,
                    instance_id: str, owner: str) -> bool:
        return (lease.workload_id == principal.workload_id and lease.instance_id == instance_id
            and job.lease_owner == owner and lease.generation == job.generation)

    def _agent_http_recipe(self, job: Job) -> dict:
        """Project only the first admitted external recipe from owner state."""
        resolved = job.resolved
        try:
            harness = resolved["harness"]
            agent_id = resolved["agent"]["id"]
            connection = resolved.get("model", {}).get("connection", {})
            timeout = resolved.get("grant", {}).get("timeout_seconds", self.platform.settings.job_timeout_seconds)
            endpoint = harness.get("endpoint")
            cancel_endpoint = harness.get("cancel_endpoint")
            credential_env = harness.get("api_key_env")
            idempotent = harness.get("idempotent", False)
        except (AttributeError, KeyError, TypeError):
            _fail("owner_unavailable", 503, "Execution owner state is unavailable")
        if (harness.get("kind") != "agent-http" or connection.get("kind") == "model-gateway"
            or not isinstance(agent_id, str) or not 1 <= len(agent_id) <= 96
            or not isinstance(endpoint, str) or not 1 <= len(endpoint) <= 16000
            or (cancel_endpoint is not None and (not isinstance(cancel_endpoint, str) or not 1 <= len(cancel_endpoint) <= 16000))
            or (credential_env is not None and (not isinstance(credential_env, str)
                or re.fullmatch(r"[A-Z][A-Z0-9_]{1,95}", credential_env) is None))
            or type(idempotent) is not bool
            or type(timeout) is not int or not 1 <= timeout <= 2_147_483_647):
            _fail("execution_profile_unavailable", 409, "Execution profile is not available to this worker")
        recipe = {"profile": "agent-http-v1", "agent_id": agent_id, "endpoint": endpoint,
            "idempotent": idempotent, "timeout_seconds": timeout}
        if cancel_endpoint is not None:
            recipe["cancel_endpoint"] = cancel_endpoint
        if credential_env is not None:
            recipe["credential_env"] = credential_env
        return recipe

    def _external_reservation_response(self, job: Job, lease: WorkerExecutionLease) -> dict:
        return {"state": "reserved", "job": {"job_id": job.id, "generation": job.generation,
            "fence": lease.fence, "lease_until": lease.lease_until, "task": job.task,
            "recipe": self._agent_http_recipe(job)}}

    def reserve(self, *, principal: ExecutionPrincipal, instance_id: str, operation_id: str,
                external_profile: str | None = None) -> dict:
        """One caller-scoped reservation; no attempt, bearer or execution deadline."""
        _validate_principal(principal); _validate_instance(instance_id); _validate_operation(operation_id)
        if external_profile not in {None, "agent-http-v1"}:
            _request_error()
        owner = self._owner_for(principal, instance_id)
        request = {"instance_id": instance_id, "operation_id": operation_id}
        if external_profile is not None:
            request["profile"] = external_profile
        request_digest = operation_request_digest(operation_kind="pull", principal=principal, request=request)
        with self.platform.db.sessions.begin() as session:
            self.platform.db.authority_lock(session)
            if self.platform.db.engine.dialect.name == "postgresql":
                session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": CLAIM_LOCK})
            # Decision time belongs to the serialized owner transaction, not
            # the caller's arrival before it waited for the locks above.
            now = time.time()
            # Prove the owner/control plane is available even for an empty queue.
            self.platform.active(session)
            prior = self._journal(session, principal, instance_id, operation_id,
                kind="pull", digest=request_digest, now=now)
            if prior is not None:
                if not isinstance(prior.result, dict):
                    _fail("owner_unavailable", 503, "Execution owner state is unavailable")
                if prior.result == {"state": "idle"} and prior.job_id is None and prior.fence is None:
                    return {"state": "idle"}
                if prior.result.get("state") != "reserved" or not prior.job_id or not prior.fence:
                    _fail("execution_fenced", 409, "Execution reservation is no longer current")
                job = session.execute(select(Job).where(Job.id == prior.job_id).with_for_update()).scalar_one_or_none()
                lease = session.get(WorkerExecutionLease, prior.job_id, with_for_update=True)
                now = time.time()
                if job is None or lease is None:
                    _fail("execution_fenced", 409, "Execution reservation is no longer current")
                self._check_lease_finite(job, lease)
                if (not self._same_owner(job, lease, principal, instance_id, owner)
                    or job.status != "running" or lease.phase != "reserved"
                    or lease.fence != prior.fence or job.lease_until is None
                    or not math.isfinite(job.lease_until) or job.lease_until <= now
                    or lease.lease_until != job.lease_until or lease.execution_deadline is not None):
                    _fail("execution_fenced", 409, "Execution reservation is no longer current")
                self._authorize_current(session, job, now)
                task_hash, resolved_hash = self._private_hashes(job)
                expected_result = {"state": "reserved", "job_id": job.id, "generation": job.generation,
                    "fence": lease.fence, "lease_until": job.lease_until,
                    "task_sha256": task_hash, "resolved_sha256": resolved_hash}
                if prior.result != expected_result:
                    _fail("execution_fenced", 409, "Execution reservation is no longer current")
                response = (self._external_reservation_response(job, lease) if external_profile is not None
                    else self._reservation_response(job, lease))
                self._check_response_size(response)
                return copy.deepcopy(response)

            if principal.compute_id != self.platform.settings.compute_id:
                _fail("execution_scope_denied", 403, "Execution owner scope is not permitted")

            candidates = session.scalars(self._eligible_query(select(Job).where(
                Job.status.in_(("queued", "retry_wait")), Job.next_attempt_at <= now,
                Job.cancel_requested.is_(False),
            )).order_by(Job.created_at, Job.id).limit(100).with_for_update(skip_locked=True)).all()
            # Candidate row locks can wait too; base each new reservation on
            # owner time observed after those locks have been acquired.
            now = time.time()
            running = Counter()
            for active_job in session.scalars(select(Job).where(Job.status == "running")):
                running[active_job.resolved.get("model", {}).get("id")] += 1
            incompatible_profile_job = False
            for job in candidates:
                if job.resolved.get("compute", {}).get("id") != principal.compute_id:
                    continue
                if job.resolved.get("repository_run") is not None:
                    # The released agent-http profile and ordinary local worker
                    # are answer-only. Repository mutation needs its separately
                    # injected owner profile; never hand it to a generic worker.
                    if external_profile is not None:
                        incompatible_profile_job = True
                    elif not self.repository_run_profile:
                        continue
                    if incompatible_profile_job:
                        continue
                existing_lease = session.get(WorkerExecutionLease, job.id, with_for_update=True)
                # This final row lock can block after candidate selection too.
                now = time.time()
                if job.next_attempt_at > now:
                    continue
                try:
                    self._authorize_current(session, job, now)
                except PlatformError as exc:
                    # Existing-job lineage can become ineligible after enqueue.
                    # These owner-declared history denials are distinct from an
                    # unavailable/corrupt current owner and may block dispatch.
                    lineage_denial = (exc.status, exc.code) in {
                        (404, "previous_attempt_unavailable"),
                        (409, "previous_attempt_unfinished"),
                        (409, "previous_effect_uncertain"),
                        (409, "repository_run_deadline"),
                        (409, "repository_currentness_revoked"),
                        (409, "repository_currentness_binding_invalid"),
                        (409, "repository_currentness_required"),
                    }
                    if exc.status != 403 and not lineage_denial:
                        raise
                    job.status, job.error_code, job.updated_at, job.finished_at = "blocked", exc.code, now, now
                    self.platform.db.audit(session, self.id, "task.dispatch_denied", job.id, {"code": exc.code})
                    continue
                if external_profile is not None:
                    try:
                        self._agent_http_recipe(job)
                    except PlatformError as exc:
                        if exc.code != "execution_profile_unavailable":
                            raise
                        # The HTTP pull endpoint admits only its server-selected
                        # profile. Leave incompatible work in the owner's queue
                        # for local or separately qualified workers.
                        incompatible_profile_job = True
                        continue
                model = job.resolved.get("model", {})
                limit = model.get("max_concurrency")
                if limit and running[model.get("id")] >= limit:
                    continue
                fence = uuid.uuid4().hex
                lease_until = now + self.platform.settings.lease_seconds
                lease = WorkerExecutionLease(job_id=job.id, workload_id=principal.workload_id,
                    instance_id=instance_id, fence=fence, generation=job.generation, phase="reserved",
                    lease_until=lease_until, execution_deadline=None, created_at=now, updated_at=now)
                if existing_lease is not None:
                    for field in ("workload_id", "instance_id", "fence", "generation", "phase", "lease_until", "execution_deadline", "created_at", "updated_at"):
                        setattr(existing_lease, field, getattr(lease, field))
                    lease = existing_lease
                response = (self._external_reservation_response(job, lease) if external_profile is not None else
                    {"state": "reserved", "job": {"job_id": job.id, "generation": job.generation,
                    "fence": fence, "lease_until": lease_until, "task": job.task, "resolved": copy.deepcopy(job.resolved)}})
                self._check_response_size(response)
                task_hash, resolved_hash = self._private_hashes(job)
                job.status, job.lease_owner, job.lease_until, job.updated_at = "running", owner, lease_until, now
                session.add(lease)
                session.add(WorkerExecutionOperation(workload_id=principal.workload_id, instance_id=instance_id,
                    operation_id=operation_id, operation_kind="pull", request_digest=request_digest,
                    job_id=job.id, fence=fence, result={"state": "reserved", "job_id": job.id,
                    "generation": job.generation, "fence": fence, "lease_until": lease_until,
                    "task_sha256": task_hash, "resolved_sha256": resolved_hash}, created_at=now, updated_at=now))
                self.platform.db.audit(session, self.id, "task.execution_reserved", job.id,
                    {"generation": job.generation, "fence": fence, "workload_id": principal.workload_id, "instance_id": instance_id})
                return copy.deepcopy(response)
            if incompatible_profile_job:
                _fail("execution_profile_unavailable", 409, "Execution profile is not available to this worker")
            idle = {"state": "idle"}
            session.add(WorkerExecutionOperation(workload_id=principal.workload_id, instance_id=instance_id,
                operation_id=operation_id, operation_kind="pull", request_digest=request_digest,
                job_id=None, fence=None, result=idle, created_at=now, updated_at=now))
            self.platform.db.audit(session, self.id, "task.execution_idle", instance_id,
                {"workload_id": principal.workload_id, "compute_id": principal.compute_id})
            return idle

    @staticmethod
    def _reservation_response(job: Job, lease: WorkerExecutionLease) -> dict:
        return {"state": "reserved", "job": {"job_id": job.id, "generation": job.generation,
            "fence": lease.fence, "lease_until": lease.lease_until, "task": job.task,
            "resolved": copy.deepcopy(job.resolved)}}

    @staticmethod
    def _check_response_size(response: dict) -> None:
        try:
            size = len(_canonical_bytes(response))
        except ValueError:
            _fail("owner_unavailable", 503, "Execution owner state is unavailable")
        if size > _MAX_RESPONSE_BYTES:
            _fail("execution_payload_too_large", 413, "Execution payload exceeds the bounded response size")

    def start(self, *, principal: ExecutionPrincipal, context: ExecutionContext,
              external_profile: str | None = None) -> dict:
        """Linearize once; only a first committed start may return execution bearer."""
        _validate_principal(principal)
        if external_profile not in {None, "agent-http-v1"}:
            _request_error()
        if not isinstance(context, ExecutionContext):
            _request_error()
        _validate_instance(context.instance_id); _validate_operation(context.operation_id)
        if (not isinstance(context.job_id, str) or not context.job_id or len(context.job_id) > 64
            or not isinstance(context.generation, str) or _GENERATION_RE.fullmatch(context.generation) is None
            or not isinstance(context.fence, str) or _FENCE_RE.fullmatch(context.fence) is None):
            _request_error()
        owner = self._owner_for(principal, context.instance_id)
        request = {"instance_id": context.instance_id, "operation_id": context.operation_id,
            "job_id": context.job_id, "generation": context.generation, "fence": context.fence}
        if external_profile is not None:
            request["profile"] = external_profile
        request_digest = operation_request_digest(operation_kind="start", principal=principal, request=request)
        with self.platform.db.sessions.begin() as session:
            self.platform.db.authority_lock(session)
            self.platform.active(session)
            now = time.time()
            prior = self._journal(session, principal, context.instance_id, context.operation_id,
                kind="start", digest=request_digest, now=now)
            if principal.compute_id != self.platform.settings.compute_id:
                _fail("execution_scope_denied", 403, "Execution owner scope is not permitted")
            job = session.execute(select(Job).where(Job.id == context.job_id).with_for_update()).scalar_one_or_none()
            if job is None or not self._eligible_job(job):
                _fail("execution_scope_denied", 403, "Execution owner scope is not permitted")
            lease = session.get(WorkerExecutionLease, context.job_id, with_for_update=True)
            if lease is None:
                _fail("execution_fenced", 409, "Execution context is no longer current")
            # Job and lease row locks may themselves wait after the shared
            # authority lock. Sample only after all blocking locks are held.
            now = time.time()
            self._check_lease_finite(job, lease)
            if not self._same_owner(job, lease, principal, context.instance_id, owner):
                _fail("execution_scope_denied", 403, "Execution owner scope is not permitted")
            if context.generation != job.generation or context.fence != lease.fence:
                _fail("execution_fenced", 409, "Execution context is no longer current")
            if prior is not None and (prior.job_id != job.id or prior.fence != lease.fence):
                _fail("idempotency_conflict", 409, "Execution operation key was already used")
            if prior is not None and not isinstance(prior.result, dict):
                _fail("owner_unavailable", 503, "Execution owner state is unavailable")
            if job.cancel_requested:
                _fail("cancelled", 409, "Execution was cancelled")
            self._authorize_current(session, job, now)
            if external_profile is not None:
                self._agent_http_recipe(job)
            if job.status != "running" or job.lease_owner != owner or job.lease_until is None:
                _fail("execution_fenced", 409, "Execution context is no longer current")
            if isinstance(job.lease_until, bool) or not isinstance(job.lease_until, (int, float)) or not math.isfinite(job.lease_until) or job.lease_until <= now or lease.lease_until != job.lease_until:
                _fail("execution_fenced", 409, "Execution context is no longer current")
            if lease.phase == "started":
                if lease.execution_deadline is None or lease.execution_deadline <= now:
                    _fail("execution_fenced", 409, "Execution context is no longer current")
                already = {"state": "already_started", "job_id": job.id, "generation": job.generation, "fence": lease.fence}
                if prior is None:
                    session.add(WorkerExecutionOperation(workload_id=principal.workload_id, instance_id=context.instance_id,
                        operation_id=context.operation_id, operation_kind="start", request_digest=request_digest,
                        job_id=job.id, fence=lease.fence, result=already, created_at=now, updated_at=now))
                    self.platform.db.audit(session, self.id, "task.execution_start_replayed", job.id,
                        {"generation": job.generation, "fence": lease.fence})
                else:
                    recorded = prior.result
                    common = {"job_id": job.id, "generation": job.generation, "fence": lease.fence}
                    if recorded.get("state") == "started":
                        shape_ok = (set(recorded) == {"state", "job_id", "generation", "fence", "lease_until", "deadline"}
                            and isinstance(recorded.get("lease_until"), (int, float))
                            and not isinstance(recorded.get("lease_until"), bool)
                            and math.isfinite(recorded["lease_until"])
                            and isinstance(recorded.get("deadline"), (int, float))
                            and not isinstance(recorded.get("deadline"), bool)
                            and math.isfinite(recorded["deadline"]))
                    else:
                        shape_ok = recorded.get("state") == "already_started" and set(recorded) == {"state", "job_id", "generation", "fence"}
                    if not shape_ok or any(recorded.get(key) != value for key, value in common.items()):
                        _fail("execution_fenced", 409, "Execution context is no longer current")
                if job.resolved.get("model", {}).get("connection", {}).get("kind") == "model-gateway":
                    from .authority import execution_authority_current
                    if not execution_authority_current(self.platform, session, job, lease, principal.workload_id, now):
                        _fail("execution_fenced", 409, "Execution context is no longer current")
                return already
            if lease.phase != "reserved" or lease.execution_deadline is not None:
                _fail("owner_unavailable", 503, "Execution owner state is unavailable")
            if prior is not None:
                _fail("idempotency_conflict", 409, "Execution operation key was already used")
            authority = session.get(ExecutionAuthority, job.id)
            if job.resolved.get("model", {}).get("connection", {}).get("kind") == "model-gateway" and authority is not None and authority.capability_digest and authority.expires_at > now:
                _fail("owner_unavailable", 503, "Execution owner state is unavailable")
            grant_timeout = job.resolved.get("grant", {}).get("timeout_seconds", self.platform.settings.job_timeout_seconds)
            if isinstance(grant_timeout, bool) or not isinstance(grant_timeout, (int, float)) or not math.isfinite(grant_timeout) or grant_timeout <= 0:
                _fail("owner_unavailable", 503, "Execution owner state is unavailable")
            deadline = now + min(float(grant_timeout), float(self.platform.settings.job_timeout_seconds))
            repository_run = job.resolved.get("repository_run")
            if repository_run is not None:
                try:
                    repository_deadline = datetime.fromisoformat(
                        repository_run["deadline"].replace("Z", "+00:00")
                    ).timestamp()
                except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
                    _fail("owner_unavailable", 503, "Repository-run owner state is unavailable")
                deadline = min(deadline, repository_deadline)
            if not math.isfinite(deadline) or deadline <= now:
                _fail("owner_unavailable", 503, "Execution owner state is unavailable")
            job.attempts += 1
            job.started_at = job.started_at or now
            job.updated_at = now
            lease.phase, lease.execution_deadline, lease.updated_at = "started", deadline, now
            execution = issue_execution(self.platform, session, job, fence=lease.fence,
                workload=principal.workload_id, now=now, deadline=deadline)
            response = {"state": "started", "job_id": job.id, "generation": job.generation,
                "fence": lease.fence, "lease_until": job.lease_until, "deadline": deadline}
            if execution is not None:
                response["execution"] = execution
            self._check_response_size(response)
            journal_result = {k: v for k, v in response.items() if k != "execution"}
            session.add(WorkerExecutionOperation(workload_id=principal.workload_id, instance_id=context.instance_id,
                operation_id=context.operation_id, operation_kind="start", request_digest=request_digest,
                job_id=job.id, fence=lease.fence, result=journal_result, created_at=now, updated_at=now))
            self.platform.db.audit(session, self.id, "task.execution_started", job.id,
                {"generation": job.generation, "fence": lease.fence, "attempt": job.attempts, "workload_id": principal.workload_id})
            return copy.deepcopy(response)

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
            if self.platform.db.engine.dialect.name == "postgresql":
                session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": CLAIM_LOCK})
            if not self.repository_worker_only:
                sweep_terminal_authority(self.platform, session)
            rows = session.scalars(self._eligible_query(select(Job).where(Job.status == "running", Job.lease_until < now)).with_for_update(skip_locked=True)).all()
            for row in rows:
                lease = session.get(WorkerExecutionLease, row.id, with_for_update=True)
                if lease is not None:
                    self._check_lease_finite(row, lease)
                    if lease.phase not in {"reserved", "started"}:
                        _fail("owner_unavailable", 503, "Execution owner state is unavailable")
                if row.resolved.get("compute", {}).get("id") != self.platform.settings.compute_id:
                    continue
                if row.cancel_requested:
                    row.status, row.error_code = "cancelled", "cancelled"
                elif lease is not None and lease.phase == "reserved":
                    # No execution started, so this reservation is free to retry.
                    row.status, row.error_code = "retry_wait", "lease_expired"
                    row.next_attempt_at = now
                elif not replay_safe(row.resolved):
                    row.status, row.error_code = "blocked", "execution_uncertain"
                elif row.attempts >= int(row.resolved["grant"].get("max_attempts", self.platform.settings.max_attempts)):
                    row.status, row.error_code = "failed", "lease_recovery_exhausted"
                else:
                    row.status, row.error_code = "retry_wait", "lease_expired"
                    row.next_attempt_at = now
                row.lease_owner, row.lease_until, row.updated_at = None, None, now
                if lease is not None:
                    lease.phase, lease.lease_until, lease.updated_at = "finished", 0, now
                revoke_execution(session, row.id)
                self._forget_fence(row.id, lease.fence if lease is not None else None)
                if row.status in TERMINAL:
                    row.finished_at = now
                self.platform.db.audit(session, self.id, "task.lease_recovered", row.id, {"status": row.status, "error_code": row.error_code})

    def claim(self) -> Job | None:
        self.recover_expired()
        principal = ExecutionPrincipal(self.platform.settings.execution_workload_identity,
                                       self.platform.settings.compute_id)
        instance_id = hashlib.sha256(self.id.encode("utf-8")).hexdigest()
        self.bind_trusted_worker(principal=principal, instance_id=instance_id)
        reserved = self.reserve(principal=principal, instance_id=instance_id, operation_id=uuid.uuid4().hex)
        if reserved.get("state") != "reserved":
            return None
        view = reserved["job"]
        context = ExecutionContext(instance_id, uuid.uuid4().hex, view["job_id"],
                                   view["generation"], view["fence"])
        started = self.start(principal=principal, context=context)
        with self.platform.db.sessions() as session:
            row = session.get(Job, started["job_id"])
            if row is None:
                _fail("execution_fenced", 409, "Execution context is no longer current")
            row.execution_fence = started["fence"]
            row.execution_deadline = started["deadline"]
            if "execution" in started:
                row.execution_capability = started["execution"]["capability"]
            self.execution_fences[row.id] = started["fence"]
            return row

    def _forget_fence(self, job_id: str, fence: str | None) -> None:
        if fence is not None and self.execution_fences.get(job_id) == fence:
            self.execution_fences.pop(job_id, None)

    def renew(self, job_id: str, fence: str | None = None) -> bool:
        with self.platform.db.sessions.begin() as session:
            self.platform.db.authority_lock(session)
            row = session.execute(select(Job).where(Job.id == job_id).with_for_update()).scalar_one_or_none()
            lease = session.get(WorkerExecutionLease, job_id, with_for_update=True)
            if not row or lease is None or not self._eligible_job(row):
                return False
            now = time.time()
            try:
                self._check_lease_finite(row, lease)
            except PlatformError:
                return False
            selected_fence = fence if fence is not None else self.execution_fences.get(job_id)
            try:
                owner = self._owner_for(ExecutionPrincipal(lease.workload_id, self.platform.settings.compute_id), lease.instance_id)
            except PlatformError:
                return False
            if (lease.phase != "started" or not self._same_owner(row, lease,
                ExecutionPrincipal(lease.workload_id, self.platform.settings.compute_id), lease.instance_id, owner)
                or row.status != "running" or row.lease_owner != owner or row.cancel_requested
                or selected_fence != lease.fence or row.lease_until is None
                or not isinstance(row.lease_until, (int, float)) or isinstance(row.lease_until, bool)
                or not math.isfinite(row.lease_until) or row.lease_until <= now
                or lease.lease_until != row.lease_until or lease.execution_deadline is None
                or lease.execution_deadline <= now):
                return False
            try:
                self._authorize_current(session, row, now)
            except PlatformError:
                return False
            is_gateway = row.resolved.get("model", {}).get("connection", {}).get("kind") == "model-gateway"
            if is_gateway and not refresh_execution(self.platform, session, row, selected_fence, now):
                return False
            until = min(now + self.platform.settings.lease_seconds, lease.execution_deadline)
            if until <= now or not math.isfinite(until):
                return False
            row.lease_until = lease.lease_until = until
            row.updated_at = lease.updated_at = now
            return True

    def hand_back(self, job_id: str, fence: str | None = None):
        """Hand back a running job after closing its complete Work-owner snapshot.

        A true result records the local atomic transition only. It does not prove
        that a child or provider stopped before producing an external effect.
        """
        from sqlalchemy import outerjoin

        from .collection_transaction import owner_transaction
        from .db import Base, MIGRATIONS, digest
        from .program_ledger import _mark_work_projection_unknown_in_transaction
        from .program_ledger import _is_lower_hex, _validate_locked_legacy_binding
        from .program_ledger_core import _baseline_payload
        from .repository_runs import RUN_SCHEMA
        from .work_projection import (
            WorkProjectionError,
            _compare_actual_work_projection,
            _raw_job_object,
            lock_work_projection_set,
            work_projections_from_job,
        )

        database = self.platform.db
        succeeded = False
        succeeded_fence = None
        try:
            with owner_transaction(database) as session:
                connection = session.connection()
                try:
                    version = database._version(connection)
                    if type(version) is not int or not 1 <= version <= database.schema_version:
                        raise _HandBackOwnerUnavailable()
                    expected_by_name = {
                        table.name: table for migration, migrated_tables in MIGRATIONS
                        if migration <= version for table in migrated_tables
                    }
                    expected_tables = list(expected_by_name.values())
                    later_table_names = {
                        table.name for migration, migrated_tables in MIGRATIONS
                        if migration > version for table in migrated_tables
                        if table.name not in expected_by_name
                    }
                    physical_names = set(inspect(connection).get_table_names(schema=database.schema))
                    if (any(table.name not in physical_names for table in expected_tables)
                            or physical_names.intersection(later_table_names)):
                        raise _HandBackOwnerUnavailable()
                    database._structure(connection, expected_tables)
                except _HandBackOwnerUnavailable:
                    raise
                except Exception:
                    raise _HandBackOwnerUnavailable() from None

                tables = Base.metadata.tables
                job_table = tables["jobs"]
                probe = session.execute(select(
                    Job.id, Job.status, Job.caller, Job.attempts, Job.generation,
                    Job.lease_owner, Job.lease_until, Job.cancel_requested,
                ).where(Job.id == job_id)).mappings().one_or_none()
                if probe is None:
                    return False
                raw = session.execute(select(cast(job_table.c.resolved, Text)).where(
                    job_table.c.id == job_id)).scalar_one_or_none()
                if type(raw) is not str:
                    raise _HandBackOwnerUnavailable()

                try:
                    root = _raw_job_object(raw)
                    if "program_ledger_projections_v1" in root:
                        raise WorkProjectionError("orphan top-level projection marker")
                    if "repository_run" in root:
                        run = root["repository_run"]
                        if type(run) is not dict or run.get("schema_version") != RUN_SCHEMA:
                            raise WorkProjectionError("repository-run marker is malformed")
                        projections_all = work_projections_from_job(raw, allow_absent=True)
                        attempts = run.get("attempts")
                        if type(attempts) is not list:
                            raise WorkProjectionError("repository-run attempts are malformed")
                        attempt_ids = []
                        for attempt in attempts:
                            if type(attempt) is not dict or type(attempt.get("id")) is not str:
                                raise WorkProjectionError("repository-run attempt identity is malformed")
                            attempt_ids.append(attempt["id"])
                        if len(set(attempt_ids)) != len(attempt_ids):
                            raise WorkProjectionError("repository-run attempt identities are duplicated")
                    else:
                        run, attempts, attempt_ids = None, [], []
                        projections_all = ()
                    if any(item.work_job_id != job_id for item in projections_all):
                        raise WorkProjectionError("projection belongs to another Job")
                except (WorkProjectionError, TypeError, ValueError, RecursionError):
                    raise _HandBackOwnerUnavailable() from None

                current_attempt = None
                current_attempt_row = None
                current_attempt_state = None
                if run is not None and probe["status"] == "running":
                    if (type(probe["attempts"]) is not int or isinstance(probe["attempts"], bool)
                            or probe["attempts"] < 1 or not attempts
                            or type(attempts[-1]) is not dict):
                        raise _HandBackOwnerUnavailable()
                    current_attempt_row = attempts[-1]
                    current_attempt = current_attempt_row.get("id")
                    current_attempt_state = current_attempt_row.get("state")
                    if (current_attempt != f"{job_id}-{probe['attempts']}"
                            or current_attempt_state not in {"preparing", "awaiting_activation", "running"}):
                        raise _HandBackOwnerUnavailable()
                elif run is not None and type(probe["attempts"]) is not int:
                    raise _HandBackOwnerUnavailable()

                if run is not None:
                    known_attempt_ids = set(attempt_ids)
                    for projection in projections_all:
                        if (projection.attempt_id not in known_attempt_ids
                                or projection.attempt_id != f"{job_id}-{int(projection.attempt_id.rsplit('-', 1)[1])}"
                                or int(projection.attempt_id.rsplit("-", 1)[1]) > probe["attempts"]):
                            raise _HandBackOwnerUnavailable()

                schema_version = version
                work_intents = []
                work_bindings = []
                work_reservations = []
                work_slots = []
                legacy_links = []
                if schema_version >= 8:
                    reservation_t = tables.get("program_reservations")
                    subject_t = tables.get("program_budget_subjects")
                    slot_t = tables.get("program_slots")
                    if reservation_t is None or subject_t is None or slot_t is None:
                        raise _HandBackOwnerUnavailable()
                    work_reservations = session.execute(select(
                        reservation_t.c.scope_id, reservation_t.c.call_id,
                        reservation_t.c.subject_id, reservation_t.c.purpose,
                    ).where(reservation_t.c.subject_id == job_id)).mappings().all()
                    joined_slots = session.execute(select(
                        slot_t.c.scope_id, slot_t.c.slot_id, slot_t.c.call_id,
                        slot_t.c.subject_id, subject_t.c.purpose.label("subject_purpose"),
                    ).select_from(outerjoin(slot_t, subject_t, and_(
                        slot_t.c.scope_id == subject_t.c.scope_id,
                        slot_t.c.subject_id == subject_t.c.subject_id,
                    ))).where(slot_t.c.subject_id == job_id)).mappings().all()
                    for slot_row in joined_slots:
                        purpose = slot_row["subject_purpose"]
                        if purpose not in {"work", "collection"}:
                            raise _HandBackOwnerUnavailable()
                        if purpose == "work":
                            work_slots.append(slot_row)
                    for reservation in work_reservations:
                        if reservation["purpose"] not in {"work", "collection"}:
                            raise _HandBackOwnerUnavailable()
                    work_reservations = [row for row in work_reservations if row["purpose"] == "work"]

                if schema_version >= 9:
                    intent_t = tables.get("program_call_intents")
                    legacy_t = tables.get("program_legacy_links")
                    if intent_t is None or legacy_t is None:
                        raise _HandBackOwnerUnavailable()
                    work_intents = session.execute(select(
                        intent_t.c.scope_id, intent_t.c.call_id,
                        intent_t.c.subject_id, intent_t.c.purpose, intent_t.c.work_job_id,
                        intent_t.c.work_attempt_id,
                    ).where(or_(
                        intent_t.c.work_job_id == job_id,
                        and_(intent_t.c.subject_id == job_id,
                             intent_t.c.purpose == "work"),
                    ))).mappings().all()
                    legacy_links = session.execute(select(legacy_t).where(
                        legacy_t.c.job_id == job_id)).mappings().all()
                    if any(row["purpose"] != "work" or row["subject_id"] != job_id
                           or row["work_job_id"] != job_id
                           or type(row["work_attempt_id"]) is not str for row in work_intents):
                        raise _HandBackOwnerUnavailable()

                if schema_version >= 10:
                    binding_t = tables.get("program_work_projection_bindings")
                    if binding_t is None:
                        raise _HandBackOwnerUnavailable()
                    work_bindings = session.execute(select(
                        binding_t.c.scope_id, binding_t.c.call_id,
                        binding_t.c.work_job_id, binding_t.c.subject_id,
                        binding_t.c.purpose, binding_t.c.attempt_id,
                    ).where(or_(
                        binding_t.c.work_job_id == job_id,
                        and_(binding_t.c.subject_id == job_id,
                             binding_t.c.purpose == "work"),
                    ))).mappings().all()
                    if any(row["work_job_id"] != job_id or row["subject_id"] != job_id
                           or row["purpose"] != "work"
                           or type(row["attempt_id"]) is not str for row in work_bindings):
                        raise _HandBackOwnerUnavailable()

                legacy_batch_keys = {(link["scope_id"], link["batch_id"]) for link in legacy_links}
                historical_calls = []
                seen_source_calls, seen_source_slots, seen_target_calls, seen_target_slots = set(), set(), set(), set()
                for link in legacy_links:
                    if (link["job_id"] != job_id or link["subject_id"] != job_id
                            or not all(type(link[name]) is str and link[name] for name in (
                                "scope_id", "batch_id", "link_id", "call_id", "slot_id",
                                "original_attempt_id", "original_call_id", "original_slot_id"))):
                        raise _HandBackOwnerUnavailable()
                    source_call = (link["job_id"], link["original_call_id"])
                    source_slot = (link["job_id"], link["original_slot_id"])
                    target_call, target_slot = (link["scope_id"], link["call_id"]), (link["scope_id"], link["slot_id"])
                    if (source_call in seen_source_calls or source_slot in seen_source_slots
                            or target_call in seen_target_calls or target_slot in seen_target_slots):
                        raise _HandBackOwnerUnavailable()
                    seen_source_calls.add(source_call); seen_source_slots.add(source_slot)
                    seen_target_calls.add(target_call); seen_target_slots.add(target_slot)
                    historical_calls.append((link["scope_id"], link["call_id"], link["slot_id"],
                                             link["subject_id"], "work", job_id))

                projection_keys = {
                    (item.scope_id, item.call_id, item.attempt_id)
                    for item in projections_all
                }
                if len(projection_keys) != len(projections_all):
                    raise _HandBackOwnerUnavailable()
                if schema_version >= 10:
                    binding_keys = {
                        (row["scope_id"], row["call_id"], row["attempt_id"])
                        for row in work_bindings
                    }
                    intent_keys = {
                        (row["scope_id"], row["call_id"], row["work_attempt_id"])
                        for row in work_intents
                    }
                    historical_target_keys = {(item[0], item[1]) for item in historical_calls}
                    historical_intent_keys = {
                        (row["scope_id"], row["call_id"], row["work_attempt_id"]) for row in work_intents
                        if (row["scope_id"], row["call_id"]) in historical_target_keys
                    }
                    allowed_keys = projection_keys | historical_intent_keys
                    if (len(binding_keys) != len(work_bindings)
                            or len(intent_keys) != len(work_intents)
                            or binding_keys != projection_keys or intent_keys != allowed_keys):
                        raise _HandBackOwnerUnavailable()
                    original_by_target = {(item[0], item[1]): next(
                        link["original_attempt_id"] for link in legacy_links
                        if (link["scope_id"], link["call_id"]) == (item[0], item[1]))
                        for item in historical_calls}
                else:
                    history_intent_keys = {(row["scope_id"], row["call_id"]) for row in work_intents}
                    if (projections_all or work_bindings
                            or history_intent_keys - {(item[0], item[1]) for item in historical_calls}):
                        raise _HandBackOwnerUnavailable()
                original_by_target = {(item[0], item[1]): next(
                    link["original_attempt_id"] for link in legacy_links
                    if (link["scope_id"], link["call_id"]) == (item[0], item[1]))
                    for item in historical_calls}
                if any((row["scope_id"], row["call_id"]) in original_by_target
                       and row["work_attempt_id"] != original_by_target[(row["scope_id"], row["call_id"])]
                       for row in work_intents):
                    raise _HandBackOwnerUnavailable()

                if schema_version >= 8:
                    expected_calls = {(item.scope_id, item.call_id) for item in projections_all}
                    expected_slots = {(item.scope_id, item.slot_id, item.call_id) for item in projections_all}
                    legacy_call_keys = {(link["scope_id"], link["call_id"]) for link in legacy_links}
                    legacy_slot_keys = {(link["scope_id"], link["slot_id"], link["call_id"]) for link in legacy_links}
                    reservation_calls = {
                        (row["scope_id"], row["call_id"]) for row in work_reservations
                    }
                    slot_keys = {
                        (row["scope_id"], row["slot_id"], row["call_id"]) for row in work_slots
                    }
                    if (len(reservation_calls) != len(work_reservations)
                            or len(slot_keys) != len(work_slots)
                            or reservation_calls != expected_calls | legacy_call_keys
                            or slot_keys != expected_slots | legacy_slot_keys):
                        raise _HandBackOwnerUnavailable()

                current = tuple(
                    item for item in projections_all
                    if current_attempt is not None and item.attempt_id == current_attempt
                )
                if current_attempt is not None and probe["status"] == "running":
                    # A projection at the current attempt must be the complete
                    # current Work call set; historical attempts remain separate.
                    current_keys = {(item.scope_id, item.call_id) for item in current}
                    binding_current = {
                        (row["scope_id"], row["call_id"]) for row in work_bindings
                        if row["attempt_id"] == current_attempt
                    }
                    intent_current = {
                        (row["scope_id"], row["call_id"]) for row in work_intents
                        if row["work_attempt_id"] == current_attempt
                    }
                    if (len(binding_current) != sum(row["attempt_id"] == current_attempt for row in work_bindings)
                            or len(intent_current) != sum(row["work_attempt_id"] == current_attempt for row in work_intents)
                            or binding_current != current_keys or intent_current != current_keys):
                        raise _HandBackOwnerUnavailable()

                if projections_all or legacy_batch_keys:
                    try:
                        lock_work_projection_set(session, projections_all,
                            additional_work_calls=tuple(historical_calls),
                            legacy_batch_keys=tuple(legacy_batch_keys), expected_job_id=job_id,
                            include_bindings=schema_version >= 10)
                    except Exception:
                        raise _HandBackOwnerUnavailable() from None
                    if legacy_links:
                        reread_legacy = session.execute(select(legacy_t).where(
                            legacy_t.c.job_id == job_id)).mappings().all()
                        if reread_legacy != legacy_links:
                            raise _HandBackOwnerUnavailable()

                row = session.execute(select(Job).where(Job.id == job_id).with_for_update()).scalar_one_or_none()
                lease = session.get(WorkerExecutionLease, job_id, with_for_update=True)
                locked_raw = session.execute(select(cast(job_table.c.resolved, Text)).where(
                    job_table.c.id == job_id).with_for_update()).scalar_one_or_none()
                if row is None:
                    return False
                if type(locked_raw) is not str:
                    raise _HandBackOwnerUnavailable()
                if (locked_raw != raw or row.status != probe["status"]
                        or row.attempts != probe["attempts"]
                        or row.generation != probe["generation"]
                        or row.lease_owner != probe["lease_owner"]
                        or row.lease_until != probe["lease_until"]
                        or row.cancel_requested != probe["cancel_requested"]):
                    raise _HandBackOwnerUnavailable()
                if (not self._eligible_job(row) or row.status != "running"
                        or row.lease_owner != self.id):
                    return False

                try:
                    locked_root = _raw_job_object(locked_raw)
                    locked_all = work_projections_from_job(locked_raw, allow_absent=True)
                except (WorkProjectionError, TypeError, ValueError, RecursionError):
                    raise _HandBackOwnerUnavailable() from None
                if locked_all != projections_all:
                    raise _HandBackOwnerUnavailable()

                if legacy_links:
                    link_t = tables["program_legacy_links"]
                    batch_t = tables["program_census_batches"]
                    policy_t = tables["program_policy_revisions"]
                    registry_t = tables["program_subject_registry"]
                    reservation_t = tables["program_reservations"]
                    slot_t = tables["program_slots"]
                    attempt_suffix = int(current_attempt.rsplit("-", 1)[1]) if current_attempt else None
                    if run is None or attempt_suffix is None:
                        raise _HandBackOwnerUnavailable()
                    source_attempts = run.get("attempts")
                    source_reservations = run.get("budget_reservations")
                    source_slots = run.get("active_slots")
                    if any(type(value) is not list for value in (source_attempts, source_reservations, source_slots)):
                        raise _HandBackOwnerUnavailable()
                    for field, rows, key in (("attempts", source_attempts, "id"),
                                             ("budget_reservations", source_reservations, "call_id")):
                        ids = [item.get(key) if type(item) is dict else None for item in rows]
                        if any(type(value) is not str for value in ids) or len(ids) != len(set(ids)):
                            raise _HandBackOwnerUnavailable()
                    if (any(type(value) is not str for value in source_slots)
                            or len(source_slots) != len(set(source_slots))):
                        raise _HandBackOwnerUnavailable()
                    attempt_map = {item["id"]: item for item in source_attempts}
                    reservation_map = {item["call_id"]: item for item in source_reservations}
                    source_keys, source_call_keys, source_slot_keys, target_calls, target_slots = set(), set(), set(), set(), set()
                    for batch_key in sorted(legacy_batch_keys):
                        batch = session.execute(select(batch_t).where(
                            batch_t.c.scope_id == batch_key[0], batch_t.c.batch_id == batch_key[1])).mappings().one_or_none()
                        links = session.execute(select(link_t).where(
                            link_t.c.scope_id == batch_key[0], link_t.c.batch_id == batch_key[1])).mappings().all()
                        links.sort(key=lambda item: item["link_id"].encode("utf-8"))
                        if (batch is None or batch["state"] != "complete"
                                or type(batch["source_count"]) is not int or batch["source_count"] != len(links)
                                or batch["mapped_count"] != batch["source_count"] or batch["unmatched_count"] != 0
                                or batch["policy_revision"] <= 0 or type(batch["snapshot_digest"]) is not str
                                or digest([_baseline_payload(item) for item in links]) != batch["snapshot_digest"]):
                            raise _HandBackOwnerUnavailable()
                        policy = session.execute(select(policy_t).where(
                            policy_t.c.scope_id == batch_key[0],
                            policy_t.c.revision == batch["policy_revision"])).mappings().one_or_none()
                        if policy is None or policy["policy_digest"] != batch["policy_digest"]:
                            raise _HandBackOwnerUnavailable()
                        totals = {name: 0 for name in ("reserved_usd_micro", "settled_usd_micro", "unknown_usd_micro",
                            "reserved_allowance_micro", "settled_allowance_micro", "unknown_allowance_micro", "held_slots", "unknown_slots")}
                        for item in links:
                            if (any(type(item[name]) is not str or not 1 <= len(item[name]) <= (64 if name == "job_id" else 96)
                                    or any(not "!" <= char <= "~" for char in item[name])
                                    for name in ("scope_id", "batch_id", "link_id", "job_id", "original_attempt_id",
                                                 "original_call_id", "original_slot_id", "subject_id", "call_id", "slot_id"))
                                    or not _is_lower_hex(item["original_digest"], 64)
                                    or digest(_baseline_payload(item)) != item["original_digest"]):
                                raise _HandBackOwnerUnavailable()
                            identity = (item["job_id"], item["original_attempt_id"], item["original_call_id"], item["original_slot_id"])
                            source_call = (item["job_id"], item["original_call_id"])
                            source_slot = (item["job_id"], item["original_slot_id"])
                            target_call = (item["scope_id"], item["call_id"])
                            target_slot = (item["scope_id"], item["slot_id"])
                            if (identity in source_keys or source_call in source_call_keys or source_slot in source_slot_keys
                                    or target_call in target_calls or target_slot in target_slots):
                                raise _HandBackOwnerUnavailable()
                            source_keys.add(identity); source_call_keys.add(source_call); source_slot_keys.add(source_slot)
                            target_calls.add(target_call); target_slots.add(target_slot)
                            reserve_usd, reserve_allowance = item["baseline_reserved_usd_micro"], item["baseline_reserved_allowance_micro"]
                            actual_usd, actual_allowance = item["baseline_actual_usd_micro"], item["baseline_actual_allowance_micro"]
                            if (type(reserve_usd) is not int or type(reserve_allowance) is not int
                                    or reserve_usd < 0 or reserve_allowance < 0
                                    or ((actual_usd is None) != (actual_allowance is None))
                                    or any(type(value) is not int or value < 0 for value in (actual_usd, actual_allowance) if value is not None)):
                                raise _HandBackOwnerUnavailable()
                            if (item["baseline_state"], item["baseline_slot_state"], actual_usd is None) == ("reserved", "held", True):
                                totals["reserved_usd_micro"] += reserve_usd
                                totals["reserved_allowance_micro"] += reserve_allowance
                                totals["held_slots"] += 1
                            elif (item["baseline_state"], item["baseline_slot_state"], actual_usd is None) == ("settled", "released", False):
                                totals["settled_usd_micro"] += actual_usd
                                totals["settled_allowance_micro"] += actual_allowance
                            elif item["baseline_state"] == "unknown" and item["baseline_slot_state"] == "unknown":
                                totals["unknown_usd_micro"] += reserve_usd if actual_usd is None else max(reserve_usd, actual_usd)
                                totals["unknown_allowance_micro"] += reserve_allowance if actual_allowance is None else max(reserve_allowance, actual_allowance)
                                totals["unknown_slots"] += 1
                            else:
                                raise _HandBackOwnerUnavailable()
                        if any(type(batch[name]) is not int or batch[name] != value for name, value in totals.items()):
                            raise _HandBackOwnerUnavailable()
                        if any((item["scope_id"], item["call_id"]) in {
                                (projection.scope_id, projection.call_id) for projection in current
                            } or (item["scope_id"], item["slot_id"]) in {
                                (projection.scope_id, projection.slot_id) for projection in current
                            } for item in links):
                            raise _HandBackOwnerUnavailable()
                        for link in (item for item in links if item["job_id"] == job_id):
                            source_attempt = attempt_map.get(link["original_attempt_id"])
                            source_reservation = reservation_map.get(link["original_call_id"])
                            if (source_attempt is None or source_attempt.get("id") != link["original_attempt_id"]
                                    or source_reservation is None
                                    or source_reservation.get("attempt_id") != link["original_attempt_id"]
                                    or source_reservation.get("slot_id") != link["original_slot_id"]
                                    or source_attempt.get("state") not in {"preparing", "running", "awaiting_activation", "interrupted", "uncertain", "failed", "cancelled", "completed", "settled"}
                                    or link["original_attempt_id"] == current_attempt
                                    or not link["original_attempt_id"].startswith(job_id + "-")
                                    or int(link["original_attempt_id"].rsplit("-", 1)[1]) >= attempt_suffix
                                    or source_slots.count(link["original_slot_id"]) != 1
                                    or (link["scope_id"], link["call_id"]) in {
                                        (projection.scope_id, projection.call_id) for projection in current
                                    } or (link["scope_id"], link["slot_id"], link["call_id"]) in {
                                        (projection.scope_id, projection.slot_id, projection.call_id) for projection in current
                                    }):
                                raise _HandBackOwnerUnavailable()
                            registry = session.execute(select(registry_t).where(
                                registry_t.c.scope_id == link["scope_id"], registry_t.c.subject_id == job_id,
                                registry_t.c.purpose == "work")).mappings().one_or_none()
                            reservation = session.execute(select(reservation_t).where(
                                reservation_t.c.scope_id == link["scope_id"], reservation_t.c.call_id == link["call_id"])).mappings().one_or_none()
                            slot = session.execute(select(slot_t).where(
                                slot_t.c.scope_id == link["scope_id"], slot_t.c.slot_id == link["slot_id"])).mappings().one_or_none()
                            job_mapping = {column.name: getattr(row, column.name) for column in job_table.columns}
                            _validate_locked_legacy_binding(link, batch, registry, job_mapping, reservation, slot)

                now = time.time()
                projected_history = bool(projections_all or legacy_links)
                if lease is not None:
                    try:
                        self._check_lease_finite(row, lease)
                    except PlatformError as error:
                        if error.code == "owner_unavailable":
                            raise _HandBackOwnerUnavailable() from None
                        raise
                    expected_fence = fence if fence is not None else (
                        None if projected_history else self.execution_fences.get(job_id)
                    )
                    if projected_history and (type(fence) is not str or not fence):
                        _fail("execution_fenced", 409, "Execution reservation is no longer current")
                    if (lease.phase != "started" or lease.fence != expected_fence
                            or row.lease_until is None or lease.lease_until != row.lease_until
                            or lease.execution_deadline is None or lease.lease_until <= now
                            or lease.execution_deadline <= now):
                        if projected_history and fence is not None and lease.fence != fence:
                            _fail("execution_fenced", 409, "Execution reservation is no longer current")
                        return False
                elif projected_history:
                    raise _HandBackOwnerUnavailable()
                elif fence is not None:
                    return False

                if projected_history:
                    if (current_attempt is None or lease is None
                            or current_attempt_row is None
                            or current_attempt_state not in {"preparing", "awaiting_activation", "running"}
                            or hashlib.sha256(lease.fence.encode("utf-8")).hexdigest()
                            != current_attempt_row.get("fence_sha256")):
                        raise _HandBackOwnerUnavailable()
                    if current:
                        try:
                            for projection in current:
                                _compare_actual_work_projection(
                                    session, scope_id=projection.scope_id,
                                    call_id=projection.call_id, allow_cancel_requested=True,
                                )
                        except Exception:
                            raise _HandBackOwnerUnavailable() from None

                next_projections = {}
                unresolved_work = False
                if current:
                    for projection in current:
                        state = (projection.intent_state, projection.reservation_state,
                                 projection.slot_state)
                        if state == ("known", "settled", "released"):
                            continue
                        if state not in {
                            ("committed", "reserved", "held"),
                            ("unknown", "unknown", "unknown"),
                        }:
                            raise _HandBackOwnerUnavailable()
                        unresolved_work = True
                        try:
                            payload = _mark_work_projection_unknown_in_transaction(
                                database, session, projection,
                                job_id=job_id, attempt_id=current_attempt,
                            )
                        except Exception:
                            raise _HandBackOwnerUnavailable() from None
                        if payload is not None:
                            next_projections[(projection.scope_id, projection.call_id)] = payload

                if row.cancel_requested:
                    row.status, row.error_code = "cancelled", "cancelled"
                elif unresolved_work or not replay_safe(row.resolved):
                    row.status, row.error_code = "blocked", "execution_uncertain"
                else:
                    row.status, row.error_code, row.next_attempt_at = "retry_wait", "worker_shutdown", now
                    if not projected_history:
                        # The legacy decrement is allowed only after the exact
                        # schema/JSON/relational empty-set proof above.
                        row.attempts = max(0, row.attempts - 1)

                if next_projections:
                    run_locked = locked_root.get("repository_run")
                    entries = run_locked.get("program_ledger_projections_v1") if type(run_locked) is dict else None
                    if type(entries) is not list:
                        raise _HandBackOwnerUnavailable()
                    run_locked["program_ledger_projections_v1"] = [
                        next_projections.get((entry.get("scope_id"), entry.get("call_id")), entry)
                        if type(entry) is dict else entry for entry in entries
                    ]
                    row.resolved = locked_root

                row.lease_owner, row.lease_until, row.updated_at = None, None, now
                if lease is not None:
                    lease.phase, lease.lease_until, lease.updated_at = "finished", 0, now
                revoke_execution(session, row.id)
                if row.status in TERMINAL:
                    row.finished_at = now
                self.platform.db.audit(session, self.id, "task.handed_back", row.id, {
                    "status": row.status, "error_code": row.error_code,
                    "work_state": "unknown_held" if unresolved_work else "unchanged",
                })
                succeeded = True
                succeeded_fence = lease.fence if lease is not None else None
        except PlatformError:
            raise
        except _HandBackOwnerUnavailable:
            _fail("execution_owner_state_unavailable", 503,
                  "Execution owner state is unavailable; retry the request")
        except Exception:
            _fail("execution_owner_state_unavailable", 503,
                  "Execution owner state is unavailable; retry the request")

        if succeeded:
            self._forget_fence(job_id, succeeded_fence)
        return succeeded

    def finish(self, job_id: str, artifact: tuple[str, str] | None = None, error: RuntimeFailure | None = None, cancelled: bool = False, fence: str | None = None):
        with self.platform.db.sessions.begin() as session:
            self.platform.db.authority_lock(session)
            row = session.execute(select(Job).where(Job.id == job_id).with_for_update()).scalar_one()
            lease = session.get(WorkerExecutionLease, job_id, with_for_update=True)
            now = time.time()
            if not self._eligible_job(row):
                return
            if row.status != "running" or row.lease_owner != self.id or row.lease_until is None or row.lease_until <= now:
                # A late process cannot publish after its lease has been reclaimed.
                return
            if lease is not None:
                try:
                    self._check_lease_finite(row, lease)
                except PlatformError:
                    return
                if (lease.phase != "started" or lease.fence != (fence or self.execution_fences.get(job_id))
                    or lease.lease_until != row.lease_until or lease.execution_deadline is None
                    or lease.execution_deadline <= now):
                    return
            if lease is None and fence is not None:
                return
            denied = None
            try:
                self.platform.authorize_existing_job(session, row,
                    purpose="active" if artifact else "observe")
            except PlatformError as exc:
                denied = exc.code
            if lease is not None and row.resolved.get("model", {}).get("connection", {}).get("kind") == "model-gateway":
                try:
                    check_execution_binding(self.platform, session, row)
                except PlatformError:
                    # A stopped/revised gateway cannot publish a result from its
                    # former binding. Leave state for explicit recovery/operator action.
                    return
                from .authority import execution_authority_current
                if not execution_authority_current(self.platform, session, row, lease,
                    lease.workload_id, now):
                    return
            if artifact:
                row.artifact_key, row.artifact_sha256 = artifact
            if denied:
                row.status, row.error_code = "blocked", denied
            elif error and error.code == "external_execution_uncertain":
                # A cancelled mutable repository attempt may already have
                # produced a commit or crossed another external effect boundary.
                row.status, row.error_code = "blocked", "execution_uncertain"
            elif cancelled or row.cancel_requested:
                row.status, row.error_code = "cancelled", "cancelled"
            elif artifact:
                row.status, row.error_code = "completed", None
                # Persistence happened before this transaction. The projection is built
                # here from constants; model/tool strings cannot enter this surface.
                row.public_projection = {"receipt": row.receipt, "status": "available"}
                row.released_at = now
                self.platform.db.audit(session, self.id, "result.release_authorized", row.id, {"grant_id": row.grant_id, "generation": row.generation, "artifact_sha256": row.artifact_sha256})
            elif error and (error.retryable and any(not tool.get("read_only", tool["kind"] == "kubernetes-read") for tool in row.resolved.get("tools", []))):
                # Whatever path discovered it, an execution whose effect is unknown waits for an operator.
                row.status, row.error_code = "blocked", "execution_uncertain"
            elif error and error.retryable and row.resolved["grant"].get("on_outage", "queue") == "queue" and row.attempts < int(row.resolved["grant"].get("max_attempts", self.platform.settings.max_attempts)):
                row.status, row.error_code = "retry_wait", error.code
                row.next_attempt_at = now + min(300, 2 ** row.attempts * 5)
            else:
                row.status, row.error_code = "failed", error.code if error else "execution_failed"
            row.lease_owner, row.lease_until, row.updated_at = None, None, now
            if lease is not None:
                lease.phase, lease.lease_until, lease.updated_at = "finished", 0, now
            revoke_execution(session, row.id)
            self._forget_fence(row.id, lease.fence if lease is not None else None)
            if row.status in TERMINAL:
                row.finished_at = now
            self.platform.db.audit(session, self.id, "task.state_changed", row.id, {"status": row.status, "error_code": row.error_code})
