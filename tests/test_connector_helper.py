import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import ssl
import time

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
import httpx
import pytest
from websockets.asyncio.server import serve

from connector_support import ASSERTION, BINDING, BODY, ROLE, assertion, challenge, context
from sovite.connector.canonical import ProtocolError
from sovite.connector.helper import LOGGER, FixedEngine, HelperSettings, PilotHelper, tls_context, validate_body
from sovite.connector.identity import ExpectedIdentity, digest, verify_connection, verify_preparation
from sovite.connector.protocol import SUBPROTOCOL, Session, pack, unpack


EVENTS = [b'event: token\ndata: {"text":"one"}\n\n', b'data: two\r\n\r\n', b'data: [DONE]\n\n']


def certificates(path):
    ca_key = Ed25519PrivateKey.generate()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'connector-test-ca')])
    now = datetime.now(timezone.utc)
    def issue(key, common_name, client=False, ca=False):
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
        builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(name).public_key(key.public_key())
                   .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
                   .not_valid_after(now + timedelta(hours=1)).add_extension(x509.BasicConstraints(ca=ca, path_length=None), True))
        if not ca:
            builder = builder.add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH if client else ExtendedKeyUsageOID.SERVER_AUTH]), False)
            if not client:
                import ipaddress
                builder = builder.add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), False)
        return builder.sign(ca_key, None)
    for label, key, client, ca in [('ca', ca_key, False, True), ('server', Ed25519PrivateKey.generate(), False, False),
                                    ('helper', Ed25519PrivateKey.generate(), True, False)]:
        (path / (label + '.pem')).write_bytes(issue(key, label if not ca else 'connector-test-ca', client, ca).public_bytes(serialization.Encoding.PEM))
        (path / (label + '.key')).write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        if label == 'helper':
            helper_key = key
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(path / 'server.pem', path / 'server.key')
    context.load_verify_locations(path / 'ca.pem')
    context.verify_mode = ssl.CERT_REQUIRED
    return context, helper_key


class EnginePeer:
    """A cooperative fake engine: the test observes stream termination, not just an ack."""
    def __init__(self, stall=False):
        self.stall = stall
        self.calls = []
        self.stopped, self.started = asyncio.Event(), asyncio.Event()
        self.handlers = set()

    async def handle(self, reader, writer):
        self.handlers.add(asyncio.current_task())
        try:
            head = await reader.readuntil(b'\r\n\r\n')
            size = int(next(line.split(b':', 1)[1] for line in head.split(b'\r\n') if line.lower().startswith(b'content-length:')))
            body = await reader.readexactly(size)
            self.calls.append((head.split(b'\r\n')[0], body))
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n')
            await writer.drain()
            self.started.set()
            if self.stall:
                writer.write(EVENTS[0])
                await writer.drain()
                await reader.read()
            else:
                # Split bytes inside SSE fields to prove boundary preservation.
                for byte in b''.join(EVENTS):
                    writer.write(bytes([byte]))
                    await writer.drain()
                    await asyncio.sleep(0)
        finally:
            self.stopped.set()
            writer.close()
            await writer.wait_closed()
            self.handlers.discard(asyncio.current_task())


class GatewayPeer:
    """Test-only gateway: no workload listener, authority service, ledger or routing."""
    def __init__(self, socket):
        self.socket, self.session = socket, Session('gateway', 'session-1')
        self.connection = None

    async def send(self, kind, **fields):
        await self.socket.send(self.session.send(kind, **fields))

    async def receive(self):
        while True:
            message = self.session.receive(await asyncio.wait_for(self.socket.recv(), 4))
            if message['type'] != 'heartbeat':
                return message
            await self.send('heartbeat', ready=True, **context())

    async def handshake(self, key):
        assert self.socket.subprotocol == SUBPROTOCOL
        assert not self.socket.protocol.extensions
        assert self.socket.transport.get_extra_info('ssl_object').version() == 'TLSv1.3'
        assert self.socket.transport.get_extra_info('peercert')
        await self.send('challenge', **challenge())
        hello = await self.receive()
        expected = ExpectedIdentity(**{k: ASSERTION[k] for k in ExpectedIdentity.__dataclass_fields__})
        self.connection = verify_connection(hello['identity'], key.public_key(), expected, now=int(time.time()))
        return expected

    async def prepare(self, key, expected, body=BODY, operation='chat'):
        await self.send('prepare', request_id='request-1', operation=operation, body_digest=digest(body), **context())
        ready = await self.receive()
        assert ready['type'] == 'prepared'
        verify_preparation(ready['identity'], key.public_key(), expected, self.connection, {**BINDING, 'wire_api': operation}, ROLE,
                           request_id='request-1', body_digest=digest(body), now=int(time.time()), mode='development')

    async def invoke(self, body=BODY, operation='chat', deadline=None, permit=None):
        now = int(time.time())
        await self.send('invoke', request_id='request-1', operation=operation, body=pack(body), deadline=deadline or now + 30,
                        permit_ttl_ms=permit if permit is not None else 3000)


