import copy

import pytest

from connector_support import BODY, KEY, NOW, assertion, challenge, context, pair, prepared, reserve
from sovite.connector.canonical import ProtocolError, encode
from sovite.connector.identity import digest, sign
from sovite.connector.protocol import (BODY_LIMIT, CHUNK_LIMIT, CONTROL_LIMIT, SCHEMAS, Session,
                                       decode_frame, encode_frame, pack)


def invoke(gateway, helper):
    helper.receive(gateway.send("invoke", request_id="request-1", operation="chat", body=pack(BODY),
                                deadline=NOW + 30, permit_ttl_ms=3000))
    gateway.receive(helper.send("accepted", request_id="request-1", **context()))


def test_all_message_schemas_and_lifecycle():
    gateway, helper = pair()
    reserve(gateway, helper)
    invoke(gateway, helper)
    gateway.receive(helper.send("chunk", request_id="request-1", data=pack(b'data: one\n\n')))
    helper.receive(gateway.send("permit", request_id="request-1", permit_ttl_ms=3000))
    gateway.receive(helper.send("heartbeat", ready=False, **context()))
    helper.receive(gateway.send("heartbeat", ready=True, **context()))
    gateway.receive(helper.send("complete", request_id="request-1", **context()))
    gateway.receive(helper.send("renew_csr", csr="schema-only-test-csr"))
    assert helper.phase == gateway.phase == "idle"
    assert set(SCHEMAS) == {"challenge", "hello", "heartbeat", "prepare", "prepared", "invoke", "accepted", "chunk",
                            "complete", "error", "permit", "cancel", "cancelled", "renew_csr"}


@pytest.mark.parametrize("phase", ["preparing", "prepared", "streaming"])
def test_BOUNDED_STOP_cancellation_acknowledgement(phase):
    gateway, helper = pair()
    if phase == "preparing":
        helper.receive(gateway.send("prepare", request_id="request-1", operation="chat", body_digest=digest(BODY), **context()))
    else:
        reserve(gateway, helper)
        if phase == "streaming":
            invoke(gateway, helper)
    helper.receive(gateway.send("cancel", request_id="request-1"))
    gateway.receive(helper.send("cancelled", request_id="request-1", **context()))
    assert helper.active is gateway.active is None


def test_replay_reused_sequence_and_out_of_order():
    for delta in (0, 2):
        gateway, helper = pair()
        message = {"type": "heartbeat", "version": 1, "session_id": "session-1", "seq": helper.sequences["helper"] + delta,
                   "ready": True, **context()}
        with pytest.raises(ProtocolError, match="sequence"):
            gateway.receive(encode_frame(message))
        assert gateway.phase == "closed"


def test_replay_reused_request_id_and_one_active_request():
    for finish in (False, True):
        gateway, helper = pair()
        reserve(gateway, helper)
        if finish:
            invoke(gateway, helper)
            gateway.receive(helper.send("complete", request_id="request-1", **context()))
        with pytest.raises(ProtocolError, match="duplicate|concurrent"):
            gateway.send("prepare", request_id="request-1", operation="chat", body_digest=digest(BODY), **context())


def test_NO_TRANSPARENT_REPLAY_disconnect_and_new_session():
    gateway, helper = pair()
    reserve(gateway, helper)
    gateway.close()
    with pytest.raises(ProtocolError, match="closed"):
        gateway.send("invoke", request_id="request-1", operation="chat", body=pack(BODY), deadline=NOW + 30, permit_ttl_ms=3000)
    replacement = Session("gateway", "session-2")
    old = {"type": "hello", "version": 1, "seq": 1, "session_id": "session-1", "identity": sign(assertion(), KEY)}
    with pytest.raises(ProtocolError, match="session"):
        replacement.receive(encode_frame(old))
    assert replacement.active is None


@pytest.mark.parametrize("kind,fields", [("complete", context()), ("accepted", context()), ("cancelled", context()),
                                          ("chunk", {"data": pack(b'x')}), ("error", {"code": "engine_unavailable"}),
                                          ("prepared", {"identity": sign(prepared(), KEY, prepared=True)})])
