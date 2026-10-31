from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from .db import AuditEvent, AuthSession, BundleRevision, GrantState, Job, Qualification, WorkerHeartbeat
from .qualification import current_qualification, is_qualified, verify_qualification
from .repository_runs import RepositoryRunError
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
    expected_generation: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")
    previous_attempt: str | None = Field(default=None, min_length=1, max_length=256)
    selection_reason: str | None = Field(default=None, min_length=1, max_length=500)


class Reason(StrictBody):
    reason: str = Field(min_length=1, max_length=500)


class BundleDraft(StrictBody):
    bundle: dict


class WorkflowDraft(StrictBody):
    workflow: dict


class WorkflowBind(WorkflowDraft):
    requirements_ref: dict
    session_policy_ref: dict
    expected_generation: str = Field(pattern=r"^sha256:[a-f0-9]{64}$", min_length=71, max_length=71)
    input_class: str | None = Field(default=None, min_length=1, max_length=96)


class WorkflowJob(StrictBody):
    binding: dict
    role: str = Field(min_length=1, max_length=96)
    task: str = Field(default="", max_length=65536)
    idempotency_key: str = Field(min_length=1, max_length=160)
    source_release: str | None = Field(default=None, min_length=1, max_length=80)
    transfer_id: str | None = Field(default=None, min_length=1, max_length=96)
    previous_attempt: str | None = Field(default=None, min_length=1, max_length=256)
    selection_reason: str | None = Field(default=None, min_length=1, max_length=500)


class RepositoryRunSubmission(StrictBody):
    schema_version: str = Field(pattern=r"^sovite-repository-run/v1$")
    preview_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    packet: dict


class CandidateRetentionRequest(StrictBody):
    workspace_id: str = Field(min_length=1, max_length=128)
    attempt_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    release_id: str = Field(pattern=r"^release_[0-9a-f]{64}$")


class OutputApproval(StrictBody):
    source_job_id: str = Field(min_length=1, max_length=64)
    source_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$", min_length=71, max_length=71)
    contract_id: str = Field(min_length=1, max_length=96)
    expected_contract_revision: str = Field(pattern=r"^sha256:[a-f0-9]{64}$", min_length=71, max_length=71)
    expected_generation: str = Field(pattern=r"^sha256:[a-f0-9]{64}$", min_length=71, max_length=71)
    content: str = Field(min_length=1, max_length=65536)
    evidence_refs: list[dict] = Field(min_length=1, max_length=100)


def token_identity(token: str, settings: Settings) -> tuple[str, str] | None:
    raw = token.encode()
    if settings.operator_token and secrets.compare_digest(raw, settings.operator_token.encode()):
        return "operator", "operator"
    for role in ("author", "activator"):
        expected = getattr(settings, role + "_token")
        if expected and secrets.compare_digest(raw, expected.encode()):
            return role, role
    for name, expected in settings.caller_tokens.items():
        if secrets.compare_digest(raw, expected.encode()):
            return name, "caller"
    return None


