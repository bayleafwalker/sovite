import asyncio
from contextlib import asynccontextmanager
from collections import deque
import copy
import json
import sqlite3
import time
from types import SimpleNamespace

import httpx
from sqlalchemy.exc import OperationalError
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
from sovite.gateway.client import AUTHORITY_CODES, AuthorityClient
from sovite.gateway.runtime import Gateway
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
            "action": {"start", "open", "hello", "touch", "fence", "authorize", "renew", "finish", "other"},
            "stage": {"connect", "setup", "authority"},
            "outcome": {"ok", "error", "cancelled", "unknown"},
            "category": {"authority_unavailable", "model_identity_mismatch", "workload_denied",
                         "model_protocol_error", "model_host_unavailable", "gateway_fenced",
                         "gateway_error", "timeout", "http_timeout", "transport_error",
                         "database_busy", "database_operational_error", "invalid_response",
                         "cancelled", "unknown", "other"},
            "cause_category": {"timeout", "http_timeout", "transport_error", "database_busy",
                               "database_operational_error", "invalid_response", "cancelled",
                               "other", "unknown", "none"},
            "status": {"2xx", "422", "4xx", "5xx", "other"},
            "owner_state": {"not_observed"},
            "predicates": {"unavailable"},
        }
        booleans = {"tls_present", "tls13", "subprotocol_ok", "no_extensions", "path_ok"}
        safe = {key: value for key, value in fields.items()
                if (key in booleans and type(value) is bool)
                or (key in allowed and type(value) is str and value in allowed[key])
                or (key == "duration_ms" and type(value) is int and 0 <= value <= 60000)}
        elapsed = min(60000, max(0, int((time.monotonic() - self.started) * 1000)))
        self.events.append({"elapsed_ms": elapsed, **safe})

    def note(self):
        def render(value):
            return "true" if value is True else "false" if value is False else str(value)
        events = "|".join(",".join(key + ":" + render(event[key]) for key in sorted(event))
                          for event in self.events)
        return "handshake_diagnostics dropped=" + str(min(self.dropped, 2**31 - 1)) + " events=" + events


def _error_category(error):
    safe_codes = {"authority_unavailable", "model_identity_mismatch", "workload_denied",
                  "model_protocol_error", "model_host_unavailable", "gateway_fenced"}
    if isinstance(error, GatewayError):
        return error.code if type(error.code) is str and error.code in safe_codes else "gateway_error"
    if isinstance(error, TimeoutError):
        return "timeout"
    return "other"


def _safe_exception_category(error):
    if isinstance(error, asyncio.CancelledError):
        return "cancelled"
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return "http_timeout" if isinstance(error, httpx.TimeoutException) else "timeout"
    if isinstance(error, httpx.TransportError):
        return "transport_error"
    if isinstance(error, OperationalError):
        code = getattr(getattr(error, "orig", None), "sqlite_errorcode", None)
        if code in {getattr(sqlite3, "SQLITE_BUSY", -1), getattr(sqlite3, "SQLITE_LOCKED", -1)}:
            return "database_busy"
        return "database_operational_error"
    if isinstance(error, (ValueError, KeyError, TypeError, json.JSONDecodeError)):
        return "invalid_response"
    return "other"


def _safe_cause_category(error):
    seen = set()
    current = error.__cause__ or error.__context__
    for _ in range(4):
        if current is None:
            return "none"
        identity = id(current)
        if identity in seen:
            return "unknown"
        seen.add(identity)
        category = _safe_exception_category(current)
        if category != "other":
            return category
        current = current.__cause__ or current.__context__
    return "unknown" if current is not None else "none"


def _status_category(status):
    if status == 422:
        return "422"
    if 200 <= status < 300:
        return "2xx"
    if 400 <= status < 500:
        return "4xx"
    if 500 <= status < 600:
        return "5xx"
    return "other"


def _best_effort_observation(callback):
    """Diagnostic failures must not replace the observed operation outcome."""
    try:
        callback()
    except asyncio.CancelledError:
        # The callback is synchronous observer code. Even if it raises
        # CancelledError, it must not replace the transport/gateway outcome.
        return
    except Exception:
        return


