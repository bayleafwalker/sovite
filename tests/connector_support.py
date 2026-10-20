"""Public synthetic identities. This fixed seed is TEST-ONLY and never trusted by runtime."""
import copy

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sovite.connector.identity import ExpectedIdentity, digest, manifest, sign
from sovite.connector.protocol import Session


TEST_ONLY_SEED = bytes(range(32))
KEY = Ed25519PrivateKey.from_private_bytes(TEST_ONLY_SEED)
NOW = 1800000000
WEIGHTS = [{"name": "model.gguf", "size": 7, "sha256": digest(b'weights')}]
ASSERTION = {
    "installation_id": "installation-test", "binding_id": "binding-a", "enrollment_id": "enrollment-a", "enrollment_role": "pilot-role",
    "helper_image_digest": "sha256:" + "1" * 64, "engine_image_digest": "sha256:" + "2" * 64,
    "engine_version": "test-engine-1", "model_id": "test-model", "load_epoch": "epoch-1",
    "weight_files": WEIGHTS, "weights_manifest_digest": manifest(WEIGHTS), "host_instance_id": "host-1",
    "engine_process_instance": "process-1", "session_id": "session-1", "wire_apis": ["chat"],
    "cancellation": "engine-cooperative", "challenge": "nonce-1", "issued_at": NOW, "expires_at": NOW + 30,
}
EXPECTED = ExpectedIdentity(**{k: ASSERTION[k] for k in ExpectedIdentity.__dataclass_fields__})
BINDING = {"id": "binding-a", "model": "test-model", "destination": "destination-a", "wire_api": "chat",
           "connection": {"kind": "model-gateway", "enrollment_role": "pilot-role"},
           "identity": {"protocol": "helper-signed-v1", "pinning": "required", "engine_version": "test-engine-1", "weights_manifest_digest": manifest(WEIGHTS)}}
ROLE = {"id": "pilot-role", "destination": "destination-a", "helper_image_digest": ASSERTION["helper_image_digest"], "engine_image_digest": ASSERTION["engine_image_digest"]}
BODY = b'{"model":"test-model","messages":[{"role":"user","content":"private-canary"}],"stream":true}'


def assertion(**changes):
    return {**copy.deepcopy(ASSERTION), **changes}


def prepared(**changes):
    return assertion(**{"request_id": "request-1", "body_digest": digest(BODY), "operation": "chat", **changes})


def context(a=ASSERTION):
    return {k: a[k] for k in ("load_epoch", "engine_process_instance", "host_instance_id")}


def challenge(a=ASSERTION):
    return {**{k: a[k] for k in ("installation_id", "binding_id", "enrollment_id", "enrollment_role")}, "nonce": a["challenge"]}


def pair():
    gateway, helper = Session("gateway", "session-1"), Session("helper", "session-1")
    helper.receive(gateway.send("challenge", **challenge()))
    gateway.receive(helper.send("hello", identity=sign(assertion(), KEY)))
    return gateway, helper


def reserve(gateway, helper):
    helper.receive(gateway.send("prepare", request_id="request-1", operation="chat", body_digest=digest(BODY), **context()))
    gateway.receive(helper.send("prepared", request_id="request-1", identity=sign(prepared(), KEY, prepared=True)))
