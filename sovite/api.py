from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from .db import AuditEvent, AuthSession, BundleRevision, GrantState, Job, Qualification, WorkerHeartbeat
from .qualification import current_qualification, is_qualified, verify_qualification
from .service import PUBLIC_STATUS, Platform, PlatformError, job_dict
from .settings import Settings
from .worker import Worker


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(StrictBody):
    token: str = Field(min_length=1, max_length=1024)


class NewJob(StrictBody):
    grant_id: str = Field(min_length=1, max_length=96)
    task: str = Field(min_length=1, max_length=65536)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=160)
    input_class: str | None = Field(default=None, min_length=1, max_length=96)


class Reason(StrictBody):
    reason: str = Field(min_length=1, max_length=500)


def token_identity(token: str, settings: Settings) -> tuple[str, str] | None:
    raw = token.encode()
    if settings.operator_token and secrets.compare_digest(raw, settings.operator_token.encode()):
        return "operator", "operator"
    for name, expected in settings.caller_tokens.items():
        if secrets.compare_digest(raw, expected.encode()):
            return name, "caller"
    return None


def create_app(settings: Settings | None = None, platform: Platform | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    platform = platform or Platform(settings)
    active_worker: Worker | None = None
    worker_task: asyncio.Task | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal active_worker, worker_task
        platform.initialize()
        if settings.embedded_worker:
            active_worker = Worker(platform)
            worker_task = asyncio.create_task(active_worker.run())
        yield
        if active_worker:
            active_worker.stop.set()
        if worker_task:
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)

    app = FastAPI(title=f"{settings.display_name} Platform", version="0.1.11", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.platform = platform

    @app.middleware("http")
    async def bounds_and_headers(request: Request, call_next):
        if request.url.path == "/mcp" and request.headers.get("origin") and request.headers["origin"].rstrip("/") != settings.public_origin:
            return JSONResponse({"detail": {"code": "origin_denied", "message": "Request origin is not permitted"}}, status_code=403)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != settings.public_origin:
                return JSONResponse({"detail": {"code": "origin_denied", "message": "Request origin is not permitted"}}, status_code=403)
            limit = settings.max_request_bytes + 4096
            length = request.headers.get("content-length")
            try:
                if length is not None and int(length) > limit:
                    return JSONResponse({"detail": {"code": "request_too_large", "message": "Request exceeds the configured limit"}}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": {"code": "invalid_request", "message": "Invalid request"}}, status_code=400)
            data = bytearray()
            async for part in request.stream():
                data.extend(part)
                if len(data) > limit:
                    return JSONResponse({"detail": {"code": "request_too_large", "message": "Request exceeds the configured limit"}}, status_code=413)
            request._body = bytes(data)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        if request.url.path.startswith(("/api/", "/mcp")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(PlatformError)
    async def platform_error(request: Request, exc: PlatformError):
        return JSONResponse({"detail": {"code": exc.code, "message": exc.message}}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Pydantic's default error includes the rejected input, which can be private.
        return JSONResponse({"detail": {"code": "invalid_request", "message": "The request does not match the documented schema"}}, status_code=422)

    async def identity(request: Request) -> tuple[str, str]:
        authorization = request.headers.get("authorization", "")
        if authorization.startswith("Bearer "):
            result = token_identity(authorization[7:], settings)
            if result:
                return result
        else:
            cookie = request.cookies.get("sovite_session")
            if cookie:
                with platform.db.sessions() as session:
                    row = session.get(AuthSession, hashlib.sha256(cookie.encode()).hexdigest())
                    if row and row.expires_at > time.time():
                        if request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("origin", "").rstrip("/") != settings.public_origin:
                            raise PlatformError("origin_required", "Cookie-authenticated changes require the configured origin", 403)
                        return row.identity, row.role
        raise PlatformError("unauthorized", "Authentication is required", 401)

    async def operator(auth=Depends(identity)) -> tuple[str, str]:
        if auth[1] != "operator":
            raise PlatformError("forbidden", "Operator access is required", 403)
        return auth

    @app.get("/health/live")
    async def liveness():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def readiness():
        try:
            with platform.db.sessions() as session:
                session.execute(select(1))
            await asyncio.to_thread(platform.store.health)
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ready", "checks": ["database", "private-artifact-store"], "scope": "Control-plane dependencies only"}

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics():
        # Unauthenticated for in-cluster scraping, so it carries numbers only: no identifiers,
        # generations or evidence. Keep it off public routes.
        bundle, generation = platform.active()
        with platform.db.sessions() as session:
            qualified = is_qualified(session, settings, generation, bundle.get("default_profile", "local"))
            row = current_qualification(session, settings, generation, bundle.get("default_profile", "local"))
            healthy = session.scalar(select(func.count()).select_from(WorkerHeartbeat).where(WorkerHeartbeat.updated_at > time.time() - 120)) or 0
        lines = [
            "# HELP sovite_qualification_required Whether dispatch requires a trusted qualification.", "# TYPE sovite_qualification_required gauge", f"sovite_qualification_required {int(settings.require_qualification)}",
            "# HELP sovite_qualified Whether the active generation may dispatch under its qualification rules.", "# TYPE sovite_qualified gauge", f"sovite_qualified {int(qualified)}",
            "# HELP sovite_qualification_expiry_timestamp_seconds Expiry of the latest trusted qualification for the active generation; 0 when there is none.", "# TYPE sovite_qualification_expiry_timestamp_seconds gauge", f"sovite_qualification_expiry_timestamp_seconds {int(row.expires_at) if row else 0}",
            "# HELP sovite_workers_healthy Workers with a heartbeat in the last 120 seconds.", "# TYPE sovite_workers_healthy gauge", f"sovite_workers_healthy {healthy}",
        ]
        return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    @app.post("/api/auth/session")
    async def login(body: Login, response: Response):
        found = token_identity(body.token, settings)
        if not found or found[1] != "operator":
            raise PlatformError("unauthorized", "Operator authentication failed", 401)
        cookie = secrets.token_urlsafe(32)
        with platform.db.sessions.begin() as session:
            session.add(AuthSession(digest=hashlib.sha256(cookie.encode()).hexdigest(), identity=found[0], role=found[1], expires_at=time.time() + settings.session_ttl_seconds))
            platform.db.audit(session, found[0], "session.created", "operator-console")
        response.set_cookie("sovite_session", cookie, max_age=settings.session_ttl_seconds, secure=settings.secure_cookies, httponly=True, samesite="strict", path="/")
        return {"identity": found[0], "role": found[1]}

    @app.get("/api/auth/session")
    async def whoami(auth=Depends(operator)):
        return {"identity": auth[0], "role": auth[1]}

    @app.delete("/api/auth/session")
    async def logout(request: Request, response: Response, auth=Depends(operator)):
        cookie = request.cookies.get("sovite_session")
        if cookie:
            with platform.db.sessions.begin() as session:
                row = session.get(AuthSession, hashlib.sha256(cookie.encode()).hexdigest())
                if row:
                    session.delete(row)
                platform.db.audit(session, auth[0], "session.ended", "operator-console")
        response.delete_cookie("sovite_session", path="/", secure=settings.secure_cookies, httponly=True, samesite="strict")
        return {"status": "signed_out"}

    @app.get("/api/openapi.json")
    async def openapi(auth=Depends(operator)):
        return app.openapi()

    @app.get("/api/overview")
    async def overview(auth=Depends(operator)):
        bundle, generation = platform.active()
        with platform.db.sessions() as session:
            counts = {key: len(bundle.get(key, [])) for key in ("agents", "models", "harnesses", "grants")}
            counts["jobs"] = session.scalar(select(func.count()).select_from(Job)) or 0
            for state in ("running", "queued", "completed", "blocked", "failed", "retry_wait"):
                counts[state] = session.scalar(select(func.count()).select_from(Job).where(Job.status == state)) or 0
            workers = [{"id": row.id, "updated_at": row.updated_at, "job_id": row.job_id, "healthy": row.updated_at > time.time() - 120} for row in session.scalars(select(WorkerHeartbeat).order_by(WorkerHeartbeat.updated_at.desc()).limit(100))]
            qualified = is_qualified(session, settings, generation, bundle.get("default_profile", "local"))
        return {"name": bundle.get("metadata", {}).get("display_name", settings.display_name), "mode": bundle.get("mode"), "generation": generation, "counts": counts, "workers": workers, "qualified": qualified, "qualification_required": settings.require_qualification, "audit": platform.db.verify_audit(), "storage": {"backend": settings.artifact_backend}, "security": {"public_origin": settings.public_origin, "secure_cookies": settings.secure_cookies}, "capabilities": {"receipt_only_mcp": True, "private_artifacts": True, "signed_qualification": True, "native_harness": "Configured separately; catalog presence is not readiness", "automatic_model_fallback": False, "config_updates": "Git/CLI only", "live_cluster_verification": False}}

    @app.get("/api/catalog")
    async def catalog(auth=Depends(operator)):
        bundle, generation = platform.active()
        return {**{key: bundle.get(key, []) for key in ("models", "harnesses", "agents", "tools", "compute", "destinations", "data_classes", "profiles")}, "generation": generation}

    @app.get("/api/flows")
    async def flows(auth=Depends(operator)):
        from .policy import flow_projection
        return flow_projection(platform.active()[0])

    @app.get("/api/grants")
    async def grants(auth=Depends(operator)):
        with platform.db.sessions() as session:
            bundle, generation = platform.active(session)
            qualified = is_qualified(session, settings, generation, bundle.get("default_profile", "local"))
            rows = []
            for grant in bundle.get("grants", []):
                runtime = session.get(GrantState, grant["id"])
                rows.append({**grant, "inhibited": bool(runtime and runtime.inhibited), "reason": runtime.reason if runtime else "", "updated_at": runtime.updated_at if runtime else None, "qualified": qualified})
            return {"items": rows}

    @app.post("/api/grants/{grant_id}/inhibit")
    async def inhibit(grant_id: str, body: Reason, auth=Depends(operator)):
        return platform.inhibit(grant_id, body.reason)

    @app.post("/api/grants/{grant_id}/resume")
    async def resume(grant_id: str, body: Reason, auth=Depends(operator)):
        return platform.inhibit(grant_id, body.reason, False)

    @app.get("/api/jobs")
    async def jobs(limit: int = 100, auth=Depends(operator)):
        with platform.db.sessions() as session:
            return {"items": [job_dict(row) for row in session.scalars(select(Job).order_by(Job.created_at.desc()).limit(max(1, min(limit, 500))))]}

    @app.post("/api/jobs", status_code=201)
    async def new_job(body: NewJob, auth=Depends(operator)):
        return job_dict(platform.submit(body.grant_id, body.task, auth[0], body.idempotency_key or secrets.token_urlsafe(24), body.input_class, is_operator=True), detail=True)

    @app.get("/api/jobs/{job_id}")
    async def job(job_id: str, auth=Depends(operator)):
        return job_dict(platform.get_job(job_id), detail=True)

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel(job_id: str, auth=Depends(operator)):
        return job_dict(platform.cancel(job_id), detail=True)

    @app.get("/api/jobs/{job_id}/artifact")
    async def artifact(job_id: str, auth=Depends(operator)):
        row = platform.get_job(job_id)
        if not row.artifact_key:
            raise PlatformError("artifact_unavailable", "A private artifact is not available", 409)
        try:
            body = await asyncio.to_thread(platform.store.get, row.artifact_key, row.artifact_sha256)
        except Exception:
            raise PlatformError("artifact_integrity_failed", "The private artifact could not be verified", 503) from None
        with platform.db.sessions.begin() as session:
            platform.db.audit(session, auth[0], "artifact.accessed", job_id, {"sha256": row.artifact_sha256})
        return Response(body, media_type="application/json", headers={"Content-Disposition": f'inline; filename="private-result-{job_id}.json"'})

    @app.get("/api/receipts")
    async def receipts(limit: int = 100, auth=Depends(operator)):
        with platform.db.sessions() as session:
            rows = session.scalars(select(Job).order_by(Job.created_at.desc()).limit(max(1, min(limit, 500)))).all()
        return {"items": [{"receipt": row.receipt, "grant_id": row.grant_id, "caller": row.caller, "status": platform.public_status(row.receipt, row.caller, audit=False)["status"], "created_at": row.created_at, "released_at": row.released_at, "job_id": row.id} for row in rows]}

    @app.get("/api/audit")
    async def audit(limit: int = 200, auth=Depends(operator)):
        with platform.db.sessions() as session:
            rows = session.scalars(select(AuditEvent).order_by(AuditEvent.sequence.desc()).limit(max(1, min(limit, 1000)))).all()
            return {"items": [{key: getattr(row, key) for key in ("sequence", "created_at", "actor", "action", "target", "detail", "previous_hash", "hash")} for row in rows], "verification": platform.db.verify_audit()}

    @app.get("/api/qualifications")
    async def qualifications(auth=Depends(operator)):
        with platform.db.sessions() as session:
            rows = session.scalars(select(Qualification).order_by(Qualification.created_at.desc()).limit(100)).all()
            return {"required": settings.require_qualification, "items": [{**{key: getattr(row, key) for key in ("id", "generation", "profile", "created_at", "expires_at", "passed", "issuer", "payload", "signature", "public_key")}, "valid": verify_qualification(row, settings)} for row in rows]}

    @app.get("/api/deployments")
    async def deployments(auth=Depends(operator)):
        with platform.db.sessions() as session:
            _, generation = platform.active(session)
            rows = session.scalars(select(BundleRevision).order_by(BundleRevision.created_at.desc()).limit(100)).all()
            return {"items": [{"id": row.generation, "generation": row.generation, "current": row.generation == generation, "created_at": row.created_at, "mode": row.bundle.get("mode"), "name": row.bundle.get("metadata", {}).get("display_name", ""), "components": {key: len(row.bundle.get(key, [])) for key in ("models", "harnesses", "agents", "compute", "grants")}} for row in rows]}

    @app.get("/mcp")
    async def no_mcp_stream():
        return Response(status_code=405, headers={"Allow": "POST"})

    @app.post("/mcp")
    async def mcp(request: Request, auth=Depends(identity)):
        if auth[1] != "caller" or request.headers.get("authorization", "").startswith("Bearer ") is False:
            raise PlatformError("forbidden", "A configured MCP caller identity is required", 403)
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or set(body) - {"jsonrpc", "id", "method", "params"}:
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
        request_id, method, params = body.get("id"), body.get("method"), body.get("params", {})
        def reply(result):
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        def rpc_error(code, message):
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
        if not isinstance(params, dict) or (request_id is not None and (not isinstance(request_id, (str, int)) or isinstance(request_id, bool))):
            return rpc_error(-32600, "Invalid request")
        if method == "notifications/initialized" and "id" not in body:
            return Response(status_code=202)
        if "id" not in body:
            return JSONResponse(rpc_error(-32600, "Unsupported notification"), status_code=400)
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in {"2025-03-26", "2025-06-18", "2025-11-25"} else "2025-11-25"
            return reply({"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": settings.display_name + " receipt gateway", "version": "0.1.11"}, "instructions": "This service accepts private work and returns opaque receipts and fixed status only. Full results require a separately authorized private operator interface."})
        if method == "ping":
            return reply({})
        if method == "tools/list":
            return reply({"tools": [
                {"name": "begin_task", "description": "Begin a task under a configured flow grant; return an opaque receipt only.", "inputSchema": {"type": "object", "properties": {"grant_id": {"type": "string"}, "task": {"type": "string"}, "idempotency_key": {"type": "string"}, "input_class": {"type": "string"}}, "required": ["grant_id", "task", "idempotency_key"], "additionalProperties": False}, "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}},
                {"name": "task_status", "description": "Read fixed receipt status. This never returns task output or a result URL.", "inputSchema": {"type": "object", "properties": {"receipt": {"type": "string"}}, "required": ["receipt"], "additionalProperties": False}, "annotations": {"readOnlyHint": True, "destructiveHint": False}},
            ]})
        if method != "tools/call":
            return rpc_error(-32601, "Method not supported")
        if set(params) - {"name", "arguments", "_meta"}:
            return rpc_error(-32602, "Invalid parameters")
        name, arguments = params.get("name"), params.get("arguments", {})
        if not isinstance(arguments, dict):
            return rpc_error(-32602, "Invalid parameters")
        try:
            if name == "begin_task":
                if set(arguments) - {"grant_id", "task", "idempotency_key", "input_class"} or not all(isinstance(arguments.get(field), str) for field in ("grant_id", "task", "idempotency_key")) or ("input_class" in arguments and not isinstance(arguments["input_class"], str)):
                    return rpc_error(-32602, "Invalid parameters")
                row = platform.submit(arguments["grant_id"], arguments["task"], auth[0], arguments["idempotency_key"], arguments.get("input_class"))
                result = platform.public_status(row.receipt, auth[0])
            elif name == "task_status":
                if set(arguments) != {"receipt"} or not isinstance(arguments["receipt"], str):
                    return rpc_error(-32602, "Invalid parameters")
                result = platform.public_status(arguments["receipt"], auth[0])
            else:
                return rpc_error(-32602, "Tool not supported")
        except PlatformError:
            return reply({"content": [{"type": "text", "text": "Task unavailable or not permitted."}], "isError": True})
        return reply({"content": [{"type": "text", "text": json.dumps(result, separators=(",", ":"))}], "structuredContent": result, "isError": False})

    static = Path(__file__).parent / "static"
    if static.exists():
        app.mount("/", StaticFiles(directory=static, html=True), name="console")
    return app


# Use `uvicorn sovite.api:create_app --factory`; keeping import side-effect free
# allows administrative CLI commands without starting or mutating the server.
