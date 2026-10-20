"""Strict transport-independent envelopes and one-request connector sessions."""
from __future__ import annotations

import base64
import binascii
import copy
from dataclasses import dataclass
import re

from .canonical import ProtocolError, decode, encode
from .identity import DIGEST, WIRE_APIS, digest, validate_assertion


VERSION = 1
SUBPROTOCOL = "sovite-inference.v1"
CONTROL_LIMIT = CHUNK_LIMIT = 65536
BODY_LIMIT = 4 * 1024 * 1024
PERMIT_SECONDS = 3
HEARTBEAT_SECONDS = 2
FRESHNESS_SECONDS = 6
ERROR_CODES = {"model_host_preempted", "engine_unavailable", "invalid_request", "deadline_exceeded",
               "permit_expired", "result_too_large", "engine_protocol_error"}
CONTEXT = {"load_epoch": "id", "engine_process_instance": "id", "host_instance_id": "id"}
SCHEMAS = {
    "challenge": {"nonce": "id", "installation_id": "id", "binding_id": "id", "enrollment_id": "id", "enrollment_role": "id"},
    "hello": {"identity": "hello"},
    "heartbeat": {"ready": "bool", **CONTEXT},
    "prepare": {"request_id": "id", "operation": "operation", "body_digest": "digest", **CONTEXT},
    "prepared": {"request_id": "id", "identity": "prepared"},
    "invoke": {"request_id": "id", "operation": "operation", "body": "body", "deadline": "time", "permit_ttl_ms": "ttl"},
    "accepted": {"request_id": "id", **CONTEXT},
    "chunk": {"request_id": "id", "data": "chunk"},
    "complete": {"request_id": "id", **CONTEXT},
    "error": {"request_id": "id", "code": "error"},
    "permit": {"request_id": "id", "permit_ttl_ms": "ttl"},
    "cancel": {"request_id": "id"},
    "cancelled": {"request_id": "id", **CONTEXT},
    "renew_csr": {"csr": "csr"},
}
GATEWAY_MESSAGES = {"challenge", "prepare", "invoke", "permit", "cancel", "heartbeat"}
HELPER_MESSAGES = set(SCHEMAS) - GATEWAY_MESSAGES | {"heartbeat"}
BASE = {"version", "session_id", "seq", "type"}