def create_app(settings: Settings | None = None, platform: Platform | None = None,
               repository_run_owner=None, candidate_publication_service=None) -> FastAPI:
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

    app = FastAPI(title=f"{settings.display_name} Platform", version="0.1.22", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.platform = platform
    app.state.repository_run_owner = repository_run_owner
    from .authority_api import install_routes
    install_routes(app, platform, settings)
    from .execution_api import install_routes as install_execution_routes
    install_execution_routes(app, platform, settings)
    from .resource_owner_api import install_routes as install_resource_owner_routes
    install_resource_owner_routes(app, platform, settings)

    @app.middleware("http")
    async def bounds_and_headers(request: Request, call_next):
        execution_route = request.url.path.startswith("/internal/execution/v1/")
        if request.url.path == "/mcp" and request.headers.get("origin") and request.headers["origin"].rstrip("/") != settings.public_origin:
            return JSONResponse({"detail": {"code": "origin_denied", "message": "Request origin is not permitted"}}, status_code=403)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != settings.public_origin:
                code = "execution_scope_denied" if execution_route else "origin_denied"
                return JSONResponse({"detail": {"code": code, "message": "Request origin is not permitted"}}, status_code=403)
            limit = 65_536 if execution_route else settings.max_request_bytes + 4096
            length = request.headers.get("content-length")
            try:
                if length is not None and int(length) > limit:
                    code = "execution_payload_too_large" if execution_route else "request_too_large"
                    return JSONResponse({"detail": {"code": code, "message": "Request exceeds the configured limit"}}, status_code=413)
            except ValueError:
                status_code = 422 if execution_route else 400
                return JSONResponse({"detail": {"code": "invalid_request", "message": "Invalid request"}}, status_code=status_code)
            data = bytearray()
            async for part in request.stream():
                data.extend(part)
                if len(data) > limit:
                    code = "execution_payload_too_large" if execution_route else "request_too_large"
                    return JSONResponse({"detail": {"code": code, "message": "Request exceeds the configured limit"}}, status_code=413)
            request._body = bytes(data)
            if (request.url.path.startswith("/api/workflows/")
                or request.url.path == "/api/output-releases"
                or request.url.path.startswith("/internal/execution/")):
                from .hosting import bounded_json
                try:
                    bounded_json(request._body, limit)
                except ValueError:
                    return JSONResponse({"detail": {"code": "invalid_request", "message": "The request must contain finite JSON with unique keys"}}, status_code=422)
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
        execution_route = request.url.path.startswith("/internal/execution/v1/")
        detail = {"code": exc.code, "message": exc.message}
        if exc.receipt is not None and not execution_route:
            detail["receipt"] = exc.receipt
        status_code = exc.status
        if execution_route:
            try:
                from .execution_contract.runtime import OPERATIONS, load_bundle
                operation_name = request.url.path.rsplit("/", 1)[-1]
                operation = OPERATIONS[operation_name]
                contract = load_bundle()
                contract.validate_response(operation, status_code, {"detail": detail})
                cap = contract.manifest["operations"][operation]["responseMaxBytes"][str(status_code)]
                if len(json.dumps({"detail": detail}, ensure_ascii=False,
                                  allow_nan=False, separators=(",", ":")).encode("utf-8")) > cap:
                    raise ValueError
            except Exception:
                status_code = 503
                detail = {"code": "owner_unavailable", "message": "Execution owner state is unavailable"}
        return JSONResponse({"detail": detail}, status_code=status_code)

    @app.exception_handler(RepositoryRunError)
    async def repository_run_error(request: Request, exc: RepositoryRunError):
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
                        enabled = (bool(settings.caller_tokens.get(row.identity)) if row.role == "caller"
                                   else bool(getattr(settings, row.role + "_token", ""))
                                   if row.role in {"operator", "author", "activator"} else False)
                        if not enabled:
                            raise PlatformError("unauthorized", "Authentication is required", 401)
                        if request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("origin", "").rstrip("/") != settings.public_origin:
                            raise PlatformError("origin_required", "Cookie-authenticated changes require the configured origin", 403)
                        return row.identity, row.role
        raise PlatformError("unauthorized", "Authentication is required", 401)

    async def operator(auth=Depends(identity)) -> tuple[str, str]:
        if auth[1] != "operator":
            raise PlatformError("forbidden", "Operator access is required", 403)
        return auth

    async def console_identity(auth=Depends(identity)) -> tuple[str, str]:
        if auth[1] not in {"operator", "author", "activator"}:
            raise PlatformError("forbidden", "Console access is required", 403)
        return auth

    async def author(auth=Depends(identity)) -> tuple[str, str]:
        platform.policy_role(auth[1], "author", api=True)
        return auth

    async def workflow_author(auth=Depends(identity)) -> tuple[str, str]:
        if auth[1] != "author" and not (auth[1] == "operator" and not settings.hosting_envelope_path):
            raise PlatformError("forbidden", "Workflow author access is required", 403)
        return auth

    def repository_owner():
        if app.state.repository_run_owner is None:
            raise RepositoryRunError("executor_unavailable", "No qualified mutable repository executor is installed", 503)
        return app.state.repository_run_owner

    async def repository_caller(auth=Depends(identity)) -> tuple[str, str]:
        if auth[1] != "caller":
            raise PlatformError("forbidden", "A configured repository caller is required", 403)
        return auth

    @app.get("/api/repository-runs/v1/capabilities")
    async def repository_run_capabilities(workspace_id: str, auth=Depends(repository_caller)):
        owner = repository_owner()
        return owner.capabilities(workspace_id, auth[0])

    @app.post("/api/repository-runs/v1/runs", status_code=201)
    async def submit_repository_run(body: RepositoryRunSubmission,
                                    idempotency_key: str = Header(alias="Idempotency-Key", min_length=16, max_length=128,
                                                                   pattern=r"^[A-Za-z0-9_.-]+$"),
                                    auth=Depends(repository_caller)):
        owner = repository_owner()
        return {"run": owner.submit(body.packet, preview_id=body.preview_id,
                                    principal_id=auth[0], idempotency_key=idempotency_key)}

    @app.get("/api/repository-runs/v1/runs")
    async def list_repository_runs(workspace_id: str, limit: int = 100,
                                   auth=Depends(repository_caller)):
        owner = repository_owner()
        return {"runs": owner.list(principal_id=auth[0], workspace_id=workspace_id, limit=limit),
                "availability": "available", "blockers": []}

    @app.get("/api/repository-runs/v1/runs/{run_id}")
    async def get_repository_run(run_id: str, workspace_id: str,
                                 auth=Depends(repository_caller)):
        owner = repository_owner()
        return {"run": owner.get(run_id, principal_id=auth[0], workspace_id=workspace_id)}

    @app.post("/api/repository-runs/v1/runs/{run_id}/cancel")
    async def cancel_repository_run(run_id: str, workspace_id: str,
                                    auth=Depends(repository_caller)):
        owner = repository_owner()
        return {"run": owner.cancel(run_id, principal_id=auth[0], workspace_id=workspace_id)}

    @app.post("/api/repository-runs/v1/runs/{run_id}/candidate-retention")
    async def retain_repository_candidate(run_id: str, body: CandidateRetentionRequest,
                                          auth=Depends(repository_caller)):
        owner = repository_owner()
        owner.get(run_id, principal_id=auth[0], workspace_id=body.workspace_id)
        service = candidate_publication_service
        if service is None or service.exporter.platform is not platform or service.exporter.execution is not owner.executor:
            raise RepositoryRunError("candidate_retention_unavailable", "Retained candidate publication is not installed", 503)
        from .repository_candidate_retention import CandidateRetentionError
        try:
            return {"receipt": await service.retain(run_id, body.attempt_id, body.release_id,
                                                    principal_id=auth[0], workspace_id=body.workspace_id)}
        except Exception:
            raise RepositoryRunError("candidate_retention_unavailable", "Retained candidate publication could not be reconciled", 409) from None

    async def activator(auth=Depends(identity)) -> tuple[str, str]:
        platform.policy_role(auth[1], "activator", api=True)
        return auth

    async def compile_draft(body: BundleDraft, auth=Depends(author)):
        try:
            return {"generation": platform.compile(body.bundle, auth[1]), "status": "compiled"}
        except ValueError:
            raise PlatformError("invalid_bundle", "The bundle exceeds the current policy or hosting envelope", 422) from None

    async def activate_bundle(generation: str, auth=Depends(activator)):
        try:
            return {"generation": platform.activate(generation, auth[1]), "status": "activated"}
        except ValueError:
            raise PlatformError("invalid_bundle", "The bundle exceeds the current policy or hosting envelope", 422) from None

    if settings.hosting_envelope_path:
        app.post("/api/bundles", status_code=201)(compile_draft)
        app.post("/api/bundles/{generation}/activate")(activate_bundle)

    from .setup_api import install_routes as install_setup_routes
    install_setup_routes(app, platform, operator)
    from .helper_admin_api import install_routes as install_helper_admin_routes
    install_helper_admin_routes(app, platform, operator)

    @app.post("/api/workflows/validate")
    async def validate_workflow(body: WorkflowDraft, auth=Depends(workflow_author)):
        from .workflows import validate_workflow as validate, workflow_revision, WorkflowError
        try:
            document = validate(body.workflow)
            return {"valid": True, "workflow_revision": workflow_revision(document), "mode": document["mode"]}
        except WorkflowError:
            raise PlatformError("invalid_workflow", "The workflow does not match the delivery contract", 422) from None

    @app.post("/api/workflows/bind")
    async def bind_workflow(body: WorkflowBind, auth=Depends(workflow_author)):
        try:
            return platform.bind_workflow(body.workflow, body.requirements_ref, body.session_policy_ref,
                body.expected_generation, input_class=body.input_class, actor=auth[0])
        except ValueError:
            raise PlatformError("invalid_workflow", "The workflow cannot bind to the current installation", 422) from None

    @app.post("/api/workflows/jobs", status_code=201)
    async def workflow_job(body: WorkflowJob, auth=Depends(identity)):
        from .results import result_status
        if auth[1] not in {"caller", "operator"}:
            raise PlatformError("forbidden", "A configured caller or operator is required", 403)
        row = platform.submit_workflow_role(body.binding, body.role, body.task, auth[0], body.idempotency_key,
            is_operator=auth[1] == "operator", source_release=body.source_release, transfer_id=body.transfer_id,
            previous_attempt=body.previous_attempt, selection_reason=body.selection_reason)
        return job_dict(row, detail=True) if auth[1] == "operator" else result_status(platform, row.receipt, auth[0])

    @app.post("/api/output-releases", status_code=201)
    async def approve_output(body: OutputApproval, auth=Depends(identity)):
        from .output_contracts import approve_output as approve
        if auth[1] not in {"caller", "operator"}:
            raise PlatformError("forbidden", "A configured caller or operator is required", 403)
        try:
            return approve(platform, **body.model_dump(), actor=auth[0], is_operator=auth[1] == "operator")
        except ValueError:
            raise PlatformError("invalid_output", "The approval does not match the output contract", 422) from None

    @app.get("/api/output-releases/{release_id}")
    async def released_output(release_id: str, auth=Depends(identity)):
        from .output_contracts import read_output
        if auth[1] not in {"caller", "operator"}:
            raise PlatformError("forbidden", "A configured caller or operator is required", 403)
        return read_output(platform, release_id, auth[0], is_operator=auth[1] == "operator")

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
        if not found or found[1] not in {"operator", "author", "activator"}:
            raise PlatformError("unauthorized", "Operator authentication failed", 401)
        cookie = secrets.token_urlsafe(32)
        with platform.db.sessions.begin() as session:
            session.add(AuthSession(digest=hashlib.sha256(cookie.encode()).hexdigest(), identity=found[0], role=found[1], expires_at=time.time() + settings.session_ttl_seconds))
            platform.db.audit(session, found[0], "session.created", "operator-console")
        response.set_cookie("sovite_session", cookie, max_age=settings.session_ttl_seconds, secure=settings.secure_cookies, httponly=True, samesite="strict", path="/")
        return {"identity": found[0], "role": found[1]}

    @app.get("/api/auth/session")
    async def whoami(auth=Depends(console_identity)):
        return {"identity": auth[0], "role": auth[1]}

    @app.delete("/api/auth/session")
    async def logout(request: Request, response: Response, auth=Depends(console_identity)):
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
        return {"name": bundle.get("metadata", {}).get("display_name", settings.display_name), "mode": bundle.get("mode"), "generation": generation, "counts": counts, "workers": workers, "qualified": qualified, "qualification_required": settings.require_qualification, "audit": platform.db.verify_audit(), "storage": {"backend": settings.artifact_backend}, "security": {"public_origin": settings.public_origin, "secure_cookies": settings.secure_cookies}, "capabilities": {"receipt_only_mcp": False, "class_aware_results": True, "scoped_result_reader": True, "private_artifacts": True, "signed_qualification": True, "native_harness": "Configured separately; catalog presence is not readiness", "automatic_model_fallback": False, "config_updates": "Git/CLI; bounded setup proposals enabled" if settings.setup_api_enabled else "Git/CLI only", "live_cluster_verification": False}}

    @app.get("/api/catalog")
    async def catalog(auth=Depends(operator)):
        bundle, generation = platform.active()
        return {**{key: bundle.get(key, []) for key in ("models", "harnesses", "agents", "tools", "compute", "destinations", "data_classes", "profiles")}, "generation": generation}

    @app.get("/api/flows")
    async def flows(auth=Depends(operator)):
        from .policy import flow_projection
        bundle, generation = platform.active()
        projection = flow_projection(bundle)
        if settings.hosting_envelope_path:
            projection["generation"] = generation
        return projection

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

    @app.post("/api/bindings/{binding_id}/inhibit")
    async def inhibit_binding(binding_id: str, body: Reason, auth=Depends(operator)):
        return platform.inhibit_binding(binding_id, body.reason)

    @app.post("/api/bindings/{binding_id}/resume")
    async def resume_binding(binding_id: str, body: Reason, auth=Depends(operator)):
        return platform.inhibit_binding(binding_id, body.reason, False)

    @app.get("/api/jobs")
    async def jobs(limit: int = 100, auth=Depends(operator)):
        with platform.db.sessions() as session:
            return {"items": [job_dict(row) for row in session.scalars(select(Job).order_by(Job.created_at.desc()).limit(max(1, min(limit, 500))))]}

    @app.post("/api/jobs", status_code=201)
    async def new_job(body: NewJob, auth=Depends(operator)):
        return job_dict(platform.submit(body.grant_id, body.task, auth[0], body.idempotency_key or secrets.token_urlsafe(24), body.input_class, is_operator=True,
            expected_generation=body.expected_generation, previous_attempt=body.previous_attempt, selection_reason=body.selection_reason), detail=True)

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
        valid_id = request_id is None or (
            isinstance(request_id, (str, int)) and not isinstance(request_id, bool))
        if isinstance(request_id, str):
            try:
                request_id.encode("utf-8")
            except UnicodeError:
                valid_id = False
        if not valid_id:
            request_id = None
            return rpc_error(-32600, "Invalid request")
        if not isinstance(params, dict):
            return rpc_error(-32600, "Invalid request")
        if method == "notifications/initialized" and "id" not in body:
            return Response(status_code=202)
        if "id" not in body:
            return JSONResponse(rpc_error(-32600, "Unsupported notification"), status_code=400)
        if method == "initialize":
            requested = params.get("protocolVersion")
            if "protocolVersion" in params and not isinstance(requested, str):
                return rpc_error(-32602, "Invalid parameters")
            version = requested if requested in {"2025-03-26", "2025-06-18", "2025-11-25"} else "2025-11-25"
            return reply({"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": settings.display_name + " task gateway", "version": "0.1.22"}, "instructions": "This service accepts private and public work under installed class and boundary policy. Class-aware grants return content to cleared callers; otherwise results require an explicitly scoped reader. Reference grants preserve receipt-only delivery."})
        if method == "ping":
            return reply({})
        if method == "tools/list":
            return reply({"tools": [
                {"name": "list_candidates", "description": "List only your installed execution candidates, declared capabilities and labelled policy observations. Capacity and interactive authentication are not verified; actual admission rechecks current authority. Preferences remain with the caller.", "inputSchema": {"type": "object", "properties": {"input_class": {"type": "string", "minLength": 1, "maxLength": 96}}, "additionalProperties": False}, "annotations": {"readOnlyHint": True, "destructiveHint": False}},
                {"name": "begin_task", "description": "Begin a task under a configured grant. Results follow its output class and return contract; omitted input_class selects the installation's most restrictive class.", "inputSchema": {"type": "object", "properties": {"grant_id": {"type": "string"}, "task": {"type": "string"}, "idempotency_key": {"type": "string"}, "input_class": {"type": "string"}, "expected_generation": {"type": "string", "pattern": "^sha256:[a-f0-9]{64}$"}, "previous_attempt": {"type": "string", "minLength": 1, "maxLength": 256}, "selection_reason": {"type": "string", "minLength": 1, "maxLength": 500}}, "required": ["grant_id", "task", "idempotency_key"], "additionalProperties": False}, "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}},
                {"name": "task_status", "description": "Read receipt status and verified content when the installed class-aware grant clears this caller.", "inputSchema": {"type": "object", "properties": {"receipt": {"type": "string"}}, "required": ["receipt"], "additionalProperties": False}, "annotations": {"readOnlyHint": True, "destructiveHint": False}},
                {"name": "read_result", "description": "Read a private result using the grant's explicit reader scope; receipt possession alone grants no access.", "inputSchema": {"type": "object", "properties": {"receipt": {"type": "string", "minLength": 1, "maxLength": 256}}, "required": ["receipt"], "additionalProperties": False}, "annotations": {"readOnlyHint": True, "destructiveHint": False}},
            ]})
        if method != "tools/call":
            return rpc_error(-32601, "Method not supported")
        if set(params) - {"name", "arguments", "_meta"}:
            return rpc_error(-32602, "Invalid parameters")
        name, arguments = params.get("name"), params.get("arguments", {})
        if not isinstance(arguments, dict):
            return rpc_error(-32602, "Invalid parameters")
        try:
            if name == "list_candidates":
                if set(arguments) - {"input_class"} or ("input_class" in arguments and (not isinstance(arguments["input_class"], str) or not 1 <= len(arguments["input_class"]) <= 96)):
                    return rpc_error(-32602, "Invalid parameters")
                from .discovery import list_candidates
                result = list_candidates(platform, auth[0], arguments.get("input_class"))
            elif name == "begin_task":
                if set(arguments) - {"grant_id", "task", "idempotency_key", "input_class", "expected_generation", "previous_attempt", "selection_reason"} or not all(isinstance(arguments.get(field), str) for field in ("grant_id", "task", "idempotency_key")) or any(field in arguments and not isinstance(arguments[field], str) for field in ("input_class", "expected_generation", "previous_attempt", "selection_reason")):
                    return rpc_error(-32602, "Invalid parameters")
                row = platform.submit(arguments["grant_id"], arguments["task"], auth[0], arguments["idempotency_key"], arguments.get("input_class"),
                    expected_generation=arguments.get("expected_generation"), previous_attempt=arguments.get("previous_attempt"),
                    selection_reason=arguments.get("selection_reason"))
                from .results import result_status
                result = result_status(platform, row.receipt, auth[0])
            elif name == "task_status":
                if set(arguments) != {"receipt"} or not isinstance(arguments["receipt"], str):
                    return rpc_error(-32602, "Invalid parameters")
                from .results import result_status
                result = result_status(platform, arguments["receipt"], auth[0])
            elif name == "read_result":
                if set(arguments) != {"receipt"} or not isinstance(arguments["receipt"], str) or not 1 <= len(arguments["receipt"]) <= 256:
                    return rpc_error(-32602, "Invalid parameters")
                from .results import scoped_read
                result = scoped_read(platform, arguments["receipt"], auth[0])
            else:
                return rpc_error(-32602, "Tool not supported")
        except PlatformError as exc:
            if exc.code in {"previous_attempt_unfinished", "previous_effect_uncertain", "idempotency_conflict"}:
                error = {"code": exc.code}
                if exc.receipt is not None:
                    error["receipt"] = exc.receipt
                return reply({"content": [{"type": "text", "text": exc.message}],
                              "structuredContent": {"error": error}, "isError": True})
            return reply({"content": [{"type": "text", "text": "Task unavailable or not permitted."}], "isError": True})
        return reply({"content": [{"type": "text", "text": json.dumps(result, separators=(",", ":"))}], "structuredContent": result, "isError": False})

    # Keep unknown API paths out of the static console mount, which otherwise
    # responds to POST with 405 even when no API route exists.
    @app.api_route("/api/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], include_in_schema=False)
    async def api_not_found(path: str):
        raise HTTPException(status_code=404)

    static = Path(__file__).parent / "static"
    if static.exists():
        app.mount("/", StaticFiles(directory=static, html=True), name="console")
    return app


# Use `uvicorn sovite.api:create_app --factory`; keeping import side-effect free
# allows administrative CLI commands without starting or mutating the server.
