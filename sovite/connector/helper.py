"""Outbound-only Phase 1 pilot. HTTP engine cancellation remains development-only."""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import inspect
import ipaddress
import json
import logging
import os
from pathlib import Path
import ssl
import time
from urllib.parse import urlsplit

import httpx
from websockets.asyncio.client import connect
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .canonical import ProtocolError
from .identity import WIRE_APIS, digest, manifest, observe_weights, sign, validate_assertion
from .protocol import (BODY_LIMIT, CHUNK_LIMIT, CONTEXT, CONTROL_LIMIT, FRESHNESS_SECONDS,
                       HEARTBEAT_SECONDS, PERMIT_SECONDS, SUBPROTOCOL, Session, decode_envelope, pack)


PATHS = {"chat": "/v1/chat/completions", "responses": "/v1/responses", "ollama-chat": "/api/chat"}
LOGGER = logging.Logger("sovite.connector.silent", level=logging.CRITICAL + 1)
LOGGER.addHandler(logging.NullHandler())
LOGGER.propagate = False


@dataclass
class HelperSettings:
    gateway_url: str
    ca_file: str
    cert_file: str
    key_file: str
    signing_key_file: str
    engine_endpoint: str
    identity_file: str
    weight_paths: dict[str, str]
    wire_api: str = "chat"
    engine_socket: str = ""
    engine_ca_file: str = ""
    engine_token_file: str = ""
    mode: str = "development"
    body_limit: int = BODY_LIMIT

    @classmethod
    def from_env(cls):
        return cls(**json.loads(Path(os.environ["SOVITE_HELPER_SETTINGS"]).read_text()))

    def validate(self):
        if type(self.wire_api) is not str or self.wire_api not in WIRE_APIS:
            raise ProtocolError("one configured wire API required")
        if not self.ca_file:
            raise ProtocolError("explicit gateway CA file required")
        gateway = urlsplit(self.gateway_url)
        if gateway.scheme != 'wss' or not gateway.hostname or gateway.username or gateway.password or gateway.fragment or gateway.query:
            raise ProtocolError("gateway requires a fixed wss URL")
        engine = urlsplit(self.engine_endpoint)
        if (not engine.hostname or engine.username or engine.password or engine.query or engine.fragment
                or engine.path not in {'', '/'}):
            raise ProtocolError("engine requires a fixed origin")
        if self.engine_socket:
            if not Path(self.engine_socket).is_absolute() or self.engine_endpoint != "http://localhost":
                raise ProtocolError("invalid Unix engine configuration")
        elif engine.scheme == 'http':
            try:
                local = ipaddress.ip_address(engine.hostname).is_loopback
            except ValueError:
                local = False
            if not local:
                raise ProtocolError("HTTP engine must be a loopback literal")
        elif engine.scheme != 'https':
            raise ProtocolError("engine requires loopback, Unix socket or validated HTTPS")
        if engine.scheme == 'https' and not self.engine_ca_file:
            raise ProtocolError("explicit HTTPS engine CA file required")
        if self.mode != "development":
            raise ProtocolError("pilot disconnect cancellation is unqualified; development mode required")
        if type(self.body_limit) is not int or not 1 <= self.body_limit <= BODY_LIMIT:
            raise ProtocolError("invalid body limit")
        if not self.weight_paths:
            raise ProtocolError("actual weight files required")


def client_tls_context(ca_file: str, *, required=True) -> ssl.SSLContext:
    if required and not ca_file:
        raise ProtocolError("explicit CA file required")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.keylog_filename = None
    context.options |= ssl.OP_NO_COMPRESSION
    if ca_file:
        context.load_verify_locations(cafile=ca_file)
    return context


def tls_context(settings: HelperSettings) -> ssl.SSLContext:
    context = client_tls_context(settings.ca_file)
    context.load_cert_chain(settings.cert_file, settings.key_file)
    return context