def pack(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unpack(value: str, limit: int) -> bytes:
    if type(value) is not str or len(value) > 4 * ((limit + 2) // 3):
        raise ProtocolError("payload size or type")
    try:
        result = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ProtocolError("invalid base64") from None
    if len(result) > limit or pack(result) != value:
        raise ProtocolError("noncanonical or oversized base64")
    return result


def validate(message: dict, body_limit=BODY_LIMIT, *, envelope=False):
    if type(body_limit) is not int or not 1 <= body_limit <= BODY_LIMIT:
        raise ProtocolError("invalid body limit")
    if type(message) is not dict or type(message.get("type")) is not str or message["type"] not in SCHEMAS:
        raise ProtocolError("unknown message")
    schema = SCHEMAS[message["type"]]
    if set(message) != BASE | set(schema) or type(message["version"]) is not int or message["version"] != VERSION:
        raise ProtocolError("invalid message fields or version")
    checks = {"session_id": "id", "seq": "seq", **schema}
    for field, kind in checks.items():
        value = message[field]
        if kind == "id":
            valid = type(value) is str and re.fullmatch(r'[A-Za-z0-9_.:-]{1,256}', value)
        elif kind in {"time", "seq"}:
            valid = type(value) is int and (1 if kind == "seq" else 0) <= value <= 2**53 - 1
        elif kind == "bool":
            valid = type(value) is bool
        elif kind == "ttl":
            valid = type(value) is int and 1 <= value <= PERMIT_SECONDS * 1000
        elif kind in {"hello", "prepared"}:
            valid = type(value) is dict and set(value) == {"assertion", "signature"}
            if valid:
                validate_assertion(value["assertion"], kind == "prepared")
                valid = type(value["signature"]) is str and re.fullmatch('[0-9a-f]{128}', value["signature"])
        elif kind in {"body", "chunk"}:
            if not envelope:
                unpack(value, body_limit if kind == "body" else CHUNK_LIMIT)
            valid = True
        elif kind == "digest":
            valid = type(value) is str and DIGEST.fullmatch(value)
        elif kind == "operation":
            valid = type(value) is str and value in WIRE_APIS
        elif kind == "error":
            valid = type(value) is str and value in ERROR_CODES
        elif kind == "csr":
            valid = type(value) is str and 1 <= len(value.encode()) <= 16384
        else:
            valid = False
        if not valid:
            raise ProtocolError("invalid message field: " + field)
    raw = encode(message, max_string=max(CONTROL_LIMIT, 4 * ((body_limit + 2) // 3)))
    limit = (4 * ((body_limit + 2) // 3) + CONTROL_LIMIT if message["type"] == "invoke"
             else 4 * ((CHUNK_LIMIT + 2) // 3) + 1024 if message["type"] == "chunk" else CONTROL_LIMIT)
    if len(raw) > limit:
        raise ProtocolError("frame size limit")


def encode_frame(message: dict, body_limit=BODY_LIMIT) -> bytes:
    validate(message, body_limit)
    return encode(message, max_string=max(CONTROL_LIMIT, 4 * ((body_limit + 2) // 3)))


@dataclass(frozen=True)
class Envelope:
    message: dict
    encoded_payload: bytes | None = None

    def payload(self, limit):
        if self.encoded_payload is None:
            raise ProtocolError("missing payload")
        try:
            return unpack(self.encoded_payload.decode('ascii'), limit)
        except UnicodeError:
            raise ProtocolError("invalid base64") from None


def decode_envelope(data: bytes, body_limit=BODY_LIMIT) -> Envelope:
    """Cap JSON parsing before decoding. Canonical body/data is the first key.

    Only opaque base64 can be detached; every other byte must fit the capped
    envelope. This also rejects oversized controls disguised with a body key.
    Payload validation can run in a worker while the event loop handles cancel.
    """
    if type(body_limit) is not int or not 1 <= body_limit <= BODY_LIMIT:
        raise ProtocolError("invalid body limit")
    if type(data) is not bytes or len(data) > 4 * ((body_limit + 2) // 3) + CONTROL_LIMIT:
        raise ProtocolError("frame size limit")
    for field, kind, limit in ((b'body', 'invoke', body_limit), (b'data', 'chunk', CHUNK_LIMIT)):
        prefix = b'{"' + field + b'":"'
        if not data.startswith(prefix):
            continue
        end = data.find(b'"', len(prefix))
        if end < 0 or end - len(prefix) > 4 * ((limit + 2) // 3) or len(data) - end + len(prefix) > CONTROL_LIMIT:
            raise ProtocolError("frame size limit")
        capped = data[:len(prefix)] + data[end:]
        message = decode(capped, limit=CONTROL_LIMIT)
        if message.get('type') != kind:
            raise ProtocolError("frame size limit")
        validate(message, body_limit, envelope=True)
        return Envelope(message, data[len(prefix):end])
    if len(data) > CONTROL_LIMIT:
        raise ProtocolError("frame size limit")
    message = decode(data, limit=CONTROL_LIMIT)
    validate(message, body_limit)
    return Envelope(message)


def decode_frame(data: bytes, body_limit=BODY_LIMIT) -> dict:
    frame = decode_envelope(data, body_limit)
    if frame.encoded_payload is not None:
        frame.payload(body_limit if frame.message['type'] == 'invoke' else CHUNK_LIMIT)
        frame.message['body' if frame.message['type'] == 'invoke' else 'data'] = frame.encoded_payload.decode('ascii')
    return frame.message


class Session:
    """A closed session is terminal. Construct a new object after reconnect; never resume."""
    def __init__(self, side: str, session_id: str, *, body_limit=BODY_LIMIT):
        if (side not in {"gateway", "helper"} or type(session_id) is not str
                or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", session_id)
                or type(body_limit) is not int or not 1 <= body_limit <= BODY_LIMIT):
            raise ProtocolError("invalid session side")
        self.side, self.session_id, self.body_limit = side, session_id, body_limit
        self.sequences = {"gateway": 0, "helper": 0}
        self.phase, self.active, self.total = "new", None, 0
        self.seen, self.challenge, self.connection = set(), None, None
        self.request, self.prepared = None, None
        self.last_finished, self.cancel_phase = None, None

    def close(self):
        self.phase, self.active = "closed", None

    def send(self, kind: str, **fields) -> bytes:
        message = {"version": VERSION, "session_id": self.session_id, "seq": self.sequences[self.side] + 1,
                   "type": kind, **fields}
        data = encode_frame(message, self.body_limit)
        self._apply(message, self.side)
        return data

    def receive(self, data: bytes) -> dict:
        try:
            message = decode_frame(data, self.body_limit)
            self._apply(message, "helper" if self.side == "gateway" else "gateway")
            return message
        except ProtocolError:
            self.close()
            raise

    def receive_envelope(self, frame: Envelope) -> dict:
        """Helper-only header admission; caller MUST verify payload before engine I/O."""
        if self.side != 'helper' or frame.message['type'] == 'chunk':
            raise ProtocolError("gateway envelope required")
        self._apply(frame.message, 'gateway', pending_payload=frame.message['type'] == 'invoke')
        return frame.message

    def _apply(self, message, sender, *, pending_payload=False):
        if self.phase == "closed":
            raise ProtocolError("NO_TRANSPARENT_REPLAY: closed session")
        try:
            self._transition(message, sender, pending_payload=pending_payload)
        except ProtocolError:
            self.close()
            raise

    def _transition(self, m, sender, *, pending_payload=False):
        kind = m["type"]
        allowed = GATEWAY_MESSAGES if sender == "gateway" else HELPER_MESSAGES
        if kind not in allowed or m["session_id"] != self.session_id or m["seq"] != self.sequences[sender] + 1:
            raise ProtocolError("INFERENCE_ONLY: direction, session or sequence mismatch")
        if kind == "challenge":
            if self.phase != "new":
                raise ProtocolError("repeated challenge")
            self.challenge, self.phase = copy.deepcopy(m), "challenged"
        elif kind == "hello":
            if self.phase != "challenged":
                raise ProtocolError("unsolicited hello")
            a = m["identity"]["assertion"]
            for field in ("installation_id", "binding_id", "enrollment_id", "enrollment_role"):
                if a[field] != self.challenge[field]:
                    raise ProtocolError("EXACT_BINDING hello mismatch")
            if a["challenge"] != self.challenge["nonce"] or a["session_id"] != self.session_id:
                raise ProtocolError("SESSION_IDENTITY hello mismatch")
            self.connection, self.phase = copy.deepcopy(a), "idle"
        elif self.phase in {"new", "challenged"}:
            raise ProtocolError("handshake required")
        elif kind == "heartbeat":
            if sender == "helper":
                self._context(m)
        elif kind == "renew_csr":
            pass
        elif kind == "prepare":
            if self.phase != "idle" or m["request_id"] in self.seen:
                raise ProtocolError("duplicate or concurrent request")
            self._context(m)
            if len(self.seen) >= 1024:
                raise ProtocolError("session request limit; reconnect required")
            self.seen.add(m["request_id"])
            self.active, self.request, self.phase, self.total = m["request_id"], copy.deepcopy(m), "preparing", 0
        else:
            if self.active is None and m.get("request_id") == self.last_finished:
                if kind == "cancel":
                    self.active, self.phase = self.last_finished, "finished"
                elif kind == "permit":
                    self.sequences[sender] = m["seq"]
                    return
            if self.active is None or m.get("request_id") != self.active:
                raise ProtocolError("unsolicited response or request ID")
            response_phase = self.cancel_phase if self.phase == "cancelling" else self.phase
            if kind == "prepared":
                if response_phase != "preparing":
                    raise ProtocolError("unsolicited prepared")
                a = m["identity"]["assertion"]
                if (a["request_id"] != self.active or a["body_digest"] != self.request["body_digest"]
                        or a["operation"] != self.request["operation"] or a['wire_apis'] != [a['operation']]):
                    raise ProtocolError("preparation digest mismatch")
                for field in self.connection.keys() - {"issued_at", "expires_at"}:
                    if a[field] != self.connection[field]:
                        raise ProtocolError("model_host_preempted")
                self.prepared = copy.deepcopy(a)
                self._response_phase("prepared")
            elif kind == "invoke":
                if (self.phase != "prepared" or m["operation"] != self.request["operation"]
                        or not pending_payload and digest(unpack(m["body"], self.body_limit)) != self.request["body_digest"]):
                    raise ProtocolError("PIN_BEFORE_PROMPT: unprepared invocation")
                self.phase = "invoked"
            elif kind == "accepted":
                if response_phase != "invoked":
                    raise ProtocolError("unsolicited accepted")
                self._context(m)
                self._response_phase("streaming")
            elif kind == "chunk":
                if response_phase != "streaming":
                    raise ProtocolError("unsolicited chunk")
                self.total += len(unpack(m["data"], CHUNK_LIMIT))
                if self.total > self.body_limit:
                    raise ProtocolError("result size limit")
            elif kind == "complete":
                if response_phase != "streaming":
                    raise ProtocolError("unsolicited complete")
                self._context(m)
                self._finish_response()
            elif kind == "error":
                if response_phase not in {"preparing", "prepared", "invoked", "streaming"}:
                    raise ProtocolError("unsolicited error")
                self._finish_response()
            elif kind == "permit":
                if self.phase not in {"invoked", "streaming"}:
                    raise ProtocolError("invalid permit renewal")
            elif kind == "cancel":
                if self.phase == "cancelling":
                    raise ProtocolError("duplicate cancel")
                self.cancel_phase, self.phase = self.phase, "cancelling"
            elif kind == "cancelled":
                if self.phase != "cancelling":
                    raise ProtocolError("unsolicited cancelled")
                self._context(m)
                self.last_finished, self.active, self.phase = self.active, None, "idle"
            else:
                raise ProtocolError("unknown transition")
        self.sequences[sender] = m["seq"]

    def _response_phase(self, phase):
        if self.phase == "cancelling":
            self.cancel_phase = phase
        else:
            self.phase = phase

    def _finish_response(self):
        if self.phase == "cancelling":
            self.cancel_phase = "finished"
        else:
            self.last_finished, self.active, self.phase = self.active, None, "idle"

    def _context(self, m):
        if any(m[field] != self.connection[field] for field in CONTEXT):
            raise ProtocolError("model_host_preempted")