def test_INFERENCE_ONLY_unsolicited_responses(kind, fields):
    gateway, helper = pair()
    with pytest.raises(ProtocolError, match="unsolicited"):
        gateway.receive(encode_frame({"version": 1, "session_id": "session-1", "seq": 2, "type": kind, "request_id": "request-1", **fields}))


@pytest.mark.parametrize("kind,fields", [("prepare", {"operation": "chat", "body_digest": digest(BODY), **context()}),
                                          ("invoke", {"operation": "chat", "body": pack(BODY), "deadline": NOW + 30, "permit_ttl_ms": 3000}),
                                          ("cancel", {}), ("permit", {"permit_ttl_ms": 3000})])
def test_INFERENCE_ONLY_NO_REVERSE_ROUTE_helper_cannot_request(kind, fields):
    gateway, helper = pair()
    with pytest.raises(ProtocolError, match="direction"):
        gateway.receive(encode_frame({"version": 1, "session_id": "session-1", "seq": 2, "type": kind, "request_id": "request-1", **fields}))


def test_PIN_BEFORE_PROMPT_invoke_requires_preparation_and_matching_digest():
    for reserve_first in (False, True):
        gateway, helper = pair()
        if reserve_first:
            reserve(gateway, helper)
        with pytest.raises(ProtocolError):
            gateway.send("invoke", request_id="request-1", operation="chat", body=pack(b'wrong body'), deadline=NOW + 30, permit_ttl_ms=3000)


def test_SESSION_IDENTITY_epoch_change_at_completion():
    gateway, helper = pair()
    reserve(gateway, helper)
    invoke(gateway, helper)
    message = {"type": "complete", "version": 1, "session_id": "session-1", "seq": 4, "request_id": "request-1",
               **context(), "load_epoch": "new-epoch"}
    with pytest.raises(ProtocolError, match="preempted"):
        gateway.receive(encode_frame(message))


@pytest.mark.parametrize("change", [{"type": "CONNECT"}, {"url": "https://invalid.example"}, {"seq": True}, {"ready": "yes"},
                                     {"version": 2}, {"load_epoch": []}, {"session_id": "other"}])
def test_malformed_frames_unknown_extra_bad_types_and_cross_session(change):
    gateway, helper = pair()
    message = {"type": "heartbeat", "version": 1, "session_id": "session-1", "seq": 2, "ready": True, **context(), **change}
    with pytest.raises(ProtocolError):
        gateway.receive(encode(message))


def test_frame_bounds_control_chunk_body_and_result():
    with pytest.raises(ProtocolError):
        decode_frame(b' ' * (BODY_LIMIT * 2))
    with pytest.raises(ProtocolError):
        encode_frame({"type": "renew_csr", "version": 1, "session_id": "session-1", "seq": 1, "csr": 'x' * CONTROL_LIMIT})
    gateway, helper = pair()
    reserve(gateway, helper)
    invoke(gateway, helper)
    chunk = {"type": "chunk", "version": 1, "session_id": "session-1", "seq": 3, "request_id": "request-1", "data": pack(b'x' * CHUNK_LIMIT)}
    assert len(decode_frame(encode_frame(chunk))["data"]) > CHUNK_LIMIT
    with pytest.raises(ProtocolError):
        encode_frame({**chunk, "data": pack(b'x' * (CHUNK_LIMIT + 1))})
    with pytest.raises(ProtocolError):
        encode_frame({"type": "invoke", "version": 1, "session_id": "session-1", "seq": 1, "request_id": "r", "operation": "chat",
                      "body": pack(b'x' * 33), "deadline": NOW, "permit_ttl_ms": 3000}, body_limit=32)
    gateway.body_limit = helper.body_limit = 100
    with pytest.raises(ProtocolError):
        helper.send("chunk", request_id="request-1", data=pack(b'x' * 101))


def test_permit_repeated_ttl_and_error_terminal():
    gateway, helper = pair()
    reserve(gateway, helper)
    invoke(gateway, helper)
    helper.receive(gateway.send("permit", request_id="request-1", permit_ttl_ms=3000))
    gateway, helper = pair()
    reserve(gateway, helper)
    gateway.receive(helper.send("error", request_id="request-1", code="engine_unavailable"))
    assert gateway.phase == "idle"


