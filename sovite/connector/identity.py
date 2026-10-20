"""helper-signed-v1 assertions; signatures identify an enrolled helper, not hardware."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path, PurePosixPath
import re

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .canonical import ProtocolError, decode, encode


DOMAIN = b"sovite/helper-signed-v1\x00"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
WIRE_APIS = {"chat", "responses", "ollama-chat"}
FIELDS = {"installation_id", "binding_id", "enrollment_id", "enrollment_role", "helper_image_digest",
          "engine_image_digest", "engine_version", "model_id", "load_epoch", "weight_files",
          "weights_manifest_digest", "host_instance_id", "engine_process_instance", "session_id",
          "wire_apis", "cancellation", "challenge", "issued_at", "expires_at"}


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def text(value, maximum=512):
    if type(value) is not str or not value or len(value.encode("utf-8")) > maximum:
        raise ProtocolError("invalid identity string")


def integer(value, minimum=0):
    if type(value) is not int or not minimum <= value <= 2**53 - 1:
        raise ProtocolError("invalid identity integer")


def manifest(files: list) -> str:
    if type(files) is not list or not 1 <= len(files) <= 256:
        raise ProtocolError("weight manifest required")
    names = []
    for item in files:
        if type(item) is not dict or set(item) != {"name", "size", "sha256"}:
            raise ProtocolError("invalid weight file")
        text(item["name"], 1024)
        name = PurePosixPath(item["name"])
        if not name.parts or name.is_absolute() or '..' in name.parts or str(name) != item["name"] or '\\' in item["name"]:
            raise ProtocolError("invalid weight name")
        integer(item["size"])
        if type(item["sha256"]) is not str or not DIGEST.fullmatch(item["sha256"]):
            raise ProtocolError("invalid weight hash")
        names.append(item["name"])
    if names != sorted(set(names)):
        raise ProtocolError("weight files must be sorted and unique")
    return digest(encode(files))


def observe_weights(paths: dict[str, str]) -> list:
    files = []
    for name, path in sorted(paths.items()):
        checksum, size = hashlib.sha256(), 0
        with Path(path).open('rb') as source:
            while block := source.read(1024 * 1024):
                checksum.update(block)
                size += len(block)
        files.append({"name": name, "size": size, "sha256": "sha256:" + checksum.hexdigest()})
    manifest(files)
    return files


def validate_assertion(assertion: dict, prepared=False):
    expected = FIELDS | ({"request_id", "body_digest", "operation"} if prepared else set())
    if type(assertion) is not dict or set(assertion) != expected:
        raise ProtocolError("invalid assertion fields")
    for field in expected - {"weight_files", "wire_apis", "issued_at", "expires_at"}:
        text(assertion[field], 1024 if field == "challenge" else 512)
    for field in {"helper_image_digest", "engine_image_digest", "weights_manifest_digest"} | ({"body_digest"} if prepared else set()):
        if not DIGEST.fullmatch(assertion[field]):
            raise ProtocolError("invalid assertion digest")
    if manifest(assertion["weight_files"]) != assertion["weights_manifest_digest"]:
        raise ProtocolError("weight manifest digest mismatch")
    apis = assertion["wire_apis"]
    if type(apis) is not list or len(apis) != 1 or type(apis[0]) is not str or apis[0] not in WIRE_APIS:
        raise ProtocolError("invalid wire APIs")
    if prepared and assertion["operation"] not in WIRE_APIS:
        raise ProtocolError("invalid preparation operation")
    if assertion["cancellation"] not in {"disconnect-only", "engine-cooperative", "supervised-engine"}:
        raise ProtocolError("unsupported cancellation mechanism")
    for field in ("issued_at", "expires_at"):
        integer(assertion[field])
    if len(encode(assertion)) > 65536:
        raise ProtocolError("assertion size limit")


def sign(assertion: dict, key: Ed25519PrivateKey, *, prepared=False) -> dict:
    validate_assertion(assertion, prepared)
    return {"assertion": assertion, "signature": key.sign(DOMAIN + encode(assertion)).hex()}


def verify_signature(signed: dict, key: Ed25519PublicKey, *, prepared=False) -> dict:
    if type(signed) is not dict or set(signed) != {"assertion", "signature"}:
        raise ProtocolError("invalid signed assertion")
    assertion = signed["assertion"]
    validate_assertion(assertion, prepared)
    signature = signed["signature"]
    if type(signature) is not str or not re.fullmatch('[0-9a-f]{128}', signature):
        raise ProtocolError("invalid signature encoding")
    try:
        key.verify(bytes.fromhex(signature), DOMAIN + encode(assertion))
    except InvalidSignature:
        raise ProtocolError("identity signature mismatch") from None
    return assertion


@dataclass(frozen=True)
class ExpectedIdentity:
    installation_id: str
    binding_id: str
    enrollment_id: str
    enrollment_role: str
    session_id: str
    challenge: str


@dataclass(frozen=True)
class Verification:
    assertion: dict
    pinning: str


def verify_connection(signed, key, expected: ExpectedIdentity, *, now: int, clock_skew=1, prepared=False) -> Verification:
    integer(now)
    if type(clock_skew) is not int or not 0 <= clock_skew <= 1:
        raise ProtocolError("clock skew exceeds bound")
    assertion = verify_signature(signed, key, prepared=prepared)
    for field, value in vars(expected).items():
        if assertion[field] != value:
            raise ProtocolError("EXACT_BINDING identity mismatch: " + field)
    issued, expires = assertion["issued_at"], assertion["expires_at"]
    if issued > now + clock_skew or expires <= now or not issued < expires <= issued + 60 or now - issued > 60 + clock_skew:
        raise ProtocolError("identity expiry or clock skew")
    return Verification(decode(encode(assertion)), "connection-only")


def verify_preparation(signed, key, expected, connection: Verification, binding: dict, role: dict,
                       *, request_id: str, body_digest: str, now: int, mode: str) -> Verification:
    result = verify_connection(signed, key, expected, now=now, prepared=True)
    assertion, original = result.assertion, connection.assertion
    if assertion["request_id"] != request_id or assertion["body_digest"] != body_digest:
        raise ProtocolError("request preparation mismatch")
    # SESSION_IDENTITY: even a correctly signed replacement must not receive this prompt.
    for field in FIELDS - {"issued_at", "expires_at"}:
        if assertion[field] != original[field]:
            raise ProtocolError("model_host_preempted: " + field)
    if mode not in {"development", "enforced"}:
        raise ProtocolError("unsupported mode")
    if mode == "enforced" and assertion["cancellation"] == "disconnect-only":
        raise ProtocolError("unqualified cancellation mechanism")
    identity = binding["identity"]
    if (binding["id"] != expected.binding_id or binding["connection"]["kind"] != "model-gateway"
            or binding["connection"]["enrollment_role"] != expected.enrollment_role
            or role["id"] != expected.enrollment_role or role["destination"] != binding["destination"]
            or assertion["model_id"] != binding["model"] or assertion["wire_apis"] != [binding["wire_api"]]
            or assertion["operation"] != binding["wire_api"]
            or identity["protocol"] != "helper-signed-v1"):
        raise ProtocolError("EXACT_BINDING configuration mismatch")
    pinning = identity["pinning"]
    if pinning == "unpinned-development":
        if mode != "development":
            raise ProtocolError("unpinned identity requires development mode")
    elif pinning == "required":
        pins = {"engine_version": identity.get("engine_version"), "weights_manifest_digest": identity.get("weights_manifest_digest"),
                "helper_image_digest": role["helper_image_digest"], "engine_image_digest": role["engine_image_digest"]}
        if any(assertion[field] != value for field, value in pins.items()):
            raise ProtocolError("PIN_BEFORE_PROMPT identity pin mismatch")
    else:
        raise ProtocolError("unsupported pinning")
    return Verification(result.assertion, pinning)


def verify_continuity(prepared: Verification, *, session_id: str, load_epoch: str,
                      engine_process_instance: str, host_instance_id: str):
    for field, value in (("session_id", session_id), ("load_epoch", load_epoch),
                         ("engine_process_instance", engine_process_instance), ("host_instance_id", host_instance_id)):
        if prepared.assertion[field] != value:
            raise ProtocolError("model_host_preempted")
