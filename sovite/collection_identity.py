"""Closed collection-owner identity and request primitives.

These helpers do not install a route or grant an execution capability.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field


CAPABILITIES = frozenset({"collection-operator", "collection-agent",
                          "collection-reconciler", "collection-recovery"})
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,79}\Z", re.ASCII)
MAX_REQUEST_BYTES = 16 * 1024
MAX_INTEGER = 2**53 - 1


class InvalidCollectionInput(ValueError):
    """A sanitized error: no submitted values are exposed."""


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise InvalidCollectionInput("invalid collection request")
        result[key] = value
    return result


def _no_float(_):
    raise InvalidCollectionInput("invalid collection request")


def parse_closed_json(raw: bytes) -> dict:
    if not isinstance(raw, bytes) or len(raw) > MAX_REQUEST_BYTES:
        raise InvalidCollectionInput("invalid collection request")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_float=_no_float, parse_constant=_no_float)
    except (UnicodeError, ValueError, RecursionError):
        raise InvalidCollectionInput("invalid collection request") from None
    if type(value) is not dict:
        raise InvalidCollectionInput("invalid collection request")
    return value


def create_request_identity(raw: bytes) -> tuple[dict, str, str]:
    """Return a closed ASCII create request, its canonical JSON and digest.

    This schema admits only ASCII strings and safe integers, making sorted,
    compact JSON equivalent to RFC 8785 for its finite input domain.
    """
    body = parse_closed_json(raw)
    fields = {"schema_version", "workspace_id", "repository_id", "generation",
              "suite_version", "suite_digest", "deployment_digest", "permit_digest",
              "deadline_ms", "retain_until_ms"}
    if set(body) != fields:
        raise InvalidCollectionInput("invalid collection request")
    if body["schema_version"] != "sovite.collection-session/v1":
        raise InvalidCollectionInput("invalid collection request")
    for name in ("workspace_id", "repository_id", "suite_version"):
        if type(body[name]) is not str or not IDENTIFIER.fullmatch(body[name]):
            raise InvalidCollectionInput("invalid collection request")
    for name in ("suite_digest", "deployment_digest", "permit_digest"):
        if type(body[name]) is not str or not re.fullmatch(r"[0-9a-f]{64}", body[name]):
            raise InvalidCollectionInput("invalid collection request")
    if type(body["generation"]) is not str or not re.fullmatch(r"sha256:[0-9a-f]{64}", body["generation"]):
        raise InvalidCollectionInput("invalid collection request")
    for name in ("deadline_ms", "retain_until_ms"):
        if type(body[name]) is not int or not 0 < body[name] <= MAX_INTEGER:
            raise InvalidCollectionInput("invalid collection request")
    if body["retain_until_ms"] < body["deadline_ms"]:
        raise InvalidCollectionInput("invalid collection request")
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    checksum = hashlib.sha256(b"sovite.collection.create-request/v1\0" + canonical.encode("utf-8")).hexdigest()
    return body, canonical, checksum


@dataclass(frozen=True)
class CollectionCredential:
    principal_id: str
    capability: str
    token: str = field(repr=False)


def validate_credentials(credentials: tuple[CollectionCredential, ...], *, reserved_tokens: tuple[str, ...]) -> None:
    if type(credentials) is not tuple or len(credentials) != len(CAPABILITIES):
        raise InvalidCollectionInput("invalid collection credentials")
    if any(type(item) is not CollectionCredential for item in credentials):
        raise InvalidCollectionInput("invalid collection credentials")
    if {item.capability for item in credentials} != CAPABILITIES:
        raise InvalidCollectionInput("invalid collection credentials")
    if any(type(item.principal_id) is not str or not IDENTIFIER.fullmatch(item.principal_id)
           or type(item.token) is not str or len(item.token) < 24 or not item.token.isascii()
           for item in credentials):
        raise InvalidCollectionInput("invalid collection credentials")
    principals = [item.principal_id for item in credentials]
    tokens = [item.token for item in credentials]
    if len(set(principals)) != len(principals) or len(set(tokens)) != len(tokens):
        raise InvalidCollectionInput("invalid collection credentials")
    if any(token in reserved_tokens for token in tokens):
        raise InvalidCollectionInput("invalid collection credentials")


def authenticate_collection(token: str, credentials: tuple[CollectionCredential, ...]) -> CollectionCredential | None:
    if type(token) is not str or not token.isascii():
        return None
    match = None
    for credential in credentials:
        if secrets.compare_digest(token, credential.token):
            match = credential
    return match
