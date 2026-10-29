#!/usr/bin/env python3
"""Provision and maintain the complete local Compose distribution.

Uses subprocess argument arrays and generated environment files; never evals shell
input. Backup and restore stop every service that can write platform state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import urllib.error
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".state"


def run(args, *, capture=False, check=True, stdout=None, stdin=None, env=None):
    return subprocess.run([str(x) for x in args], cwd=ROOT, check=check, text=stdout is None,
                          capture_output=capture, stdout=stdout, stdin=stdin, env=env)


def compose(*args, **kw):
    if not shutil.which("docker"):
        raise RuntimeError("Docker with the Compose plugin is required for this installation profile")
    extra = []
    saved = read_env() if (ROOT / ".env").exists() else {}
    selected = os.environ.get("SOVITE_COMPOSE_OVERRIDES", saved.get("SOVITE_COMPOSE_OVERRIDES", ""))
    for path in selected.split(os.pathsep):
        if path:
            extra += ["-f", path]
    return run(["docker", "compose", "-f", "compose.yaml", *extra, *args], **kw)


def up(args):
    # Bring up only the database/object store first, run the explicit migration
    # command as a one-shot control container, then start runtime workloads.
    _, running = writer_state()
    if running:
        raise RuntimeError("Stop existing writers before database migration: " + ", ".join(running))
    compose("up", "-d", "--wait", "postgres", "garage")
    compose("run", "--rm", "--no-deps", "control", "db", "migrate")
    compose("up", "-d", "--wait")


def private_write(path: Path, data: str | bytes):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data.encode() if isinstance(data, str) else data)


def read_env():
    result = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        if value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        result[key] = value
    return result


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, newurl):
        raise urllib.error.HTTPError(request.full_url, code, "Redirect refused", headers, file)


def private_request(origin, path, token=None):
    parsed = urlsplit(origin)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise RuntimeError("Origin must contain only scheme, host and optional port")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")):
        raise RuntimeError("Use HTTPS or a loopback origin for authenticated platform requests")
    headers = {"Authorization": "Bearer " + token} if token else {}
    request = urllib.request.Request(origin.rstrip("/") + path, headers=headers)
    # Credentials are not sent through environment-provided proxies or redirects.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirects())
    return opener.open(request, timeout=30)


def writer_state():
    output = compose("ps", "--all", "--format", "json", capture=True).stdout.strip()
    if not output:
        return [], []
    rows = json.loads(output) if output.startswith("[") else [json.loads(line) for line in output.splitlines()]
    writers = [row["Service"] for row in rows if row.get("Service") in {"control", "worker", "native-harness"}]
    running = [row["Service"] for row in rows if row.get("Service") in writers and row.get("State") in {"running", "restarting"}]
    return sorted(set(writers)), sorted(set(running))


def initialize(args):
    STATE.mkdir(exist_ok=True, mode=0o700)
    (STATE / "trust").mkdir(exist_ok=True, mode=0o755)
    (STATE / "keys").mkdir(exist_ok=True, mode=0o700)
    path = ROOT / ".env"
    if path.exists():
        raise RuntimeError(".env already exists; it was preserved. Edit its configuration deliberately.")
    bundle = Path(args.bundle or ("config/platform.ollama.yaml" if args.profile == "ollama" else "config/platform.local.yaml"))
    if not (ROOT / bundle).is_file():
        raise RuntimeError(f"Bundle does not exist: {bundle}")
    overrides = ["deploy/compose/native-codex.yaml"] if args.profile == "ollama" else []
    workspace = None
    if args.workspace:
        if args.profile != "ollama":
            raise RuntimeError("An approved native workspace requires the Ollama/native installation profile")
        workspace = Path(args.workspace).resolve()
        if not workspace.is_dir() or workspace in {Path("/"), Path.home(), ROOT}:
            raise RuntimeError("Select an existing task-specific input directory, not the host root, home or platform installation")
        overrides.append("deploy/compose/native-workspace.yaml")
    values = {
        "SOVITE_DISPLAY_NAME": "Sovite Platform",
        "SOVITE_BUNDLE": str(bundle) if bundle.is_absolute() else "./" + str(bundle),
        "SOVITE_COMPUTE_ID": args.compute,
        "SOVITE_PUBLIC_ORIGIN": args.origin,
        "SOVITE_SECURE_COOKIES": "true" if args.origin.startswith("https://") else "false",
        "SOVITE_REQUIRE_QUALIFICATION": "true",
        "SOVITE_OPERATOR_TOKEN": secrets.token_urlsafe(40),
        "SOVITE_CALLER_TOKENS": json.dumps({"cloud-codex": secrets.token_urlsafe(40), "operations": secrets.token_urlsafe(40)}, separators=(",", ":")),
        "SOVITE_HARNESS_TOKEN": secrets.token_urlsafe(40),
        "POSTGRES_PASSWORD": secrets.token_hex(32),
        "AWS_ACCESS_KEY_ID": "GK" + secrets.token_hex(16),
        "AWS_SECRET_ACCESS_KEY": secrets.token_hex(32),
        "GARAGE_RPC_SECRET": secrets.token_hex(32),
        "GARAGE_ADMIN_TOKEN": secrets.token_urlsafe(40),
        "SOVITE_STAGE_MODEL": args.model,
        "SOVITE_COMPOSE_OVERRIDES": os.pathsep.join(overrides),
    }
    if workspace:
        values["SOVITE_NATIVE_WORKSPACE_SOURCE"] = str(workspace)
    if any("\n" in value or "\r" in value or "'" in value for value in values.values()):
        raise RuntimeError("Installation values must be single-line and cannot contain apostrophes in the generated environment format")
    private_write(path, "# Generated private installation credentials; never commit this file.\n" +
                  "".join(f"{key}='{value}'\n" for key, value in values.items()))
    # Public trust is injected separately; the signer never runs inside the API or worker.
    print("Created .env and private state directories. Credentials were not printed.")
    print("Next: build the image, run keys-init, then up. Runtime dispatch stays blocked until qualification.")


def keys_init(args):
    if (STATE / "keys" / "qualification-private.pem").exists():
        raise RuntimeError("A signing key already exists; refusing to replace it")
    if args.local:
        from sovite.qualification import initialize_keys
        from sovite.settings import Settings
        initialize_keys(Settings(qualification_private_key=str(STATE / "keys" / "qualification-private.pem"),
                                 qualification_public_keys=[str(STATE / "keys" / "qualification-public.pem")]))
    else:
        compose("run", "--rm", "--no-deps", "--user", f"{os.getuid()}:{os.getgid()}",
                "-v", f"{STATE / 'keys'}:/keys",
                "-e", "SOVITE_QUALIFICATION_PRIVATE_KEY=/keys/qualification-private.pem",
                "-e", 'SOVITE_QUALIFICATION_PUBLIC_KEYS=["/keys/qualification-public.pem"]',
                "control", "keys", "init")
    public = STATE / "keys" / "qualification-public.pem"
    (STATE / "trust").mkdir(parents=True, exist_ok=True, mode=0o755)
    shutil.copyfile(public, STATE / "trust" / public.name)
    os.chmod(STATE / "trust" / public.name, 0o644)
    print("Created the local qualification signing key and published its public trust file.")


def health(args):
    compose("ps")
    env = read_env()
    origin = args.origin or env["SOVITE_PUBLIC_ORIGIN"]
    for path in ("/health/live", "/health/ready"):
        with private_request(origin, path) as response:
            print(path, response.status, response.read().decode())
    compose("exec", "-T", "control", "sovite", "audit", "verify")
    compose("exec", "-T", "worker", "sovite", "worker-health")


def stage(args):
    env = read_env()
    print(f"Staging {env.get('SOVITE_STAGE_MODEL', 'qwen3:4b')} into the inference volume.")
    output = compose("ps", "--all", "--format", "json", capture=True).stdout.strip()
    rows = (json.loads(output) if output.startswith("[") else [json.loads(line) for line in output.splitlines()]) if output else []
    consumers = {"worker", "native-harness", "ollama"}
    existing = sorted({row["Service"] for row in rows if row.get("Service") in consumers})
    running = sorted({row["Service"] for row in rows if row.get("Service") in consumers and row.get("State") == "running"})
    if existing:
        compose("stop", *existing)
    try:
        compose("run", "--rm", "--no-deps", "model-stage")
    finally:
        if running:
            compose("up", "-d", *running)
    print("Model cache updated. Resolve and qualify its observed digest before enabling a production grant.")


def _safe_manifest(directory):
    if any(path.is_symlink() for path in directory.rglob("*")):
        raise RuntimeError("State archive contains a symbolic link; inspect the state before backup")
    return {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(directory.rglob("*")) if path.is_file() and path.name != "manifest.json"}


def backup(args):
    destination = Path(args.output).resolve()
    if destination.exists():
        raise RuntimeError("Backup destination already exists; refusing to overwrite")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="sovite-backup-", dir=destination.parent) as tmp:
        directory = Path(tmp)
        os.chmod(directory, 0o700)
        writers, running = writer_state()
        if writers:
            compose("stop", *writers)
        try:
            with (directory / "control.dump").open("wb") as stream:
                compose("exec", "-T", "postgres", "pg_dump", "-U", "sovite", "-d", "sovite", "-Fc", stdout=stream)
            compose("run", "--rm", "--no-deps", "--user", "0:0", "--entrypoint", "python",
                    "-v", f"{directory}:/backup", "control", "/app/scripts/state_archive.py", "dump", "/backup/artifacts.ndjson")
            env = read_env()
            shutil.copyfile(ROOT / env["SOVITE_BUNDLE"], directory / "bundle.yaml")
            shutil.copytree(STATE / "trust", directory / "trust")
            if "native-harness" in writers:
                compose("cp", "native-harness:/state/harness/.", str(directory / "native-harness"))
            manifest = {"format": "sovite-backup-v1", "created_at": time.time(), "consistency": "control-and-workers-stopped",
                        "database": "postgresql-17", "files": _safe_manifest(directory), "writers": writers,
                        "excludes": ["runtime credentials", "offline signing private key", "model weights", "approved native input snapshots", "external tool data"]}
            (directory / "manifest.json").write_text(json.dumps(manifest, indent=2))
            with tarfile.open(destination, "x:gz") as archive:
                for path in sorted(directory.iterdir()):
                    archive.add(path, arcname=path.name)
            os.chmod(destination, 0o600)
        finally:
            if running:
                compose("up", "-d", *running)
    print(f"Consistent backup written: {destination}. It contains private data; retain it in approved storage.")


def restore(args):
    source = Path(args.archive).resolve()
    if not args.accept_replace:
        raise RuntimeError("Restore replaces this installation's state. Rerun with --accept-replace after reviewing the target.")
    with tempfile.TemporaryDirectory(prefix="sovite-restore-", dir=STATE) as tmp:
        directory = Path(tmp)
        with tarfile.open(source, "r:gz") as archive:
            members = archive.getmembers()
            if any(m.issym() or m.islnk() or m.isdev() or Path(m.name).is_absolute() or ".." in Path(m.name).parts for m in members):
                raise RuntimeError("Unsafe backup archive member")
            archive.extractall(directory, members=members, filter="data")
        manifest = json.loads((directory / "manifest.json").read_text())
        if manifest.get("format") != "sovite-backup-v1" or manifest["files"] != _safe_manifest(directory):
            raise RuntimeError("Backup manifest or checksums do not match")
        writers, running = writer_state()
        if "native-harness" in manifest.get("writers", []) and "native-harness" not in writers:
            raise RuntimeError("Backup includes a native harness; select and create its deployment profile before restore")
        if writers:
            compose("stop", *writers)
        # On any failure, writers remain stopped. Do not restore stale privileges accidentally.
        with (directory / "control.dump").open("rb") as stream:
            compose("exec", "-T", "postgres", "pg_restore", "-U", "sovite", "-d", "sovite",
                    "--clean", "--if-exists", "--single-transaction", stdin=stream)
        compose("run", "--rm", "--no-deps", "--user", "0:0", "--entrypoint", "python",
                "-v", f"{directory}:/backup:ro", "control", "/app/scripts/state_archive.py", "restore", "/backup/artifacts.ndjson")
        compose("run", "--rm", "--no-deps", "control", "recovery-lock", "--reason", "Restored backup; operator review required")
        compose("run", "--rm", "--no-deps", "control", "audit", "verify")
        if (directory / "native-harness").exists():
            compose("cp", str(directory / "native-harness") + "/.", "native-harness:/state/harness")
            compose("run", "--rm", "--no-deps", "--user", "0:0", "--entrypoint", "python", "native-harness", "-c",
                    "import os; from pathlib import Path; p=Path('/state/harness'); os.chown(p,10001,10001); "
                    "[(os.chown(x,10001,10001)) for x in p.rglob('*')]")
        if running:
            compose("up", "-d", *running)
    print("Restored state and inhibited all grants. Current desired config and trust were retained; review before resume.")


def export(args):
    destination = Path(args.output).resolve()
    if destination.exists():
        raise RuntimeError("Export destination already exists")
    env = read_env()
    data = {"format": "sovite-operator-export-v1", "created_at": time.time()}
    origin = args.origin or env["SOVITE_PUBLIC_ORIGIN"]
    for resource in ("overview", "catalog", "grants", "jobs", "receipts", "audit", "qualifications", "deployments"):
        with private_request(origin, "/api/" + resource, env["SOVITE_OPERATOR_TOKEN"]) as response:
            data[resource] = json.load(response)
    private_write(destination, json.dumps(data, indent=2))
    print(f"Operator export written: {destination}. It is a bounded API snapshot, not a backup; private result bodies are excluded.")


def kubernetes_secret(args):
    source = read_env()
    keys = {"SOVITE_OPERATOR_TOKEN", "SOVITE_CALLER_TOKENS", "SOVITE_HARNESS_TOKEN", "POSTGRES_PASSWORD", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "GARAGE_RPC_SECRET", "GARAGE_ADMIN_TOKEN"}
    values = {key: source[key] for key in keys}
    values["SOVITE_DATABASE_URL"] = f"postgresql+psycopg://sovite:{source['POSTGRES_PASSWORD']}@{args.database_host}:5432/sovite"
    private_write(Path(args.output).resolve(), json.dumps(values, indent=2))
    print("Created a private Kubernetes runtime secret input. It was not printed or applied.")


def assess(args):
    evidence = Path(args.output).resolve()
    evidence.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if evidence.exists():
        raise RuntimeError("Evidence output already exists; preserve the prior record and select a new path")
    compose("run", "--rm", "--no-deps", "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{evidence.parent}:/evidence", "control", "assess", "--output", "/evidence/" + evidence.name)


def qualify(args):
    evidence = Path(args.evidence).resolve()
    if not evidence.is_file():
        raise RuntimeError("Evidence file not found")
    compose("run", "--rm", "--no-deps", "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{evidence}:/evidence.json:ro", "-v", f"{STATE / 'keys'}:/signer:ro",
            "-e", "SOVITE_QUALIFICATION_PRIVATE_KEY=/signer/qualification-private.pem",
            "control", "qualify", "--evidence", "/evidence.json", "--profile", args.profile)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("compose-init", aliases=["credentials-init"])
    init.add_argument("--profile", choices=["demo", "ollama"], default="demo")
    init.add_argument("--bundle")
    init.add_argument("--compute", default="local")
    init.add_argument("--origin", default="http://localhost:8080")
    init.add_argument("--model", default="qwen3:4b")
    init.add_argument("--workspace", help="Explicit approved task input directory, mounted read-only into the isolated native service")
    init.set_defaults(fn=initialize)
    for command in ("keys-init", "health", "stage-model", "build", "up", "down", "logs"):
        child = sub.add_parser(command)
        child.add_argument("--origin")
        if command == "keys-init":
            child.add_argument("--local", action="store_true", help="Generate offline Ed25519 keys using the installed Python package; no Docker required")
    item = sub.add_parser("backup")
    item.add_argument("output")
    item.set_defaults(fn=backup)
    item = sub.add_parser("restore")
    item.add_argument("archive")
    item.add_argument("--accept-replace", action="store_true")
    item.set_defaults(fn=restore)
    item = sub.add_parser("export")
    item.add_argument("output")
    item.add_argument("--origin")
    item.set_defaults(fn=export)
    item = sub.add_parser("kubernetes-secret")
    item.add_argument("output")
    item.add_argument("--database-host", default="sovite-postgres")
    item.set_defaults(fn=kubernetes_secret)
    item = sub.add_parser("assess-synthetic")
    item.add_argument("output")
    item.set_defaults(fn=assess)
    item = sub.add_parser("qualify")
    item.add_argument("evidence")
    item.add_argument("--profile", required=True)
    item.set_defaults(fn=qualify)
    args = parser.parse_args()
    mapping = {"keys-init": keys_init, "health": health, "stage-model": stage,
               "build": lambda _: compose("build"),
               "up": up,
               "down": lambda _: compose("down"),
               "logs": lambda _: compose("logs", "--tail", "100")}
    try:
        getattr(args, "fn", mapping.get(args.command))(args)
    except (RuntimeError, subprocess.CalledProcessError, OSError, ValueError) as error:
        print(f"Installation operation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
