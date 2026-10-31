"""Worker-side, exact-binding inference client for the isolated gateway."""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from .adapters import RuntimeFailure
from .connector.protocol import BODY_LIMIT


_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_GENERATION = re.compile(r"^sha256:[0-9a-f]{64}$")
_FENCE = re.compile(r"^[0-9a-f]{32}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_RESPONSE_LIMIT = 4 * 1024 * 1024
_DEPTH_LIMIT = 32
_NODE_LIMIT = 10_000

_RETRYABLE_ERRORS = {
    "authority_unavailable": ("upstream_unavailable", True),
    "binding_busy": ("upstream_unavailable", True),
    "model_host_preempted": ("model_host_unavailable", True),
    "model_host_unavailable": ("model_host_unavailable", True),
    "permit_expired": ("execution_timeout", False),
    "deadline_exceeded": ("execution_timeout", False),
}
_OWNER_ERRORS = {
    "authorization_revoked", "model_identity_mismatch", "model_protocol_error",
}


def _pairs_no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError


def _preflight(source: str) -> None:
    depth = nodes = index = 0
    while index < len(source):
        char = source[index]
        if char in " \t\r\n,:":
            index += 1
        elif char in "{[":
            depth += 1
            nodes += 1
            index += 1
            if depth > _DEPTH_LIMIT:
                raise ValueError
        elif char in "}]":
            depth -= 1
            index += 1
            if depth < 0:
                raise ValueError
        elif char == '"':
            nodes += 1
            index += 1
            while index < len(source):
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
            while index < len(source) and source[index] not in " \t\r\n,:{}[]":
                index += 1
        if nodes > _NODE_LIMIT:
            raise ValueError
    if depth:
        raise ValueError


def _decode_json(raw: bytes) -> dict:
    if not raw or len(raw) > _RESPONSE_LIMIT:
        raise ValueError
    source = raw.decode("utf-8")
    if source.startswith("\ufeff"):
        raise ValueError
    _preflight(source)
    value = json.loads(source, object_pairs_hook=_pairs_no_duplicates,
                       parse_constant=_reject_constant)
    if not isinstance(value, dict):
        raise ValueError
    return value


def _gateway_failure(code: str) -> RuntimeFailure:
    if not isinstance(code, str):
        return RuntimeFailure("upstream_rejected")
    retry = _RETRYABLE_ERRORS.get(code)
    if retry:
        return RuntimeFailure(retry[0], retry[1])
    if code in _OWNER_ERRORS:
        return RuntimeFailure(code)
    return RuntimeFailure("upstream_rejected")


@dataclass(frozen=True)
class ResponsesResult:
    """Completed bounded stream; usage absence is unknown, never zero cost."""
    sse: bytes = field(repr=False)
    response: dict = field(repr=False)
    usage: dict | None


def _responses_result(raw: bytes, model_id: str) -> ResponsesResult:
    """Require one complete terminal event before disclosing buffered bytes."""
    try:
        source = raw.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')
        if not source.endswith('\n\n'):
            raise RuntimeFailure('external_execution_uncertain')
        completed = None
        for block in source.split('\n\n'):
            event, data = '', []
            for line in block.split('\n'):
                if not line or line.startswith(':'):
                    continue
                field, _, value = line.partition(':')
                value = value[1:] if value.startswith(' ') else value
                if field == 'event':
                    event = value
                elif field == 'data':
                    data.append(value)
            if not data:
                continue
            payload = '\n'.join(data)
            if payload == '[DONE]':
                if completed is None:
                    raise RuntimeFailure('external_execution_uncertain')
                continue
            value = _decode_json(payload.encode('utf-8'))
            if event == 'sovite_error':
                raise _gateway_failure(value.get('code', ''))
            kind = value.get('type')
            if not isinstance(kind, str) or (event and event != kind):
                raise RuntimeFailure('model_protocol_error')
            if kind in {'error', 'response.failed', 'response.incomplete'}:
                raise RuntimeFailure('upstream_rejected')
            if completed is not None:
                raise RuntimeFailure('model_protocol_error')
            if kind == 'response.completed':
                response = value.get('response')
                if (not isinstance(response, dict) or response.get('status') != 'completed'
                        or response.get('model') != model_id or not isinstance(response.get('id'), str)
                        or not isinstance(response.get('output'), list)):
                    raise RuntimeFailure('model_protocol_error')
                completed = response
        if completed is None:
            raise RuntimeFailure('external_execution_uncertain')
        usage = completed.get('usage')
        if usage is not None:
            if (not isinstance(usage, dict) or any(type(usage.get(key)) is not int or usage[key] < 0
                    for key in ('input_tokens', 'output_tokens', 'total_tokens'))
                    or usage['input_tokens'] + usage['output_tokens'] != usage['total_tokens']):
                raise RuntimeFailure('model_protocol_error')
            for name, total in (('input_tokens_details', usage['input_tokens']),
                                ('output_tokens_details', usage['output_tokens'])):
                details = usage.get(name)
                if details is not None and (not isinstance(details, dict)
                        or any(type(count) is not int or not 0 <= count <= total for count in details.values())):
                    raise RuntimeFailure('model_protocol_error')
        return ResponsesResult(raw, completed, usage)
    except RuntimeFailure:
        raise
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise RuntimeFailure('model_protocol_error') from None


class ModelGatewayInference:
    """One-job capability binding; never logs or persists its bearer material."""

    def __init__(self, *, origin: str, token: str, verify: bool | str,
                 binding_id: str, model_id: str, generation: str,
                 job_id: str, fence: str, deadline: float,
                 capability: str, workload: str, binding_timeout: int):
        self.origin, self.token, self.verify = origin, token, verify
        self.binding_id, self.model_id = binding_id, model_id
        self.generation, self.job_id, self.fence = generation, job_id, fence
        self.deadline, self.capability, self.workload = deadline, capability, workload
        self.binding_timeout = binding_timeout

    @classmethod
    def from_environment(cls, *, model: dict, generation: str, job_id: str,
                         fence: str, deadline: float, execution: dict):
        connection = model.get("connection")
        binding_id = model.get("id")
        if (not isinstance(connection, dict) or connection.get("kind") != "model-gateway"
                or not isinstance(binding_id, str) or _ID.fullmatch(binding_id) is None
                or not isinstance(model.get("model"), str) or not model["model"]
                or len(model["model"]) > 256 or _CONTROL.search(model["model"])
                or type(model.get("timeout_seconds")) is not int
                or not 1 <= model["timeout_seconds"] <= 600):
            raise RuntimeFailure("model_gateway_adapter_unavailable")
        if (not isinstance(execution, dict) or set(execution) != {"capability", "context"}
                or not isinstance(execution.get("context"), dict)):
            raise RuntimeFailure("model_gateway_adapter_unavailable")
        context = execution["context"]
        if set(context) != {"generation", "grant_id", "caller", "submission_role",
                            "binding_id", "lease_owner", "workload"}:
            raise RuntimeFailure("model_gateway_adapter_unavailable")
        capability = execution.get("capability")
        try:
            if type(deadline) is int:
                valid_deadline = abs(deadline) <= 1_000_000_000_000
                numeric_deadline = float(deadline) if valid_deadline else 0.0
            elif type(deadline) is float:
                valid_deadline = math.isfinite(deadline)
                numeric_deadline = deadline
            else:
                valid_deadline, numeric_deadline = False, 0.0
        except OverflowError:
            valid_deadline, numeric_deadline = False, 0.0
        if (not isinstance(capability, str) or not 24 <= len(capability) <= 128
                or _CONTROL.search(capability)
                or context.get("generation") != generation
                or context.get("binding_id") != binding_id
                or not isinstance(context.get("workload"), str)
                or not 1 <= len(context["workload"]) <= 160
                or _CONTROL.search(context["workload"])
                or any(not isinstance(context.get(field), str) or not context[field]
                       or len(context[field]) > 160 or _CONTROL.search(context[field])
                       for field in ("grant_id", "caller", "lease_owner"))
                or (context["submission_role"] is not None
                    and (not isinstance(context["submission_role"], str)
                         or len(context["submission_role"]) > 160
                         or _CONTROL.search(context["submission_role"])))
                or not isinstance(generation, str) or _GENERATION.fullmatch(generation) is None
                or not isinstance(job_id, str) or not 1 <= len(job_id) <= 64
                or not job_id.isascii() or _CONTROL.search(job_id)
                or not isinstance(fence, str) or _FENCE.fullmatch(fence) is None
                or not valid_deadline
                or not time.time() < numeric_deadline <= time.time() + 86_400):
            raise RuntimeFailure("model_gateway_adapter_unavailable")

        raw_origin = os.environ.get("SOVITE_INFERENCE_URL", "")
        if not raw_origin or _CONTROL.search(raw_origin) or "?" in raw_origin or "#" in raw_origin:
            raise RuntimeFailure("model_gateway_adapter_unavailable")
        try:
            parsed = urlsplit(raw_origin)
            parsed.port
        except ValueError:
            raise RuntimeFailure("model_gateway_adapter_unavailable") from None
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment):
            raise RuntimeFailure("model_gateway_adapter_unavailable")
        origin = raw_origin[:-1] if parsed.path == "/" else raw_origin
        token = os.environ.get("SOVITE_INFERENCE_TOKEN", "")
        if (len(token) < 24 or token != token.strip() or _CONTROL.search(token)
                or token == os.environ.get("SOVITE_EXECUTION_TOKEN")):
            raise RuntimeFailure("adapter_credential_missing")
        ca_bundle = os.environ.get("SOVITE_INFERENCE_CA_BUNDLE")
        if ca_bundle:
            path = Path(ca_bundle)
            if not path.is_absolute() or not path.is_file() or not os.access(path, os.R_OK):
                raise RuntimeFailure("model_trust_missing")
            verify: bool | str = str(path)
        else:
            verify = True
        # Fence and lease tuple come from the exact started owner response.
        # This method validates the caller's copy below; it does not accept the
        # fence from the recipe or from environment configuration.
        return cls(origin=origin, token=token, verify=verify, binding_id=binding_id,
                   model_id=model["model"], generation=generation, job_id=job_id,
                   fence=fence, deadline=numeric_deadline, capability=capability,
                   workload=context["workload"], binding_timeout=model["timeout_seconds"])

    async def chat(self, model: dict, payload: dict, authorization_check=None) -> dict:
        if (model.get("adapter") != "openai-compatible"
                or model.get("wire_api", "chat") != "chat"
                or model.get("id") != self.binding_id
                or model.get("model") != self.model_id
                or payload.get("model") != self.model_id
                or payload.get("stream") is not False):
            raise RuntimeFailure("unsupported_model_adapter")
        if authorization_check is not None:
            authorization_check()
        request_deadline = min(self.deadline, time.time() + self.binding_timeout)
        remaining = request_deadline - time.time()
        if remaining <= 0:
            raise RuntimeFailure("execution_timeout")
        try:
            body = json.dumps(payload, allow_nan=False, ensure_ascii=False,
                              separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
            raise RuntimeFailure("model_protocol_error") from None
        if not body or len(body) > BODY_LIMIT:
            raise RuntimeFailure("model_protocol_error")
        # These two values are bound by the exact started lease, never by task
        # content or model configuration.
        if not _FENCE.fullmatch(self.fence):
            raise RuntimeFailure("model_gateway_adapter_unavailable")
        url = (self.origin + "/inference/" + quote(self.binding_id, safe="") + "/chat")
        timeout = max(0.1, min(600.0, remaining))
        headers = {
            "Authorization": "Bearer " + self.token,
            "X-Sovite-Capability": self.capability,
            "X-Sovite-Job": self.job_id,
            "X-Sovite-Fence": self.fence,
            "X-Sovite-Deadline": str(math.floor(request_deadline)),
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout), follow_redirects=False,
                                          trust_env=False, verify=self.verify) as client:
                async with client.stream("POST", url, content=body, headers=headers) as response:
                    raw = bytearray()
                    async for block in response.aiter_bytes():
                        raw.extend(block)
                        if len(raw) > _RESPONSE_LIMIT:
                            raise RuntimeFailure("upstream_response_too_large")
                    response_body = bytes(raw)
                    if response.status_code >= 300:
                        try:
                            error = _decode_json(response_body)
                        except (ValueError, UnicodeDecodeError, RecursionError, OverflowError):
                            raise RuntimeFailure("upstream_rejected") from None
                        raise _gateway_failure(error.get("code", ""))
        except RuntimeFailure:
            raise
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, httpx.InvalidURL, TimeoutError, ValueError):
            raise RuntimeFailure("upstream_unavailable", True) from None

        lines = response_body.splitlines()
        for index, line in enumerate(lines):
            if line == b"event: sovite_error":
                if index + 1 >= len(lines) or not lines[index + 1].startswith(b"data: "):
                    raise RuntimeFailure("model_protocol_error")
                try:
                    event = _decode_json(lines[index + 1][6:])
                except (ValueError, UnicodeDecodeError, RecursionError, OverflowError):
                    raise RuntimeFailure("model_protocol_error") from None
                raise _gateway_failure(event.get("code", ""))
        try:
            return _decode_json(response_body)
        except (ValueError, UnicodeDecodeError, RecursionError, OverflowError):
            raise RuntimeFailure("model_protocol_error") from None


    async def responses(self, model: dict, payload: dict, authorization_check=None) -> ResponsesResult:
        """Forward one native stateless request through this exact owner binding.

        The caller reserves cost first and retains it on every unknown outcome.
        This method never retries, launches tools, settles usage or qualifies a
        model helper. Session metadata/cache keys stop at the local bridge.
        """
        if (model.get('adapter') != 'openai-compatible' or model.get('wire_api') != 'responses'
                or model.get('id') != self.binding_id or model.get('model') != self.model_id
                or type(payload) is not dict or payload.get('model') != self.model_id
                or payload.get('stream') is not True):
            raise RuntimeFailure('unsupported_model_adapter')
        try:
            original = json.dumps(payload, allow_nan=False, ensure_ascii=False,
                                  separators=(',', ':')).encode('utf-8')
            if len(original) > BODY_LIMIT:
                raise ValueError
            forwarded = {key: value for key, value in payload.items()
                         if key not in {'client_metadata', 'prompt_cache_key'}}
            body = json.dumps(forwarded, allow_nan=False, ensure_ascii=False,
                              separators=(',', ':')).encode('utf-8')
            from .connector.helper import validate_body
            validate_body('responses', body, self.model_id)
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise RuntimeFailure('model_protocol_error') from None
        if not _FENCE.fullmatch(self.fence):
            raise RuntimeFailure('model_gateway_adapter_unavailable')
        if authorization_check is not None:
            authorization_check()
        request_deadline = min(self.deadline, time.time() + self.binding_timeout)
        remaining = request_deadline - time.time()
        if remaining <= 0:
            raise RuntimeFailure('execution_timeout')
        headers = {'Authorization': 'Bearer ' + self.token,
                   'X-Sovite-Capability': self.capability, 'X-Sovite-Job': self.job_id,
                   'X-Sovite-Fence': self.fence, 'X-Sovite-Deadline': str(math.floor(request_deadline)),
                   'Content-Type': 'application/json', 'Accept': 'text/event-stream'}
        url = self.origin + '/inference/' + quote(self.binding_id, safe='') + '/responses'
        try:
            async with asyncio.timeout(remaining):
                async with httpx.AsyncClient(timeout=httpx.Timeout(remaining), follow_redirects=False,
                                              trust_env=False, verify=self.verify) as client:
                    async with client.stream('POST', url, content=body, headers=headers) as response:
                        raw = bytearray()
                        async for block in response.aiter_bytes():
                            if authorization_check is not None:
                                authorization_check()
                            if time.time() >= request_deadline:
                                raise RuntimeFailure('execution_timeout')
                            raw.extend(block)
                            if len(raw) > _RESPONSE_LIMIT:
                                raise RuntimeFailure('upstream_response_too_large')
                        if response.status_code >= 300:
                            try:
                                error = _decode_json(bytes(raw))
                            except (ValueError, UnicodeError, RecursionError, OverflowError):
                                raise RuntimeFailure('upstream_rejected') from None
                            raise _gateway_failure(error.get('code', ''))
                        if response.headers.get('content-type', '').split(';', 1)[0].strip() != 'text/event-stream':
                            raise RuntimeFailure('model_protocol_error')
                result = _responses_result(bytes(raw), self.model_id)
                if authorization_check is not None:
                    authorization_check()
                if time.time() >= request_deadline:
                    raise RuntimeFailure('execution_timeout')
                return result
        except RuntimeFailure:
            raise
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise RuntimeFailure('execution_timeout') from None
        except (httpx.HTTPError, httpx.InvalidURL, ValueError):
            # A disconnect may follow provider invocation. Preserve the budget
            # reservation; never turn missing completion into a retry or $0.
            raise RuntimeFailure('external_execution_uncertain') from None