def validate_body(operation: str, body: bytes, model_id: str):
    # INFERENCE_ONLY, NO_REVERSE_ROUTE: no caller-selectable endpoints or hosted tools.
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ProtocolError("duplicate inference field")
            result[key] = value
        return result
    def bad_number(value):
        raise ProtocolError("invalid inference number")
    try:
        value = json.loads(body, object_pairs_hook=pairs, parse_constant=bad_number)
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError("invalid inference JSON") from None
    common = {"model", "stream", "temperature", "top_p", "tools", "tool_choice", "seed"}
    allowed = {
        "chat": common | {"messages", "max_tokens", "max_completion_tokens", "stop", "frequency_penalty", "presence_penalty", "response_format", "parallel_tool_calls", "stream_options"},
        "responses": common | {"input", "instructions", "max_output_tokens", "text", "reasoning", "parallel_tool_calls", "store", "background"},
        "ollama-chat": {"model", "messages", "stream", "options", "format", "tools", "keep_alive"},
    }
    if operation not in allowed or type(value) is not dict or set(value) - allowed[operation]:
        raise ProtocolError("unsupported inference fields")
    if value.get("model") != model_id or type(value.get("stream")) is not bool:
        raise ProtocolError("EXACT_BINDING model mismatch")
    if value.get("background", False) is not False or value.get("store", False) is not False:
        raise ProtocolError("background generation or storage denied")
    if operation == 'responses' and value.get('store') is not False:
        raise ProtocolError("explicit store false required")
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 24:
            raise ProtocolError('inference depth limit')
        if type(item) is dict:
            pending.extend((child, depth + 1) for child in item.values())
        elif type(item) is list:
            pending.extend((child, depth + 1) for child in item)
    validate_text_inputs(operation, value)
    return value


def validate_text_inputs(operation, value):
    """Allow API structures explicitly; function arguments/results remain inert data."""
    def obj(item, allowed, required=()):
        if type(item) is not dict or set(item) - set(allowed) or not set(required) <= set(item):
            raise ProtocolError('unsupported inference object fields')

    def string(item):
        if type(item) is not str:
            raise ProtocolError('text required')

    def content(item):
        if type(item) is str:
            return
        if type(item) is not list:
            raise ProtocolError('text-only content required')
        for part in item:
            obj(part, {'type', 'text'}, {'type', 'text'})
            if type(part['type']) is not str or part['type'] not in ({'input_text', 'output_text'} if operation == 'responses' else {'text'}):
                raise ProtocolError('text-only part required')
            string(part['text'])

    def function(item, responses=False, declaration=False):
        keys = {'name', 'description', 'parameters', 'strict'} if declaration else {'name', 'arguments'}
        obj(item, keys | ({'type'} if responses else set()), {'name'})
        string(item['name'])
        if 'arguments' in item:
            # Ollama arguments may be a JSON object. No part of it is executed here.
            if operation != 'ollama-chat' or type(item['arguments']) is not dict:
                string(item['arguments'])

    messages = value.get('input', []) if operation == 'responses' else value.get('messages', [])
    if operation == 'responses' and type(messages) is str:
        messages = []
    if type(messages) is not list:
        raise ProtocolError('message list required')
    for message in messages:
        if operation == 'responses':
            kind = message.get('type', 'message') if type(message) is dict else None
            if kind == 'function_call':
                obj(message, {'type', 'id', 'call_id', 'name', 'arguments', 'status'}, {'call_id', 'name', 'arguments'})
                for field in ('call_id', 'name', 'arguments'):
                    string(message[field])
                continue
            if kind == 'function_call_output':
                obj(message, {'type', 'id', 'call_id', 'output', 'status'}, {'call_id', 'output'})
                string(message['call_id'])
                string(message['output'])
                continue
            if kind != 'message':
                raise ProtocolError('unsupported input type')
            keys = {'type', 'id', 'role', 'content', 'status'}
        else:
            keys = {'role', 'content', 'tool_calls', 'tool_call_id', 'name'} if operation == 'chat' else {'role', 'content', 'tool_calls', 'tool_name', 'thinking'}
        obj(message, keys, {'role'})
        if type(message['role']) is not str or message['role'] not in {'system', 'developer', 'user', 'assistant', 'tool', 'function'}:
            raise ProtocolError('unsupported message role')
        if 'content' in message and message['content'] is not None:
            content(message['content'])
        elif not message.get('tool_calls'):
            raise ProtocolError('message content required')
        for field in set(message) - {'type', 'content', 'tool_calls'}:
            string(message[field])
        calls = message.get('tool_calls', [])
        if type(calls) is not list:
            raise ProtocolError('tool call list required')
        for call in calls:
            obj(call, {'id', 'type', 'function', 'index'}, {'function'})
            if call.get('type', 'function') != 'function':
                raise ProtocolError('function tools only')
            if 'id' in call:
                string(call['id'])
            if 'index' in call and (type(call['index']) is not int or call['index'] < 0):
                raise ProtocolError('invalid tool call index')
            function(call['function'])
    tools = value.get('tools', [])
    if type(tools) is not list:
        raise ProtocolError('tool list required')
    for tool in tools:
        if operation == 'responses':
            if type(tool) is not dict or tool.get('type') != 'function':
                raise ProtocolError('function tools only')
            function(tool, responses=True, declaration=True)
        else:
            obj(tool, {'type', 'function'}, {'type', 'function'})
            if tool['type'] != 'function':
                raise ProtocolError('function tools only')
            function(tool['function'], declaration=True)
    choice = value.get('tool_choice', 'auto')
    if type(choice) is dict:
        obj(choice, {'type', 'name'} if operation == 'responses' else {'type', 'function'}, {'type'})
        if choice['type'] != 'function':
            raise ProtocolError('function tools only')
        if operation != 'responses':
            obj(choice.get('function'), {'name'}, {'name'})
            string(choice['function']['name'])
        else:
            string(choice.get('name'))
    elif type(choice) is not str or choice not in {'auto', 'none', 'required'}:
        raise ProtocolError('unsupported tool choice')


