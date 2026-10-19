#!/usr/bin/env python3
"""Install and run the full console/control/worker stack without containers.

This profile uses durable SQLite/filesystem state and explicitly synthetic inference.
The Compose and Kubernetes distributions replace infrastructure bindings, not the UI.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time
import venv

from installation import private_request, private_write

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".state" / "local"


def settings():
    return json.loads((LOCAL / "installation.json").read_text())


def environment(info, *, signer=False):
    result = {**os.environ, **info["environment"]}
    if not signer:
        result.pop("SOVITE_QUALIFICATION_PRIVATE_KEY", None)
    return result


def command(info, *args, **kwargs):
    return subprocess.run([info["cli"], *args], cwd=ROOT, env=environment(info, signer=args[0] in {"keys", "qualify"}), check=True, **kwargs)


def init(args):
    if (LOCAL / "installation.json").exists():
        if args.resume:
            finish_init(settings())
            return
        raise RuntimeError("Local installation already exists; use init --resume to complete an interrupted setup")
    LOCAL.mkdir(parents=True, exist_ok=True, mode=0o700)
    venv_path = Path(args.venv).resolve() if args.venv else ROOT / ".venv"
    if not (venv_path / "bin" / "python").exists():
        venv.EnvBuilder(with_pip=True).create(venv_path)
    py = venv_path / "bin" / "python"
    if not args.skip_dependencies:
        subprocess.run([str(py), "-m", "pip", "install", "-r", str(ROOT / "requirements.lock")], check=True)
    subprocess.run([str(py), "-m", "pip", "install", "--no-deps", "--no-build-isolation", "-e", str(ROOT)], check=True)
    info = {"version": 1, "cli": str(venv_path / "bin" / "sovite"), "environment": {
        "SOVITE_DISPLAY_NAME": "Sovite Platform",
        "SOVITE_DATABASE_URL": "sqlite:///" + str(LOCAL / "control.db"),
        "SOVITE_BUNDLE_PATH": str(ROOT / "config" / "platform.local.yaml"),
        "SOVITE_ARTIFACT_BACKEND": "filesystem",
        "SOVITE_ARTIFACT_PATH": str(LOCAL / "artifacts"),
        "SOVITE_OPERATOR_TOKEN": secrets.token_urlsafe(40),
        "SOVITE_CALLER_TOKENS": json.dumps({"cloud-codex": secrets.token_urlsafe(40), "operations": secrets.token_urlsafe(40)}),
        "SOVITE_PUBLIC_ORIGIN": f"http://127.0.0.1:{args.port}",
        "SOVITE_SECURE_COOKIES": "false",
        "SOVITE_REQUIRE_QUALIFICATION": "true",
        "SOVITE_QUALIFICATION_PRIVATE_KEY": str(LOCAL / "keys" / "qualification-private.pem"),
        "SOVITE_QUALIFICATION_PUBLIC_KEYS": json.dumps([str(LOCAL / "keys" / "qualification-public.pem")]),
        "SOVITE_EMBEDDED_WORKER": "false",
        "SOVITE_COMPUTE_ID": "local",
    }, "port": args.port}
    private_write(LOCAL / "installation.json", json.dumps(info, indent=2))
    finish_init(info)


def finish_init(info):
    private = Path(info["environment"]["SOVITE_QUALIFICATION_PRIVATE_KEY"])
    public = Path(json.loads(info["environment"]["SOVITE_QUALIFICATION_PUBLIC_KEYS"])[0])
    if not private.exists() and not public.exists():
        command(info, "keys", "init")
    elif not private.exists() or not public.exists():
        raise RuntimeError("The signing key pair is incomplete; restore its original matching files before continuing")
    command(info, "init")
    evidence = LOCAL / ("assessment.json" if not (LOCAL / "assessment.json").exists() else f"assessment-{time.time_ns()}.json")
    command(info, "assess", "--output", str(evidence))
    command(info, "qualify", "--evidence", str(evidence), "--profile", "local")
    info["initialization_status"] = "complete"
    (LOCAL / "installation.json").write_text(json.dumps(info, indent=2))
    os.chmod(LOCAL / "installation.json", 0o600)
    print("Installed and qualified the synthetic development profile. Model and cluster qualification remain distinct.")


def process_identity(pid):
    try:
        # Linux proc start time protects against terminating a different process after PID reuse.
        value = Path(f"/proc/{pid}/stat").read_text()
        fields = value.rpartition(")")[2].split()
        return None if fields[0] == "Z" else fields[19]
    except (OSError, IndexError):
        return None


def read_processes():
    path = LOCAL / "processes.json"
    return json.loads(path.read_text()) if path.exists() else {}


def start(args):
    info = settings()
    current = read_processes()
    if any(process_identity(row["pid"]) == row["identity"] for row in current.values()):
        raise RuntimeError("A managed platform process is already running; use status or stop first")
    records = {}
    for name, argv in (("control", ["serve", "--host", "127.0.0.1", "--port", str(info["port"])]), ("worker", ["worker"])):
        with (LOCAL / f"{name}.log").open("ab") as log:
            process = subprocess.Popen([info["cli"], *argv], cwd=ROOT, env=environment(info),
                                       stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        records[name] = {"pid": process.pid, "identity": process_identity(process.pid)}
    (LOCAL / "processes.json").write_text(json.dumps(records))
    os.chmod(LOCAL / "processes.json", 0o600)
    origin = info["environment"]["SOVITE_PUBLIC_ORIGIN"]
    for _ in range(40):
        try:
            with private_request(origin, "/health/ready") as response:
                if response.status == 200:
                    print(f"Sovite Platform is running at {origin}; operator credentials are in .state/local/installation.json.")
                    return
        except OSError:
            time.sleep(0.5)
    raise RuntimeError("Control did not become ready; inspect .state/local/control.log and run stop before retrying")


def stop(args):
    records = read_processes()
    for name in ("worker", "control"):
        row = records.get(name)
        if row and process_identity(row["pid"]) == row["identity"]:
            os.kill(row["pid"], signal.SIGTERM)
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        if all(process_identity(row["pid"]) != row["identity"] for row in records.values()):
            (LOCAL / "processes.json").unlink(missing_ok=True)
            print("Local platform processes stopped; durable state was retained.")
            return
        time.sleep(0.25)
    raise RuntimeError("A process has not exited; investigate rather than terminating an unknown process")


def status(args):
    info = settings()
    with private_request(info["environment"]["SOVITE_PUBLIC_ORIGIN"], "/api/overview", info["environment"]["SOVITE_OPERATOR_TOKEN"]) as response:
        print(json.dumps(json.load(response), indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="action", required=True)
    child = subs.add_parser("init")
    child.add_argument("--venv")
    child.add_argument("--skip-dependencies", action="store_true", help="Use an already provisioned environment")
    child.add_argument("--resume", action="store_true", help="Complete an interrupted setup while preserving its identity and state")
    child.add_argument("--port", type=int, default=8080)
    for name in ("start", "stop", "status"):
        subs.add_parser(name)
    args = parser.parse_args()
    try:
        {"init": init, "start": start, "stop": stop, "status": status}[args.action](args)
    except (RuntimeError, subprocess.CalledProcessError, OSError, ValueError) as error:
        print(f"Local platform operation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
