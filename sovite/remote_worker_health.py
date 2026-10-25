"""Local event-loop heartbeat for the isolated remote worker probes.

The heartbeat is separate from the execution recovery journal. Probe reads do
not acquire the journal lock and cannot observe, clear, or replay an operation.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path


HEALTH_PATH_ENV = "SOVITE_REMOTE_WORKER_HEALTH_PATH"
HEARTBEAT_SECONDS = 5.0
MAX_HEARTBEAT_AGE_SECONDS = 30.0


def _health_path() -> Path:
    value = os.environ.get(HEALTH_PATH_ENV, "")
    if not value or not Path(value).is_absolute():
        raise ValueError(f"{HEALTH_PATH_ENV} must be an absolute path")
    return Path(value)


def write_heartbeat(path: Path, now: float | None = None) -> None:
    """Atomically publish event-loop progress without touching operation state."""
    stamp = time.monotonic() if now is None else now
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(f"{stamp:.9f}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def heartbeat_is_fresh(path: Path, *, now: float | None = None,
                       max_age: float = MAX_HEARTBEAT_AGE_SECONDS) -> bool:
    """Return true only for a well-formed recent heartbeat from this boot."""
    try:
        stamp = float(path.read_text(encoding="ascii").strip())
    except (OSError, UnicodeError, ValueError):
        return False
    current = time.monotonic() if now is None else now
    age = current - stamp
    return 0 <= age <= max_age


async def maintain_heartbeat(path: Path, stop: asyncio.Event) -> None:
    """Update only while the worker event loop is responsive."""
    while not stop.is_set():
        write_heartbeat(path)
        try:
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_SECONDS)
        except asyncio.TimeoutError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sovite-remote-worker-health",
        description="Check the local remote-worker event-loop heartbeat; no owner or journal access.",
    )
    parser.parse_args(argv)
    try:
        healthy = heartbeat_is_fresh(_health_path())
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not healthy:
        print("Remote worker event-loop heartbeat is missing or stale", file=sys.stderr)
        return 1
    return 0
