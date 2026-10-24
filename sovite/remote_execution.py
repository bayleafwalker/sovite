"""Authenticated remote execution operations over the existing lease owner."""
from __future__ import annotations

import copy
import math
import time
from typing import Any

from sqlalchemy import select

from .authority import check_execution_binding, execution_authority_current, revoke_execution, refresh_execution
from .db import Job, WorkerExecutionLease, WorkerExecutionOperation
from .lease_service import (
    ExecutionContext,
    ExecutionPrincipal,
    LeaseService,
    _FENCE_RE,
    _GENERATION_RE,
    _INSTANCE_RE,
    _OP_DIGEST_SCHEMA,
    _OPERATION_RE,
    _WORKLOAD_RE,
    _canonical_bytes,
    _digest,
    _validate_principal,
    replay_safe,
)
from .service import Platform, PlatformError, TERMINAL


MAX_RESULT_BYTES = 48 * 1024
_RESULT_DEPTH = 32
_RESULT_NODES = 10_000
_OPERATION_KINDS = {"pull", "start", "renew", "handback", "complete"}
_RESULT_STATES = {
    "idle", "reserved", "started", "already_started", "renewed", "handed_back",
    "completed", "failed", "cancelled", "retry_wait", "blocked",
}
_ERROR_CODES = {
    "adapter_credential_missing", "agent_endpoint_missing", "authorization_revoked",
    "execution_failed", "execution_timeout", "external_execution_uncertain",
    "incompatible_demo_configuration", "internal_execution_error", "invalid_tool_calls",
    "model_empty_result", "model_gateway_adapter_unavailable", "model_identity_missing",
    "model_identity_mismatch", "model_identity_protocol_unsupported", "model_identity_unpinned",
    "model_output_filtered", "model_output_truncated", "model_protocol_error", "model_trust_missing",
    "native_empty_result", "native_incomplete_result", "native_process_requires_external_isolation",
    "native_provider_mismatch", "native_protocol_error", "native_result_missing", "native_result_too_large",
    "native_runtime_failed", "native_runtime_unavailable", "native_tools_unsupported", "step_limit_reached",
    "subscription_credential_changed", "subscription_credential_invalid", "subscription_credential_missing",
    "subscription_credential_unavailable", "subscription_profile_busy", "tool_agent_requires_chat_api",
    "tool_credential_missing", "tool_operation_denied", "unsupported_harness", "unsupported_model_adapter",
    "unsupported_tool", "upstream_protocol_error", "upstream_rejected", "upstream_response_too_large",
    "upstream_unavailable",
}


def _request_digest(kind: str, principal: ExecutionPrincipal, request: dict[str, Any]) -> str:
    if kind not in {"renew", "handback", "complete"}:
        raise PlatformError("invalid_request", "Execution owner request is invalid", 422)
    try:
        return _digest({
            "schema_version": _OP_DIGEST_SCHEMA,
            "operation_kind": kind,
            "workload_id": principal.workload_id,
            "compute_id": principal.compute_id,
            "request": request,
        })
    except ValueError:
        raise PlatformError("invalid_request", "Execution owner request is invalid", 422) from None


def _validate_context(context: ExecutionContext, principal: ExecutionPrincipal) -> None:
    _validate_principal(principal)
    if (
        not isinstance(context, ExecutionContext)
        or not isinstance(context.instance_id, str)
        or _INSTANCE_RE.fullmatch(context.instance_id) is None
        or not isinstance(context.operation_id, str)
        or _OPERATION_RE.fullmatch(context.operation_id) is None
        or not isinstance(context.job_id, str)
        or not 1 <= len(context.job_id) <= 64
        or not isinstance(context.generation, str)
        or _GENERATION_RE.fullmatch(context.generation) is None
        or not isinstance(context.fence, str)
        or _FENCE_RE.fullmatch(context.fence) is None
    ):
        raise PlatformError("invalid_request", "Execution owner request is invalid", 422)