@asynccontextmanager
async def pilot(tmp_path, scenario, *, stall=False, operation="chat"):
    server_tls, key = certificates(tmp_path)
    engine = EnginePeer(stall)
    http_server = await asyncio.start_server(engine.handle, '127.0.0.1', 0)
    engine_port = http_server.sockets[0].getsockname()[1]
    metadata = {k: v for k, v in assertion().items() if k not in {'weight_files', 'weights_manifest_digest', 'session_id', 'challenge', 'issued_at', 'expires_at'}}
    metadata['cancellation'] = 'disconnect-only'
    (tmp_path / 'identity.json').write_text(json.dumps(metadata))
    (tmp_path / 'model.gguf').write_bytes(b'weights')
    finished = asyncio.get_running_loop().create_future()
    async def handle(socket):
        try:
            peer = GatewayPeer(socket)
            expected = await peer.handshake(key)
            result = await scenario(peer, engine, key, expected)
            finished.set_result(result)
        except Exception as error:
            finished.set_exception(error)
    async with serve(handle, '127.0.0.1', 0, ssl=server_tls, subprotocols=[SUBPROTOCOL], compression=None, logger=LOGGER) as gateway:
        port = gateway.sockets[0].getsockname()[1]
        settings = HelperSettings(gateway_url=f'wss://127.0.0.1:{port}/connector', ca_file=str(tmp_path / 'ca.pem'),
                                  cert_file=str(tmp_path / 'helper.pem'), key_file=str(tmp_path / 'helper.key'), signing_key_file=str(tmp_path / 'helper.key'),
                                  engine_endpoint=f'http://127.0.0.1:{engine_port}', identity_file=str(tmp_path / 'identity.json'),
                                  weight_paths={'model.gguf': str(tmp_path / 'model.gguf')}, wire_api=operation)
        helper = PilotHelper(settings)
        task = asyncio.create_task(helper.run())
        try:
            yield finished, engine, helper, settings
            await asyncio.wait_for(finished, 10)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            http_server.close()
            await http_server.wait_closed()
            if engine.handlers:
                await asyncio.wait_for(asyncio.gather(*engine.handlers, return_exceptions=True), 3)


@pytest.mark.parametrize('operation', ['chat', 'responses', 'ollama-chat'])
def test_helper_tls_mtls_EXACT_BINDING_stream_boundaries_and_no_payload_logging(tmp_path, caplog, operation):
    caplog.set_level(logging.DEBUG)
    body = BODY if operation != 'responses' else b'{"model":"test-model","input":"private-canary","stream":true,"store":false}'
    async def scenario(peer, engine, key, expected):
        await peer.prepare(key, expected, body, operation)
        assert not engine.calls  # PIN_BEFORE_PROMPT: preparation delivered no prompt.
        await peer.invoke(body, operation)
        kinds, chunks = [], []
        while True:
            message = await peer.receive()
            kinds.append(message['type'])
            if message['type'] == 'chunk':
                chunks.append(unpack(message['data'], 65536))
            if message['type'] in {'complete', 'error'}:
                break
        assert kinds[0] == 'accepted' and kinds[-1] == 'complete'
        assert b''.join(chunks) == b''.join(EVENTS)
        if operation != 'ollama-chat':
            assert chunks == EVENTS
        from sovite.connector.helper import PATHS
        assert engine.calls == [(f'POST {PATHS[operation]} HTTP/1.1'.encode(), body)]
    async def run():
        async with pilot(tmp_path, scenario, operation=operation):
            pass
    asyncio.run(run())
    assert 'private-canary' not in caplog.text
    assert pack(body) not in caplog.text


