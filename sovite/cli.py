from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from sqlalchemy import select

from . import policy
from .db import Job, WorkerHeartbeat
from .qualification import initialize_keys, issue_qualification, runtime_settings_fingerprint
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
        ephemeral = replace(settings, database_url=f"sqlite:///{work}/control.db", artifact_backend="filesystem", artifact_path=f"{work}/artifacts", require_qualification=False, compute_id=grant["compute"])
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
    commands.add_parser("init", help="Initialize schema and import the first configuration revision")
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8080, type=int)
    commands.add_parser("worker")
    commands.add_parser("worker-health")
    commands.add_parser("runtime-binding", help="Print the nonsecret deployment-settings fingerprint for qualification evidence")
    for name in ("validate", "reload"):
        command = commands.add_parser(name)
        command.add_argument("path", nargs="?")
    keys = commands.add_parser("keys")
    keys.add_argument("action", choices=["init"])
    qualify = commands.add_parser("qualify")
    qualify.add_argument("--evidence", required=True)
    qualify.add_argument("--profile")
    qualify.add_argument("--valid-days", default=30, type=int)
    qualify.add_argument("--issuer", default="operator")
    assess = commands.add_parser("assess")
    assess.add_argument("--output", required=True)
    audit = commands.add_parser("audit")
    audit.add_argument("action", choices=["verify"])
    recovery = commands.add_parser("recovery-lock")
    recovery.add_argument("--reason", required=True)
    args = parser.parse_args()
    try:
        if args.command == "validate":
            bundle, generation = policy.load_bundle(args.path or "config/platform.local.yaml")
            emit({"valid": True, "generation": generation, "mode": bundle["mode"], "grants": len(bundle["grants"]), "scope": "Declared configuration constraints only"})
            return
        settings = Settings.from_env()
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
            emit({"status": "initialized", "generation": platform.active()[1]})
        elif args.command == "reload":
            emit({"status": "activated", "generation": platform.reload(args.path or settings.bundle_path), "existing_sessions": "Previous revisions are blocked; running tasks receive cancellation intent"})
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
            row = issue_qualification(platform.db, settings, generation, args.profile or bundle.get("default_profile", "local"), evidence, args.valid_days, args.issuer)
            emit({"id": row.id, "generation": row.generation, "passed": row.passed, "expires_at": row.expires_at, "scope": evidence["scope"]})
            if not row.passed:
                raise SystemExit(1)
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
        if isinstance(exc, (ValueError, policy.PolicyError)):
            print(str(exc), file=sys.stderr)
        else:
            print(f"Operation failed ({type(exc).__name__}); inspect private operational state.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