def test_BOUNDED_STOP_cancel_crosses_inflight_chunks_and_completion():
    gateway, helper = pair()
    reserve(gateway, helper)
    invoke(gateway, helper)
    chunk = helper.send('chunk', request_id='request-1', data=pack(b'already emitted'))
    complete = helper.send('complete', request_id='request-1', **context())
    cancel = gateway.send('cancel', request_id='request-1')
    gateway.receive(chunk)
    gateway.receive(complete)
    assert gateway.phase == 'cancelling'
    helper.receive(cancel)
    gateway.receive(helper.send('cancelled', request_id='request-1', **context()))
    assert gateway.phase == helper.phase == 'idle'


def test_cancel_does_not_allow_unsolicited_chunks_before_acceptance():
    gateway, helper = pair()
    reserve(gateway, helper)
    helper.receive(gateway.send('cancel', request_id='request-1'))
    with pytest.raises(ProtocolError, match='unsolicited'):
        gateway.receive(encode_frame({'version': 1, 'session_id': 'session-1', 'seq': 3, 'type': 'chunk',
                                      'request_id': 'request-1', 'data': pack(b'unsolicited')}))


def schema_message(kind):
    samples = {'id': 'id', 'bool': True, 'time': NOW, 'ttl': 3000, 'seq': 1, 'operation': 'chat', 'digest': digest(BODY),
               'body': pack(BODY), 'chunk': pack(b'data'), 'error': 'engine_unavailable', 'csr': 'schema-only',
               'hello': sign(assertion(), KEY), 'prepared': sign(prepared(), KEY, prepared=True)}
    return {'version': 1, 'session_id': 'session-1', 'seq': 1, 'type': kind,
            **{field: samples[descriptor] for field, descriptor in SCHEMAS[kind].items()}}


@pytest.mark.parametrize('kind', sorted(SCHEMAS))
def test_every_schema_rejects_extra_and_missing_fields(kind):
    message = schema_message(kind)
    assert decode_frame(encode_frame(message)) == message
    with pytest.raises(ProtocolError):
        decode_frame(encode({**message, 'extra': True}))
    for field in SCHEMAS[kind]:
        with pytest.raises(ProtocolError):
            encode_frame({k: v for k, v in message.items() if k != field})


@pytest.mark.parametrize('kind,field', [(kind, field) for kind, schema in SCHEMAS.items() for field in schema])
def test_every_schema_rejects_bad_field_types(kind, field):
    with pytest.raises(ProtocolError):
        encode_frame({**schema_message(kind), field: []})


@pytest.mark.parametrize('disguise', [False, True])
def test_frame_size_before_parsing_oversize_control_without_full_decode(monkeypatch, disguise):
    import sovite.connector.protocol as protocol
    body = b'x' * (CONTROL_LIMIT + 1)
    raw = (b'{"body":"' + body + b'","type":"heartbeat"}' if disguise
           else b'{"csr":"' + body + b'","type":"renew_csr"}')
    original = protocol.decode
    calls = []
    def bounded(data, **kwargs):
        calls.append(len(data))
        assert len(data) <= CONTROL_LIMIT, 'oversized control reached JSON decoder'
        return original(data, **kwargs)
    monkeypatch.setattr(protocol, 'decode', bounded)
    with pytest.raises(ProtocolError):
        decode_frame(raw)
    assert not calls if not disguise else max(calls) <= CONTROL_LIMIT


@pytest.mark.parametrize('ttl', [0, -1, 3001, True, 1.5, '3000'])
@pytest.mark.parametrize('kind', ['invoke', 'permit'])
def test_permit_clock_skew_ttl_bounds(kind, ttl):
    with pytest.raises(ProtocolError):
        encode_frame({**schema_message(kind), 'permit_ttl_ms': ttl})


def test_permit_clock_skew_replacement_renewals():
    gateway, helper = pair()
    reserve(gateway, helper)
    invoke(gateway, helper)
    for ttl in (3000, 3000, 1, 2000):
        helper.receive(gateway.send('permit', request_id='request-1', permit_ttl_ms=ttl))
    assert helper.phase == 'streaming'
