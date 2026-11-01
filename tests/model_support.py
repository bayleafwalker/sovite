import asyncio
from contextlib import asynccontextmanager
from collections import deque
from unittest.mock import patch
import copy
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from websockets.asyncio.server import serve
import yaml

from connector_support import ASSERTION, BODY
from test_connector_helper import EnginePeer, certificates
from test_migrations import database
from test_policy_v1alpha2 import bundle as v2_bundle
from sovite.api import create_app
from sovite.authority import Authority, provision_enrollment
from sovite.connector.helper import LOGGER, HelperSettings, PilotHelper
from sovite.connector.identity import digest, manifest
from sovite.connector.protocol import SUBPROTOCOL
from sovite.gateway.client import AuthorityClient
from sovite.gateway.runtime import Gateway, HelperConnection
from sovite.gateway.client import GatewayError
from sovite.service import Platform
from sovite.settings import Settings
from sovite.worker import Worker


class _HandshakeDiagnostics:
    """Bounded test-only events; never store inputs or exception messages."""
    def __init__(self):
        self.started = time.monotonic()
        self.events = deque(maxlen=16)
        self.dropped = 0

    def add(self, **fields):
        if len(self.events) == self.events.maxlen:
            self.dropped += 1
        allowed = {
            "action": {"start", "open", "hello", "touch", "fence"},
            "stage": {"connect", "serve"},
            "outcome": {"ok", "error", "enter"},
            "category": {"authority_unavailable", "model_identity_mismatch", "workload_denied",
                         "model_protocol_error", "model_host_unavailable", "gateway_fenced",
                         "gateway_error", "timeout", "other"},
            "predicates": {"unavailable"},
        }
        booleans = {"tls_present", "tls13", "subprotocol_ok", "no_extensions", "path_ok"}
        safe = {key: value for key, value in fields.items()
                if (key in booleans and type(value) is bool)
                or (key in allowed and type(value) is str and value in allowed[key])}
        elapsed = min(60000, max(0, int((time.monotonic() - self.started) * 1000)))
        self.events.append({"elapsed_ms": elapsed, **safe})

    def note(self):
        return "handshake_diagnostics=" + json.dumps(
            {"events": list(self.events), "dropped": min(self.dropped, 2**31 - 1)},
            sort_keys=True, separators=(",", ":"))


def _error_category(error):
    safe_codes = {"authority_unavailable", "model_identity_mismatch", "workload_denied",
                  "model_protocol_error", "model_host_unavailable", "gateway_fenced"}
    if isinstance(error, GatewayError):
        return error.code if type(error.code) is str and error.code in safe_codes else "gateway_error"
    if isinstance(error, TimeoutError):
        return "timeout"
    return "other"


class _DiagnosticGateway(Gateway):
    """Call the real gateway while retaining only fixed diagnostic categories."""
    def __init__(self, authority, diagnostics):
        super().__init__(authority)
        self.diagnostics = diagnostics

    async def call(self, action, **fields):
        recorded = type(action) is str and action in {"start", "open", "hello", "touch", "fence"}
        try:
            result = await super().call(action, **fields)
        except Exception as error:
            if recorded:
                self.diagnostics.add(action=action, outcome="error", category=_error_category(error))
            raise
        if recorded:
            self.diagnostics.add(action=action, outcome="ok")
        return result

    async def connect(self, socket):
        try:
            tls = socket.transport.get_extra_info("ssl_object")
            self.diagnostics.add(stage="connect", tls_present=tls is not None,
                tls13=bool(tls and tls.version() == "TLSv1.3"),
                subprotocol_ok=socket.subprotocol == SUBPROTOCOL,
                no_extensions=not bool(socket.protocol.extensions),
                path_ok=socket.request.path == "/connector")
        except Exception:
            self.diagnostics.add(stage="connect", predicates="unavailable")
        return await super().connect(socket)