class FixedConnect(connect):
    def process_redirect(self, exc):
        return exc


class FixedEngine:
    def __init__(self, settings: HelperSettings):
        settings.validate()
        self.settings = settings
        self.weight_files = observe_weights(settings.weight_paths)
        self.weight_stats = self._weight_stats()
        verify = client_tls_context(settings.engine_ca_file, required=urlsplit(settings.engine_endpoint).scheme == 'https')
        self.client = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=settings.engine_socket or None,
                                                                        verify=verify, trust_env=False),
                                        trust_env=False, follow_redirects=False, timeout=None)
        self.headers = {}
        if settings.engine_token_file:
            self.headers["Authorization"] = "Bearer " + Path(settings.engine_token_file).read_text().strip()

    def _weight_stats(self):
        return {name: (Path(path).stat().st_size, Path(path).stat().st_mtime_ns, Path(path).stat().st_ino)
                for name, path in self.settings.weight_paths.items()}

    def snapshot(self) -> dict:
        try:
            path = Path(self.settings.identity_file)
            if path.stat().st_size > CONTROL_LIMIT:
                raise ProtocolError("identity file size limit")
            value = json.loads(path.read_text())
            if not isinstance(value, dict):
                raise ProtocolError("invalid identity file")
            stats = self._weight_stats()
        except (OSError, ValueError):
            raise ProtocolError("engine_unavailable") from None
        # Snapshot is operator/supervisor input, never engine-reported manifest aliases.
        if stats != self.weight_stats:
            raise ProtocolError("model_host_preempted")
        return {**value, "wire_apis": [self.settings.wire_api], "weight_files": self.weight_files, "weights_manifest_digest": manifest(self.weight_files)}

    async def stream(self, operation: str, body: bytes, model_id: str, *, value=None):
        if operation != self.settings.wire_api:
            raise ProtocolError('invalid_request')
        if value is None:
            value = validate_body(operation, body, model_id)
        async with self.client.stream("POST", self.settings.engine_endpoint.rstrip('/') + PATHS[operation],
                                      content=body, headers={**self.headers, "Content-Type": "application/json", "Accept-Encoding": "identity"}) as response:
            if response.status_code != 200:
                raise ProtocolError("engine_unavailable")
            if response.headers.get("content-encoding", "identity") != "identity":
                raise ProtocolError("engine_protocol_error")
            streaming = value['stream']
            sse = operation != "ollama-chat" and streaming
            if sse and response.headers.get("content-type", "").split(';')[0] != "text/event-stream":
                raise ProtocolError("engine_protocol_error")
            pending, total, scan, line_start = bytearray(), 0, 0, 0
            def frames(eof=False):
                nonlocal scan, line_start
                while scan < len(pending):
                    byte = pending[scan]
                    if sse and byte == 13 and scan + 1 == len(pending) and not eof:
                        break
                    if byte == 10 or sse and byte == 13:
                        end = scan + (2 if sse and byte == 13 and pending[scan + 1:scan + 2] == b'\n' else 1)
                        if not sse or scan == line_start:
                            if end > CHUNK_LIMIT:
                                raise ProtocolError("result_too_large")
                            frame = bytes(pending[:end])
                            del pending[:end]
                            scan = line_start = 0
                            yield frame
                            continue
                        line_start, scan = end, end
                    else:
                        scan += 1
                if len(pending) > CHUNK_LIMIT:
                    raise ProtocolError("result_too_large")
            async for block in response.aiter_bytes():
                total += len(block)
                if total > self.settings.body_limit:
                    raise ProtocolError("result_too_large")
                if not streaming:
                    for offset in range(0, len(block), CHUNK_LIMIT):
                        yield block[offset:offset + CHUNK_LIMIT]
                else:
                    pending.extend(block)
                    for frame in frames():
                        yield frame
            for frame in frames(eof=True):
                yield frame
            if pending:
                if sse:
                    raise ProtocolError("engine_protocol_error")
                yield bytes(pending)

    async def close(self):
        await self.client.aclose()


