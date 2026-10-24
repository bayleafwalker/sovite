"""Run the opt-in HTTP worker without constructing a local control Platform."""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import signal
import sys
from pathlib import Path


_CONTROL_OR_STORE_ENV = (
    "SOVITE_DATABASE_URL",
    "SOVITE_DATABASE_SCHEMA",
    "SOVITE_DATABASE_MIGRATE",
    "SOVITE_ARTIFACT_PATH",
    "SOVITE_ARTIFACT_BACKEND",
    "SOVITE_S3_ENDPOINT",
    "SOVITE_S3_BUCKET",
    "SOVITE_S3_REGION",
    "SOVITE_OPERATOR_TOKEN",
    "SOVITE_AUTHOR_TOKEN",
    "SOVITE_ACTIVATOR_TOKEN",
    "SOVITE_AUTHORITY_TOKEN",
    "SOVITE_CALLER_TOKENS",
    "SOVITE_EXECUTION_TOKENS",
    "SOVITE_HOSTING_ENVELOPE",
    "SOVITE_QUALIFICATION_PRIVATE_KEY",
    "SOVITE_QUALIFICATION_PUBLIC_KEYS",
)


def _bounded_seconds(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a finite number in [{minimum}, {maximum}]") from None
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be a finite number in [{minimum}, {maximum}]")
    return value


async def _run(
    instance_id: str,
    journal_path: str,
    poll_seconds: float,
    lease_seconds: float,
    ca_bundle: str | None,
) -> None:
    from .execution_client import ExecutionClient
    from .remote_journal import ExecutionJournal
    from .worker import RemoteWorker

    journal = ExecutionJournal(journal_path)
    try:
        worker = RemoteWorker(
            ExecutionClient.from_env(verify=ca_bundle or True),
            instance_id,
            journal=journal,
            poll_seconds=poll_seconds,
            lease_seconds=lease_seconds,
        )
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, worker.stop.set)
        await worker.run()
    finally:
        journal.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sovite-remote-worker",
        description="Run the opt-in execution API worker without local SQL or artifact storage.",
    )
    parser.parse_args(argv)

    present = [name for name in _CONTROL_OR_STORE_ENV if os.environ.get(name)]
    if present:
        print(
            "Refusing remote worker startup while local control or artifact-store credentials/configuration are present: "
            + ", ".join(present),
            file=sys.stderr,
        )
        return 2
    instance_id = os.environ.get("SOVITE_EXECUTION_INSTANCE_ID", "")
    if not instance_id:
        print("SOVITE_EXECUTION_INSTANCE_ID is required", file=sys.stderr)
        return 2
    journal_path = os.environ.get("SOVITE_EXECUTION_JOURNAL_PATH", "")
    if not journal_path or not Path(journal_path).is_absolute():
        print("SOVITE_EXECUTION_JOURNAL_PATH must be an absolute path", file=sys.stderr)
        return 2
    try:
        poll_seconds = _bounded_seconds("SOVITE_EXECUTION_POLL_SECONDS", 2.0, 0.1, 60.0)
        lease_seconds = _bounded_seconds("SOVITE_EXECUTION_LEASE_SECONDS", 90.0, 1.0, 3600.0)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    ca_bundle = os.environ.get("SOVITE_EXECUTION_CA_BUNDLE") or None
    if ca_bundle is not None and (not Path(ca_bundle).is_file() or not os.access(ca_bundle, os.R_OK)):
        print("SOVITE_EXECUTION_CA_BUNDLE must name a readable CA file", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(_run(instance_id, journal_path, poll_seconds, lease_seconds, ca_bundle))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        # Avoid printing HTTP bodies, task data, credentials, or exception text.
        print(f"Remote worker stopped ({type(exc).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
