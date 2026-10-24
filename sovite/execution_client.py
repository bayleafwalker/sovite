"""Opt-in HTTP client for the scoped worker execution owner API.

This module is intentionally not selected by default worker composition. A
caller must explicitly provide the control origin and execution token. Every
request is sent once; ambiguous mutations raise ``ExecutionOutcomeUnknown``
with the exact lookup key so the caller can reconcile without replaying them.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx


TIMEOUT_SECONDS = 15.0
MAX_REQUEST_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_FENCE = re.compile(r"^[0-9a-f]{32}$")
_GENERATION = re.compile(r"^sha256:[0-9a-f]{64}$")
_ERROR_CODE = re.compile(r"^[a-z_]{1,80}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class ExecutionClientError(RuntimeError):
    """Bounded diagnostic for a rejected or malformed owner response."""


class ExecutionOutcomeUnknown(ExecutionClientError):
    """A mutation may have committed; use the exact lookup key before acting."""

    def __init__(self, operation_id: str):
        self.operation_id = operation_id
        super().__init__(
            "Execution outcome is unknown; do not repeat this mutation. "
            f"Call lookup with the same instance_id and operation_id ({operation_id})."
        )


@dataclass(frozen=True)
class ExecutionContext:
    instance_id: str
    operation_id: str
    job_id: str
    generation: str
    fence: str


def _identifier(value, name: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ExecutionClientError(f"{name} is invalid")
    return value


def _context(context: ExecutionContext) -> dict:
    if not isinstance(context, ExecutionContext):
        raise ExecutionClientError("Execution context is invalid")
    values = {
        "instance_id": _identifier(context.instance_id, "instance_id"),
        "operation_id": _identifier(context.operation_id, "operation_id"),
        "job_id": _identifier(context.job_id, "job_id"),
        "generation": context.generation,
        "fence": context.fence,
    }
    if not isinstance(values["generation"], str) or _GENERATION.fullmatch(values["generation"]) is None:
        raise ExecutionClientError("generation is invalid")
    if not isinstance(values["fence"], str) or _FENCE.fullmatch(values["fence"]) is None:
        raise ExecutionClientError("fence is invalid")
    return values


def _env_target(env) -> tuple[str, str]:
    raw = env.get("SOVITE_CONTROL_URL") or ""
    if not raw or _CONTROL.search(raw) or "?" in raw or "#" in raw:
        raise ExecutionClientError("SOVITE_CONTROL_URL must be a plain HTTPS origin")
    try:
        parsed = urlsplit(raw)
        parsed.port
    except ValueError:
        raise ExecutionClientError("SOVITE_CONTROL_URL must be a plain HTTPS origin") from None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise ExecutionClientError("SOVITE_CONTROL_URL must be a plain HTTPS origin")
    token = env.get("SOVITE_EXECUTION_TOKEN") or ""
    if len(token) < 24 or _CONTROL.search(token):
        raise ExecutionClientError("SOVITE_EXECUTION_TOKEN is missing or invalid")
    return raw[:-1] if parsed.path == "/" else raw, token


class ExecutionClient:
    """One-shot bounded HTTP calls using only the dedicated execution token."""

    def __init__(self, origin: str, token: str, *, verify: bool | str = True,
                 transport: httpx.BaseTransport | None = None):
        # Reuse the strict origin checks without reading process credentials.
        target, checked_token = _env_target({"SOVITE_CONTROL_URL": origin,
                                             "SOVITE_EXECUTION_TOKEN": token})
        self._origin = target
        self._token = checked_token
        self._verify = verify
        self._transport = transport

    def __repr__(self) -> str:
        return f"ExecutionClient(origin={self._origin!r}, token=<redacted>)"

    @classmethod
    def from_env(cls, *, env=None, verify: bool | str = True,
                 transport: httpx.BaseTransport | None = None) -> "ExecutionClient":
        target, token = _env_target(os.environ if env is None else env)
        return cls(target, token, verify=verify, transport=transport)

    def _post(self, operation: str, body: dict, *, mutation: bool) -> dict:
        op_id = body.get("operation_id", "")
        try:
            encoded = json.dumps(body, ensure_ascii=False, allow_nan=False,
                                 separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError):
            raise ExecutionClientError("Execution request is invalid") from None
        if len(encoded) > MAX_REQUEST_BYTES:
            raise ExecutionClientError("Execution request exceeds the control limit")
        headers = {"Authorization": "Bearer " + self._token,
                   "Accept": "application/json", "Accept-Encoding": "identity",
                   "Content-Type": "application/json"}
        try:
            with httpx.Client(timeout=httpx.Timeout(TIMEOUT_SECONDS), trust_env=False,
                              follow_redirects=False, verify=self._verify,
                              transport=self._transport) as client:
                with client.stream("POST", self._origin + "/internal/execution/" + operation,
                                   headers=headers, content=encoded) as response:
                    if response.status_code < 200 or response.status_code >= 300:
                        # Typed client/validation refusals have not performed the requested
                        # transition. Server failures can occur after a commit and are unknown.
                        if mutation and (response.status_code < 400 or response.status_code >= 500
                                         or response.status_code in {408, 425, 429}):
                            raise ExecutionOutcomeUnknown(op_id)
                        raise ExecutionClientError(
                            f"Execution request was rejected (HTTP {response.status_code})"
                        )
                    encoding = (response.headers.get("content-encoding") or "").lower().strip()
                    declared = (response.headers.get("content-length") or "").strip()
                    if encoding not in {"", "identity"} or (declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES):
                        raise ExecutionOutcomeUnknown(op_id) if mutation else ExecutionClientError(
                            "Execution owner returned an invalid response"
                        )
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_RESPONSE_BYTES:
                            raise ExecutionOutcomeUnknown(op_id) if mutation else ExecutionClientError(
                                "Execution owner returned an oversized response"
                            )
        except ExecutionClientError:
            raise
        except (httpx.HTTPError, OSError):
            if mutation:
                raise ExecutionOutcomeUnknown(op_id) from None
            raise ExecutionClientError("Execution owner request failed") from None
        try:
            result = json.loads(bytes(data))
        except (TypeError, ValueError, UnicodeDecodeError):
            if mutation:
                raise ExecutionOutcomeUnknown(op_id) from None
            raise ExecutionClientError("Execution owner returned an invalid response") from None
        if not isinstance(result, dict):
            if mutation:
                raise ExecutionOutcomeUnknown(op_id)
            raise ExecutionClientError("Execution owner returned an invalid response")
        return result

    def pull(self, instance_id: str, operation_id: str) -> dict:
        return self._post("pull", {"instance_id": _identifier(instance_id, "instance_id"),
                                    "operation_id": _identifier(operation_id, "operation_id")}, mutation=True)

    def start(self, context: ExecutionContext) -> dict:
        return self._post("start", _context(context), mutation=True)

    def renew(self, context: ExecutionContext) -> dict:
        return self._post("renew", _context(context), mutation=True)

    def status(self, context: ExecutionContext) -> dict:
        body = _context(context)
        body.pop("operation_id")
        return self._post("status", body, mutation=False)

    def handback(self, context: ExecutionContext) -> dict:
        return self._post("handback", _context(context), mutation=True)

    def complete(self, context: ExecutionContext, *, outcome: str,
                 result: dict | None = None, error_code: str | None = None) -> dict:
        if not isinstance(outcome, str) or outcome not in {"completed", "failed", "retryable_failure", "cancelled"}:
            raise ExecutionClientError("outcome is invalid")
        if error_code is not None and (not isinstance(error_code, str) or _ERROR_CODE.fullmatch(error_code) is None):
            raise ExecutionClientError("error_code is invalid")
        body = _context(context)
        body.update(outcome=outcome, result=result, error_code=error_code)
        return self._post("complete", body, mutation=True)

    def lookup(self, instance_id: str, operation_id: str) -> dict:
        return self._post("lookup", {
            "instance_id": _identifier(instance_id, "instance_id"),
            "operation_id": _identifier(operation_id, "operation_id"),
        }, mutation=False)
