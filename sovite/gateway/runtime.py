from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
import uuid

from cryptography.hazmat.primitives import serialization
from pydantic import ValidationError

from ..connector.canonical import ProtocolError
from ..connector.helper import validate_body
from ..connector.identity import ExpectedIdentity, digest, verify_connection, verify_preparation
from ..connector.protocol import BODY_LIMIT, CONTEXT, FRESHNESS_SECONDS, HEARTBEAT_SECONDS, SUBPROTOCOL, Session, pack, unpack
from .client import GatewayError
from .requests import Authorize


class HelperConnection:
    def __init__(self, gateway, socket, configuration):
        self.gateway, self.socket, self.configuration = gateway, socket, configuration
        self.session = Session("gateway", configuration["session_id"])
        self.key = serialization.load_pem_public_key(configuration["public_key"].encode())
        self.expected = ExpectedIdentity(**{k: configuration[k] for k in ("installation_id", "binding_id", "enrollment_id", "enrollment_role")},
                                         session_id=self.session.session_id, challenge=configuration["nonce"])
        self.connection = None
        self.events = asyncio.Queue(maxsize=4)
        self.send_lock, self.busy = asyncio.Lock(), False
        self.closed = asyncio.Event()

    async def send(self, kind, **fields):
        async with self.send_lock:
            await asyncio.wait_for(self.socket.send(self.session.send(kind, **fields)), 1)

    def context(self):
        return {k: self.connection.assertion[k] for k in CONTEXT}

    async def receive(self, timeout):
        if self.closed.is_set():
            raise GatewayError("model_host_unavailable")
        event = asyncio.create_task(self.events.get())
        closed = asyncio.create_task(self.closed.wait())
        try:
            done, _ = await asyncio.wait({event, closed}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if closed in done:
                raise GatewayError("model_host_unavailable")
            if event not in done:
                raise GatewayError("deadline_exceeded")
            return event.result()
        finally:
            event.cancel()
            closed.cancel()
            await asyncio.gather(event, closed, return_exceptions=True)

    async def heartbeats(self):
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await self.send("heartbeat", ready=not self.busy, **self.context())

    async def serve(self):
        c = self.configuration
        await self.send("challenge", **{k: c[k] for k in ("nonce", "installation_id", "binding_id", "enrollment_id", "enrollment_role")})
        hello = self.session.receive(await asyncio.wait_for(self.socket.recv(), FRESHNESS_SECONDS))
        if hello["type"] != "hello":
            raise ProtocolError("hello required")
        self.connection = verify_connection(hello["identity"], self.key, self.expected, now=int(time.time()))
        await self.gateway.call("hello", session_id=self.session.session_id, identity=hello["identity"])
        self.gateway.sessions[c["binding_id"]] = self
        heartbeat = asyncio.create_task(self.heartbeats())
        receiver = asyncio.create_task(self.socket.recv())
        try:
            while True:
                done, _ = await asyncio.wait({receiver, heartbeat}, timeout=FRESHNESS_SECONDS, return_when=asyncio.FIRST_COMPLETED)
                if heartbeat in done:
                    heartbeat.result()
                    return
                if receiver not in done:
                    raise GatewayError("model_host_unavailable")
                raw = receiver.result()
                receiver = asyncio.create_task(self.socket.recv())
                message = self.session.receive(raw)
                if message["type"] == "heartbeat":
                    await self.gateway.call("touch", session_id=self.session.session_id)
                elif message["type"] == "renew_csr":
                    raise ProtocolError("renewal unavailable")
                else:
                    try:
                        self.events.put_nowait(message)
                    except asyncio.QueueFull:
                        raise ProtocolError("response backpressure limit") from None
        finally:
            receiver.cancel()
            heartbeat.cancel()
            await asyncio.gather(receiver, heartbeat, return_exceptions=True)

    async def close(self):
        self.closed.set()
        self.session.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.socket.close(), 1)


