import copy
from dataclasses import replace
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization

from connector_support import (ASSERTION, BINDING, BODY, EXPECTED, KEY, NOW, ROLE, TEST_ONLY_SEED,
                               assertion, context, prepared)
from sovite.connector.canonical import ProtocolError, decode, encode
from sovite.connector.identity import (DOMAIN, digest, manifest, observe_weights, sign, verify_connection,
                                       verify_continuity, verify_preparation, verify_signature)


def connection():
    return verify_connection(sign(assertion(), KEY), KEY.public_key(), EXPECTED, now=NOW)


def preparation(a=None, binding=None, role=None, mode="enforced"):
    return verify_preparation(sign(a or prepared(), KEY, prepared=True), KEY.public_key(), EXPECTED, connection(),
                              binding or BINDING, role or ROLE, request_id="request-1", body_digest=digest(BODY), now=NOW, mode=mode)


@pytest.mark.parametrize("field", sorted(ASSERTION))
def test_tampering_every_signed_field(field):
    signed = sign(assertion(), KEY)
    value = signed["assertion"][field]
    if type(value) is int:
        signed["assertion"][field] += 1
    elif type(value) is list:
        signed["assertion"][field] = list(reversed(value)) if len(value) > 1 else []
    else:
        signed["assertion"][field] += 'x'
    with pytest.raises(ProtocolError):
        verify_signature(signed, KEY.public_key())


@pytest.mark.parametrize("field", ["request_id", "body_digest", "operation"])
def test_tampering_prepared_signed_fields(field):
    signed = sign(prepared(), KEY, prepared=True)
    signed["assertion"][field] = "other" if field == "request_id" else "sha256:" + "0" * 64
    with pytest.raises(ProtocolError):
        verify_signature(signed, KEY.public_key(), prepared=True)


@pytest.mark.parametrize("field", ["installation_id", "binding_id", "enrollment_id", "enrollment_role", "session_id", "challenge"])
def test_EXACT_BINDING_SESSION_IDENTITY_cross_binding_and_old_nonce(field):
    expected = replace(EXPECTED, **{field: "other"})
    with pytest.raises(ProtocolError, match=field):
        verify_connection(sign(assertion(), KEY), KEY.public_key(), expected, now=NOW)


@pytest.mark.parametrize("issued,expires,now", [(NOW, NOW + 30, NOW + 30), (NOW + 2, NOW + 30, NOW),
                                                 (NOW, NOW, NOW), (NOW, NOW + 61, NOW)])
def test_identity_expiry_clock_skew(issued, expires, now):
    with pytest.raises(ProtocolError, match="expiry|skew"):
        verify_connection(sign(assertion(issued_at=issued, expires_at=expires), KEY), KEY.public_key(), EXPECTED, now=now)


def test_clock_skew_is_bounded():
    verify_connection(sign(assertion(issued_at=NOW + 1), KEY), KEY.public_key(), EXPECTED, now=NOW)
    with pytest.raises(ProtocolError, match="skew"):
        verify_connection(sign(assertion(), KEY), KEY.public_key(), EXPECTED, now=NOW, clock_skew=2)


@pytest.mark.parametrize("field", ["engine_version", "weights_manifest_digest", "helper_image_digest", "engine_image_digest"])
def test_PIN_BEFORE_PROMPT_each_v1alpha2_pin(field):
    binding, role = copy.deepcopy(BINDING), copy.deepcopy(ROLE)
    target = binding["identity"] if field in binding["identity"] else role
    target[field] = "different" if field == "engine_version" else "sha256:" + "0" * 64
    with pytest.raises(ProtocolError, match="PIN_BEFORE_PROMPT"):
        preparation(binding=binding, role=role)


@pytest.mark.parametrize("field", ["load_epoch", "engine_process_instance", "host_instance_id"])
def test_SESSION_IDENTITY_epoch_change_hello_prepare_refused(field):
    with pytest.raises(ProtocolError, match="preempted"):
        preparation(prepared(**{field: "replacement"}))