class PilotHelper:
    def __init__(self, settings: HelperSettings):
        settings.validate()
        self.settings = settings
        key = serialization.load_pem_private_key(Path(settings.signing_key_file).read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ProtocolError("Ed25519 helper key required")
        self.key, self.engine = key, FixedEngine(settings)
        self.session, self.socket, self.task = None, None, None
        self.send_lock = asyncio.Lock()
        self.permit_until, self.deadline_until, self.last_gateway = 0, 0, 0
        self.reservation = None

    def assertion(self, challenge, session_id, **extra):
        now = int(time.time())
        value = {**self.engine.snapshot(), "session_id": session_id, "challenge": challenge["nonce"],
                 "issued_at": now, "expires_at": now + 30, **extra}
        validate_assertion(value, bool(extra))
        for field in ("installation_id", "binding_id", "enrollment_id", "enrollment_role"):
            if value[field] != challenge[field]:
                raise ProtocolError("EXACT_BINDING challenge mismatch")
        if value["cancellation"] != "disconnect-only":
            raise ProtocolError("pilot cancellation mechanism must be explicit")
        return value

    async def send(self, kind, **fields):
        async with self.send_lock:
            raw = self.session.send(kind, **fields)
            await asyncio.wait_for(self.socket.send(raw), 1)

    def context(self):
        return {field: self.session.connection[field] for field in CONTEXT}

    def check_epoch(self):
        observed = self.engine.snapshot()
        expected = {k: v for k, v in self.session.connection.items() if k not in {"session_id", "challenge", "issued_at", "expires_at"}}
        for field in observed.keys() | expected.keys():
            if observed.get(field) != expected.get(field):
                raise ProtocolError("model_host_preempted")

    def check_generation_bounds(self):
        now = asyncio.get_running_loop().time()
        if now >= self.deadline_until:
            raise ProtocolError("deadline_exceeded")
        if now >= self.permit_until:
            raise ProtocolError("permit_expired")

    def expiry(self, ttl_ms: int):
        if type(ttl_ms) is not int or not 1 <= ttl_ms <= PERMIT_SECONDS * 1000:
            raise ProtocolError("permit_expired")
        return asyncio.get_running_loop().time() + ttl_ms / 1000

    def decode_invocation(self, frame, model_id, body_digest):
        body = frame.payload(self.settings.body_limit)
        if digest(body) != body_digest:
            raise ProtocolError('invalid_request')
        return body, validate_body(frame.message['operation'], body, model_id)

    async def generate(self, frame):
        message = frame.message
        request_id = message["request_id"]
        try:
            self.check_generation_bounds()
            self.check_epoch()
            try:
                body, value = await asyncio.to_thread(self.decode_invocation, frame,
                                                      self.session.connection['model_id'], self.reservation['body_digest'])
            except ProtocolError:
                raise ProtocolError("invalid_request") from None
            self.check_generation_bounds()
            self.check_epoch()
            await self.send("accepted", request_id=request_id, **self.context())
            async with contextlib.aclosing(self.engine.stream(message["operation"], body, self.session.connection["model_id"], value=value)) as stream:
                async for block in stream:
                    self.check_generation_bounds()
                    self.check_epoch()
                    await self.send("chunk", request_id=request_id, data=pack(block))
            self.check_generation_bounds()
            self.check_epoch()
            await self.send("complete", request_id=request_id, **self.context())
        except asyncio.CancelledError:
            raise
        except (ProtocolError, httpx.HTTPError) as error:
            code = "engine_unavailable"
            if str(error) in {"model_host_preempted", "result_too_large", "engine_protocol_error", "invalid_request", "deadline_exceeded", "permit_expired"}:
                code = str(error)
            if self.session.phase not in {"closed", "cancelling", "idle"}:
                await self.send("error", request_id=request_id, code=code)

    async def stop(self):
        task, self.task = self.task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        self.reservation = None

    async def watchdog(self):
        while True:
            await asyncio.sleep(0.05)
            now = asyncio.get_running_loop().time()
            try:
                self.check_epoch()
            except ProtocolError:
                request_id = self.session.active
                await self.stop()
                if self.generation_active(request_id):
                    await self.send("error", request_id=request_id, code="model_host_preempted")
                await self.socket.close()
                return
            if now - self.last_gateway > FRESHNESS_SECONDS:
                await self.stop()
                await self.socket.close()
                return
            if self.session.phase in {"invoked", "streaming"} and now >= min(self.deadline_until, self.permit_until):
                code = "deadline_exceeded" if now >= self.deadline_until else "permit_expired"
                request_id = self.session.active
                await self.stop()
                if self.generation_active(request_id):
                    await self.send("error", request_id=request_id, code=code)

    def generation_active(self, request_id):
        return request_id is not None and self.session.active == request_id and self.session.phase in {'invoked', 'streaming'}

    async def heartbeats(self):
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await self.send("heartbeat", ready=self.session.phase == "idle", **self.context())

    async def serve(self, socket):
        self.socket = socket
        if socket.subprotocol != SUBPROTOCOL:
            raise ProtocolError("required subprotocol missing")
        raw = await asyncio.wait_for(socket.recv(), FRESHNESS_SECONDS)
        frame = decode_envelope(raw, self.settings.body_limit)
        challenge = frame.message
        self.session = Session("helper", challenge["session_id"], body_limit=self.settings.body_limit)
        challenge = self.session.receive_envelope(frame)
        if challenge["type"] != "challenge":
            raise ProtocolError("challenge required")
        assertion = self.assertion(challenge, self.session.session_id)
        await self.send("hello", identity=sign(assertion, self.key))
        self.last_gateway = asyncio.get_running_loop().time()
        watchdog, heartbeat = asyncio.create_task(self.watchdog()), asyncio.create_task(self.heartbeats())
        receiver = asyncio.create_task(socket.recv())
        try:
            while True:
                pending = {receiver, watchdog, heartbeat}
                if self.task is not None:
                    pending.add(self.task)
                done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                if self.task is not None and self.task in done:
                    completed, self.task = self.task, None
                    completed.result()
                for background in (watchdog, heartbeat):
                    if background in done:
                        background.result()
                        return
                if receiver not in done:
                    continue
                raw = receiver.result()
                receiver = asyncio.create_task(socket.recv())
                frame = decode_envelope(raw, self.settings.body_limit)
                m = self.session.receive_envelope(frame)
                self.last_gateway = asyncio.get_running_loop().time()
                kind, request_id = m["type"], m.get("request_id")
                if kind == "prepare":
                    if m['operation'] != self.settings.wire_api:
                        await self.send('error', request_id=request_id, code='invalid_request')
                        continue
                    try:
                        self.check_epoch()
                        a = self.assertion(challenge, self.session.session_id, request_id=request_id,
                                           body_digest=m["body_digest"], operation=m['operation'])
                        self.reservation = a
                        await self.send("prepared", request_id=request_id, identity=sign(a, self.key, prepared=True))
                    except ProtocolError:
                        await self.send("error", request_id=request_id, code="model_host_preempted")
                elif kind == "invoke":
                    try:
                        self.check_epoch()
                        if self.reservation is None or self.reservation["expires_at"] <= time.time():
                            raise ProtocolError("invalid_request")
                        self.permit_until = self.expiry(m["permit_ttl_ms"])
                        now, monotonic = time.time(), asyncio.get_running_loop().time()
                        if not now < m["deadline"] <= now + 600:
                            raise ProtocolError("deadline_exceeded")
                        self.deadline_until = monotonic + m["deadline"] - now
                    except ProtocolError as error:
                        await self.send("error", request_id=request_id, code=str(error))
                        continue
                    self.task = asyncio.create_task(self.generate(frame))
                elif kind == "permit" and self.session.active is not None:
                    try:
                        if asyncio.get_running_loop().time() >= min(self.permit_until, self.deadline_until):
                            raise ProtocolError("permit_expired")
                        self.permit_until = self.expiry(m["permit_ttl_ms"])
                    except ProtocolError:
                        await self.stop()
                        if self.generation_active(request_id):
                            await self.send("error", request_id=request_id, code="permit_expired")
                elif kind == "cancel":
                    await self.stop()
                    await self.send("cancelled", request_id=request_id, **self.context())
        finally:
            receiver.cancel()
            watchdog.cancel()
            heartbeat.cancel()
            await self.stop()
            await asyncio.gather(receiver, watchdog, heartbeat, return_exceptions=True)
            self.session.close()

    async def run(self):
        # NO_TRANSPARENT_REPLAY: a disconnect ends this process's connector run.
        options = {"ssl": tls_context(self.settings), "subprotocols": [SUBPROTOCOL], "compression": None,
                   "max_size": 4 * ((self.settings.body_limit + 2) // 3) + CONTROL_LIMIT,
                   "max_queue": 4, "write_limit": 32768, "open_timeout": 6, "close_timeout": 1,
                   "logger": LOGGER}
        if "proxy" in inspect.signature(connect).parameters:
            options["proxy"] = None
        try:
            async with FixedConnect(self.settings.gateway_url, **options) as socket:
                await self.serve(socket)
        finally:
            await self.engine.close()


def main():
    try:
        asyncio.run(PilotHelper(HelperSettings.from_env()).run())
    except KeyboardInterrupt:
        return
    except Exception:
        raise SystemExit("Connector stopped; inspect private operational state.") from None


if __name__ == "__main__":
    main()
