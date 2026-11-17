from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import logging
import os
import re
import signal
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from sqlalchemy import select

from . import __version__, policy
from .db import Database, DatabaseSchemaError, Job, WorkerExecutionLease, WorkerHeartbeat
from .hosting import compile_bundle, load_envelope, load_json
from .qualification import export_qualification, import_qualification, initialize_keys, issue_qualification, runtime_settings_fingerprint, write_qualification
from .service import Platform
from .settings import Settings
from .worker import Worker, healthy_worker_count


_COMMAND_GROUPS = (
    ("Start here", ("init",)),
    ("Daily operations", ("serve", "worker", "worker-health", "workflow")),
    ("Inspect", ("validate", "runtime-binding", "assess", "audit")),
    ("Change", ("compile", "reload", "activate", "qualify", "recovery-lock")),
    ("Maintain", ("qualification-export", "qualification-import")),
    ("Plumbing", ("db", "keys", "helper", "gateway")),
)


class OperatorHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Keep command discovery grouped while retaining argparse's option help."""

    def _format_action(self, action):
        if not isinstance(action, argparse._SubParsersAction):
            return super()._format_action(action)
        by_name = {item.dest: item for item in action._choices_actions}
        indent = self._current_indent
        rows = []
        for title, names in _COMMAND_GROUPS:
            entries = [by_name[name] for name in names if name in by_name]
            if not entries:
                continue
            rows.append(" " * indent + title + ":\n")
            for item in entries:
                command = item.dest
                help_text = item.help or ""
                rows.append(" " * (indent + 2) + f"{command:<24} {help_text}\n")
        return "".join(rows)


class OperatorArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        match = re.search(r"invalid choice: '([^']+)'", message)
        if match:
            command = match.group(1)
            candidates = [
                name
                for action in self._actions
                if isinstance(action, argparse._SubParsersAction)
                for name in action.choices
            ]
            suggestion = difflib.get_close_matches(command, candidates, n=1, cutoff=0.55)
            if suggestion:
                self.print_usage(sys.stderr)
                self.exit(2, f"{self.prog}: error: unknown command '{command}'\n  did you mean {suggestion[0]}?\n")
        super().error(message)


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
        platform.db.migrate()
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
        # Produce a real started owner before simulating expiry; recovery must
        # never manufacture the matching lease or fence from a partial Job row.
        interrupted = Worker(platform).claim()
        assert interrupted is not None and interrupted.id == recovered.id
        with platform.db.sessions.begin() as session:
            row = session.get(Job, recovered.id)
            lease = session.get(WorkerExecutionLease, recovered.id)
            assert lease is not None and lease.phase == "started"
            row.lease_until = lease.lease_until = time.time() - 10
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
    parser = OperatorArgumentParser(
        prog="sovite", description="Operate the provisioned agent platform.",
        formatter_class=OperatorHelpFormatter,
        epilog=("Examples:\n"
                "  sovite db check\n"
                "  sovite validate config/platform.local.yaml\n"
                "  sovite workflow validate config/workflows/software-delivery.yaml\n\n"
                "Run 'sovite <command> --help' for command details."),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}",
                        help="Show the installed version and exit")
    commands = parser.add_subparsers(dest="command", required=True, metavar="{command}", title="Commands")
    database = commands.add_parser("db", help="Check compatibility or migrate the database",
                                   description="Check database compatibility or apply explicit schema migrations.")
    database.add_argument("action", choices=["migrate", "check"], metavar="{migrate,check}",
                          help="migrate applies schema changes; check is read-only")
    commands.add_parser("init", help="Initialize the platform and import its first configuration revision",
                        description="Initialize platform state and import the first configuration revision.")
    serve = commands.add_parser("serve", help="Run the operator API and console",
                                description="Run the Sovite operator API and web console.")
    serve.add_argument("--host", default="127.0.0.1", metavar="HOST", help="Bind address (default: 127.0.0.1)")
    serve.add_argument("--port", default=8080, type=int, metavar="PORT", help="TCP port (default: 8080)")
    commands.add_parser("helper", help="Run the development connector pilot",
                        description="Run the development connector pilot with preprovisioned credentials.")
    commands.add_parser("gateway", help="Run the isolated model gateway",
                        description="Run the isolated model gateway; workers do not route through it yet.")
    commands.add_parser("worker", help="Run the configured execution worker",
                        description="Run the configured platform and repository execution worker.")
    commands.add_parser("worker-health", help="Report healthy workers in the configured compute pool",
                        description="Report healthy workers for this host and configured compute pool.")
    commands.add_parser("runtime-binding", help="Print the nonsecret deployment settings fingerprint",
                        description="Print the nonsecret runtime settings fingerprint used by qualification evidence.")
    for name in ("validate", "compile", "reload"):
        descriptions = {
            "validate": ("Validate a bundle without activating it", "Validate a bundle and report its resolved generation; no activation occurs."),
            "compile": ("Compile a bundle into a configuration revision", "Compile a bundle into a configuration revision (writes platform state)."),
            "reload": ("Compile and activate a bundle revision", "Compile and activate a bundle revision (changes active configuration)."),
        }
        short, long = descriptions[name]
        command = commands.add_parser(name, help=short, description=long)
        command.add_argument("path", nargs="?", metavar="BUNDLE", help="Bundle YAML path (defaults to SOVITE_BUNDLE_PATH)")
    workflow = commands.add_parser("workflow", help="Validate or bind a delivery workflow",
                                   description="Validate a delivery workflow or compile its immutable binding without dispatching it.")
    workflow_commands = workflow.add_subparsers(dest="workflow_command", required=True)
    workflow_validate = workflow_commands.add_parser("validate", help="Validate a workflow definition")
    workflow_validate.add_argument("path", metavar="WORKFLOW", help="Workflow YAML definition path")
    workflow_compile = workflow_commands.add_parser("compile", help="Compile an immutable workflow binding")
    workflow_compile.add_argument("path", metavar="WORKFLOW", help="Workflow YAML definition path")
    workflow_compile.add_argument("--bundle", required=True, metavar="BUNDLE", help="Platform bundle YAML path")
    workflow_compile.add_argument("--installation-id", required=True, metavar="ID", help="Target installation identity")
    workflow_compile.add_argument("--requirements-ref", required=True, help="JSON revision reference, not requirements content")
    workflow_compile.add_argument("--session-policy-ref", required=True, help="JSON revision reference, not dispatch authority")
    workflow_compile.add_argument("--input-class", help="Defaults to the most restrictive class in the bundle")
    workflow_compile.add_argument("--output", help="Write the immutable binding to a new JSON file")
    activate = commands.add_parser("activate", help="Activate a compiled configuration generation",
                                   description="Activate an already compiled configuration generation.")
    activate.add_argument("generation", metavar="GENERATION", help="Compiled generation identifier")
    keys = commands.add_parser("keys", help="Initialize qualification signing keys",
                               description="Create the qualification signing key pair if it is not already initialized.")
    keys.add_argument("action", choices=["init"], metavar="{init}", help="init creates the signing keys")
    qualify = commands.add_parser("qualify", help="Sign evidence for a platform generation",
                                  description="Sign supplied evidence for the active generation; this command does not run deployment tests.")
    qualify.add_argument("--evidence", required=True, metavar="FILE", help="JSON evidence file to sign")
    qualify.add_argument("--profile", metavar="NAME", help="Deployment profile (defaults to the active bundle profile)")
    validity = qualify.add_mutually_exclusive_group()
    validity.add_argument("--valid-days", type=int, help="Time-bound signature, 1..90 days (default: 30)")
    validity.add_argument("--until-change", action="store_true", help="Commissioning evidence with explicit observation lifetimes; no calendar renewal")
    qualify.add_argument("--issuer", default="operator", metavar="NAME", help="Qualification issuer (default: operator)")
    qualify.add_argument("--role", choices=["operator", "platform", "tenant"], default="operator",
                         metavar="{operator,platform,tenant}", help="Qualification role (default: operator)")
    qualify.add_argument("--output", help="Sign offline to a file without opening the database")
    export = commands.add_parser("qualification-export", help="Export a qualification record to a file",
                                 description="Export a stored qualification record to a JSON file.")
    export.add_argument("id", metavar="ID", help="Qualification identifier")
    export.add_argument("--output", required=True, metavar="FILE", help="Destination JSON file path")
    ingest = commands.add_parser("qualification-import", help="Import a signed qualification record",
                                  description="Verify and store a signed qualification record for the active generation.")
    ingest.add_argument("path", metavar="FILE", help="Signed qualification JSON file")
    assess = commands.add_parser("assess", help="Run the synthetic integration assessment",
                                 description="Exercise the synthetic installation and write assessment evidence; real deployments need separate evidence.")
    assess.add_argument("--output", required=True, metavar="FILE", help="Destination path for synthetic assessment evidence")
    audit = commands.add_parser("audit", help="Verify the platform audit chain",
                                description="Verify the stored platform audit chain.")
    audit.add_argument("action", choices=["verify"], metavar="{verify}", help="verify checks the audit chain")
    recovery = commands.add_parser("recovery-lock", help="Inhibit all grants after recovery",
                                   description="Inhibit every active grant and record the recovery lock in the audit chain.")
    recovery.add_argument("--reason", required=True, metavar="TEXT", help="Reason recorded with the recovery lock")
    if len(sys.argv) == 1:
        parser.print_help()
        return
    args = parser.parse_args()
    try:
        if args.command == "workflow":
            from .workflows import compile_workflow, load_workflow
            definition, revision = load_workflow(args.path)
            if args.workflow_command == "validate":
                emit({"valid": True, "workflow_revision": revision, "mode": definition["mode"],
                      "scope": "Workflow definition only; no installed capability or execution is asserted"})
                return
            envelope = load_envelope(os.environ.get("SOVITE_HOSTING_ENVELOPE") or None)
            if envelope and args.installation_id != envelope["installation_id"]:
                raise ValueError("Installation identity does not match the hosting envelope")
            bundle, generation = compile_bundle(policy.load_bundle(args.bundle)[0], envelope)
            binding = compile_workflow(definition, bundle, installation_id=args.installation_id,
                generation=generation, requirements_ref=load_json(args.requirements_ref),
                session_policy_ref=load_json(args.session_policy_ref), input_class=args.input_class)
            payload = json.dumps(binding, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
            if args.output:
                destination = Path(args.output)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("x", encoding="utf-8") as output:
                    output.write(payload)
            sys.stdout.write(payload)
            return
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
        elif args.command in {"worker", "worker-health"}:
            settings = Settings.from_env(auth_role="worker")
        elif hosted and args.command != "serve":
            role = {"compile": "author", "activate": "activator"}.get(args.command, "operator")
            settings = Settings.from_env(auth_role=role)
        else:
            settings = Settings.from_env()
        if args.command == "qualify" and args.output:
            bundle, generation = compile_bundle(policy.load_bundle(settings.bundle_path)[0], load_envelope(settings.hosting_envelope_path))
            evidence = load_json(args.evidence, 2_000_000)
            row = issue_qualification(None, settings, generation, args.profile or bundle.get("default_profile", "local"), evidence, args.valid_days, args.issuer, args.role, until_change=args.until_change)
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
                from .repository_runtime import compose_repository_runtime
                runtime = compose_repository_runtime(settings, platform)
                worker = Worker(platform, **({"repository_run_executor": runtime.execution}
                                             if runtime.execution is not None else {}))
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
            row = issue_qualification(platform.db, settings, generation, args.profile or bundle.get("default_profile", "local"), evidence, args.valid_days, args.issuer, args.role, until_change=args.until_change)
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