@pytest.mark.parametrize("field", ["session_id", "load_epoch", "engine_process_instance", "host_instance_id"])
def test_SESSION_IDENTITY_epoch_change_prepared_complete_preempted(field):
    values = {"session_id": EXPECTED.session_id, **context()}
    verify_continuity(preparation(), **values)
    with pytest.raises(ProtocolError, match="preempted"):
        verify_continuity(preparation(), **{**values, field: "replacement"})


def test_unpinned_development_visible_and_denied_enforced():
    binding = copy.deepcopy(BINDING)
    binding["identity"] = {"protocol": "helper-signed-v1", "pinning": "unpinned-development"}
    assert preparation(binding=binding, mode="development").pinning == "unpinned-development"
    with pytest.raises(ProtocolError, match="development"):
        preparation(binding=binding)


def test_weight_manifest_actual_file_hashes_not_ollama_alias(tmp_path):
    (tmp_path / "a").write_bytes(b'actual-file')
    files = observe_weights({"model.gguf": str(tmp_path / "a")})
    assert files == [{"name": "model.gguf", "size": 11, "sha256": digest(b'actual-file')}]
    assert manifest(files) == digest(encode(files))
    for bad in ([], files * 2, [{**files[0], "name": "../escape"}], [{**files[0], "size": True}]):
        with pytest.raises(ProtocolError):
            manifest(bad)


@pytest.mark.parametrize("data", [b'{"a":1,"a":2}', b'{"b":1,"a":2}', b'{ "a":1}', b'1.0', b'-0', b'01', b'1e0',
                                   b'9007199254740992', b'NaN', b'null', b'"\\u0061"', b'"\\ud800"', b'"\\n"', b'"\xff"', b'true\n'])
def test_malformed_noncanonical_duplicate_keys_floats(data):
    with pytest.raises(ProtocolError):
        decode(data)


def test_canonical_roundtrip_and_bounds():
    value = {"é": ['\n', '"', '\\', True, False, -1, 2**53 - 1], "a": {}}
    assert decode(encode(value)) == value
    for value in (None, 1.0, 2**53, '\ud800', 'x' * 65537, [0] * 1025, [[[[[0]]]]] * 1025):
        with pytest.raises(ProtocolError):
            encode(value)
    with pytest.raises(ProtocolError):
        decode(b'[' * 26 + b'0' + b']' * 26)


def vectors():
    hello, ready = sign(assertion(), KEY), sign(prepared(), KEY, prepared=True)
    from test_connector_protocol import schema_message
    from sovite.connector.protocol import encode_frame
    return {"format": "sovite.connector-v1.vectors", "test_only_seed_hex": TEST_ONLY_SEED.hex(),
            "public_key_hex": KEY.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex(),
            "domain_hex": DOMAIN.hex(), "now": NOW,
            "frames": [{"message": message, "canonical_utf8": encode_frame(message).decode()}
                       for message in (schema_message('invoke'), schema_message('permit'))],
            "assertions": [{"kind": kind, "canonical_utf8": encode(signed["assertion"]).decode(), "signed": signed,
                            "expected": "verified"} for kind, signed in (("hello", hello), ("prepared", ready))],
            "negative": [
                {"case": "old-nonce", "signed": hello, "expected_identity": {**vars(EXPECTED), "challenge": "old"}, "expected": "rejected"},
                {"case": "tampered", "signed": {**hello, "assertion": {**hello["assertion"], "engine_version": "tampered"}}, "expected": "rejected"},
                {"case": "epoch-changed", "signed": sign(prepared(load_epoch="new"), KEY, prepared=True), "expected": "model_host_preempted"}],
            "invalid_canonical_hex": [raw.hex() for raw in (b'{"a":1,"a":2}', b'1.0', b'-0', b'1e0')],
            "canonical": {"value": {"a": [True, -1, "é\n"]}, "utf8_hex": encode({"a": [True, -1, "é\n"]}).hex()}}


