"""Run the opt-in HTTP worker without constructing a local control Platform."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import signal
import sys
from pathlib import Path

from .runtime_boundary import CONTROL_OR_STORE_ENV

_CONTROL_OR_STORE_ENV = (
    *CONTROL_OR_STORE_ENV,
    "SOVITE_HOSTING_ENVELOPE",
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
    from .remote_worker_health import _health_path, maintain_heartbeat
    from .worker import RemoteWorker

    journal = ExecutionJournal(journal_path)
    heartbeat_stop = asyncio.Event()
    heartbeat_task = None
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
        heartbeat_task = asyncio.create_task(maintain_heartbeat(_health_path(), heartbeat_stop))
        await worker.run()
    finally:
        heartbeat_stop.set()
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        try:
            _health_path().unlink()
        except (FileNotFoundError, ValueError):
            pass
        journal.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sovite-remote-worker",
        description="Run the opt-in execution API worker without local SQL or artifact storage.",
    )
    parser.add_argument(
        "--inspect-journal", action="store_true",
        help="read the stopped worker's bounded local journal without contacting the owner",
    )
    args = parser.parse_args(argv)

    journal_path = os.environ.get("SOVITE_EXECUTION_JOURNAL_PATH", "")
    if args.inspect_journal:
        if not journal_path or not Path(journal_path).is_absolute():
            print("SOVITE_EXECUTION_JOURNAL_PATH must be an absolute path", file=sys.stderr)
            return 2
        from .remote_journal import ExecutionJournal, JournalError

        try:
            record = ExecutionJournal.inspect(journal_path)
        except JournalError as exc:
            print(f"Execution journal inspection stopped: {exc}", file=sys.stderr)
            return 1
        if record is None:
            report = {
                "state": "empty",
                "next_action": "worker_may_start_after_current_owner_checks",
            }
        else:
            report = {
                "state": "manual_reconciliation_required",
                "record": record,
                "next_action": (
                    "use an owner-authorized lookup for this exact instance and operation; "
                    "match the captured job and fence when present; do not retry or clear "
                    "the record based only on this local snapshot"
                ),
            }
        print(json.dumps(report, allow_nan=False, ensure_ascii=True,
                         separators=(",", ":")))
        return 0

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
    if not journal_path or not Path(journal_path).is_absolute():
        print("SOVITE_EXECUTION_JOURNAL_PATH must be an absolute path", file=sys.stderr)
        return 2
    from .remote_worker_health import _health_path

    try:
        _health_path()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
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