@pytest.mark.parametrize('stop', ['cancel', 'permit', 'deadline', 'disconnect'])
def test_BOUNDED_STOP_real_engine_stream_termination_and_ack(tmp_path, stop):
    async def scenario(peer, engine, key, expected):
        await peer.prepare(key, expected)
        # Integer Unix deadlines need a full admission interval; +1 can expire
        # at the next clock boundary before the real helper accepts the frame.
        sent_wall, sent_mono = time.time(), time.monotonic()
        deadline = int(sent_wall) + 2 if stop == 'deadline' else None
        deadline_mono = sent_mono + deadline - sent_wall if deadline is not None else None
        await peer.invoke(deadline=deadline)
        first = await peer.receive()
        assert first['type'] == 'accepted', first.get('code')
        await asyncio.wait_for(engine.started.wait(), 1)
        began = time.monotonic()
        if stop == 'cancel':
            await peer.send('cancel', request_id='request-1')
        elif stop == 'disconnect':
            await peer.socket.close()
        if stop != 'disconnect':
            while True:
                message = await peer.receive()
                if message['type'] in {'cancelled', 'error'}:
                    break
            assert message['type'] == ('cancelled' if stop == 'cancel' else 'error')
            if stop != 'cancel':
                assert message['code'] == ('permit_expired' if stop == 'permit' else 'deadline_exceeded')
        await asyncio.wait_for(engine.stopped.wait(), 3.2)
        stopped = time.monotonic()
        if stop == 'deadline':
            # The real watchdog polls every50ms; measured stop lag was26–40ms.
            # Bound shutdown against the frozen deadline, not its floor phase.
            assert stopped <= deadline_mono + 0.25
            assert stopped - sent_mono <= 2.25
        else:
            assert stopped - began < (3.2 if stop == 'permit' else 2)
        assert len(engine.calls) == 1  # NO_TRANSPARENT_REPLAY
    async def run():
        async with pilot(tmp_path, scenario, stall=True):
            pass
    asyncio.run(run())


def test_BOUNDED_STOP_permit_renewal(tmp_path):
    async def scenario(peer, engine, key, expected):
        await peer.prepare(key, expected)
        await peer.invoke()
        assert (await peer.receive())['type'] == 'accepted'
        first_expiry = time.monotonic() + 3
        await asyncio.sleep(1.1)
        await peer.send('permit', request_id='request-1', permit_ttl_ms=3000)
        await asyncio.sleep(max(0, first_expiry - time.monotonic() + 0.1))
        assert not engine.stopped.is_set()
        await peer.send('cancel', request_id='request-1')
        while (await peer.receive())['type'] != 'cancelled':
            pass
        await asyncio.wait_for(engine.stopped.wait(), 1)
    async def run():
        async with pilot(tmp_path, scenario, stall=True):
            pass
    asyncio.run(run())


@pytest.mark.parametrize('phase', ['before-prepare', 'after-prepare'])
def test_SESSION_IDENTITY_pilot_epoch_change_refuses_prompt(tmp_path, phase):
    async def scenario(peer, engine, key, expected):
        if phase == 'after-prepare':
            await peer.prepare(key, expected)
        path = tmp_path / 'identity.json'
        value = json.loads(path.read_text())
        value['load_epoch'] = 'replacement'
        path.write_text(json.dumps(value))
        if phase == 'before-prepare':
            await peer.send('prepare', request_id='request-1', operation='chat', body_digest=digest(BODY), **context())
        else:
            await peer.invoke()
        result = await peer.receive()
        assert result['type'] == 'error' and result['code'] == 'model_host_preempted'
        assert not engine.calls
    async def run():
        async with pilot(tmp_path, scenario):
            pass
    asyncio.run(run())


@pytest.mark.parametrize('endpoint', ['http://service.invalid', 'http://localhost', 'ftp://127.0.0.1', 'https://host.invalid/path',
                                       'https://user@host.invalid', 'https://host.invalid?next=x', 'http://192.0.2.1'])
def test_NO_REVERSE_ROUTE_refuses_nonconfigured_or_unsafe_engine_destination(endpoint):
    settings = HelperSettings('wss://gateway.invalid', 'ca', 'cert', 'key', 'signing', endpoint, 'identity', {'m': 'weights'})
    with pytest.raises(ProtocolError):
        settings.validate()


@pytest.mark.parametrize('body', [
    {"model": "different", "messages": []}, {"model": "test-model", "url": "https://other.invalid"},
    {"model": "test-model", "messages": [{"content": [{"type": "image_url", "image_url": {"url": "https://other.invalid"}}]}]},
    {"model": "test-model", "tools": [{"type": "web_search_preview"}]},
    {"model": "test-model", "background": True}, {"model": "test-model", "store": True},
    {"model": "test-model", "previous_response_id": "previous"},
])
def test_INFERENCE_ONLY_NO_REVERSE_ROUTE_request_destination_and_hosted_tools_denied(body):
    with pytest.raises(ProtocolError):
        validate_body('responses', json.dumps(body).encode(), 'test-model')


