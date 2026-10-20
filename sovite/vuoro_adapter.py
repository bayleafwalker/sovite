"""Local, owner-owned receipt adapter. No model policy or artifact bytes cross this API.

This evaluates an explicitly configured local capability, not hosted delegation.
The binding is selected from the authenticated workspace/repository/principal tuple.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx
from vuoro_adapter_kit import object_schema, operation_spec


class SoviteInvocationRefused(ValueError):
    pass


@dataclass(frozen=True)
class LocalBinding:
    workspace_id: str
    repo_id: str
    principal_id: str
    endpoint: str
    token: str = field(repr=False)
    grants: frozenset[str] = field(default_factory=frozenset)
    reference_key: bytes = field(default=b"", repr=False)

    def __post_init__(self):
        url = urlsplit(self.endpoint)
        if (
            url.scheme != "http"
            or url.hostname != "127.0.0.1"
            or not url.port
            or url.username
            or url.password
            or url.path
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "Local Sovite binding requires an explicit loopback origin"
            )
        if not all(
            (
                self.workspace_id,
                self.repo_id,
                self.principal_id,
                self.token,
                self.grants,
            )
        ):
            raise ValueError("A complete authenticated local binding is required")
        if not isinstance(self.reference_key, bytes) or len(self.reference_key) < 32:
            raise ValueError(
                "Receipt correlation needs a dedicated persistent integrity key"
            )
        if not isinstance(self.grants, frozenset):
            raise ValueError("Grant allowlist must be frozen")


class LocalSoviteApplication:
    """Transport + configured scope intersection; work ownership stays in its owner."""

    def __init__(
        self,
        bindings: tuple[LocalBinding, ...],
        authorize_work: Callable,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.bindings = {
            (b.workspace_id, b.repo_id, b.principal_id): b for b in bindings
        }
        if len(self.bindings) != len(bindings) or not callable(authorize_work):
            raise ValueError("Bindings must be unique and require a work-owner guard")
        # Sharing one upstream caller credential between distinct principals would
        # collapse the receipt ownership boundary.
        if len({b.token for b in bindings}) != len(bindings):
            raise ValueError(
                "Local principals require dedicated Sovite caller credentials"
            )
        self.authorize_work, self.transport = authorize_work, transport

    @staticmethod
    def seal(binding: LocalBinding, arguments: dict, receipt: str) -> str:
        # Integrity of a task reference, never an authorization credential. Every
        # use independently rechecks the authenticated binding and work owner.
        body = {
            "workspace": binding.workspace_id,
            "repo": binding.repo_id,
            "principal": binding.principal_id,
            "endpoint": binding.endpoint,
            "work": arguments["work_id"],
            "run": arguments["run_id"],
            "grant": arguments["grant_id"],
            "receipt": receipt,
        }
        raw = (
            b"sovite-work-receipt/v1\0"
            + json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        )
        return hmac.new(binding.reference_key, raw, hashlib.sha256).hexdigest()

    async def invoke(self, operation: str, arguments: dict, context: Any) -> dict:
        identity = context.identity
        binding = self.bindings.get(
            (identity.workspace_id, context.repo_id, identity.principal_id)
        )
        authority = (
            "sovite:request" if operation == "sovite.task.request-v1" else "sovite:read"
        )
        if (
            operation not in {"sovite.task.request-v1", "sovite.task.status-v1"}
            or not binding
            or authority not in identity.authorities
            or not identity.authorizes_repo(context.repo_id)
            or (
                identity.expires_at is not None
                and identity.expires_at <= datetime.now(timezone.utc)
            )
        ):
            raise SoviteInvocationRefused("Sovite capability unavailable")
        # This mandatory composition callback resolves the run with the work owner,
        # validates the current work/attempt binding, and rejects stale sessions.
        self.authorize_work(context, arguments["work_id"], arguments["run_id"])
        if arguments["grant_id"] not in binding.grants:
            raise SoviteInvocationRefused("Sovite grant unavailable")
        if operation == "sovite.task.request-v1":
            if not context.idempotency_key:
                raise SoviteInvocationRefused("Sovite request unavailable")
            key = hashlib.sha256(
                json.dumps(
                    {
                        "workspace": binding.workspace_id,
                        "repo": binding.repo_id,
                        "principal": binding.principal_id,
                        "work": arguments["work_id"],
                        "run": arguments["run_id"],
                        "key": context.idempotency_key,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            name, payload = (
                "begin_task",
                {
                    "grant_id": arguments["grant_id"],
                    "task": arguments["task"],
                    "idempotency_key": "vuoro_" + key,
                },
            )
        else:
            if not re.fullmatch(r"rcpt_[A-Za-z0-9_-]{20,90}", arguments["receipt"]):
                raise SoviteInvocationRefused("Sovite receipt unavailable")
            if not hmac.compare_digest(
                self.seal(binding, arguments, arguments["receipt"]),
                arguments["task_binding"],
            ):
                raise SoviteInvocationRefused("Sovite work/receipt binding unavailable")
            name, payload = "task_status", {"receipt": arguments["receipt"]}
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=20,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with client.stream(
                    "POST",
                    binding.endpoint + "/mcp",
                    headers={"Authorization": "Bearer " + binding.token},
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": payload},
                    },
                ) as response:
                    if response.status_code != 200:
                        raise SoviteInvocationRefused("Sovite request unavailable")
                    raw = bytearray()
                    async for part in response.aiter_bytes():
                        raw.extend(part)
                        if len(raw) > 4096:
                            raise SoviteInvocationRefused(
                                "Sovite response exceeds receipt boundary"
                            )
                    result = json.loads(raw).get("result", {})
            if result.get("isError") is not False:
                raise SoviteInvocationRefused("Sovite request unavailable")
            view = result.get("structuredContent")
            if (
                not isinstance(view, dict)
                or set(view) != {"receipt", "status"}
                or not isinstance(view["receipt"], str)
                or not re.fullmatch(r"rcpt_[A-Za-z0-9_-]{20,90}", view["receipt"])
                or view["status"]
                not in {"pending", "working", "available", "unavailable"}
                or (name == "task_status" and view["receipt"] != arguments["receipt"])
            ):
                raise SoviteInvocationRefused(
                    "Sovite response violates receipt boundary"
                )
            self.authorize_work(context, arguments["work_id"], arguments["run_id"])
            if identity.expires_at is not None and identity.expires_at <= datetime.now(
                timezone.utc
            ):
                raise SoviteInvocationRefused("Sovite capability expired")
            return {
                **view,
                "task_binding": self.seal(binding, arguments, view["receipt"]),
            }
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            raise SoviteInvocationRefused("Sovite request unavailable") from None


def register_sovite_catalog(registry: Any, application: LocalSoviteApplication):
    # Same optional integration surface used by other owners; no import at module
    # load time or dependency on Vuoro service state machines.
    from vuoro_service.catalog import OperationRejectedError
    from vuoro_service.contracts import OperationDefinition

    common = {
        "work_id": {"type": "integer", "minimum": 1},
        "run_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "grant_id": {"type": "string", "minLength": 1, "maxLength": 96},
    }
    receipt = {"type": "string", "pattern": "^rcpt_[A-Za-z0-9_-]{20,90}$"}
    seal = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    result = object_schema(
        {
            "receipt": receipt,
            "task_binding": seal,
            "status": {"enum": ["pending", "working", "available", "unavailable"]},
        },
        required=("receipt", "status", "task_binding"),
    )
    for suffix, fields, authority, semantics, idempotency in (
        (
            "request-v1",
            {"task": {"type": "string", "minLength": 1, "maxLength": 65536}},
            "sovite:request",
            "enqueue",
            "required",
        ),
        (
            "status-v1",
            {"receipt": receipt, "task_binding": seal},
            "sovite:read",
            "read",
            "not-allowed",
        ),
    ):
        operation = "sovite.task." + suffix
        spec = operation_spec(
            operation,
            input_schema=object_schema(
                {**common, **fields}, required=tuple(common) + tuple(fields)
            ),
            result_schema=result,
            required_authority=authority,
            execution_semantics=semantics,
            idempotency=idempotency,
            repo_scoped=True,
        )

        async def handler(arguments, context, operation=operation):
            try:
                return await application.invoke(operation, arguments, context)
            except SoviteInvocationRefused:
                raise OperationRejectedError(
                    "sovite-unavailable",
                    "Sovite capability unavailable",
                    http_status=403,
                ) from None

        registry.register(OperationDefinition(**spec), handler)
