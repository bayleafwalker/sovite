"""Authenticated control-plane client for hosted compilation and activation.

Hosted ``compile`` and ``activate`` are API operations. They use no database
credential and never read the hosting envelope: the running service stays the
provider of both the policy envelope and role enforcement. This module sends
exactly one bounded request per invocation and never retries.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import policy


CONTROL_TIMEOUT_SECONDS = 10.0
MAX_REQUEST_BYTES = 65536
MAX_RESPONSE_BYTES = 65536
DEFAULT_BUNDLE_PATH = "config/platform.local.yaml"
AUTHOR_TOKEN_ENV = "SOVITE_AUTHOR_TOKEN"
ACTIVATOR_TOKEN_ENV = "SOVITE_ACTIVATOR_TOKEN"
GENERATION = re.compile(r"sha256:[0-9a-f]{64}")
_UNSAFE_URL = re.compile(r"[\s\x00-\x1f\x7f]")
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")


class ControlError(ValueError):
    """The control request produced no confirmed result.

    Messages are fixed diagnostics. They never contain the selected token, the
    bundle, response text, upstream exception details or URL credentials.
    """


@dataclass(frozen=True)
class ControlTarget:
    origin: str
    verify: bool | str


def target_from_env(env=None) -> ControlTarget:
    """Read the explicit control endpoint; never derive it from another setting."""
    env = os.environ if env is None else env
    raw = env.get("SOVITE_CONTROL_URL") or ""
    if not raw:
        raise ControlError("SOVITE_CONTROL_URL is required for hosted compile and activate")
    if _UNSAFE_URL.search(raw):
        raise ControlError("SOVITE_CONTROL_URL must be a plain HTTPS origin without whitespace or control characters")
    # Check the raw text: an empty query or fragment delimiter still parses to an
    # empty component, and the path concatenation would then drop or corrupt it.
    if "?" in raw or "#" in raw:
        raise ControlError("SOVITE_CONTROL_URL must not contain a query or fragment")
    try:
        parsed = urlsplit(raw)
        parsed.port  # force port validation
    except ValueError:
        raise ControlError("SOVITE_CONTROL_URL must be a plain HTTPS origin") from None
    if parsed.scheme != "https" or not parsed.hostname:
        raise ControlError("SOVITE_CONTROL_URL must be an HTTPS origin")
    if parsed.username is not None or parsed.password is not None:
        raise ControlError("SOVITE_CONTROL_URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ControlError("SOVITE_CONTROL_URL must not contain a query or fragment")
    if parsed.path not in {"", "/"}:
        raise ControlError("SOVITE_CONTROL_URL must not contain a path")
    origin = raw[:-1] if parsed.path == "/" else raw
    ca_file = env.get("SOVITE_CONTROL_CA_FILE") or ""
    verify: bool | str = True
    if ca_file:
        try:
            usable = Path(ca_file).is_file()
        except OSError:
            usable = False
        if not usable:
            raise ControlError("SOVITE_CONTROL_CA_FILE must name a readable certificate file")
        verify = ca_file
    return ControlTarget(origin=origin, verify=verify)


def role_token(env, name: str) -> str:
    """Load exactly one role token, without consulting any other role."""
    value = env.get(name) or ""
    if len(value) < 24:
        raise ControlError(f"{name} must contain at least 24 characters")
    if _CONTROL_CHARACTER.search(value):
        raise ControlError(f"{name} must not contain control characters")
    return value


def validated_generation(generation) -> str:
    if not isinstance(generation, str) or GENERATION.fullmatch(generation) is None:
        raise ControlError("Generation must be sha256: followed by 64 lower-case hexadecimal digits")
    return generation


def _client(*, verify: bool | str, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(CONTROL_TIMEOUT_SECONDS),
        trust_env=False,
        follow_redirects=False,
        verify=verify,
        transport=transport,
    )


def _bundle_body(path: str | Path) -> bytes:
    try:
        bundle, _ = policy.load_bundle(path)
    except policy.PolicyError:
        # Policy text names rejected fields and values. Hosted diagnostics are a
        # bounded constant; standalone policy behavior keeps its detailed message.
        raise ControlError("The bundle failed policy validation for hosted compilation") from None
    except OSError:
        raise ControlError("The bundle file could not be read") from None
    try:
        encoded = json.dumps({"bundle": bundle}, ensure_ascii=False, separators=(",", ":")).encode()
    except (TypeError, ValueError):
        raise ControlError("The bundle could not be encoded for transport") from None
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ControlError("The encoded compile request exceeds the 64 KiB control limit")
    return encoded


def _transport_failure(operation: str) -> ControlError:
    if operation == "Activation":
        return ControlError(
            "Activation outcome is unknown: the control request did not complete. "
            "Inspect current server state before retrying; no automatic retry was attempted."
        )
    return ControlError(
        "Compilation did not complete: the control request failed before a confirmed result. "
        "No automatic retry was attempted."
    )


def _post(*, operation: str, url: str, token: str, body: bytes, target: ControlTarget,
          expected_status: int, expected_state: str, transport: httpx.BaseTransport | None = None,
          requested_generation: str | None = None) -> dict:
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
        "Accept-Encoding": "identity",
    }
    if body:
        headers["Content-Type"] = "application/json"
    try:
        with _client(verify=target.verify, transport=transport) as client:
            with client.stream("POST", url, headers=headers, content=body) as response:
                if response.status_code != expected_status:
                    raise ControlError(
                        f"{operation} failed: the control server returned status {response.status_code}"
                    )
                encoding = (response.headers.get("content-encoding") or "").strip().lower()
                if encoding not in {"", "identity"}:
                    raise ControlError(f"{operation} failed: the control server returned an encoded response")
                declared = (response.headers.get("content-length") or "").strip()
                if declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                    raise ControlError(f"{operation} failed: the control server response exceeded 64 KiB")
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_RESPONSE_BYTES:
                        raise ControlError(f"{operation} failed: the control server response exceeded 64 KiB")
    except (httpx.HTTPError, OSError):
        raise _transport_failure(operation) from None
    try:
        payload = json.loads(bytes(data))
    except (TypeError, ValueError, UnicodeDecodeError):
        raise ControlError(f"{operation} failed: the control server returned an invalid response") from None
    if not isinstance(payload, dict) or set(payload) != {"status", "generation"}:
        raise ControlError(f"{operation} failed: the control server returned an invalid response")
    state = payload["status"]
    generation = payload["generation"]
    if state != expected_state:
        raise ControlError(f"{operation} failed: the control server reported an unexpected state")
    if not isinstance(generation, str) or GENERATION.fullmatch(generation) is None:
        raise ControlError(f"{operation} failed: the control server returned an invalid generation")
    if requested_generation is not None and generation != requested_generation:
        raise ControlError(f"{operation} failed: the control server returned a different generation")
    return {"status": state, "generation": generation}


def compile_command(path: str | None = None, *, env=None, transport: httpx.BaseTransport | None = None) -> dict:
    """Compile one bundle as the author role through the control API."""
    env = os.environ if env is None else env
    target = target_from_env(env)
    token = role_token(env, AUTHOR_TOKEN_ENV)
    bundle_path = path or env.get("SOVITE_BUNDLE_PATH") or DEFAULT_BUNDLE_PATH
    body = _bundle_body(bundle_path)
    return _post(
        operation="Compilation",
        url=target.origin + "/api/bundles",
        token=token,
        body=body,
        target=target,
        expected_status=201,
        expected_state="compiled",
        transport=transport,
    )


def activate_command(generation: str, *, env=None, transport: httpx.BaseTransport | None = None) -> dict:
    """Activate one compiled generation as the activator role through the control API."""
    env = os.environ if env is None else env
    target = target_from_env(env)
    token = role_token(env, ACTIVATOR_TOKEN_ENV)
    requested = validated_generation(generation)
    return _post(
        operation="Activation",
        url=target.origin + "/api/bundles/" + requested + "/activate",
        token=token,
        body=b"",
        target=target,
        expected_status=200,
        expected_state="activated",
        transport=transport,
        requested_generation=requested,
    )