def test_NO_REVERSE_ROUTE_redirect_and_ambient_proxy_disabled(tmp_path, monkeypatch):
    async def run():
        settings = HelperSettings('wss://gateway.invalid', 'ca', 'cert', 'key', 'signing', 'https://engine.invalid', 'identity', {'m': str(tmp_path / 'weights')})
        certificates(tmp_path)
        settings.engine_ca_file = str(tmp_path / 'ca.pem')
        (tmp_path / 'weights').write_bytes(b'weights')
        calls = []
        def respond(request):
            calls.append(str(request.url))
            return httpx.Response(302, headers={'location': 'https://forbidden.invalid'})
        monkeypatch.setenv('HTTPS_PROXY', 'http://forbidden.invalid:9999')
        engine = FixedEngine(settings)
        assert not engine.client._trust_env
        assert not engine.client.follow_redirects
        await engine.client.aclose()
        engine.client = httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False, follow_redirects=False)
        try:
            with pytest.raises(ProtocolError, match='unavailable'):
                async for _ in engine.stream('chat', BODY, 'test-model'):
                    pass
            assert calls == ['https://engine.invalid/v1/chat/completions']
            with pytest.raises(ProtocolError):
                async for _ in engine.stream('CONNECT', BODY, 'test-model'):
                    pass
            assert len(calls) == 1
        finally:
            await engine.close()
    asyncio.run(run())


def test_pilot_refuses_enforced_mode_and_bad_gateway_tls(tmp_path):
    settings = HelperSettings('ws://gateway.invalid', 'ca', 'cert', 'key', 'signing', 'http://127.0.0.1', 'identity', {'m': 'weights'})
    with pytest.raises(ProtocolError):
        settings.validate()
    settings.gateway_url = 'wss://gateway.invalid'
    settings.mode = 'enforced'
    with pytest.raises(ProtocolError, match='unqualified'):
        settings.validate()


def test_SESSION_IDENTITY_epoch_change_during_generation_preempted(tmp_path):
    async def scenario(peer, engine, key, expected):
        await peer.prepare(key, expected)
        await peer.invoke()
        assert (await peer.receive())['type'] == 'accepted'
        await engine.started.wait()
        path = tmp_path / 'identity.json'
        value = json.loads(path.read_text())
        value['load_epoch'] = 'replacement'
        path.write_text(json.dumps(value))
        while True:
            message = await peer.receive()
            if message['type'] == 'error':
                assert message['code'] == 'model_host_preempted'
                break
        await asyncio.wait_for(engine.stopped.wait(), 1)
        assert len(engine.calls) == 1
    async def run():
        async with pilot(tmp_path, scenario, stall=True):
            pass
    asyncio.run(run())


def test_INFERENCE_ONLY_NO_REVERSE_ROUTE_malicious_body_never_connects(tmp_path):
    async def scenario(peer, engine, key, expected):
        forbidden_calls = []
        async def forbidden(reader, writer):
            forbidden_calls.append(True)
            writer.close()
        trap = await asyncio.start_server(forbidden, '127.0.0.1', 0)
        try:
            port = trap.sockets[0].getsockname()[1]
            body = json.dumps({'model': 'test-model', 'messages': [], 'url': f'http://127.0.0.1:{port}'}).encode()
            await peer.prepare(key, expected, body)
            await peer.invoke(body)
            message = await peer.receive()
            assert message['type'] == 'error' and message['code'] == 'invalid_request'
            assert not engine.calls and not forbidden_calls
        finally:
            trap.close()
            await trap.wait_closed()
    async def run():
        async with pilot(tmp_path, scenario):
            pass
    asyncio.run(run())


def test_BOUNDED_STOP_output_backpressure_does_not_starve_cancel(tmp_path):
    helper_ref = []
    blocked = None
    async def scenario(peer, engine, key, expected):
        helper = helper_ref[0]
        original_send = helper.socket.send
        async def congested(data):
            from sovite.connector.protocol import decode_frame
            await original_send(data)
            if decode_frame(data)['type'] == 'chunk':
                blocked.set()
                await asyncio.Event().wait()
        helper.socket.send = congested
        await peer.prepare(key, expected)
        await peer.invoke()
        assert (await peer.receive())['type'] == 'accepted'
        await asyncio.wait_for(blocked.wait(), 1)
        began = time.monotonic()
        await peer.send('cancel', request_id='request-1')
        while True:
            message = await peer.receive()
            if message['type'] == 'cancelled':
                break
        await asyncio.wait_for(engine.stopped.wait(), 1)
        assert time.monotonic() - began < 2
    async def run():
        nonlocal blocked
        blocked = asyncio.Event()
        async with pilot(tmp_path, scenario, stall=True) as (_, _, helper, _):
            helper_ref.append(helper)
    asyncio.run(run())


