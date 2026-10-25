"""Authenticated HTTP adapter for remote worker execution operations."""
from __future__ import annotations

import secrets
from typing import Literal

from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .lease_service import ExecutionContext, ExecutionPrincipal
from .remote_execution import RemoteExecution


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ExecutionIdentity(StrictBody):
    instance_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    job_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    generation: str = Field(pattern=r"^sha256:[0-9a-f]{64}$", min_length=71, max_length=71)
    fence: str = Field(pattern=r"^[0-9a-f]{32}$", min_length=32, max_length=32)


class ExecutionMutation(ExecutionIdentity):
    operation_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class Complete(ExecutionMutation):
    outcome: Literal["completed", "failed", "retryable_failure", "cancelled"]
    result: dict | None = None
    error_code: str | None = Field(default=None, min_length=1, max_length=80, pattern=r"^[a-z_]+$")


class Pull(StrictBody):
    instance_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    operation_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class OperationLookup(StrictBody):
    instance_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    operation_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


def _context(body: ExecutionMutation | ExecutionIdentity) -> ExecutionContext:
    return ExecutionContext(
        instance_id=body.instance_id,
        operation_id=getattr(body, "operation_id", "status"),
        job_id=body.job_id,
        generation=body.generation,
        fence=body.fence,
    )


def install_routes(app, platform, settings):
    service = RemoteExecution(platform)

    def authenticate(authorization: str = Header(default="")) -> ExecutionPrincipal:
        if not authorization.startswith("Bearer "):
            raise HTTPException(403, {"code": "execution_scope_denied", "message": "Execution owner scope is not permitted"})
        token = authorization[7:]
        if not token:
            raise HTTPException(403, {"code": "execution_scope_denied", "message": "Execution owner scope is not permitted"})
        for workload_id, expected in settings.execution_tokens.items():
            if secrets.compare_digest(token.encode(), expected.encode()):
                return ExecutionPrincipal(workload_id, settings.compute_id)
        raise HTTPException(403, {"code": "execution_scope_denied", "message": "Execution owner scope is not permitted"})

    @app.post("/internal/execution/renew")
    def renew(body: ExecutionMutation, principal=Depends(authenticate)):
        return service.renew(principal, _context(body))

    @app.post("/internal/execution/pull")
    def pull(body: Pull, principal=Depends(authenticate)):
        return service.leases.reserve(
            principal=principal,
            instance_id=body.instance_id,
            operation_id=body.operation_id,
            external_profile="agent-http-v1",
        )

    @app.post("/internal/execution/start")
    def start(body: ExecutionMutation, principal=Depends(authenticate)):
        return service.leases.start(principal=principal, context=_context(body), external_profile="agent-http-v1")

    @app.post("/internal/execution/status")
    def status(body: ExecutionIdentity, principal=Depends(authenticate)):
        return service.status(principal, _context(body))

    @app.post("/internal/execution/handback")
    def handback(body: ExecutionMutation, principal=Depends(authenticate)):
        return service.handback(principal, _context(body))

    @app.post("/internal/execution/complete")
    def complete(body: Complete, principal=Depends(authenticate)):
        return service.complete(
            principal,
            _context(body),
            outcome=body.outcome,
            result_value=body.result,
            error_code=body.error_code,
        )

    @app.post("/internal/execution/lookup")
    def lookup(body: OperationLookup, principal=Depends(authenticate)):
        return service.lookup(principal, instance_id=body.instance_id, operation_id=body.operation_id)