def test_published_vectors_regenerate_and_verify():
    stored = json.loads((Path(__file__).parent / "vectors/connector-v1.json").read_text())
    assert stored == vectors()
    from sovite.connector.protocol import decode_frame
    for item in stored['frames']:
        assert decode_frame(item['canonical_utf8'].encode()) == item['message']
    for item in stored["assertions"]:
        assert decode(item["canonical_utf8"].encode()) == item["signed"]["assertion"]
        verify_signature(item["signed"], KEY.public_key(), prepared=item["kind"] == "prepared")
    assert preparation().pinning == "required"
    from sovite.connector.identity import ExpectedIdentity
    for item in stored["negative"]:
        with pytest.raises(ProtocolError) as failure:
            if item["case"] == "old-nonce":
                verify_connection(item["signed"], KEY.public_key(), ExpectedIdentity(**item["expected_identity"]), now=NOW)
            elif item["case"] == "tampered":
                verify_signature(item["signed"], KEY.public_key())
            else:
                verify_preparation(item["signed"], KEY.public_key(), EXPECTED, connection(), BINDING, ROLE,
                                   request_id="request-1", body_digest=digest(BODY), now=NOW, mode="enforced")
        if item["expected"] == "model_host_preempted":
            assert "model_host_preempted" in str(failure.value)
    for raw in stored["invalid_canonical_hex"]:
        with pytest.raises(ProtocolError):
            decode(bytes.fromhex(raw))


@pytest.mark.parametrize('field', ['name', 'size', 'sha256'])
def test_tampering_weight_manifest_each_file_field(field):
    signed = sign(assertion(), KEY)
    file = signed['assertion']['weight_files'][0]
    file[field] = {'name': 'replacement.gguf', 'size': 8, 'sha256': digest(b'replacement')}[field]
    signed['assertion']['weights_manifest_digest'] = manifest(signed['assertion']['weight_files'])
    with pytest.raises(ProtocolError, match='signature'):
        verify_signature(signed, KEY.public_key())


def test_identity_domain_separation_and_wrong_enrolled_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    signed = sign(assertion(), KEY)
    signed['signature'] = KEY.sign(encode(signed['assertion'])).hex()
    with pytest.raises(ProtocolError, match='signature'):
        verify_signature(signed, KEY.public_key())
    with pytest.raises(ProtocolError, match='signature'):
        verify_signature(sign(assertion(), KEY), Ed25519PrivateKey.generate().public_key())


def test_enforced_refuses_disconnect_only_even_with_matching_pins():
    a = assertion(cancellation='disconnect-only')
    connection = verify_connection(sign(a, KEY), KEY.public_key(), EXPECTED, now=NOW)
    scoped = {**a, 'operation': 'chat', 'request_id': 'request-1', 'body_digest': digest(BODY)}
    with pytest.raises(ProtocolError, match='unqualified'):
        verify_preparation(sign(scoped, KEY, prepared=True), KEY.public_key(), EXPECTED, connection, BINDING, ROLE,
                           request_id='request-1', body_digest=digest(BODY), now=NOW, mode='enforced')


def test_operation_not_bound_to_advertised_wire_api_signed_preparation():
    with pytest.raises(ProtocolError, match='EXACT_BINDING'):
        preparation(prepared(operation='responses'))
    signed = sign(prepared(), KEY, prepared=True)
    signed['assertion']['operation'] = 'responses'
    with pytest.raises(ProtocolError, match='signature'):
        verify_signature(signed, KEY.public_key(), prepared=True)
    for apis in ([], ['chat', 'responses']):
        with pytest.raises(ProtocolError, match='wire APIs'):
            sign(assertion(wire_apis=apis), KEY)