def _valid_json(value: Any) -> bool:
    stack = [(value, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > _RESULT_NODES or depth > _RESULT_DEPTH:
            return False
        if item is None or type(item) is bool:
            continue
        if type(item) is int:
            if abs(item) > 9_007_199_254_740_991:
                return False
            continue
        if isinstance(item, float):
            if not math.isfinite(item):
                return False
            continue
        if isinstance(item, str):
            if any(0xD800 <= ord(char) <= 0xDFFF for char in item):
                return False
            continue
        if isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
            continue
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or any(0xD800 <= ord(char) <= 0xDFFF for char in key):
                    return False
                stack.append((child, depth + 1))
            continue
        return False
    return True


class RemoteExecution:
    """Remote worker surface; all writes use the Platform's existing owner rows."""

    def __init__(self, platform: Platform):
        self.platform = platform
        self.leases = LeaseService(platform, "remote-execution-api", {})

    def _owner(self, principal: ExecutionPrincipal, context: ExecutionContext) -> str:
        if principal.compute_id != self.platform.settings.compute_id:
            raise PlatformError("execution_scope_denied", "Execution owner scope is not permitted", 403)
        return self.leases._owner_for(principal, context.instance_id)

    def _job_lease(self, session, principal: ExecutionPrincipal, context: ExecutionContext, owner: str):
        job = session.execute(
            select(Job).where(Job.id == context.job_id).with_for_update()
        ).scalar_one_or_none()
        if job is None:
            raise PlatformError("execution_scope_denied", "Execution owner scope is not permitted", 403)
        lease = session.get(WorkerExecutionLease, context.job_id, with_for_update=True)
        if lease is None:
            raise PlatformError("execution_fenced", "Execution context is no longer current", 409)
        self.leases._check_lease_finite(job, lease)
        if lease.workload_id != principal.workload_id or lease.instance_id != context.instance_id:
            raise PlatformError("execution_scope_denied", "Execution owner scope is not permitted", 403)
        if (
            context.generation != job.generation
            or context.generation != lease.generation
            or context.fence != lease.fence
            or not self.leases._same_owner(job, lease, principal, context.instance_id, owner)
        ):
            raise PlatformError("execution_fenced", "Execution context is no longer current", 409)
        return job, lease

    def _require_started(
        self, job: Job, lease: WorkerExecutionLease, now: float, *, allow_cancelled: bool = False
    ) -> None:
        if (
            job.status != "running"
            or lease.phase != "started"
            or (job.cancel_requested and not allow_cancelled)
            or job.lease_owner is None
            or job.lease_until is None
            or isinstance(job.lease_until, bool)
            or not isinstance(job.lease_until, (int, float))
            or not math.isfinite(job.lease_until)
            or job.lease_until <= now
            or lease.lease_until != job.lease_until
            or lease.execution_deadline is None
            or isinstance(lease.execution_deadline, bool)
            or not isinstance(lease.execution_deadline, (int, float))
            or not math.isfinite(lease.execution_deadline)
            or lease.execution_deadline <= now
        ):
            raise PlatformError("execution_fenced", "Execution context is no longer current", 409)

    def _prior(self, session, principal, context, kind, digest):
        prior = self.leases._journal(
            session,
            principal,
            context.instance_id,
            context.operation_id,
            kind=kind,
            digest=digest,
            now=time.time(),
        )
        if prior is None:
            return None
        if (
            prior.job_id != context.job_id
            or prior.fence != context.fence
            or not isinstance(prior.result, dict)
            or prior.result.get("state") not in _RESULT_STATES
        ):
            raise PlatformError("execution_fenced", "Execution context is no longer current", 409)
        self.leases._check_response_size(prior.result)
        return copy.deepcopy(prior.result)

    def _record(self, session, principal, context, kind, digest, result, now):
        self.leases._check_response_size(result)
        session.add(WorkerExecutionOperation(
            workload_id=principal.workload_id,
            instance_id=context.instance_id,
            operation_id=context.operation_id,
            operation_kind=kind,
            request_digest=digest,
            job_id=context.job_id,
            fence=context.fence,
            result=copy.deepcopy(result),
            created_at=now,
            updated_at=now,
        ))

    def renew(self, principal: ExecutionPrincipal, context: ExecutionContext) -> dict:
        _validate_context(context, principal)
        owner = self._owner(principal, context)
        request = {
            "instance_id": context.instance_id,
            "operation_id": context.operation_id,
            "job_id": context.job_id,
            "generation": context.generation,
            "fence": context.fence,
        }
        digest = _request_digest("renew", principal, request)
        with self.platform.db.sessions.begin() as session:
            self.platform.db.authority_lock(session)
            self.platform.active(session)
            prior = self._prior(session, principal, context, "renew", digest)
            if prior is not None:
                return prior
            job, lease = self._job_lease(session, principal, context, owner)
            now = time.time()
            self._require_started(job, lease, now)
            self.leases._authorize_current(session, job, now)
            gateway = job.resolved.get("model", {}).get("connection", {}).get("kind") == "model-gateway"
            if gateway and not refresh_execution(self.platform, session, job, context.fence, now):
                raise PlatformError("execution_fenced", "Execution context is no longer current", 409)
            until = min(now + self.platform.settings.lease_seconds, lease.execution_deadline)
            if not math.isfinite(until) or until <= now:
                raise PlatformError("execution_fenced", "Execution context is no longer current", 409)
            job.lease_until = lease.lease_until = until
            job.updated_at = lease.updated_at = now
            result = {"state": "renewed", "job_id": job.id, "generation": job.generation,
                      "fence": lease.fence, "lease_until": until, "deadline": lease.execution_deadline}
            self._record(session, principal, context, "renew", digest, result, now)
            self.platform.db.audit(session, self.leases.id, "task.execution_renewed", job.id,
                {"workload_id": principal.workload_id, "compute_id": principal.compute_id,
                 "instance_id": context.instance_id, "generation": job.generation,
                 "fence": lease.fence, "operation_id": context.operation_id})
            return result

    def status(self, principal: ExecutionPrincipal, context: ExecutionContext) -> dict:
        _validate_context(context, principal)
        owner = self._owner(principal, context)
        with self.platform.db.sessions() as session:
            job = session.get(Job, context.job_id)
            lease = session.get(WorkerExecutionLease, context.job_id)
            if job is None or lease is None:
                raise PlatformError("execution_scope_denied", "Execution owner scope is not permitted", 403)
            self.leases._check_lease_finite(job, lease)
            if lease.workload_id != principal.workload_id or lease.instance_id != context.instance_id:
                raise PlatformError("execution_scope_denied", "Execution owner scope is not permitted", 403)
            tuple_matches = (
                context.generation == job.generation == lease.generation
                and context.fence == lease.fence
            )
            if not tuple_matches:
                state = "superseded"
            elif lease.phase == "finished" or job.status != "running":
                state = "finished"
            elif job.lease_owner != owner:
                state = "superseded"
            else:
                now = time.time()
                if lease.lease_until <= now or (
                    lease.execution_deadline is not None and lease.execution_deadline <= now
                ):
                    state = "expired"
                else:
                    state = "current"
            if job.status not in {"queued", "running", "retry_wait", "completed", "failed", "cancelled", "blocked"}:
                raise PlatformError("owner_unavailable", "Execution owner state is unavailable", 503)
            result = {
                "state": state,
                "job_id": context.job_id,
                "generation": context.generation,
                "fence": context.fence,
                "phase": lease.phase,
                "job_status": job.status,
                "lease_until": lease.lease_until,
                "deadline": lease.execution_deadline,
            }
            self.leases._check_response_size(result)
            return result

    def handback(self, principal: ExecutionPrincipal, context: ExecutionContext) -> dict:
        _validate_context(context, principal)
        owner = self._owner(principal, context)
        request = {
            "instance_id": context.instance_id,
            "operation_id": context.operation_id,
            "job_id": context.job_id,
            "generation": context.generation,
            "fence": context.fence,
        }
        digest = _request_digest("handback", principal, request)
        with self.platform.db.sessions.begin() as session:
            self.platform.db.authority_lock(session)
            self.platform.active(session)
            prior = self._prior(session, principal, context, "handback", digest)
            if prior is not None:
                return prior
            job, lease = self._job_lease(session, principal, context, owner)
            now = time.time()
            self._require_started(job, lease, now, allow_cancelled=True)
            if job.cancel_requested:
                job.status, job.error_code = "cancelled", "cancelled"
                state = "cancelled"
            elif not replay_safe(job.resolved):
                job.status, job.error_code = "blocked", "execution_uncertain"
                state = "blocked"
            else:
                job.status, job.error_code, job.next_attempt_at = "retry_wait", "worker_shutdown", now
                job.attempts = max(0, job.attempts - 1)
                state = "retry_wait"
            job.lease_owner, job.lease_until, job.updated_at = None, None, now
            lease.phase, lease.lease_until, lease.updated_at = "finished", 0, now
            revoke_execution(session, job.id)
            if job.status in TERMINAL:
                job.finished_at = now
            result = {"state": "handed_back", "job_id": job.id, "generation": job.generation,
                      "fence": lease.fence, "disposition": state}
            self._record(session, principal, context, "handback", digest, result, now)
            self.platform.db.audit(session, self.leases.id, "task.execution_handed_back", job.id,
                {"workload_id": principal.workload_id, "compute_id": principal.compute_id,
                 "instance_id": context.instance_id, "generation": job.generation,
                 "fence": lease.fence, "operation_id": context.operation_id,
                 "disposition": state})
            return result

    def _completed_prior(self, principal, context, digest):
        owner = self._owner(principal, context)
        with self.platform.db.sessions.begin() as session:
            self.platform.db.authority_lock(session)
            self.platform.active(session)
            prior = self._prior(session, principal, context, "complete", digest)
            if prior is not None:
                return prior
            job, lease = self._job_lease(session, principal, context, owner)
            now = time.time()
            self._require_started(job, lease, now, allow_cancelled=True)
            self.platform.authorize_existing_job(session, job)
        return None

    def complete(
        self,
        principal: ExecutionPrincipal,
        context: ExecutionContext,
        *,
        outcome: str,
        result_value: dict[str, Any] | None,
        error_code: str | None,
    ) -> dict:
        _validate_context(context, principal)
        if outcome not in {"completed", "failed", "retryable_failure", "cancelled"}:
            raise PlatformError("invalid_request", "Execution owner request is invalid", 422)
        if outcome == "completed":
            if error_code is not None or not isinstance(result_value, dict) or not _valid_json(result_value):
                raise PlatformError("invalid_request", "Execution owner request is invalid", 422)
            try:
                result_bytes = _canonical_bytes(result_value)
            except ValueError:
                raise PlatformError("invalid_request", "Execution owner request is invalid", 422) from None
            if len(result_bytes) > MAX_RESULT_BYTES:
                raise PlatformError("execution_payload_too_large", "Execution result exceeds the bounded response size", 413)
        elif result_value is not None or (outcome == "cancelled" and error_code is not None):
            raise PlatformError("invalid_request", "Execution owner request is invalid", 422)
        elif outcome in {"failed", "retryable_failure"} and error_code not in _ERROR_CODES:
            raise PlatformError("invalid_request", "Execution owner request is invalid", 422)
        request = {
            "instance_id": context.instance_id,
            "operation_id": context.operation_id,
            "job_id": context.job_id,
            "generation": context.generation,
            "fence": context.fence,
            "outcome": outcome,
            "result": result_value,
            "error_code": error_code,
        }
        digest = _request_digest("complete", principal, request)
        prior = self._completed_prior(principal, context, digest)
        if prior is not None:
            return prior
        artifact = self.platform.store.put(context.job_id, result_value) if outcome == "completed" else None
        owner = self._owner(principal, context)
        with self.platform.db.sessions.begin() as session:
            self.platform.db.authority_lock(session)
            self.platform.active(session)
            prior = self._prior(session, principal, context, "complete", digest)
            if prior is not None:
                return prior
            job, lease = self._job_lease(session, principal, context, owner)
            now = time.time()
            self._require_started(job, lease, now, allow_cancelled=True)
            denied = None
            try:
                self.platform.authorize_existing_job(session, job)
            except PlatformError as exc:
                denied = exc.code
            gateway = job.resolved.get("model", {}).get("connection", {}).get("kind") == "model-gateway"
            if gateway:
                try:
                    check_execution_binding(self.platform, session, job)
                except PlatformError:
                    raise PlatformError("execution_fenced", "Execution context is no longer current", 409) from None
                if not execution_authority_current(self.platform, session, job, lease, principal.workload_id, now):
                    raise PlatformError("execution_fenced", "Execution context is no longer current", 409)
            if denied:
                job.status, job.error_code = "blocked", denied
            elif outcome == "cancelled" or job.cancel_requested:
                job.status, job.error_code = "cancelled", "cancelled"
            elif outcome == "completed" and artifact is not None:
                job.artifact_key, job.artifact_sha256 = artifact
                job.status, job.error_code = "completed", None
                job.public_projection = {"receipt": job.receipt, "status": "available"}
                job.released_at = now
                self.platform.db.audit(session, self.leases.id, "result.release_authorized", job.id,
                    {"grant_id": job.grant_id, "generation": job.generation,
                     "artifact_sha256": job.artifact_sha256})
            elif error_code == "external_execution_uncertain":
                job.status, job.error_code = "blocked", "execution_uncertain"
            elif outcome == "retryable_failure" and replay_safe(job.resolved) and job.resolved["grant"].get("on_outage", "queue") == "queue" and job.attempts < int(job.resolved["grant"].get("max_attempts", self.platform.settings.max_attempts)):
                job.status, job.error_code = "retry_wait", error_code
                job.next_attempt_at = now + min(300, 2 ** job.attempts * 5)
            elif outcome == "retryable_failure" and not replay_safe(job.resolved):
                job.status, job.error_code = "blocked", "execution_uncertain"
            else:
                job.status, job.error_code = "failed", error_code or "execution_failed"
            job.lease_owner, job.lease_until, job.updated_at = None, None, now
            lease.phase, lease.lease_until, lease.updated_at = "finished", 0, now
            revoke_execution(session, job.id)
            self.leases._forget_fence(job.id, lease.fence)
            if job.status in TERMINAL:
                job.finished_at = now
            state = job.status
            response = {"state": state, "job_id": job.id, "generation": job.generation,
                        "fence": lease.fence, "error_code": job.error_code}
            if artifact is not None and job.status == "completed":
                response["artifact_sha256"] = job.artifact_sha256
            self._record(session, principal, context, "complete", digest, response, now)
            self.platform.db.audit(session, self.leases.id, "task.execution_completed", job.id,
                {"workload_id": principal.workload_id, "compute_id": principal.compute_id,
                 "instance_id": context.instance_id, "generation": job.generation,
                 "fence": lease.fence, "operation_id": context.operation_id,
                 "status": job.status, "error_code": job.error_code})
            return response

    def lookup(self, principal: ExecutionPrincipal, *, instance_id: str, operation_id: str) -> dict:
        _validate_principal(principal)
        if not isinstance(instance_id, str) or _INSTANCE_RE.fullmatch(instance_id) is None or not isinstance(operation_id, str) or _OPERATION_RE.fullmatch(operation_id) is None:
            raise PlatformError("invalid_request", "Execution owner request is invalid", 422)
        with self.platform.db.sessions() as session:
            row = session.scalar(select(WorkerExecutionOperation).where(
                WorkerExecutionOperation.workload_id == principal.workload_id,
                WorkerExecutionOperation.instance_id == instance_id,
                WorkerExecutionOperation.operation_id == operation_id,
            ))
            if row is None:
                raise PlatformError("not_found", "Execution operation was not found", 404)
            if row.operation_kind not in _OPERATION_KINDS or not isinstance(row.result, dict):
                raise PlatformError("owner_unavailable", "Execution owner state is unavailable", 503)
            state = row.result.get("state")
            if state not in _RESULT_STATES:
                raise PlatformError("owner_unavailable", "Execution owner state is unavailable", 503)
            response = {
                "state": "found",
                "operation_id": row.operation_id,
                "operation_kind": row.operation_kind,
                "job_id": row.job_id,
                "fence": row.fence,
                "result_state": state,
            }
            self.leases._check_response_size(response)
            return response