@pytest.fixture
def authority_env(database, tmp_path, v2_bundle):
    bundle = copy.deepcopy(v2_bundle)
    bundle['mode'] = 'development'
    model, role = bundle['models'][0], bundle['connector_roles'][0]
    model.update(model=ASSERTION['model_id'])
    model['identity'].update(engine_version=ASSERTION['engine_version'], weights_manifest_digest=ASSERTION['weights_manifest_digest'])
    role.update(helper_image_digest=ASSERTION['helper_image_digest'], engine_image_digest=ASSERTION['engine_image_digest'])
    path = tmp_path / 'bundle.yaml'
    path.write_text(yaml.safe_dump(bundle))
    settings = Settings(database_url=database.engine.url.render_as_string(hide_password=False), database_schema=database.schema,
                        bundle_path=str(path), artifact_path=str(tmp_path / 'artifacts'), require_qualification=False,
                        authority_token='authority-test-credential-' + 'a' * 24, operator_token='operator-test-credential-' + 'o' * 24,
                        caller_tokens={'operations': 'caller-test-credential-' + 'c' * 24})
    platform = Platform(settings)
    platform.db.engine.dispose()
    platform.db = database
    platform.db.migrate()
    platform.initialize()
    job = platform.submit('incident-forensics', 'private-task', 'operations', 'request-1')
    worker = Worker(platform)
    job = worker.claim()
    assert job is not None
    app = create_app(settings, platform)
    return SimpleNamespace(platform=platform, settings=settings, authority=Authority(platform), worker=worker, job=job, app=app,
                           bundle=bundle, path=tmp_path, model=model, role=role)


@asynccontextmanager
async def connected(env, *, stall=False, helper_type=PilotHelper):
    tls, key = certificates(env.path)
    provision_enrollment(env.platform, enrollment_id=ASSERTION['enrollment_id'], binding_id=env.model['id'],
                         installation_id=ASSERTION['installation_id'], role=env.role['id'], certificate=(env.path / 'helper.pem').read_bytes())
    engine = EnginePeer(stall)
    http_server = await asyncio.start_server(engine.handle, '127.0.0.1', 0)
    metadata = {k: v for k, v in ASSERTION.items() if k not in {'weight_files', 'weights_manifest_digest', 'session_id', 'challenge', 'issued_at', 'expires_at'}}
    metadata.update(binding_id=env.model['id'], enrollment_role=env.role['id'], cancellation='disconnect-only')
    (env.path / 'identity.json').write_text(json.dumps(metadata))
    (env.path / 'model.gguf').write_bytes(b'weights')
    client = AuthorityClient('http://control.test', env.settings.authority_token, transport=httpx.ASGITransport(app=env.app))
    diagnostics = _HandshakeDiagnostics()
    gateway = _DiagnosticGateway(client, diagnostics)
    await gateway.start()
    original_serve = HelperConnection.serve
    async def diagnostic_serve(connection):
        if connection.gateway is not gateway:
            return await original_serve(connection)
        diagnostics.add(stage="serve", outcome="enter")
        try:
            return await original_serve(connection)
        except Exception as error:
            diagnostics.add(stage="serve", outcome="error", category=_error_category(error))
            raise
    with patch.object(HelperConnection, "serve", diagnostic_serve):
        async with serve(gateway.connect, '127.0.0.1', 0, ssl=tls, subprotocols=[SUBPROTOCOL], compression=None, logger=LOGGER) as listener:
            settings = HelperSettings(gateway_url=f'wss://127.0.0.1:{listener.sockets[0].getsockname()[1]}/connector',
                ca_file=str(env.path / 'ca.pem'), cert_file=str(env.path / 'helper.pem'), key_file=str(env.path / 'helper.key'),
                signing_key_file=str(env.path / 'helper.key'), engine_endpoint=f'http://127.0.0.1:{http_server.sockets[0].getsockname()[1]}',
                identity_file=str(env.path / 'identity.json'), weight_paths={'model.gguf': str(env.path / 'model.gguf')})
            helper = helper_type(settings)
            task = asyncio.create_task(helper.run())
            try:
                try:
                    async with asyncio.timeout(5):
                        while env.model['id'] not in gateway.sessions:
                            if task.done():
                                task.result()
                            await asyncio.sleep(.01)
                except Exception as error:
                    error.add_note(diagnostics.note())
                    raise
                env.gateway, env.helper, env.engine, env.helper_settings = gateway, helper, engine, settings
                yield env
            finally:
                await gateway.close()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                http_server.close()
                await http_server.wait_closed()
                if engine.handlers:
                    await asyncio.wait_for(asyncio.gather(*engine.handlers, return_exceptions=True), 3)


async def infer(env, **changes):
    values = dict(job_id=env.job.id, fence=env.job.execution_fence, capability=env.job.execution_capability,
                  workload='worker', operation='chat', body=BODY, deadline=int(time.time()) + 25)
    values.update(changes)
    return await env.gateway.infer(env.model['id'], **values)


async def consume(request):
    return b''.join([block async for block in request.stream()])
