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
    result_json: str | None = Field(default=None, max_length=60000)
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
    # The generated contract runtime is a Python 3.12+ dependency; keep the
    # remote boundary closed on older installs that do not carry it.
    import sys
    if sys.version_info < (3, 12) or not settings.execution_tokens:
        return
    from .execution_contract.runtime import load_bundle, validate_request, validate_response

    load_bundle()
    service = RemoteExecution(platform)

    def admitted(operation, body):
        try:
            validate_request(operation, body.model_dump(exclude_unset=True))
        except Exception:
            raise HTTPException(422, {"code": "invalid_request", "message": "Execution request is invalid"}) from None

    def projected(operation, value):
        try:
            import json
            from .execution_contract.runtime import OPERATIONS
            contract_operation = load_bundle().manifest["operations"][OPERATIONS[operation]]
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
            if len(encoded) > contract_operation["responseMaxBytes"]["200"]:
                raise ValueError
            validate_response(operation, 200, value)
        except Exception:
            # The owner has committed before this check. The remote client must
            # treat the missing valid response as uncertain and reconcile by key.
            raise HTTPException(503, {"code": "owner_unavailable", "message": "Execution owner state is unavailable"}) from None
        return value

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

    @app.post("/internal/execution/v1/renew")
    def renew(body: ExecutionMutation, principal=Depends(authenticate)):
        admitted("renew", body)
        return projected("renew", service.renew(principal, _context(body)))

    @app.post("/internal/execution/v1/pull")
    def pull(body: Pull, principal=Depends(authenticate)):
        admitted("pull", body)
        value = service.leases.reserve(
            principal=principal,
            instance_id=body.instance_id,
            operation_id=body.operation_id,
            external_profile="agent-http-v1",
        )
        return projected("pull", value)

    @app.post("/internal/execution/v1/start")
    def start(body: ExecutionMutation, principal=Depends(authenticate)):
        admitted("start", body)
        return projected("start", service.leases.start(principal=principal, context=_context(body), external_profile="agent-http-v1"))

    @app.post("/internal/execution/v1/status")
    def status(body: ExecutionIdentity, principal=Depends(authenticate)):
        admitted("status", body)
        return projected("status", service.status(principal, _context(body)))

    @app.post("/internal/execution/v1/handback")
    def handback(body: ExecutionMutation, principal=Depends(authenticate)):
        admitted("handback", body)
        return projected("handback", service.handback(principal, _context(body)))

    @app.post("/internal/execution/v1/complete")
    def complete(body: Complete, principal=Depends(authenticate)):
        admitted("complete", body)
        result_value = None
        if body.result_json is not None:
            import json
            try:
                def unique_pairs(pairs):
                    parsed = {}
                    for key, value in pairs:
                        if key in parsed:
                            raise ValueError
                        parsed[key] = value
                    return parsed
                result_value = json.loads(
                    body.result_json,
                    object_pairs_hook=unique_pairs,
                    parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
                )
            except (ValueError, TypeError, RecursionError):
                raise HTTPException(422, {"code": "invalid_request", "message": "Execution request is invalid"}) from None
            if not isinstance(result_value, dict):
                raise HTTPException(422, {"code": "invalid_request", "message": "Execution request is invalid"})
        value = service.complete(principal, _context(body), outcome=body.outcome,
                                 result_value=result_value, error_code=body.error_code)
        return projected("complete", value)

    @app.post("/internal/execution/v1/lookup")
    def lookup(body: OperationLookup, principal=Depends(authenticate)):
        admitted("lookup", body)
        return projected("lookup", service.lookup(principal, instance_id=body.instance_id, operation_id=body.operation_id))