def test_NO_REVERSE_ROUTE_gateway_redirect_not_followed(tmp_path):
    from websockets.exceptions import InvalidStatus
    from sovite.connector.helper import FixedConnect, LOGGER
    async def run():
        server_tls, _ = certificates(tmp_path)
        trap_calls = []
        async def trap(reader, writer):
            trap_calls.append(True)
            writer.close()
        trap_server = await asyncio.start_server(trap, '127.0.0.1', 0)
        port = trap_server.sockets[0].getsockname()[1]
        def redirect(connection, request):
            response = connection.respond(302, 'redirect denied')
            response.headers['Location'] = f'wss://127.0.0.1:{port}/forbidden'
            return response
        async def unused(socket):
            pytest.fail('redirect must not open WebSocket')
        async with serve(unused, '127.0.0.1', 0, ssl=server_tls, process_request=redirect, logger=LOGGER) as server:
            settings = HelperSettings(f'wss://127.0.0.1:{server.sockets[0].getsockname()[1]}', str(tmp_path / 'ca.pem'),
                                      str(tmp_path / 'helper.pem'), str(tmp_path / 'helper.key'), '', '', '', {})
            with pytest.raises(InvalidStatus):
                async with FixedConnect(settings.gateway_url, ssl=tls_context(settings), proxy=None, logger=LOGGER):
                    pytest.fail('redirect accepted')
        assert not trap_calls
        trap_server.close()
        await trap_server.wait_closed()
    asyncio.run(run())


@pytest.mark.parametrize('wire', [b'data: a\r\rdata: b\n\r\ndata: c\r\n\n', b'data: a\r\n\r\ndata: b\n\n'])
def test_sse_mixed_line_endings_preserved(tmp_path, wire):
    class BytesStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for byte in wire:
                yield bytes([byte])
    async def run():
        certificates(tmp_path)
        (tmp_path / 'weights').write_bytes(b'weights')
        settings = HelperSettings('wss://gateway.invalid', str(tmp_path / 'ca.pem'), '', '', '', 'https://engine.invalid', '',
                                  {'m': str(tmp_path / 'weights')}, engine_ca_file=str(tmp_path / 'ca.pem'))
        engine = FixedEngine(settings)
        await engine.client.aclose()
        engine.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=BytesStream())))
        try:
            chunks = [chunk async for chunk in engine.stream('chat', BODY, 'test-model')]
            assert b''.join(chunks) == wire
            assert len(chunks) == wire.count(b'data:')
        finally:
            await engine.close()
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['no-client-certificate', 'untrusted-server', 'tls-1.2'])
def test_tls_authentication_guards_fail_closed(tmp_path, failure):
    from websockets.exceptions import InvalidMessage
    from sovite.connector.helper import FixedConnect
    async def run():
        server_tls, _ = certificates(tmp_path)
        calls = []
        async def handler(socket):
            calls.append(True)
        async with serve(handler, '127.0.0.1', 0, ssl=server_tls, logger=LOGGER) as server:
            context = ssl.create_default_context(cafile=str(tmp_path / 'ca.pem') if failure != 'untrusted-server' else None)
            context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_3
            if failure != 'no-client-certificate':
                context.load_cert_chain(tmp_path / 'helper.pem', tmp_path / 'helper.key')
            if failure == 'tls-1.2':
                context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_2
            with pytest.raises((OSError, InvalidMessage)):
                async with FixedConnect(f'wss://127.0.0.1:{server.sockets[0].getsockname()[1]}', ssl=context, proxy=None, logger=LOGGER):
                    pytest.fail('TLS guard accepted invalid peer')
        assert not calls
    asyncio.run(run())


def test_helper_entrypoint_never_prints_remote_exception_payload(monkeypatch):
    from sovite.connector import helper
    class Failed:
        async def run(self):
            raise RuntimeError('private-exception-canary')
    monkeypatch.setattr(helper.HelperSettings, 'from_env', lambda: None)
    monkeypatch.setattr(helper, 'PilotHelper', lambda settings: Failed())
    with pytest.raises(SystemExit) as stopped:
        helper.main()
    assert str(stopped.value) == 'Connector stopped; inspect private operational state.'
    assert 'private-exception-canary' not in str(stopped.value)


@pytest.mark.parametrize('configured', ['chat', 'responses', 'ollama-chat'])
def test_operation_not_bound_to_advertised_wire_api(tmp_path, configured):
    async def scenario(peer, engine, key, expected):
        assert peer.connection.assertion['wire_apis'] == [configured]
        for index, wrong in enumerate(sorted({'chat', 'responses', 'ollama-chat'} - {configured})):
            await peer.send('prepare', request_id=f'wrong-{index}', operation=wrong, body_digest=digest(BODY), **context())
            result = await peer.receive()
            assert result['type'] == 'error' and result['code'] == 'invalid_request'
            assert not engine.calls
    async def run():
        async with pilot(tmp_path, scenario, operation=configured):
            pass
    asyncio.run(run())