class Inference:
    def __init__(self, gateway, helper, request, capability, workload, body):
        self.gateway, self.helper, self.request = gateway, helper, request
        self.capability, self.workload, self.body = capability, workload, body
        self.id = uuid.uuid4().hex
        self.permit, self.valid_until = None, 0
        self.output = asyncio.Queue(maxsize=4)
        self.done = asyncio.Event()
        self.error, self.task, self.renewal = None, None, None
        self.invoked = False
        self.pending_chunk = None

    async def authority(self, action, **fields):
        return await self.gateway.call(action, request_id=self.id, capability=self.capability, workload=self.workload, **fields)

    def install_permit(self, value):
        self.permit, self.valid_until = value["permit"], value["valid_until"]

    def ttl(self):
        ttl = int((min(self.valid_until, time.monotonic() + self.request["deadline"] - time.time()) - time.monotonic()) * 1000)
        if ttl < 1:
            raise GatewayError("permit_expired")
        return min(3000, ttl)

    async def start(self):
        try:
            value = await self.authority("authorize", request=self.request)
            self.install_permit(value)
        except BaseException as error:
            self.helper.busy = False
            if isinstance(error, GatewayError) and error.code == "authority_unavailable":
                with contextlib.suppress(Exception):
                    await self.authority("finish", permit=None, stopped=True, outcome="authority_unavailable")
            self.body = b""
            raise
        self.task = asyncio.create_task(self.run())
        self.gateway.requests.add(self)
        return self

    async def renew(self):
        while True:
            await asyncio.sleep(min(1, max(0.01, (self.valid_until - time.monotonic()) / 3)))
            self.ttl()
            previous = self.valid_until
            value = await self.authority("renew", permit=self.permit)
            if time.monotonic() >= previous:
                raise GatewayError("permit_expired")
            self.install_permit(value)
            if self.invoked and self.helper.session.phase in {"invoked", "streaming"}:
                await self.helper.send("permit", request_id=self.id, permit_ttl_ms=self.ttl())

    async def event(self, timeout):
        received = asyncio.create_task(self.helper.receive(timeout))
        try:
            done, _ = await asyncio.wait({received, self.renewal}, return_when=asyncio.FIRST_COMPLETED)
            if self.renewal in done:
                self.renewal.result()
                raise GatewayError("authority_unavailable")
            self.ttl()
            return received.result()
        finally:
            received.cancel()
            await asyncio.gather(received, return_exceptions=True)

    async def cancel(self):
        helper = self.helper
        if helper.closed.is_set() or helper.session.phase == "closed":
            return False
        bound = min(6, helper.configuration["binding"]["cancel_bound_seconds"])
        try:
            until = time.monotonic() + bound
            await helper.send("cancel", request_id=self.id)
            while time.monotonic() < until:
                message = await helper.receive(max(0.01, until - time.monotonic()))
                if message["type"] == "cancelled":
                    return True
        except Exception:
            pass
        return False

    async def run(self):
        helper, stopped, outcome = self.helper, False, "model_host_unavailable"
        self.renewal = asyncio.create_task(self.renew())
        try:
            self.ttl()
            await helper.send("prepare", request_id=self.id, operation=self.request["operation"], body_digest=self.request["body_digest"], **helper.context())
            message = await self.event(min(FRESHNESS_SECONDS, self.request["deadline"] - time.time()))
            if message["type"] != "prepared":
                raise GatewayError("model_protocol_error")
            try:
                verify_preparation(message["identity"], helper.key, helper.expected, helper.connection,
                                   helper.configuration["binding"], helper.configuration["role"], request_id=self.id,
                                   body_digest=self.request["body_digest"], now=int(time.time()), mode=helper.configuration["mode"])
            except ProtocolError:
                raise GatewayError("model_identity_mismatch") from None
            # PIN_BEFORE_PROMPT + CURRENT_AUTHORITY: prepared identity is checked in control too.
            value = await self.authority("renew", permit=self.permit, preparation=message["identity"])
            self.ttl()
            self.install_permit(value)
            await helper.send("invoke", request_id=self.id, operation=self.request["operation"], body=pack(self.body),
                              deadline=self.request["deadline"], permit_ttl_ms=self.ttl())
            self.body = b""
            self.invoked = True
            first_token = time.monotonic() + helper.configuration["binding"]["first_token_timeout_seconds"]
            while True:
                timeout = min(self.request["deadline"] - time.time(), first_token - time.monotonic())
                if timeout <= 0:
                    raise GatewayError("deadline_exceeded")
                message = await self.event(timeout)
                kind = message["type"]
                if kind == "chunk":
                    first_token = float("inf")
                    try:
                        if self.pending_chunk is not None:
                            self.output.put_nowait(self.pending_chunk)
                        self.pending_chunk = unpack(message["data"], 65536)
                    except asyncio.QueueFull:
                        raise GatewayError("downstream_backpressure") from None
                elif kind == "complete":
                    if self.pending_chunk is not None and self.output.full():
                        raise GatewayError("downstream_backpressure")
                    await self.authority("finish", permit=self.permit, outcome="completed", stopped=True)
                    # Hold the terminal chunk until completion authority commits.
                    if self.pending_chunk is not None:
                        self.output.put_nowait(self.pending_chunk)
                        self.pending_chunk = None
                    stopped, outcome = True, "completed"
                    break
                elif kind == "error":
                    raise GatewayError("model_host_preempted" if message["code"] == "model_host_preempted" else "model_protocol_error")
                elif kind != "accepted":
                    raise GatewayError("model_protocol_error")
        except BaseException as error:
            outcome = error.code if isinstance(error, GatewayError) else "gateway_interrupted" if isinstance(error, asyncio.CancelledError) else "model_host_unavailable"
            self.error = GatewayError(outcome)
            self.renewal.cancel()
            await asyncio.gather(self.renewal, return_exceptions=True)
            stopped = await self.cancel()
            with contextlib.suppress(Exception):
                await self.authority("finish", permit=self.permit, outcome=outcome, stopped=stopped)
            if not stopped or outcome in {"model_identity_mismatch", "model_protocol_error"}:
                await helper.close()
        finally:
            self.body, self.pending_chunk = b"", None
            self.renewal.cancel()
            await asyncio.gather(self.renewal, return_exceptions=True)
            helper.busy = False
            self.done.set()
            self.gateway.requests.discard(self)

    async def stream(self):
        try:
            while not self.done.is_set() or not self.output.empty():
                if self.error:
                    raise self.error
                value = asyncio.create_task(self.output.get())
                finished = asyncio.create_task(self.done.wait())
                try:
                    done, _ = await asyncio.wait({value, finished}, return_when=asyncio.FIRST_COMPLETED)
                    if value in done:
                        if self.error:
                            raise self.error
                        yield value.result()
                finally:
                    value.cancel()
                    finished.cancel()
                    await asyncio.gather(value, finished, return_exceptions=True)
            if self.error:
                raise self.error
        finally:
            if not self.done.is_set():
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)