class _DiagnosticASGITransport(httpx.AsyncBaseTransport):
    """Pass through ASGI requests while retaining bounded, body-free diagnostics."""
    def __init__(self, transport, diagnostics):
        self.transport = transport
        self.diagnostics = diagnostics

    async def handle_async_request(self, request):
        action = request.url.path.rsplit("/", 1)[-1]
        if action not in {"start", "open", "hello", "touch", "fence", "authorize", "renew", "finish"}:
            action = "other"
        started = time.monotonic()
        try:
            response = await self.transport.handle_async_request(request)
        except asyncio.CancelledError:
            _best_effort_observation(lambda: self.diagnostics.add(
                action=action, stage="authority", outcome="cancelled", category="cancelled",
                duration_ms=min(60000, max(0, int((time.monotonic() - started) * 1000)))))
            raise
        except Exception as error:
            _best_effort_observation(lambda: self.diagnostics.add(
                action=action, stage="authority", outcome="error", category=_safe_exception_category(error),
                duration_ms=min(60000, max(0, int((time.monotonic() - started) * 1000)))))
            raise
        _best_effort_observation(lambda: self.diagnostics.add(
            action=action, stage="authority", outcome="ok" if response.status_code == 200 else "error",
            status=_status_category(response.status_code),
            duration_ms=min(60000, max(0, int((time.monotonic() - started) * 1000)))))
        return response

    async def aclose(self):
        await self.transport.aclose()


class _DiagnosticGateway(Gateway):
    """Call the real gateway while retaining only fixed diagnostic categories."""
    def __init__(self, authority, diagnostics):
        super().__init__(authority)
        self.diagnostics = diagnostics

    async def call(self, action, **fields):
        recorded = type(action) is str and action in {"start", "open", "hello", "touch", "fence", "authorize", "renew", "finish"}
        try:
            result = await super().call(action, **fields)
        except asyncio.CancelledError:
            if recorded:
                _best_effort_observation(lambda: self.diagnostics.add(
                    action=action, stage="authority", outcome="cancelled", category="cancelled"))
            raise
        except Exception as error:
            if recorded:
                def observe_failure():
                    event = {"action": action, "stage": "authority", "outcome": "error",
                             "category": _error_category(error), "cause_category": _safe_cause_category(error),
                             "owner_state": "not_observed"}
                    self.diagnostics.add(**event)
                    if isinstance(error, GatewayError):
                        error.add_note(self.diagnostics.note())

                _best_effort_observation(observe_failure)
            raise
        if recorded:
            _best_effort_observation(lambda: self.diagnostics.add(
                action=action, stage="authority", outcome="ok"))
        return result

    async def connect(self, socket):
        def observe_connect():
            try:
                tls = socket.transport.get_extra_info("ssl_object")
                fields = {"tls_present": tls is not None,
                          "tls13": bool(tls and tls.version() == "TLSv1.3"),
                          "subprotocol_ok": socket.subprotocol == SUBPROTOCOL,
                          "no_extensions": not bool(socket.protocol.extensions),
                          "path_ok": socket.request.path == "/connector"}
            except Exception:
                fields = {"predicates": "unavailable"}
            self.diagnostics.add(stage="connect", **fields)

        _best_effort_observation(observe_connect)
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


class _SetupOpenAuthorityClient(AuthorityClient):
    """Test-only allowance for selected ASGI actions during fixture setup.

    All other calls and all calls after readiness retain the production timeout.
    """
    def __init__(self, *args, setup_actions=frozenset({'open'}), **kwargs):
        super().__init__(*args, **kwargs)
        self.preparing = True
        self.setup_actions = setup_actions

    async def call(self, action, **fields):
        if action not in self.setup_actions or not self.preparing:
            return await super().call(action, **fields)
        try:
            response = await asyncio.wait_for(
                self.client.post('/internal/authority/' + action, json=fields), 4)
            if response.status_code != 200:
                if response.status_code == 422:
                    raise GatewayError('inference_request_denied')
                payload = response.json()
                detail = payload.get('detail', {}) if isinstance(payload, dict) else {}
                code = detail.get('code') if isinstance(detail, dict) else None
                raise GatewayError(code if code in AUTHORITY_CODES else 'authority_unavailable')
            return response.json()
        except GatewayError:
            raise
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError):
            raise GatewayError('authority_unavailable') from None