def test_permit_clock_skew(tmp_path, monkeypatch):
    import sovite.connector.helper as implementation
    # Keep identity wall time intact; the permit conversion must never consult it.
    async def scenario(peer, engine, key, expected):
        await peer.prepare(key, expected)
        real_expiry = helper_ref[0].expiry
        def lagged_clock(ttl):
            with monkeypatch.context() as patch:
                patch.setattr(implementation.time, 'time', lambda: 1)
                return real_expiry(ttl)
        helper_ref[0].expiry = lagged_clock
        await peer.invoke(permit=1000)
        assert (await peer.receive())['type'] == 'accepted'
        await engine.started.wait()
        first = helper_ref[0].permit_until
        await asyncio.sleep(0.1)
        await peer.send('permit', request_id='request-1', permit_ttl_ms=1000)
        while helper_ref[0].permit_until == first:
            await asyncio.sleep(0)
        assert helper_ref[0].permit_until > first
        await peer.send('cancel', request_id='request-1')
        while (await peer.receive())['type'] != 'cancelled':
            pass
    helper_ref = []
    async def run():
        async with pilot(tmp_path, scenario, stall=True) as (_, _, helper, _):
            helper_ref.append(helper)
    asyncio.run(run())


@pytest.mark.parametrize('cause', ['epoch', 'permit', 'deadline'])
@pytest.mark.parametrize('finish', ['cancel', 'complete', 'replacement'])
def test_watchdog_cancel_race(cause, finish):
    from connector_support import pair, reserve
    from test_connector_protocol import invoke
    async def run():
        gateway, session = pair()
        reserve(gateway, session)
        invoke(gateway, session)
        helper = PilotHelper.__new__(PilotHelper)
        helper.session = session
        now = asyncio.get_running_loop().time()
        helper.last_gateway = now
        helper.permit_until = 0 if cause == 'permit' else now + 30
        helper.deadline_until = 0 if cause == 'deadline' else now + 30
        stopping, release, checked = asyncio.Event(), asyncio.Event(), asyncio.Event()
        sent = []
        def epoch():
            if cause == 'epoch':
                raise ProtocolError('model_host_preempted')
        helper.check_epoch = epoch
        async def stop():
            stopping.set()
            await release.wait()
        helper.stop = stop
        async def send(*args, **kwargs):
            sent.append((args, kwargs))
        helper.send = send
        class Socket:
            async def close(self):
                pass
        helper.socket = Socket()
        original = helper.generation_active
        def active(request_id):
            result = original(request_id)
            checked.set()
            return result
        helper.generation_active = active
        watchdog = asyncio.create_task(helper.watchdog())
        try:
            await asyncio.wait_for(stopping.wait(), 1)
            # stop() is suspended while the receive loop finishes the request.
            if finish == 'cancel':
                session.receive(gateway.send('cancel', request_id='request-1'))
                gateway.receive(session.send('cancelled', request_id='request-1', **context()))
            else:
                gateway.receive(session.send('complete', request_id='request-1', **context()))
                if finish == 'replacement':
                    session.receive(gateway.send('prepare', request_id='request-2', operation='chat', body_digest=digest(BODY), **context()))
            release.set()
            await asyncio.wait_for(checked.wait(), 1)
            assert not sent
            assert session.phase != 'closed'
        finally:
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)
    asyncio.run(run())


