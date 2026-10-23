from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from sqlalchemy import select

from . import policy
from .db import Database, DatabaseSchemaError, Job, WorkerHeartbeat
from .hosting import compile_bundle, load_envelope, load_json
from .qualification import export_qualification, import_qualification, initialize_keys, issue_qualification, runtime_settings_fingerprint, write_qualification
from .service import Platform
from .settings import Settings
from .worker import Worker, healthy_worker_count


def emit(value):
    print(json.dumps(value, indent=2, ensure_ascii=False))


async def assessment(settings: Settings, output: str) -> dict:
    bundle, generation = policy.load_bundle(settings.bundle_path)
    if bundle.get("mode") != "development" or any(model["adapter"] != "demo" for model in bundle["models"]):
        raise ValueError("Built-in assessment applies only to explicit synthetic development bundles. Real deployments need separately collected deployment evidence.")
    enabled = [grant for grant in bundle["grants"] if grant.get("enabled", True) and grant["callers"]]
    if not enabled:
        raise ValueError("Assessment requires an enabled grant with a configured caller")
    grant = enabled[0]
    checks = []
    with tempfile.TemporaryDirectory(prefix="sovite-assessment-") as work:
        ephemeral = replace(settings, database_url=f"sqlite:///{work}/control.db", database_schema=None, database_migrate="startup", artifact_backend="filesystem", artifact_path=f"{work}/artifacts", require_qualification=False, compute_id=grant["compute"])
        platform = Platform(ephemeral)
        platform.initialize()
        checks.append({"name": "bundle-validation", "status": "passed", "detail": "Current bundle parses, resolves and passes declared policy constraints."})
        canary = "SYNTHETIC-PRIVATE-" + str(time.time_ns())
        job = platform.submit(grant["id"], canary, grant["callers"][0], "assessment-request")
        repeated = platform.submit(grant["id"], canary, grant["callers"][0], "assessment-request")
        assert repeated.id == job.id
        worker = Worker(platform)
        assert await worker.run_once()
        completed = platform.get_job(job.id)
        assert completed.status == "completed"
        receipt = platform.public_status(job.receipt, job.caller)
        assert set(receipt) == {"receipt", "status"} and canary not in json.dumps(receipt)
        assert canary in platform.store.get(completed.artifact_key, completed.artifact_sha256).decode()
        checks.append({"name": "receipt-boundary", "status": "passed", "detail": "Canary retained in private filesystem artifact; public projection contains only receipt and fixed status."})
        checks.append({"name": "idempotency", "status": "passed", "detail": "Repeated request returns the existing private task."})
        platform.inhibit(grant["id"], "Synthetic assessment revocation")
        assert platform.public_status(job.receipt, job.caller)["status"] == "unavailable"
        platform.inhibit(grant["id"], "Synthetic assessment re-enablement", False)
        checks.append({"name": "revocation", "status": "passed", "detail": "Completed receipt becomes unavailable under current operational inhibit."})
        recovered = platform.submit(grant["id"], "synthetic interrupted task", grant["callers"][0], "assessment-recovery")
        with platform.db.sessions.begin() as session:
            row = session.get(Job, recovered.id)
            row.status, row.lease_owner, row.lease_until, row.attempts = "running", "stopped-worker", time.time() - 10, 1
        restarted = Platform(ephemeral)
        restarted.initialize()
        assert await Worker(restarted).run_once()
        assert restarted.get_job(recovered.id).status == "completed"
        checks.append({"name": "recovery", "status": "passed", "detail": "New worker recovers expired synthetic task lease from durable SQLite state."})
        assert platform.db.verify_audit()["valid"]
        checks.append({"name": "audit-chain", "status": "passed", "detail": "Audit chain verifies after execution, revocation, and recovery."})
    evidence = {"schema": "sovite.assessment/v1", "generation": generation, "runtime_binding": runtime_settings_fingerprint(settings), "profile": bundle.get("default_profile", "local"), "evidence_kind": "synthetic-integration", "scope": "Synthetic integration on SQLite and filesystem storage with deterministic execution. Does not assess real model competence, native Codex compatibility, PostgreSQL concurrency, S3 durability, Kubernetes reconciliation, or production network isolation.", "created_at": time.time(), "checks": checks}
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(evidence, indent=2) + "\n")
    return evidence


