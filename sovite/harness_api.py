"""Separately deployable fixed native harness: no platform DB or object-store access."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .adapters import AdapterRunner, RuntimeFailure
from .db import canonical
from .hosting import compile_bundle, load_envelope
from .policy import load_bundle, native_harness_kind, resolve_recipe
from .runtime_boundary import CONTROL_OR_STORE_ENV


def create_app() -> FastAPI:
    forbidden = [name for name in (*CONTROL_OR_STORE_ENV, "SOVITE_EXECUTION_TOKEN")
                 if os.environ.get(name)]
    if forbidden:
        # Check before loading configuration, creating a ledger, or starting a
        # runner. Values never appear in this fixed diagnostic.
        raise ValueError("Native harness requires an isolated environment; remove control or artifact authority: "
                         + ", ".join(forbidden))
    bundle, _ = load_bundle(os.environ.get("SOVITE_BUNDLE_PATH", "config/platform.ollama.yaml"))
    bundle, generation = compile_bundle(bundle, load_envelope(os.environ.get("SOVITE_HOSTING_ENVELOPE") or None))
    agent_id = os.environ.get("SOVITE_HARNESS_AGENT_ID", "code-review")
    token = os.environ.get("SOVITE_HARNESS_TOKEN", "")
    if len(token) < 24:
        raise ValueError("SOVITE_HARNESS_TOKEN must contain at least 24 characters")
    grants = [grant for grant in bundle["grants"] if grant["agent"] == agent_id]
    if not grants:
        raise ValueError("Configured native harness agent has no grant")
    recipe = resolve_recipe(bundle, grants[0]["id"])
    recipe["generation"] = generation
    native_kind = native_harness_kind(recipe["harness"])
    if native_kind is None:
        raise ValueError("Native service requires exactly one configured native harness capability")
    if recipe["tools"]:
        raise ValueError("Native service does not translate HTTP tool bindings into native tools")
    if native_kind == "native-codex" and recipe["model"]["adapter"] != "codex-subscription" and recipe["model"].get("wire_api") != "responses":
        raise ValueError("Native service requires an explicit Responses provider")
    state_root = Path(os.environ.get("SOVITE_HARNESS_STATE_PATH", ".state/native-harness"))
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    ledger_path = state_root / "tasks.sqlite3"

    def connect():
        conn = sqlite3.connect(ledger_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    with connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, digest TEXT NOT NULL, state TEXT NOT NULL, result TEXT, code TEXT, created_at REAL NOT NULL)")
        conn.execute("UPDATE tasks SET state='uncertain',code='execution_uncertain' WHERE state='running'")
    runner, semaphore = AdapterRunner(), asyncio.Semaphore(1)
    active_tasks: dict[str, asyncio.Task] = {}
    app = FastAPI(title="Isolated native harness", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready():
        executable = recipe["harness"].get("executable", {"native-codex": "codex", "native-claude": "claude", "native-opencode": "opencode"}[native_kind])
        if not shutil.which(executable):
            return JSONResponse({"status": "unavailable", "code": "native_runtime_unavailable"}, status_code=503)
        return {"status": "ready", "scope": "Native binary and fixed configuration only; provider compatibility requires a real request"}

    @app.post("/run")
    async def run(request: Request):
        supplied = request.headers.get("authorization", "")
        if not supplied.startswith("Bearer ") or not secrets.compare_digest(supplied[7:].encode(), token.encode()):
            return JSONResponse({"code": "unauthorized"}, status_code=401)
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 70000:
                return JSONResponse({"code": "request_too_large"}, status_code=413)
        try:
            body = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"code": "invalid_request"}, status_code=400)
        if not isinstance(body, dict) or set(body) != {"task_id", "agent_id", "configuration_revision", "input"} or body.get("agent_id") != agent_id or body.get("configuration_revision") != generation:
            return JSONResponse({"code": "binding_mismatch"}, status_code=409)
        task_id, task_input = body["task_id"], body["input"]
        if not isinstance(task_id, str) or not 1 <= len(task_id) <= 64 or request.headers.get("idempotency-key") != task_id or not isinstance(task_input, dict) or set(task_input) != {"task"} or not isinstance(task_input["task"], str):
            return JSONResponse({"code": "invalid_request"}, status_code=422)
        body_digest = hashlib.sha256(canonical(body)).hexdigest()
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if existing:
                if existing["digest"] != body_digest:
                    return JSONResponse({"code": "idempotency_conflict"}, status_code=409)
                if existing["state"] == "completed":
                    return JSONResponse(json.loads(existing["result"]), headers={"Cache-Control": "no-store"})
                return JSONResponse({"code": existing["code"] or "execution_in_progress"}, status_code=409)
            conn.execute("INSERT INTO tasks(id,digest,state,created_at) VALUES(?,?,'running',?)", (task_id, body_digest, time.time()))
        try:
            async with semaphore:
                with connect() as conn:
                    queued = conn.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
                    if queued["state"] == "cancelled":
                        return JSONResponse({"code": "cancelled"}, status_code=409)
                operation = asyncio.create_task(runner.run_native(recipe, task_input["task"], task_id, isolated=True))
                active_tasks[task_id] = operation
                result = await asyncio.wait_for(operation, timeout=int(recipe["grant"].get("timeout_seconds", 300)))
            with connect() as conn:
                conn.execute("UPDATE tasks SET state='completed',result=? WHERE id=?", (json.dumps(result), task_id))
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except (RuntimeFailure, asyncio.TimeoutError) as exc:
            code = exc.code if isinstance(exc, RuntimeFailure) else "execution_timeout"
            with connect() as conn:
                conn.execute("UPDATE tasks SET state='failed',code=? WHERE id=?", (code, task_id))
            return JSONResponse({"code": code}, status_code=502)
        except asyncio.CancelledError:
            with connect() as conn:
                conn.execute("UPDATE tasks SET state='cancelled',code='cancelled' WHERE id=?", (task_id,))
            return JSONResponse({"code": "cancelled"}, status_code=409)
        except Exception:
            with connect() as conn:
                conn.execute("UPDATE tasks SET state='uncertain',code='execution_uncertain' WHERE id=?", (task_id,))
            return JSONResponse({"code": "execution_uncertain"}, status_code=502)
        finally:
            active_tasks.pop(task_id, None)

    @app.post("/cancel")
    async def cancel(request: Request):
        supplied = request.headers.get("authorization", "")
        if not supplied.startswith("Bearer ") or not secrets.compare_digest(supplied[7:].encode(), token.encode()):
            return JSONResponse({"code": "unauthorized"}, status_code=401)
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 4096:
                return JSONResponse({"code": "invalid_request"}, status_code=413)
        try:
            body = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"code": "invalid_request"}, status_code=400)
        if not isinstance(body, dict) or set(body) != {"task_id", "agent_id", "configuration_revision"} or body.get("agent_id") != agent_id or body.get("configuration_revision") != generation or not isinstance(body.get("task_id"), str):
            return JSONResponse({"code": "binding_mismatch"}, status_code=409)
        task_id = body["task_id"]
        operation = active_tasks.get(task_id)
        if operation:
            operation.cancel()
            return {"status": "cancellation_requested"}
        with connect() as conn:
            existing = conn.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
            if existing is None:
                return JSONResponse({"code": "not_found"}, status_code=404)
            if existing["state"] == "running":
                conn.execute("UPDATE tasks SET state='cancelled',code='cancelled' WHERE id=?", (task_id,))
                return {"status": "cancellation_requested"}
        return {"status": "not_running"}

    return app
