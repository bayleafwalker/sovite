"""Bounded API between execution workloads and an enrolled resource gateway."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import uuid

from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .authority import execution_authority_current
from .flow import check_destination, class_ranks
from .db import (Job, ResourceGatewayEnrollment, ResourceOutputGrant,
                 ResourceOwnerRequest, ResourceWorkloadScope, WorkerExecutionLease)
from .lease_service import ExecutionContext, ExecutionPrincipal
from .remote_execution import RemoteExecution


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Enroll(StrictBody):
    gateway_id: str = Field(min_length=1, max_length=96)
    tenant_id: str = Field(min_length=1, max_length=128)
    workspace_id: str = Field(min_length=1, max_length=128)
    domain_id: str = Field(min_length=1, max_length=128)
    expires_at: float


class WorkloadScope(StrictBody):
    workload_id: str = Field(min_length=1, max_length=160)
    gateway_id: str = Field(min_length=1, max_length=96)
    tenant_id: str = Field(min_length=1, max_length=128)
    workspace_id: str = Field(min_length=1, max_length=128)
    domain_id: str = Field(min_length=1, max_length=128)
    observation_id: str = Field(min_length=1, max_length=128)
    namespace: str = Field(min_length=1, max_length=63, pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    pod_name: str = Field(min_length=1, max_length=253, pattern=r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")


class Submit(StrictBody):
    job_id: str = Field(min_length=1, max_length=64)
    instance_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    generation: str = Field(pattern=r"^sha256:[0-9a-f]{64}$", min_length=71, max_length=71)
    fence: str = Field(pattern=r"^[0-9a-f]{32}$", min_length=32, max_length=32)
    operation_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class Binding(StrictBody):
    request_id: str = Field(min_length=1, max_length=64)
    fence_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class Poll(StrictBody):
    after: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class Current(Binding):
    policy_revision: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    destination: str = Field(min_length=1, max_length=96)
    actor: str = Field(min_length=1, max_length=160)


class Result(Binding):
    observation_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    observation_b64: str = Field(max_length=6000)
    release: str = Field(pattern=r"^(allowed|denied)$")
    policy_revision: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    destination: str = Field(min_length=1, max_length=96)
    actor: str = Field(min_length=1, max_length=160)


class OutputGrant(StrictBody):
    workload_id: str = Field(min_length=1, max_length=160)
    principal_id: str = Field(min_length=1, max_length=160)
    tenant_id: str = Field(min_length=1, max_length=128)
    workspace_id: str = Field(min_length=1, max_length=128)
    domain_id: str = Field(min_length=1, max_length=128)
    observation_id: str = Field(min_length=1, max_length=128)
    policy_revision: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ResultRead(StrictBody):
    request_id: str = Field(min_length=1, max_length=64)
    instance_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    job_id: str = Field(min_length=1, max_length=64)
    generation: str = Field(pattern=r"^sha256:[0-9a-f]{64}$", min_length=71, max_length=71)
    fence: str = Field(pattern=r"^[0-9a-f]{32}$", min_length=32, max_length=32)
    operation_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


def install_routes(app, platform, settings):
    def operator(authorization: str = Header(default="")):
        from .api import token_identity
        if not authorization.startswith("Bearer "):
            raise HTTPException(403, "operator_required")
        identity = token_identity(authorization[7:], settings)
        if identity is None or identity[1] != "operator":
            raise HTTPException(403, "operator_required")
        return identity[0]

    def workload(authorization: str = Header(default="")):
        if not authorization.startswith("Bearer "):
            raise HTTPException(403, "workload_denied")
        token = authorization[7:]
        for key, expected in settings.execution_tokens.items():
            if secrets.compare_digest(token.encode(), expected.encode()):
                return key
        raise HTTPException(403, "workload_denied")

    def gateway(authorization: str = Header(default="")):
        if not authorization.startswith("Bearer "):
            raise HTTPException(403, "gateway_denied")
        token = authorization[7:]
        if not token:
            raise HTTPException(403, "gateway_denied")
        now = time.time()
        with platform.db.sessions() as session:
            rows = session.scalars(select(ResourceGatewayEnrollment).where(
                ResourceGatewayEnrollment.revoked.is_(False),
                ResourceGatewayEnrollment.expires_at > now,
                ResourceGatewayEnrollment.audience == "resource-gateway")).all()
            for row in rows:
                if secrets.compare_digest(row.token_digest, hashlib.sha256(token.encode()).hexdigest()):
                    return row.id
        raise HTTPException(403, "gateway_denied")

    @app.post("/internal/resource-owner/v1/enrollments")
    def enroll(body: Enroll, _operator=Depends(operator)):
        now = time.time()
        if body.expires_at <= now or body.expires_at > now + 90 * 86400:
            raise HTTPException(422, "invalid_expiry")
        token = secrets.token_urlsafe(32)
        row = ResourceGatewayEnrollment(id=uuid.uuid4().hex, gateway_id=body.gateway_id,
            audience="resource-gateway", tenant_id=body.tenant_id, workspace_id=body.workspace_id,
            domain_id=body.domain_id, token_digest=hashlib.sha256(token.encode()).hexdigest(),
            expires_at=body.expires_at, revoked=False, created_at=now)
        with platform.db.sessions.begin() as session:
            platform.db.authority_lock(session)
            session.add(row)
        return {"enrollment_id": row.id, "gateway_id": row.gateway_id,
                "audience": row.audience, "bearer_token": token, "expires_at": row.expires_at}

    @app.delete("/internal/resource-owner/v1/enrollments/{enrollment_id}")
    def revoke_enrollment(enrollment_id: str, _operator=Depends(operator)):
        with platform.db.sessions.begin() as session:
            platform.db.authority_lock(session)
            row = session.get(ResourceGatewayEnrollment, enrollment_id)
            if row is None:
                raise HTTPException(404, "enrollment_not_found")
            row.revoked = True
        return {"revoked": True}

    @app.put("/internal/resource-owner/v1/workload-scopes")
    def set_scope(body: WorkloadScope, _operator=Depends(operator)):
        if body.workload_id not in settings.execution_tokens:
            raise HTTPException(422, "unknown_workload")
        with platform.db.sessions.begin() as session:
            platform.db.authority_lock(session)
            row = session.get(ResourceWorkloadScope, body.workload_id)
            if row is None:
                row = ResourceWorkloadScope(workload_id=body.workload_id)
                session.add(row)
            for field in ("gateway_id", "tenant_id", "workspace_id", "domain_id", "observation_id", "namespace", "pod_name"):
                setattr(row, field, getattr(body, field))
            row.updated_at = time.time()
        return {"workload_id": body.workload_id, "updated": True}

    @app.post("/internal/resource-owner/v1/output-grants")
    def install_output_grant(body: OutputGrant, _operator=Depends(operator)):
        with platform.db.sessions.begin() as session:
            platform.db.authority_lock(session)
            scope = session.get(ResourceWorkloadScope, body.workload_id)
            if (scope is None or body.workload_id not in settings.execution_tokens
                    or (scope.tenant_id, scope.workspace_id, scope.domain_id, scope.observation_id)
                    != (body.tenant_id, body.workspace_id, body.domain_id, body.observation_id)):
                raise HTTPException(422, "scope_mismatch")
            row = ResourceOutputGrant(id=uuid.uuid4().hex, workload_id=body.workload_id,
                gateway_id=scope.gateway_id,
                principal_id=body.principal_id, tenant_id=body.tenant_id,
                workspace_id=body.workspace_id, domain_id=body.domain_id,
                observation_id=body.observation_id,
                schema_version="sovite-resource-pod-observation/v1", destination="workbench",
                policy_revision=body.policy_revision, active=True, created_at=time.time())
            session.add(row)
        return {"grant_id": row.id, "schema_version": row.schema_version,
                "destination": row.destination, "policy_revision": row.policy_revision}

    @app.delete("/internal/resource-owner/v1/output-grants/{grant_id}")
    def revoke_output_grant(grant_id: str, _operator=Depends(operator)):
        with platform.db.sessions.begin() as session:
            platform.db.authority_lock(session)
            row = session.get(ResourceOutputGrant, grant_id)
            if row is None:
                raise HTTPException(404, "grant_not_found")
            row.active = False
        return {"revoked": True}

    @app.post("/internal/resource-owner/v1/requests")
    def submit(body: Submit, workload_id=Depends(workload)):
        remote = RemoteExecution(platform)
        principal = ExecutionPrincipal(workload_id, settings.compute_id)
        context = ExecutionContext(instance_id=body.instance_id, operation_id=body.operation_id,
                                   job_id=body.job_id, generation=body.generation, fence=body.fence)
        with platform.db.sessions.begin() as session:
            platform.db.authority_lock(session)
            now = time.time()
            job = session.get(Job, body.job_id)
            try:
                owner = remote._owner(principal, context)
                job, lease = remote._job_lease(session, principal, context, owner)
                remote._require_started(job, lease, now)
                platform.active(session)
                remote.leases._authorize_current(session, job, now)
                if (job.resolved.get("model", {}).get("connection", {}).get("kind") == "model-gateway"
                        and not execution_authority_current(platform, session, job, lease, workload_id, now)):
                    raise HTTPException(403, "execution_scope_denied")
            except Exception:
                raise HTTPException(403, "execution_scope_denied") from None
            scope = session.get(ResourceWorkloadScope, workload_id)
            if scope is None or not job.caller:
                raise HTTPException(403, "execution_scope_denied")
            request = {"workspace_id": scope.workspace_id,
                "resource_domain_id": scope.domain_id, "observation_id": scope.observation_id}
            request_id = hashlib.sha256(f"{body.job_id}:{body.operation_id}".encode()).hexdigest()
            prior = session.get(ResourceOwnerRequest, request_id)
            if prior:
                if (prior.fence != lease.fence or prior.instance_id != body.instance_id
                        or prior.generation != body.generation or prior.request != request
                        or prior.principal_id != job.caller
                        or (prior.gateway_id, prior.tenant_id, prior.workspace_id, prior.domain_id,
                            prior.observation_id, prior.namespace, prior.pod_name)
                        != (scope.gateway_id, scope.tenant_id, scope.workspace_id, scope.domain_id,
                            scope.observation_id, scope.namespace, scope.pod_name)
                        or prior.workload_token_digest != hashlib.sha256(settings.execution_tokens[workload_id].encode()).hexdigest()):
                    raise HTTPException(409, "operation_conflict")
                try:
                    remote._require_started(job, lease, time.time())
                except Exception:
                    raise HTTPException(403, "execution_scope_denied") from None
                return {"request_id": prior.id, "state": prior.state,
                        "fence_fingerprint": "sha256:" + hashlib.sha256(prior.fence.encode()).hexdigest()}
            row = ResourceOwnerRequest(id=request_id, job_id=job.id, instance_id=body.instance_id,
                generation=body.generation, workload_id=workload_id, gateway_id=scope.gateway_id,
                workload_token_digest=hashlib.sha256(settings.execution_tokens[workload_id].encode()).hexdigest(),
                principal_id=job.caller, fence=lease.fence, tenant_id=scope.tenant_id,
                workspace_id=scope.workspace_id, domain_id=scope.domain_id,
                observation_id=scope.observation_id, namespace=scope.namespace,
                pod_name=scope.pod_name, request=request, state="pending",
                observation=None, observation_digest=None, release=None, created_at=now, updated_at=now)
            session.add(row)
            try:
                remote._require_started(job, lease, time.time())
            except Exception:
                raise HTTPException(403, "execution_scope_denied") from None
            return {"request_id": request_id, "state": "pending",
                    "fence_fingerprint": "sha256:" + hashlib.sha256(lease.fence.encode()).hexdigest()}

    def valid_request(session, row, enrollment_id, fingerprint):
        enrollment = session.get(ResourceGatewayEnrollment, enrollment_id)
        job = session.get(Job, row.job_id)
        lease = session.get(WorkerExecutionLease, row.job_id)
        scope = session.get(ResourceWorkloadScope, row.workload_id)
        now = time.time()
        current_token = settings.execution_tokens.get(row.workload_id)
        valid = bool(current_token and secrets.compare_digest(row.workload_token_digest, hashlib.sha256(current_token.encode()).hexdigest())
            and enrollment and not enrollment.revoked and enrollment.expires_at > now
            and enrollment.audience == "resource-gateway" and enrollment.tenant_id == row.tenant_id
            and enrollment.gateway_id == row.gateway_id
            and enrollment.workspace_id == row.workspace_id and enrollment.domain_id == row.domain_id
            and job and job.caller == row.principal_id and job.status == "running"
            and lease and lease.phase == "started" and lease.lease_until > now
            and job.lease_owner is not None and job.lease_until == lease.lease_until
            and lease.execution_deadline is not None and lease.execution_deadline > now
            and not job.cancel_requested
            and lease.workload_id == row.workload_id and lease.fence == row.fence
            and scope and (scope.gateway_id, scope.tenant_id, scope.workspace_id, scope.domain_id, scope.observation_id,
                           scope.namespace, scope.pod_name)
                == (row.gateway_id, row.tenant_id, row.workspace_id, row.domain_id, row.observation_id,
                    row.namespace, row.pod_name)
            and secrets.compare_digest("sha256:" + hashlib.sha256(row.fence.encode()).hexdigest(), fingerprint))
        if not valid:
            return False
        remote = RemoteExecution(platform)
        principal = ExecutionPrincipal(row.workload_id, settings.compute_id)
        context = ExecutionContext(instance_id=row.instance_id, operation_id="resource-owner-check",
            job_id=row.job_id, generation=row.generation, fence=row.fence)
        try:
            owner = remote._owner(principal, context)
            current_job, current_lease = remote._job_lease(session, principal, context, owner)
            remote._require_started(current_job, current_lease, now)
            bundle, _ = platform.active(session)
            recipe = remote.leases._authorize_current(session, current_job, now)
            ranks = class_ranks(bundle)
            if (ranks['C1'] > ranks[recipe['grant']['input_class']]
                    or ranks['C1'] > ranks[current_job.resolved['input_class']]):
                return False
            for destination in recipe['destinations']:
                check_destination(bundle, destination, 'C1')
            if (current_job.resolved.get("model", {}).get("connection", {}).get("kind") == "model-gateway"
                    and not execution_authority_current(platform, session, current_job, current_lease,
                                                       row.workload_id, now)):
                return False
        except Exception:
            return False
        return True

    @app.post("/internal/resource-owner/v1/poll")
    def poll(body: Poll, enrollment_id=Depends(gateway)):
        now = time.time()
        with platform.db.sessions.begin() as session:
            platform.db.authority_lock(session)
            enrollment = session.get(ResourceGatewayEnrollment, enrollment_id)
            query = select(ResourceOwnerRequest).where(
                ResourceOwnerRequest.state == "pending",
                ResourceOwnerRequest.gateway_id == enrollment.gateway_id,
                ResourceOwnerRequest.tenant_id == enrollment.tenant_id,
                ResourceOwnerRequest.workspace_id == enrollment.workspace_id,
                ResourceOwnerRequest.domain_id == enrollment.domain_id,
            )
            if body.after is not None:
                query = query.where(ResourceOwnerRequest.id > body.after)
            rows = session.scalars(query.order_by(ResourceOwnerRequest.id)).yield_per(100)
            items = []
            for row in rows:
                if valid_request(session, row, enrollment_id, "sha256:" + hashlib.sha256(row.fence.encode()).hexdigest()):
                    lease = session.get(WorkerExecutionLease, row.job_id)
                    items.append({"request_id": row.id, "job_id": row.job_id,
                        "principal_id": row.principal_id, "audience": enrollment.audience,
                        "instance_id": row.instance_id, "generation": row.generation,
                        "gateway_id": enrollment.gateway_id, "tenant_id": row.tenant_id,
                        "workspace_id": row.workspace_id, "domain_id": row.domain_id,
                        "observation_id": row.observation_id, "workload": row.workload_id,
                        "fence_fingerprint": "sha256:" + hashlib.sha256(row.fence.encode()).hexdigest(),
                        "request": row.request, "deadline": lease.execution_deadline})
                    if len(items) == 100:
                        break
            return {"requests": items, "next_cursor": items[-1]["request_id"] if len(items) == 100 else None}

    @app.post("/internal/resource-owner/v1/current")
    def current(body: Current, enrollment_id=Depends(gateway)):
        with platform.db.sessions.begin() as session:
            platform.db.authority_lock(session)
            row = session.get(ResourceOwnerRequest, body.request_id)
            if not row or not valid_request(session, row, enrollment_id, body.fence_fingerprint):
                return {"current": False}
            grant = session.scalars(select(ResourceOutputGrant).where(
                ResourceOutputGrant.active.is_(True), ResourceOutputGrant.workload_id == row.workload_id,
                ResourceOutputGrant.gateway_id == row.gateway_id, ResourceOutputGrant.principal_id == row.principal_id,
                ResourceOutputGrant.tenant_id == row.tenant_id, ResourceOutputGrant.workspace_id == row.workspace_id,
                ResourceOutputGrant.domain_id == row.domain_id, ResourceOutputGrant.observation_id == row.observation_id,
                ResourceOutputGrant.schema_version == "sovite-resource-pod-observation/v1",
                ResourceOutputGrant.destination == body.destination,
                ResourceOutputGrant.policy_revision == body.policy_revision)).first()
            return {"current": bool(grant and body.actor == row.principal_id
                                    and valid_request(session, row, enrollment_id, body.fence_fingerprint))}

    @app.post("/internal/resource-owner/v1/result")
    def result(body: Result, enrollment_id=Depends(gateway)):
        try:
            content = base64.b64decode(body.observation_b64, validate=True)
        except (ValueError, TypeError):
            raise HTTPException(422, "invalid_observation") from None
        if len(content) > 4096 or "sha256:" + hashlib.sha256(content).hexdigest() != body.observation_digest:
            raise HTTPException(422, "invalid_observation")
        try:
            observation = json.loads(content)
        except (ValueError, TypeError):
            raise HTTPException(422, "invalid_observation") from None
        expected_fields = {"schema_version", "workspace_id", "resource_domain_id", "observation_id", "namespace", "pod_name", "phase"}
        if (not isinstance(observation, dict) or set(observation) != expected_fields
                or observation.get("schema_version") != "sovite-resource-pod-observation/v1"
                or not isinstance(observation.get("phase"), str)
                or observation.get("phase") not in {"Pending", "Running", "Succeeded", "Failed", "Unknown"}
                or json.dumps(observation, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode() != content):
            raise HTTPException(422, "invalid_observation")
        with platform.db.sessions.begin() as session:
            platform.db.authority_lock(session)
            row = session.get(ResourceOwnerRequest, body.request_id)
            current = bool(row and valid_request(session, row, enrollment_id, body.fence_fingerprint))
            if not current:
                return {"accepted": False, "state": "fenced"}
            scope = session.get(ResourceWorkloadScope, row.workload_id)
            grant = session.scalars(select(ResourceOutputGrant).where(
                ResourceOutputGrant.active.is_(True), ResourceOutputGrant.workload_id == row.workload_id,
                ResourceOutputGrant.gateway_id == row.gateway_id,
                ResourceOutputGrant.principal_id == row.principal_id,
                ResourceOutputGrant.tenant_id == row.tenant_id,
                ResourceOutputGrant.workspace_id == row.workspace_id,
                ResourceOutputGrant.domain_id == row.domain_id,
                ResourceOutputGrant.observation_id == row.observation_id,
                ResourceOutputGrant.schema_version == observation["schema_version"],
                ResourceOutputGrant.destination == body.destination,
                ResourceOutputGrant.policy_revision == body.policy_revision)).first()
            expected = {"schema_version": "sovite-resource-pod-observation/v1",
                "workspace_id": row.workspace_id, "resource_domain_id": row.domain_id,
                "observation_id": row.observation_id,
                "namespace": row.namespace, "pod_name": row.pod_name,
                "phase": observation["phase"]}
            if (grant is None or body.actor != row.principal_id or body.release != "allowed"
                    or scope is None or observation != expected):
                return {"accepted": False, "state": "withheld"}
            if row.observation is not None:
                replay = (row.observation == content.decode("utf-8")
                    and row.observation_digest == body.observation_digest[7:]
                    and row.release == body.release
                    and row.enrollment_id == enrollment_id
                    and row.output_schema == grant.schema_version
                    and row.output_destination == grant.destination
                    and row.output_policy_revision == grant.policy_revision
                    and row.output_actor == body.actor)
                return {"accepted": replay, "state": "stored" if replay else "conflict"}
            row.observation = content.decode("utf-8")
            row.observation_digest = body.observation_digest[7:]
            row.release = body.release
            row.enrollment_id = enrollment_id
            row.output_schema = grant.schema_version
            row.output_destination = grant.destination
            row.output_policy_revision = grant.policy_revision
            row.output_actor = body.actor
            row.state = "observed"
            row.updated_at = time.time()
            return {"accepted": True, "state": "stored"}

    @app.post("/internal/resource-owner/v1/result/read")
    def read_result(body: ResultRead, workload_id=Depends(workload)):
        principal = ExecutionPrincipal(workload_id, settings.compute_id)
        context = ExecutionContext(instance_id=body.instance_id, operation_id=body.operation_id,
            job_id=body.job_id, generation=body.generation, fence=body.fence)
        with platform.db.sessions.begin() as session:
            platform.db.authority_lock(session)
            now = time.time()
            row = session.get(ResourceOwnerRequest, body.request_id)
            if (row is None or row.job_id != body.job_id or row.workload_id != workload_id
                    or row.fence != body.fence or row.observation is None or row.release != "allowed"):
                raise HTTPException(404, "result_withheld")
            remote = RemoteExecution(platform)
            try:
                owner = remote._owner(principal, context)
                job, lease = remote._job_lease(session, principal, context, owner)
                remote._require_started(job, lease, now)
                platform.active(session)
                remote.leases._authorize_current(session, job, now)
                if (job.resolved.get("model", {}).get("connection", {}).get("kind") == "model-gateway"
                        and not execution_authority_current(platform, session, job, lease, workload_id, now)):
                    raise HTTPException(403, "execution_scope_denied")
            except Exception:
                raise HTTPException(403, "execution_scope_denied") from None
            grant = session.scalars(select(ResourceOutputGrant).where(
                ResourceOutputGrant.active.is_(True), ResourceOutputGrant.workload_id == workload_id,
                ResourceOutputGrant.gateway_id == row.gateway_id,
                ResourceOutputGrant.principal_id == job.caller,
                ResourceOutputGrant.tenant_id == row.tenant_id,
                ResourceOutputGrant.workspace_id == row.workspace_id,
                ResourceOutputGrant.domain_id == row.domain_id,
                ResourceOutputGrant.observation_id == row.observation_id,
                ResourceOutputGrant.schema_version == row.output_schema,
                ResourceOutputGrant.destination == row.output_destination,
                ResourceOutputGrant.policy_revision == row.output_policy_revision)).first()
            if (grant is None
                    or row.output_actor != row.principal_id):
                raise HTTPException(404, "result_withheld")
            fingerprint = "sha256:" + hashlib.sha256(row.fence.encode()).hexdigest()
            if not row.enrollment_id or not valid_request(session, row, row.enrollment_id, fingerprint):
                raise HTTPException(404, "result_withheld")
            try:
                content = row.observation.encode('utf-8')
                observation = json.loads(content)
                expected = {"schema_version": "sovite-resource-pod-observation/v1",
                    "workspace_id": row.workspace_id, "resource_domain_id": row.domain_id,
                    "observation_id": row.observation_id, "namespace": row.namespace,
                    "pod_name": row.pod_name, "phase": observation["phase"]}
                if (len(content) > 4096 or not isinstance(observation["phase"], str)
                        or observation["phase"] not in {"Pending", "Running", "Succeeded", "Failed", "Unknown"}
                        or observation != expected
                        or json.dumps(observation, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode() != content
                        or hashlib.sha256(content).hexdigest() != row.observation_digest):
                    raise ValueError
            except (ValueError, TypeError, KeyError, UnicodeError):
                raise HTTPException(404, "result_withheld") from None
            if not valid_request(session, row, row.enrollment_id, fingerprint):
                raise HTTPException(404, "result_withheld")
            return {"request_id": row.id, "observation": observation,
                "sha256": "sha256:" + row.observation_digest,
                "schema_version": grant.schema_version, "destination": grant.destination,
                "policy_revision": grant.policy_revision}