def main():
    parser = argparse.ArgumentParser(prog="sovite", description="Operate the provisioned agent platform")
    commands = parser.add_subparsers(dest="command", required=True)
    database = commands.add_parser("db", help="Explicit database lifecycle; no bundle activation")
    database.add_argument("action", choices=["migrate", "check"])
    commands.add_parser("init", help="Initialize schema and import the first configuration revision")
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8080, type=int)
    commands.add_parser("helper", help="Run the development connector pilot with preprovisioned credentials")
    commands.add_parser("gateway", help="Run the isolated model gateway; not yet routed from workers (PR 4)")
    commands.add_parser("worker")
    commands.add_parser("worker-health")
    commands.add_parser("runtime-binding", help="Print the nonsecret deployment-settings fingerprint for qualification evidence")
    for name in ("validate", "compile", "reload"):
        command = commands.add_parser(name)
        command.add_argument("path", nargs="?")
    activate = commands.add_parser("activate", help="Activate an already compiled generation")
    activate.add_argument("generation")
    keys = commands.add_parser("keys")
    keys.add_argument("action", choices=["init"])
    qualify = commands.add_parser("qualify")
    qualify.add_argument("--evidence", required=True)
    qualify.add_argument("--profile")
    qualify.add_argument("--valid-days", default=30, type=int)
    qualify.add_argument("--issuer", default="operator")
    qualify.add_argument("--role", choices=["operator", "platform", "tenant"], default="operator")
    qualify.add_argument("--output", help="Sign offline to a file without opening the database")
    export = commands.add_parser("qualification-export")
    export.add_argument("id")
    export.add_argument("--output", required=True)
    ingest = commands.add_parser("qualification-import")
    ingest.add_argument("path")
    assess = commands.add_parser("assess")
    assess.add_argument("--output", required=True)
    audit = commands.add_parser("audit")
    audit.add_argument("action", choices=["verify"])
    recovery = commands.add_parser("recovery-lock")
    recovery.add_argument("--reason", required=True)
    args = parser.parse_args()
    try:
        if args.command == "db":
            settings = Settings.from_database_env()
            db = Database(settings.database_url, schema=settings.database_schema)
            try:
                getattr(db, args.action)()
                emit({"status": "compatible", "schema_version": db.schema_version})
            finally:
                db.engine.dispose()
            return
        if args.command == "gateway":
            from .gateway.server import main as gateway_main
            gateway_main()
            return
        if args.command == "helper":
            from .connector.helper import main as helper_main
            helper_main()
            return
        if args.command == "validate":
            envelope = load_envelope(os.environ.get("SOVITE_HOSTING_ENVELOPE") or None)
            default_path = os.environ.get("SOVITE_BUNDLE_PATH", "config/platform.local.yaml") if envelope else "config/platform.local.yaml"
            bundle, generation = compile_bundle(policy.load_bundle(args.path or default_path)[0], envelope)
            emit({"valid": True, "generation": generation, "mode": bundle["mode"], "grants": len(bundle["grants"]), "scope": "Declared configuration constraints only"})
            return
        hosted = bool(os.environ.get("SOVITE_HOSTING_ENVELOPE"))
        if hosted and args.command in {"compile", "activate", "reload"}:
            # Hosted compile and activate are authenticated API clients. They run before
            # settings, database and Platform construction, and never read the envelope
            # file: the serving process is the policy and role authority.
            from . import control_client
            if args.command == "compile":
                emit(control_client.compile_command(args.path))
            elif args.command == "activate":
                emit(control_client.activate_command(args.generation))
            else:
                raise control_client.ControlError(
                    "Hosted mode does not support reload: compile the bundle with an author credential, "
                    "then activate the resulting generation with an activator credential.")
            return
        offline = (hosted and args.command in {"keys", "runtime-binding"}) or (args.command == "qualify" and args.output)
        if offline:
            settings = Settings.from_env(require_auth=False)
        elif hosted and args.command != "serve":
            role = {"compile": "author", "activate": "activator"}.get(args.command, "operator")
            settings = Settings.from_env(auth_role=role)
        else:
            settings = Settings.from_env()
        if args.command == "qualify" and args.output:
            bundle, generation = compile_bundle(policy.load_bundle(settings.bundle_path)[0], load_envelope(settings.hosting_envelope_path))
            evidence = load_json(args.evidence, 2_000_000)
            row = issue_qualification(None, settings, generation, args.profile or bundle.get("default_profile", "local"), evidence, args.valid_days, args.issuer, args.role)
            write_qualification(row, args.output)
            emit({"id": row.id, "generation": generation, "passed": row.passed, "output": args.output})
            return
        if args.command == "runtime-binding":
            emit({"runtime_binding": runtime_settings_fingerprint(settings)})
            return
        if args.command == "keys":
            private, public = initialize_keys(settings)
            emit({"private_key": private, "public_key": public})
            return
        if args.command == "assess":
            emit(asyncio.run(assessment(settings, args.output)))
            return
        platform = Platform(settings)
        if args.command == "serve":
            import uvicorn
            from .api import create_app
            uvicorn.run(create_app(settings, platform), host=args.host, port=args.port, access_log=False)
            return
        platform.initialize()
        if args.command == "init":
            emit({"status": "awaiting_activation"} if settings.hosting_envelope_path else {"status": "initialized", "generation": platform.active()[1]})
        elif args.command == "compile":
            from .api import token_identity
            auth = token_identity(os.environ.get("SOVITE_ADMIN_TOKEN", ""), settings)
            generation = platform.compile(policy.load_bundle(args.path or settings.bundle_path)[0], auth[1] if auth else "operator")
            emit({"status": "compiled", "generation": generation})
        elif args.command == "reload":
            from .api import token_identity
            auth = token_identity(os.environ.get("SOVITE_ADMIN_TOKEN", ""), settings)
            emit({"status": "activated", "generation": platform.reload(args.path or settings.bundle_path, role=auth[1] if auth else "operator"), "existing_sessions": "Previous revisions are blocked; running tasks receive cancellation intent"})
        elif args.command == "activate":
            from .api import token_identity
            auth = token_identity(os.environ.get("SOVITE_ADMIN_TOKEN", ""), settings)
            emit({"status": "activated", "generation": platform.activate(args.generation, role=auth[1] if auth else "operator")})
        elif args.command == "worker":
            logging.basicConfig(level=logging.INFO)
            async def run_worker():
                worker = Worker(platform)
                loop = asyncio.get_running_loop()
                for sig in (signal.SIGTERM, signal.SIGINT):
                    loop.add_signal_handler(sig, worker.stop.set)
                await worker.run()
            asyncio.run(run_worker())
        elif args.command == "worker-health":
            count = healthy_worker_count(platform)
            emit({"healthy_workers": count, "compute_id": settings.compute_id, "scope": "This host and configured compute pool"})
            if not count:
                raise SystemExit(1)
        elif args.command == "qualify":
            bundle, generation = platform.active()
            evidence = json.loads(Path(args.evidence).read_text())
            row = issue_qualification(platform.db, settings, generation, args.profile or bundle.get("default_profile", "local"), evidence, args.valid_days, args.issuer, args.role)
            emit({"id": row.id, "generation": row.generation, "passed": row.passed, "expires_at": row.expires_at, "scope": evidence["scope"]})
            if not row.passed:
                raise SystemExit(1)
        elif args.command == "qualification-export":
            export_qualification(platform.db, args.id, args.output)
            emit({"id": args.id, "output": args.output})
        elif args.command == "qualification-import":
            row = import_qualification(platform.db, settings, platform.active()[1], args.path)
            emit({"id": row.id, "generation": row.generation, "passed": row.passed})
        elif args.command == "audit":
            result = platform.db.verify_audit()
            emit(result)
            if not result["valid"]:
                raise SystemExit(1)
        elif args.command == "recovery-lock":
            bundle, generation = platform.active()
            for grant in bundle["grants"]:
                platform.inhibit(grant["id"], args.reason)
            with platform.db.sessions.begin() as session:
                platform.db.audit(session, "operator", "recovery.locked", generation, {"reason": args.reason[:500], "grants": len(bundle["grants"])})
            emit({"status": "locked", "grants": len(bundle["grants"]), "generation": generation})
    except Exception as exc:
        # Administrative diagnostics contain configuration validation details only.
        # Do not print upstream exception bodies or credentials.
        if isinstance(exc, DatabaseSchemaError):
            print(json.dumps({"code": exc.code, "message": str(exc)}), file=sys.stderr)
        elif isinstance(exc, (ValueError, policy.PolicyError)):
            print(str(exc), file=sys.stderr)
        else:
            print(f"Operation failed ({type(exc).__name__}); inspect private operational state.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
