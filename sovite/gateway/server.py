from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import ssl
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from websockets.asyncio.server import serve

from ..connector.helper import LOGGER
from ..connector.protocol import BODY_LIMIT, CONTROL_LIMIT, SUBPROTOCOL
from .client import AuthorityClient, GatewayError
from .runtime import Gateway


@dataclass
class GatewaySettings:
    authority_url: str
    authority_token: str
    authority_ca_file: str
    workload_tokens: dict[str, str]
    cert_file: str
    key_file: str
    helper_ca_file: str
    connector_host: str = "0.0.0.0"
    connector_port: int = 8443
    inference_host: str = "127.0.0.1"
    inference_port: int = 8444

    @classmethod
    def from_env(cls):
        # Deliberately never loads Settings.from_env or the shared runtime environment.
        return cls(**json.loads(Path(os.environ["SOVITE_GATEWAY_SETTINGS"]).read_text()))

    def validate(self):
        origin = urlsplit(self.authority_url)
        if (origin.scheme != "https" or not origin.hostname or origin.username or origin.password
                or origin.path not in {"", "/"} or origin.query or origin.fragment or not self.authority_ca_file):
            raise ValueError("authority requires a fixed HTTPS origin and explicit trust")
        tokens = [self.authority_token, *self.workload_tokens.values()]
        if (not self.workload_tokens or not all(type(t) is str and len(t) >= 24 for t in tokens)
                or len(tokens) != len(set(tokens)) or any(not k or len(k) > 160 for k in self.workload_tokens)):
            raise ValueError("gateway and execution workload credentials must be distinct and at least 24 characters")


def connector_tls(settings):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(settings.cert_file, settings.key_file)
    context.load_verify_locations(settings.helper_ca_file)
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def create_inference_app(gateway, workload_tokens):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/inference/{binding_id}/{operation}")
    async def inference(binding_id: str, operation: str, request: Request):
        raw = request.headers.get("authorization", "").encode()
        workload = next((name for name, token in workload_tokens.items()
                         if secrets.compare_digest(raw, ("Bearer " + token).encode())), None)
        if workload is None:
            return JSONResponse({"code": "workload_denied"}, status_code=403)
        body = bytearray()
        async for block in request.stream():
            body.extend(block)
            if len(body) > BODY_LIMIT:
                return JSONResponse({"code": "inference_request_denied"}, status_code=413)
        try:
            deadline = int(request.headers.get("x-sovite-deadline", "0"))
            result = await gateway.infer(binding_id, job_id=request.headers.get("x-sovite-job", ""),
                fence=request.headers.get("x-sovite-fence", ""), capability=request.headers.get("x-sovite-capability", ""),
                workload=workload, operation=operation, body=bytes(body), deadline=deadline)
        except (ValueError, GatewayError) as error:
            code = error.code if isinstance(error, GatewayError) else "inference_request_denied"
            return JSONResponse({"code": code}, status_code=409 if code == "binding_busy" else 403)
        async def stream():
            try:
                async for block in result.stream():
                    yield block
            except GatewayError as error:
                yield b'event: sovite_error\ndata: ' + json.dumps({"code": error.code}).encode() + b'\n\n'
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    return app


async def run(settings):
    import uvicorn
    settings.validate()
    trust = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    trust.load_verify_locations(settings.authority_ca_file)
    trust.minimum_version = trust.maximum_version = ssl.TLSVersion.TLSv1_3
    gateway = Gateway(AuthorityClient(settings.authority_url, settings.authority_token, verify=trust))
    await gateway.start()
    app = create_inference_app(gateway, settings.workload_tokens)
    config = uvicorn.Config(app, host=settings.inference_host, port=settings.inference_port, access_log=False,
                            ssl_certfile=settings.cert_file, ssl_keyfile=settings.key_file, log_level="critical")
    config.load()
    config.ssl.minimum_version = config.ssl.maximum_version = ssl.TLSVersion.TLSv1_3
    try:
        async with serve(gateway.connect, settings.connector_host, settings.connector_port, ssl=connector_tls(settings),
                         subprotocols=[SUBPROTOCOL], compression=None, max_size=4 * ((BODY_LIMIT + 2) // 3) + CONTROL_LIMIT,
                         max_queue=4, write_limit=32768, open_timeout=6, close_timeout=1, logger=LOGGER):
            await uvicorn.Server(config).serve()
    finally:
        await gateway.close()


def main():
    try:
        asyncio.run(run(GatewaySettings.from_env()))
    except KeyboardInterrupt:
        return
    except Exception:
        raise SystemExit("Gateway stopped; inspect private operational state.") from None