def test_tls_contexts_from_environment(tmp_path, monkeypatch):
    from sovite.connector.helper import client_tls_context
    certificates(tmp_path)
    ambient = tmp_path / 'ambient'
    ambient.mkdir()
    certificates(ambient)
    keylog = tmp_path / 'keylog'
    monkeypatch.setenv('SSLKEYLOGFILE', str(keylog))
    monkeypatch.setenv('SSL_CERT_FILE', str(ambient / 'ca.pem'))
    monkeypatch.setenv('SSL_CERT_DIR', str(ambient))
    def forbidden(*args, **kwargs):
        pytest.fail('ambient/default trust context was used')
    monkeypatch.setattr(ssl, 'create_default_context', forbidden)
    monkeypatch.setattr(ssl.SSLContext, 'load_default_certs', forbidden)
    settings = HelperSettings('wss://gateway.invalid', str(tmp_path / 'ca.pem'), str(tmp_path / 'helper.pem'),
                              str(tmp_path / 'helper.key'), '', 'https://engine.invalid', '', {'m': str(tmp_path / 'weights')},
                              engine_ca_file=str(tmp_path / 'ca.pem'))
    (tmp_path / 'weights').write_bytes(b'weights')
    gateway = tls_context(settings)
    async def run():
        engine = FixedEngine(settings)
        try:
            engine_tls = engine.client._transport._pool._ssl_context
            expected = ssl.PEM_cert_to_DER_cert((tmp_path / 'ca.pem').read_text())
            for ctx in (gateway, engine_tls):
                assert ctx.get_ca_certs(binary_form=True) == [expected]
                assert ctx.minimum_version == ssl.TLSVersion.TLSv1_3
                assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
                assert ctx.keylog_filename is None
                assert ctx.options & ssl.OP_NO_COMPRESSION
            assert not keylog.exists()
        finally:
            await engine.close()
    asyncio.run(run())
    for field in ('ca_file', 'engine_ca_file'):
        previous = getattr(settings, field)
        setattr(settings, field, '')
        with pytest.raises(ProtocolError, match='explicit'):
            settings.validate()
        setattr(settings, field, previous)
    with pytest.raises(ProtocolError, match='explicit'):
        client_tls_context('')


def test_frame_size_before_parsing_heartbeat_cancel_during_max_invoke_decode(tmp_path, monkeypatch):
    import threading
    from sovite.connector.protocol import BODY_LIMIT, decode_frame
    original = PilotHelper.decode_invocation
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    parses = []
    def slow_decode(self, frame, model, expected_digest):
        entered.set()
        assert release.wait(5), 'test did not release decoder'
        try:
            result = original(self, frame, model, expected_digest)
            parses.append(result)
            return result
        finally:
            finished.set()
    monkeypatch.setattr(PilotHelper, 'decode_invocation', slow_decode)
    prefix = b'{"model":"test-model","messages":[{"role":"user","content":"'
    suffix = b'"}],"stream":true}'
    body = prefix + b'x' * (BODY_LIMIT - len(prefix) - len(suffix)) + suffix
    helpers = []
    async def scenario(peer, engine, key, expected):
        helper = helpers[0]
        await peer.prepare(key, expected, body)
        await peer.invoke(body)
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await peer.send('heartbeat', ready=True, **context())
            await peer.send('cancel', request_id='request-1')
            message = await peer.receive()
            assert message['type'] == 'cancelled'
            assert helper.session.sequences['gateway'] == peer.session.sequences['gateway']
            assert not release.is_set() and not finished.is_set()
            assert not engine.calls
        finally:
            release.set()
            assert await asyncio.to_thread(finished.wait, 2)
        assert len(parses) == 1 and parses[0][0] == body
    async def run():
        async with pilot(tmp_path, scenario) as (_, _, helper, _):
            helpers.append(helper)
    asyncio.run(run())


@pytest.mark.parametrize('operation', ['chat', 'responses', 'ollama-chat'])
@pytest.mark.parametrize('stream', [None, 0, 'true'])
def test_engine_defaults_bypass_body_policy_stream(operation, stream):
    body = {'model': 'test-model', 'messages': [], 'store': False} if operation == 'responses' else {'model': 'test-model', 'messages': []}
    if operation == 'responses':
        body['input'] = body.pop('messages')
    if stream is not None:
        body['stream'] = stream
    with pytest.raises(ProtocolError):
        validate_body(operation, json.dumps(body).encode(), 'test-model')


def test_engine_defaults_bypass_body_policy_store():
    body = {'model': 'test-model', 'input': 'text', 'stream': False}
    for store in ('missing', None, True, 0):
        candidate = body if store == 'missing' else {**body, 'store': store}
        with pytest.raises(ProtocolError):
            validate_body('responses', json.dumps(candidate).encode(), 'test-model')
    assert validate_body('responses', json.dumps({**body, 'store': False}).encode(), 'test-model')['store'] is False


@pytest.mark.parametrize('oversize', [False, True])
def test_engine_defaults_bypass_body_policy_ollama_ndjson(tmp_path, oversize):
    from sovite.connector.protocol import CHUNK_LIMIT
    records = [b'{"message":{"content":"one"}}\n', b'{"done":true}\n']
    wire = b''.join(records) if not oversize else b'x' * (CHUNK_LIMIT + 1) + b'\n'
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            # Both joined and split records must retain line boundaries.
            yield wire[:5]
            yield wire[5:]
    async def run():
        (tmp_path / 'weights').write_bytes(b'weights')
        settings = HelperSettings('wss://gateway.invalid', 'explicit-unused-ca', '', '', '', 'http://127.0.0.1', '',
                                  {'m': str(tmp_path / 'weights')}, wire_api='ollama-chat')
        engine = FixedEngine(settings)
        await engine.client.aclose()
        engine.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream())))
        try:
            if oversize:
                with pytest.raises(ProtocolError, match='result_too_large'):
                    _ = [chunk async for chunk in engine.stream('ollama-chat', BODY, 'test-model')]
            else:
                assert [chunk async for chunk in engine.stream('ollama-chat', BODY, 'test-model')] == records
        finally:
            await engine.close()
    asyncio.run(run())