@asynccontextmanager
async def connected(env, *, stall=False, helper_type=PilotHelper, diagnostics_factory=_HandshakeDiagnostics,
                    relaxed_open_setup=False, relaxed_hello_setup=False):
    tls, key = certificates(env.path)
    provision_enrollment(env.platform, enrollment_id=ASSERTION['enrollment_id'], binding_id=env.model['id'],
                         installation_id=ASSERTION['installation_id'], role=env.role['id'], certificate=(env.path / 'helper.pem').read_bytes())
    engine = EnginePeer(stall)
    http_server = await asyncio.start_server(engine.handle, '127.0.0.1', 0)
    metadata = {k: v for k, v in ASSERTION.items() if k not in {'weight_files', 'weights_manifest_digest', 'session_id', 'challenge', 'issued_at', 'expires_at'}}
    metadata.update(binding_id=env.model['id'], enrollment_role=env.role['id'], cancellation='disconnect-only')
    (env.path / 'identity.json').write_text(json.dumps(metadata))
    (env.path / 'model.gguf').write_bytes(b'weights')
    diagnostics = diagnostics_factory()
    transport = _DiagnosticASGITransport(httpx.ASGITransport(app=env.app), diagnostics)
    if relaxed_open_setup or relaxed_hello_setup:
        actions = frozenset(action for action, enabled in (
            ('open', relaxed_open_setup), ('hello', relaxed_hello_setup)) if enabled)
        client = _SetupOpenAuthorityClient('http://control.test', env.settings.authority_token,
                                           transport=transport, setup_actions=actions)
    else:
        client = AuthorityClient('http://control.test', env.settings.authority_token, transport=transport)
    helper_holder = {"value": None}

    gateway = _DiagnosticGateway(client, diagnostics)
    try:
        await gateway.start()
    except BaseException as error:
        def observe_start_failure():
            diagnostics.add(stage="setup", outcome="cancelled" if isinstance(error, asyncio.CancelledError) else "error")
            error.add_note(diagnostics.note())

        _best_effort_observation(observe_start_failure)
        try:
            await gateway.close()
        except (Exception, asyncio.CancelledError):
            pass
        try:
            http_server.close()
        except (Exception, asyncio.CancelledError):
            pass
        try:
            await http_server.wait_closed()
        except (Exception, asyncio.CancelledError):
            pass
        raise
    async with serve(gateway.connect, '127.0.0.1', 0, ssl=tls, subprotocols=[SUBPROTOCOL], compression=None, logger=LOGGER) as listener:
        settings = HelperSettings(gateway_url=f'wss://127.0.0.1:{listener.sockets[0].getsockname()[1]}/connector',
            ca_file=str(env.path / 'ca.pem'), cert_file=str(env.path / 'helper.pem'), key_file=str(env.path / 'helper.key'),
            signing_key_file=str(env.path / 'helper.key'), engine_endpoint=f'http://127.0.0.1:{http_server.sockets[0].getsockname()[1]}',
            identity_file=str(env.path / 'identity.json'), weight_paths={'model.gguf': str(env.path / 'model.gguf')})
        helper = helper_type(settings)
        helper_holder["value"] = helper
        task = asyncio.create_task(helper.run())
        try:
            try:
                async with asyncio.timeout(5):
                    while env.model['id'] not in gateway.sessions:
                        if task.done():
                            task.result()
                        await asyncio.sleep(.01)
            except asyncio.CancelledError as error:
                def observe_setup_cancellation():
                    diagnostics.add(stage="setup", outcome="unknown")
                    error.add_note(diagnostics.note())

                _best_effort_observation(observe_setup_cancellation)
                raise
            except Exception as error:
                def observe_setup_failure():
                    diagnostics.add(stage="setup", outcome="unknown")
                    error.add_note(diagnostics.note())

                _best_effort_observation(observe_setup_failure)
                raise
            if relaxed_open_setup or relaxed_hello_setup:
                client.preparing = False
            env.gateway, env.helper, env.engine, env.helper_settings = gateway, helper, engine, settings
            env.authority_diagnostics = diagnostics
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