class Gateway:
    def __init__(self, authority):
        self.authority, self.id = authority, uuid.uuid4().hex
        self.sessions, self.requests = {}, set()

    async def call(self, action, **fields):
        return await self.authority.call(action, gateway_id=self.id, **fields)

    async def start(self):
        await self.call("start")

    async def connect(self, socket):
        helper = None
        try:
            tls = socket.transport.get_extra_info("ssl_object")
            cert = tls.getpeercert(binary_form=True) if tls else None
            if not cert or tls.version() != "TLSv1.3" or socket.subprotocol != SUBPROTOCOL or socket.protocol.extensions:
                raise GatewayError("workload_denied")
            if socket.request.path != "/connector":
                raise GatewayError("model_protocol_error")
            config = await self.call("open", certificate_digest=hashlib.sha256(cert).hexdigest())
            helper = HelperConnection(self, socket, config)
            await helper.serve()
        except Exception:
            pass
        finally:
            if helper:
                await helper.close()
                if self.sessions.get(helper.configuration["binding_id"]) is helper:
                    del self.sessions[helper.configuration["binding_id"]]
                for attempt in range(3):
                    try:
                        await self.call("fence", session_id=helper.session.session_id)
                        break
                    except GatewayError as error:
                        if error.code != "authority_unavailable" or attempt == 2:
                            break
                        await asyncio.sleep(.1)
            else:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(socket.close(), 1)

    async def infer(self, binding_id, *, job_id, fence, capability, workload, operation, body, deadline):
        helper = self.sessions.get(binding_id)
        if helper is None or helper.closed.is_set():
            raise GatewayError("model_host_unavailable")
        if helper.busy:
            raise GatewayError("binding_busy")
        if type(body) is not bytes or not 0 < len(body) <= BODY_LIMIT:
            raise GatewayError("inference_request_denied")
        request = {"job_id": job_id, "fence": fence, "session_id": helper.session.session_id, "operation": operation,
                   "body_digest": digest(body), "body_size": len(body), "deadline": deadline}
        try:
            Authorize(gateway_id=self.id, request_id=uuid.uuid4().hex, request=request, capability=capability, workload=workload)
        except ValidationError:
            raise GatewayError("inference_request_denied") from None
        try:
            await asyncio.to_thread(validate_body, operation, body, helper.configuration["binding"]["model"])
        except (ProtocolError, ValueError, TypeError, KeyError):
            raise GatewayError("inference_request_denied") from None
        # Recheck after validation yields; never queue a second request for later replay.
        if helper.busy or helper.closed.is_set():
            raise GatewayError("binding_busy")
        helper.busy = True
        return await Inference(self, helper, request, capability, workload, body).start()

    async def close(self):
        for request in list(self.requests):
            request.task.cancel()
        await asyncio.gather(*(r.task for r in list(self.requests)), return_exceptions=True)
        for helper in list(self.sessions.values()):
            await helper.close()
        await self.authority.close()
