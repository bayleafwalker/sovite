"""Opt-in HTTP client for the scoped worker execution owner API.

This module is intentionally not selected by default worker composition. A
caller must explicitly provide the control origin and execution token. Every
request is sent once; ambiguous mutations raise ``ExecutionOutcomeUnknown``
with the exact lookup key so the caller can reconcile without replaying them.
"""
from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx


TIMEOUT_SECONDS = 15.0
MAX_REQUEST_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 10_000
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_FENCE = re.compile(r"^[0-9a-f]{32}$")
_GENERATION = re.compile(r"^sha256:[0-9a-f]{64}$")
_ERROR_CODE = re.compile(r"^[a-z_]{1,80}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class ExecutionClientError(RuntimeError):
    """Bounded diagnostic for a rejected or malformed owner response."""


class ExecutionOutcomeUnknown(ExecutionClientError):
    """A mutation may have committed; use the exact lookup key before acting."""

    def __init__(self, instance_id: str, operation_id: str):
        self.instance_id = instance_id
        self.operation_id = operation_id
        super().__init__(
            "Execution outcome is unknown; do not repeat this mutation. "
            "Call lookup with the same instance_id and operation_id "
            f"({instance_id}, {operation_id})."
        )


class ExecutionLookupNotFound(ExecutionClientError):
    """No journal row is visible yet; this does not settle a prior mutation."""

    def __init__(self, instance_id: str, operation_id: str):
        self.instance_id = instance_id
        self.operation_id = operation_id
        super().__init__(
            "Execution operation lookup returned not_found; the outcome remains unknown. "
            "Do not replay the mutation. Preserve and reconcile this exact lookup key "
            f"({instance_id}, {operation_id})."
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


def _preflight_json(data: bytes) -> str:
    """Bound structural complexity before json.loads allocates response objects."""
    try:
        source = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError from None
    depth = nodes = 0
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        if char in " \t\r\n,:":
            index += 1
        elif char in "{[":
            nodes += 1
            depth += 1
            index += 1
            if depth > MAX_JSON_DEPTH:
                raise ValueError
        elif char in "}]":
            depth -= 1
            index += 1
            if depth < 0:
                raise ValueError
        elif char == '"':
            nodes += 1
            index += 1
            while index < length:
                current = source[index]
                if current == "\\":
                    index += 2
                    continue
                index += 1
                if current == '"':
                    break
            else:
                raise ValueError
        else:
            nodes += 1
            while index < length and source[index] not in " \t\r\n,:{}[]":
                index += 1
        if nodes > MAX_JSON_NODES:
            raise ValueError
    if depth != 0:
        raise ValueError
    return source


def _object_no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError


def _finite_number(value) -> bool:
    return (type(value) in {int, float} and math.isfinite(value)
            and (type(value) is not int or abs(value) <= 9_007_199_254_740_991))


def _validate_response(operation: str, result: dict, request: dict) -> None:
    """Check fixed public projections; task/resolved payload shape is provisional."""
    def exact(fields):
        if set(result) != set(fields):
            raise ValueError

    def identity():
        if (not isinstance(result.get("job_id"), str) or _ID.fullmatch(result["job_id"]) is None
                or not isinstance(result.get("generation"), str)
                or _GENERATION.fullmatch(result["generation"]) is None
                or not isinstance(result.get("fence"), str) or _FENCE.fullmatch(result["fence"]) is None):
            raise ValueError

    state = result.get("state")
    if operation == "pull":
        if state == "idle":
            exact({"state"})
            return
        exact({"state", "job"})
        job = result["job"]
        if not isinstance(job, dict) or set(job) != {
            "job_id", "generation", "fence", "lease_until", "task", "resolved"
        }:
            raise ValueError
        if (not isinstance(job["job_id"], str) or _ID.fullmatch(job["job_id"]) is None
                or not isinstance(job["generation"], str) or _GENERATION.fullmatch(job["generation"]) is None
                or not isinstance(job["fence"], str) or _FENCE.fullmatch(job["fence"]) is None
                or not _finite_number(job["lease_until"])
                or not isinstance(job["task"], dict) or not isinstance(job["resolved"], dict)):
            raise ValueError
        return
    if operation == "start":
        if state == "already_started":
            exact({"state", "job_id", "generation", "fence"})
            identity()
            if any(key in result for key in ("execution", "capability")):
                raise ValueError
            return
        exact({"state", "job_id", "generation", "fence", "lease_until", "deadline"}
              | ({"execution"} if "execution" in result else set()))
        identity()
        if not _finite_number(result["lease_until"]) or not _finite_number(result["deadline"]):
            raise ValueError
        if "execution" in result:
            execution = result["execution"]
            if (not isinstance(execution, dict) or set(execution) != {"capability", "context"}
                    or not isinstance(execution["capability"], str) or not execution["capability"]
                    or not isinstance(execution["context"], dict)):
                raise ValueError
        return
    if operation == "renew":
        exact({"state", "job_id", "generation", "fence", "lease_until", "deadline"})
        identity()
        if state != "renewed" or not _finite_number(result["lease_until"]) or not _finite_number(result["deadline"]):
            raise ValueError
        return
    if operation == "status":
        exact({"state", "job_id", "generation", "fence", "phase", "job_status", "lease_until", "deadline"})
        identity()
        if (state not in {"current", "superseded", "finished", "expired"}
                or result["job_id"] != request["job_id"]
                or result["generation"] != request["generation"] or result["fence"] != request["fence"]
                or result["phase"] not in {None, "reserved", "started", "finished"}
                or result["job_status"] not in {None, "queued", "running", "retry_wait", "completed", "failed", "cancelled", "blocked"}
                or any(value is not None and not _finite_number(value)
                       for value in (result["lease_until"], result["deadline"]))):
            raise ValueError
        return
    if operation == "handback":
        exact({"state", "job_id", "generation", "fence", "disposition"})
        identity()
        if state != "handed_back" or result["disposition"] not in {"retry_wait", "blocked", "cancelled"}:
            raise ValueError
        return
    if operation == "complete":
        allowed = {"state", "job_id", "generation", "fence", "error_code", "artifact_sha256"}
        if set(result) - allowed or not {"state", "job_id", "generation", "fence", "error_code"} <= set(result):
            raise ValueError
        identity()
        if (state not in {"completed", "failed", "retry_wait", "cancelled", "blocked"}
                or (result["error_code"] is not None and
                    (not isinstance(result["error_code"], str) or _ERROR_CODE.fullmatch(result["error_code"]) is None))):
            raise ValueError
        digest = result.get("artifact_sha256")
        if digest is not None and (not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
            raise ValueError
        return
    if operation == "lookup":
        exact({"state", "operation_id", "operation_kind", "job_id", "fence", "result_state"})
        idle_pull = (result.get("operation_kind") == "pull" and result.get("result_state") == "idle"
                     and result.get("job_id") is None and result.get("fence") is None)
        if (state != "found" or result["operation_id"] != request["operation_id"]
                or result["operation_kind"] not in {"pull", "start", "renew", "handback", "complete"}
                or not idle_pull and (not isinstance(result["job_id"], str) or _ID.fullmatch(result["job_id"]) is None
                                      or not isinstance(result["fence"], str) or _FENCE.fullmatch(result["fence"]) is None)
                or result["result_state"] not in {"idle", "reserved", "started", "already_started", "renewed", "handed_back", "completed", "failed", "cancelled", "retry_wait", "blocked"}):
            raise ValueError
        return
    raise ValueError


class ExecutionClient:
    """One-shot bounded HTTP calls using only the dedicated execution token."""

    def __init__(self, origin: str, token: str, *, verify: bool | str = True,
                 transport: httpx.BaseTransport | None = None):
        # Reuse the strict origin checks without reading process credentials.
        target, checked_token = _env_target({"SOVITE_CONTROL_URL": origin,
                                             "SOVITE_EXECUTION_TOKEN": token})
        if verify is False or not isinstance(verify, (bool, str)):
            raise ExecutionClientError("TLS certificate verification must remain enabled")
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
        except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
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
                            raise ExecutionOutcomeUnknown(body.get("instance_id", ""), op_id)
                        if operation == "lookup" and response.status_code == 404:
                            raise ExecutionLookupNotFound(body.get("instance_id", ""), op_id)
                        raise ExecutionClientError(
                            f"Execution request was rejected (HTTP {response.status_code})"
                        )
                    encoding = (response.headers.get("content-encoding") or "").lower().strip()
                    declared = (response.headers.get("content-length") or "").strip()
                    if encoding not in {"", "identity"} or (declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES):
                        raise ExecutionOutcomeUnknown(body.get("instance_id", ""), op_id) if mutation else ExecutionClientError(
                            "Execution owner returned an invalid response"
                        )
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_RESPONSE_BYTES:
                            raise ExecutionOutcomeUnknown(body.get("instance_id", ""), op_id) if mutation else ExecutionClientError(
                                "Execution owner returned an oversized response"
                            )
        except ExecutionClientError:
            raise
        except (httpx.HTTPError, OSError):
            if mutation:
                raise ExecutionOutcomeUnknown(body.get("instance_id", ""), op_id) from None
            raise ExecutionClientError("Execution owner request failed") from None
        try:
            source = _preflight_json(bytes(data))
            result = json.loads(source, object_pairs_hook=_object_no_duplicates,
                                parse_constant=_reject_constant)
        except (TypeError, ValueError, UnicodeDecodeError, RecursionError):
            if mutation:
                raise ExecutionOutcomeUnknown(body.get("instance_id", ""), op_id) from None
            raise ExecutionClientError("Execution owner returned an invalid response") from None
        if not isinstance(result, dict):
            if mutation:
                raise ExecutionOutcomeUnknown(body.get("instance_id", ""), op_id)
            raise ExecutionClientError("Execution owner returned an invalid response")
        try:
            _validate_response(operation, result, body)
        except (KeyError, TypeError, ValueError, OverflowError, RecursionError):
            if mutation:
                raise ExecutionOutcomeUnknown(body.get("instance_id", ""), op_id) from None
            raise ExecutionClientError("Execution owner returned an invalid response") from None
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