@pytest.mark.parametrize('operation', ['chat', 'responses', 'ollama-chat'])
@pytest.mark.parametrize('location', ['message', 'part'])
@pytest.mark.parametrize('field', ['images', 'source', 'document', 'attachments', 'image', 'remote_asset'])
def test_remote_fetch_denylist_replaced_with_allowlist(operation, location, field):
    part = {'type': 'input_text' if operation == 'responses' else 'text', 'text': 'hello'}
    message = {'role': 'user', 'content': [part]}
    (message if location == 'message' else part)[field] = {'url': 'https://remote.invalid'}
    body = {'model': 'test-model', 'stream': False, 'input' if operation == 'responses' else 'messages': [message]}
    if operation == 'responses':
        body['store'] = False
    with pytest.raises(ProtocolError):
        validate_body(operation, json.dumps(body).encode(), 'test-model')


@pytest.mark.parametrize('operation', ['chat', 'responses', 'ollama-chat'])
def test_remote_fetch_allowlist_preserves_tool_data(operation):
    # URL-shaped argument data and schema property names are inert, not routes.
    arguments = {'url': 'https://plain-data.invalid', 'images': ['plain-data']}
    if operation == 'responses':
        messages = [{'type': 'function_call', 'call_id': 'c', 'name': 'f', 'arguments': json.dumps(arguments)},
                    {'type': 'function_call_output', 'call_id': 'c', 'output': json.dumps(arguments)}]
        body = {'input': messages, 'store': False, 'tools': [{'type': 'function', 'name': 'f', 'parameters': {'type': 'object', 'properties': {'url': {'type': 'string'}}}}]}
    else:
        messages = [{'role': 'assistant', 'content': None, 'tool_calls': [{'type': 'function', 'id': 'c', 'function': {'name': 'f', 'arguments': arguments if operation == 'ollama-chat' else json.dumps(arguments)}}]},
                    {'role': 'tool', 'content': json.dumps(arguments)}]
        body = {'messages': messages, 'tools': [{'type': 'function', 'function': {'name': 'f', 'parameters': {'type': 'object', 'properties': {'url': {'type': 'string'}}}}}]}
    body.update(model='test-model', stream=False)
    assert validate_body(operation, json.dumps(body).encode(), 'test-model') == body


def test_frame_size_before_parsing_invoke_body_validated_once(tmp_path, monkeypatch):
    import sovite.connector.helper as implementation
    original = implementation.validate_body
    calls = []
    def count(*args):
        calls.append(args)
        return original(*args)
    monkeypatch.setattr(implementation, 'validate_body', count)
    async def scenario(peer, engine, key, expected):
        await peer.prepare(key, expected)
        await peer.invoke()
        while (await peer.receive())['type'] != 'complete':
            pass
        assert len(calls) == 1 and calls[0] == ('chat', BODY, 'test-model')
        assert len(engine.calls) == 1
    async def run():
        async with pilot(tmp_path, scenario):
            pass
    asyncio.run(run())


@pytest.mark.parametrize('payload', ['wrong-digest', 'invalid-base64'])
def test_frame_size_before_parsing_deferred_payload_fails_closed(tmp_path, payload):
    from sovite.connector.protocol import encode_frame
    async def scenario(peer, engine, key, expected):
        await peer.prepare(key, expected)
        raw = encode_frame({'version': 1, 'session_id': 'session-1', 'seq': peer.session.sequences['gateway'] + 1,
                            'type': 'invoke', 'request_id': 'request-1', 'operation': 'chat',
                            'body': pack(b'wrong'), 'deadline': int(time.time()) + 30, 'permit_ttl_ms': 3000})
        if payload == 'invalid-base64':
            raw = raw.replace(pack(b'wrong').encode(), b'!invalid')
        await peer.socket.send(raw)
        result = await peer.receive()
        assert result['type'] == 'error' and result['code'] == 'invalid_request'
        assert not engine.calls
    async def run():
        async with pilot(tmp_path, scenario):
            pass
    asyncio.run(run())
