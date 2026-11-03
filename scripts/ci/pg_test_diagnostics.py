"""Run the Work projection PostgreSQL CI tests with bounded diagnostics.

This file also supplies the pytest plugin loaded by the managed child process.
The supervisor discards raw pytest output and persists only bounded, sanitized
progress, stack, activity, and exit-status records.
"""

from __future__ import annotations

import argparse
import faulthandler
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import threading
import time
from typing import Any

PROGRESS_LIMIT = 256 * 1024
STACK_LIMIT = 256 * 1024
SNAPSHOT_LIMIT = 128 * 1024
SNAPSHOT_TOTAL_SECONDS = 5
TERM_WAIT_SECONDS = 5
KILL_WAIT_SECONDS = 5
PIPE_DRAIN_SECONDS = 1
DEFAULT_TEST_TIMEOUT_SECONDS = 8 * 60
STACK_DUMP_INTERVAL_SECONDS = 60

ACTIVITY_SQL = """
WITH owned AS (
    SELECT pid, state, wait_event_type, wait_event, query_start, backend_start
    FROM pg_catalog.pg_stat_activity
    WHERE usename = current_user
    ORDER BY pid
    LIMIT 100
),
activity_rows AS (
    SELECT jsonb_agg(jsonb_build_object(
        'pid', pid,
        'state', state,
        'wait_event_type', wait_event_type,
        'wait_event', wait_event,
        'query_age_ms', CASE WHEN query_start IS NULL THEN NULL
            ELSE GREATEST(0, FLOOR(EXTRACT(EPOCH FROM clock_timestamp() - query_start) * 1000))::bigint END,
        'backend_age_ms', CASE WHEN backend_start IS NULL THEN NULL
            ELSE GREATEST(0, FLOOR(EXTRACT(EPOCH FROM clock_timestamp() - backend_start) * 1000))::bigint END
    )) AS rows
    FROM owned
),
lock_rows AS (
    SELECT jsonb_agg(jsonb_build_object(
        'pid', lock.pid,
        'locktype', lock.locktype,
        'mode', lock.mode,
        'granted', lock.granted,
        'relation', CASE WHEN lock.relation IS NULL THEN NULL ELSE lock.relation::regclass::text END,
        'transaction_id', lock.transactionid::text
    )) AS rows
    FROM (
        SELECT pid, locktype, mode, granted, relation, transactionid
        FROM pg_catalog.pg_locks
        WHERE pid IN (SELECT pid FROM owned)
        ORDER BY pid, locktype, mode
        LIMIT 200
    ) AS lock
)
SELECT jsonb_build_object(
    'activity', COALESCE((SELECT rows FROM activity_rows), '[]'::jsonb),
    'locks', COALESCE((SELECT rows FROM lock_rows), '[]'::jsonb)
)
"""


def _json_line(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _pwrite_all(fd: int, data: bytes, offset: int) -> None:
    view = memoryview(data)
    while view:
        written = os.pwrite(fd, view, offset)
        view = view[written:]
        offset += written


def safe_test_identity(nodeid: str) -> tuple[str, str]:
    """Return the unparameterized test name and a digest of the full id."""
    full_id = nodeid.encode("utf-8", errors="replace")
    test_part = nodeid.partition("::")[2].split("[", 1)[0]
    safe_name = test_part[:160] or "unknown"
    return safe_name, hashlib.sha256(full_id).hexdigest()


class ProgressJournal:
    """Append bounded JSONL and maintain a fixed-size truncation marker."""

    _MARKER_SIZE = 128

    def __init__(self, path: Path, max_bytes: int = PROGRESS_LIMIT) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        self._written = os.fstat(self._fd).st_size
        self._marker_offset: int | None = None
        self._dropped = 0

    def write(self, event: dict[str, Any]) -> None:
        line = _json_line(event)
        with self._lock:
            if self._marker_offset is None and self._written + len(line) <= self.max_bytes - self._MARKER_SIZE:
                os.lseek(self._fd, 0, os.SEEK_END)
                _write_all(self._fd, line)
                os.fsync(self._fd)
                self._written += len(line)
                return
            self._dropped += 1
            if self._marker_offset is None:
                self._marker_offset = self._written
            marker = _json_line({"event": "truncated", "dropped_events": self._dropped})
            if len(marker) > self._MARKER_SIZE:
                marker = _json_line({"event": "truncated", "dropped_events": ">=1"})
            marker = marker[:-1].ljust(self._MARKER_SIZE - 1, b" ") + b"\n"
            _pwrite_all(self._fd, marker, self._marker_offset)
            os.fsync(self._fd)

    def close(self) -> None:
        with self._lock:
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1


class BoundedRing:
    def __init__(self, limit: int = STACK_LIMIT) -> None:
        self.limit = limit
        self._data = bytearray()
        self.truncated = False

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        if len(chunk) >= self.limit:
            self._data[:] = chunk[-self.limit :]
            self.truncated = True
            return
        overflow = len(self._data) + len(chunk) - self.limit
        if overflow > 0:
            del self._data[:overflow]
            self.truncated = True
        self._data.extend(chunk)

    def bytes(self) -> bytes:
        if not self.truncated:
            return bytes(self._data[-self.limit :])
        marker = b"[truncated]\n"
        if len(marker) >= self.limit:
            marker = b"~"
        keep = max(0, self.limit - len(marker))
        suffix = bytes(self._data[-keep:]) if keep else b""
        return marker + suffix


_PLUGIN_JOURNAL: ProgressJournal | None = None
_PLUGIN_STACK_STREAM: Any = None
_PLUGIN_SIGNAL_REGISTERED = False
_PLUGIN_TIMER_STARTED = False


def pytest_configure(config: Any) -> None:
    """Pytest plugin hook. The helper is inert outside its CI child process."""
    global _PLUGIN_JOURNAL
    diag_dir = os.environ.get("SOVITE_CI_DIAG_DIR")
    if diag_dir:
        _PLUGIN_JOURNAL = ProgressJournal(Path(diag_dir) / "progress.jsonl")


def pytest_sessionstart(session: Any) -> None:
    global _PLUGIN_STACK_STREAM, _PLUGIN_SIGNAL_REGISTERED, _PLUGIN_TIMER_STARTED
    diag_dir = os.environ.get("SOVITE_CI_DIAG_DIR")
    raw_fd = os.environ.get("SOVITE_CI_DIAG_STACK_FD")
    if not diag_dir or raw_fd is None:
        return
    try:
        stack_fd = int(raw_fd)
        _PLUGIN_STACK_STREAM = os.fdopen(stack_fd, "w", buffering=1, encoding="utf-8", closefd=False)
        faulthandler.register(signal.SIGUSR1, file=_PLUGIN_STACK_STREAM, all_threads=True, chain=False)
        _PLUGIN_SIGNAL_REGISTERED = True
        faulthandler.dump_traceback_later(
            STACK_DUMP_INTERVAL_SECONDS,
            repeat=True,
            file=_PLUGIN_STACK_STREAM,
        )
        _PLUGIN_TIMER_STARTED = True
        ready = Path(diag_dir) / "plugin.ready"
        fd = os.open(ready, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            _write_all(fd, b"ready\n")
            os.fsync(fd)
        finally:
            os.close(fd)
    except Exception as exc:  # keep exception text and paths out of diagnostics
        if _PLUGIN_JOURNAL is not None:
            _PLUGIN_JOURNAL.write({"event": "plugin_setup_failed", "error_type": type(exc).__name__})


def pytest_runtest_logstart(nodeid: str, location: Any) -> None:
    if _PLUGIN_JOURNAL is None:
        return
    name, identity = safe_test_identity(nodeid)
    _PLUGIN_JOURNAL.write({
        "event": "test_start",
        "test": name,
        "id_sha256": identity,
        "utc": _utc_now(),
        "monotonic_ns": time.monotonic_ns(),
    })


def pytest_runtest_logreport(report: Any) -> None:
    if _PLUGIN_JOURNAL is None:
        return
    name, identity = safe_test_identity(report.nodeid)
    location = getattr(report, "location", None)
    filename = Path(str(location[0])).name[:160] if location else None
    line_number = location[1] if location and isinstance(location[1], int) else None
    _PLUGIN_JOURNAL.write({
        "event": "test_phase",
        "test": name,
        "id_sha256": identity,
        "phase": str(report.when)[:16],
        "outcome": str(report.outcome)[:16],
        "duration_ms": max(0, int(float(report.duration) * 1000)),
        "failure_file": filename if report.failed else None,
        "failure_line": line_number if report.failed else None,
        "utc": _utc_now(),
        "monotonic_ns": time.monotonic_ns(),
    })


def pytest_unconfigure(config: Any) -> None:
    global _PLUGIN_JOURNAL, _PLUGIN_STACK_STREAM, _PLUGIN_SIGNAL_REGISTERED, _PLUGIN_TIMER_STARTED
    if _PLUGIN_TIMER_STARTED:
        try:
            faulthandler.cancel_dump_traceback_later()
        finally:
            _PLUGIN_TIMER_STARTED = False
    if _PLUGIN_SIGNAL_REGISTERED:
        try:
            faulthandler.unregister(signal.SIGUSR1)
        finally:
            _PLUGIN_SIGNAL_REGISTERED = False
    if _PLUGIN_STACK_STREAM is not None:
        try:
            _PLUGIN_STACK_STREAM.close()
        except Exception:
            pass
        _PLUGIN_STACK_STREAM = None
    if _PLUGIN_JOURNAL is not None:
        _PLUGIN_JOURNAL.close()
        _PLUGIN_JOURNAL = None


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _write_json(path: Path, value: dict[str, Any], max_bytes: int = SNAPSHOT_LIMIT) -> None:
    raw = _json_line(value)
    if len(raw) > max_bytes:
        raw = _json_line({"outcome": "truncated", "bytes": len(raw)})
    temp_path = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temp_path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        _write_all(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temp_path, path)


def _snapshot_connection_kwargs() -> dict[str, Any]:
    if os.environ.get("SOVITE_TEST_POSTGRES_CI") != "1":
        raise ValueError("ci_opt_in_required")
    if os.environ.get("SOVITE_WORK_PROJECTION_REQUIRE_PG") != "1":
        raise ValueError("pg_required_flag_required")
    if any(key.startswith("PG") for key in os.environ):
        raise ValueError("inherited_libpq_environment_refused")

    from sqlalchemy.engine import make_url

    raw_url = os.environ.get("SOVITE_TEST_POSTGRES_URL", "")
    url = make_url(raw_url)
    if (
        url.drivername != "postgresql+psycopg"
        or url.host != "127.0.0.1"
        or url.port != 5432
        or url.username != "sovite_resource_test"
        or url.database != "sovite_resource_test"
        or url.query
        or url.password is None
    ):
        raise ValueError("non_owned_service_url_refused")
    return {
        "host": url.host,
        "port": url.port,
        "dbname": url.database,
        "user": url.username,
        "password": url.password,
        "connect_timeout": 3,
        "options": "-c statement_timeout=1000",
    }


def _run_snapshot_bounded(diag_dir: Path, env: dict[str, str], script_path: Path) -> str:
    try:
        result = subprocess.run(
            [sys.executable, str(script_path), "--snapshot"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=SNAPSHOT_TOTAL_SECONDS,
            check=False,
        )
        if result.returncode != 0:
            return "unavailable"
        try:
            outcome = json.loads((diag_dir / "activity.json").read_text()).get("outcome")
            return outcome if outcome in {"captured", "unavailable", "timeout"} else "unavailable"
        except Exception:
            return "unavailable"
    except subprocess.TimeoutExpired:
        _write_json(diag_dir / "activity.json", {"outcome": "timeout"})
        return "timeout"
    except Exception as exc:
        _write_json(diag_dir / "activity.json", {"outcome": "unavailable", "error_type": type(exc).__name__})
        return "unavailable"


def _group_has_live_processes(pgid: int) -> bool:
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            fields = stat[stat.rfind(")") + 2 :].split()
            state, process_group = fields[0], int(fields[2])
        except (FileNotFoundError, PermissionError, IndexError, ValueError):
            continue
        if process_group == pgid and state != "Z":
            return True
    return False


def _signal_group(pgid: int, sig: int) -> bool:
    if not _group_has_live_processes(pgid):
        return False
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _cleanup_group(pgid: int, leader: subprocess.Popen[bytes] | None = None) -> dict[str, Any]:
    sent_term = _signal_group(pgid, signal.SIGTERM)
    term_deadline = time.monotonic() + TERM_WAIT_SECONDS
    while _group_has_live_processes(pgid) and time.monotonic() < term_deadline:
        time.sleep(0.05)
    sent_kill = False
    if _group_has_live_processes(pgid):
        sent_kill = _signal_group(pgid, signal.SIGKILL)
    kill_deadline = time.monotonic() + KILL_WAIT_SECONDS
    while _group_has_live_processes(pgid) and time.monotonic() < kill_deadline:
        time.sleep(0.05)
    leader_reaped = True
    if leader is not None:
        try:
            leader.wait(timeout=KILL_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            leader_reaped = False
    return {
        "term_sent": sent_term,
        "kill_sent": sent_kill,
        "group_empty": not _group_has_live_processes(pgid),
        "leader_reaped": leader_reaped,
    }


def _emit_new_progress(progress_path: Path, offset: int) -> int:
    try:
        with progress_path.open("rb") as stream:
            stream.seek(offset)
            while True:
                line = stream.readline(2048)
                if not line:
                    break
                offset = stream.tell()
                try:
                    parsed = json.loads(line)
                    safe_line = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
                except Exception:
                    safe_line = '{"event":"progress_record_unavailable"}'
                sys.stdout.write(f"pg-test-progress {safe_line}\n")
                sys.stdout.flush()
        return offset
    except FileNotFoundError:
        return offset


def _write_stack_artifact(diag_dir: Path, ring: BoundedRing) -> None:
    raw = ring.bytes()
    fd = os.open(diag_dir / "stack.txt", os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        _write_all(fd, raw if raw else b"[no stack bytes captured]\n")
        os.fsync(fd)
    finally:
        os.close(fd)


def _supervise(
    diag_dir: Path,
    timeout_seconds: float,
    command: list[str] | None = None,
) -> int:
    script_path = Path(__file__).resolve()
    progress_path = diag_dir / "progress.jsonl"
    stack_read, stack_write = os.pipe()
    env = os.environ.copy()
    env["SOVITE_CI_DIAG_DIR"] = str(diag_dir)
    env["SOVITE_CI_DIAG_STACK_FD"] = str(stack_write)
    helper_dir = str(script_path.parent)
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = helper_dir + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
    if command is None:
        command = [
            sys.executable,
            "-u",
            "-m",
            "pytest",
            "tests/test_work_projection_rows.py",
            "-vv",
            "-o",
            "faulthandler_timeout=60",
            "-p",
            "pg_test_diagnostics",
        ]
    start = time.monotonic()
    _write_json(diag_dir / "summary.json", {
        "event": "supervisor_start",
        "test_timeout_seconds": timeout_seconds,
        "raw_pytest_output": "discarded",
        "utc": _utc_now(),
    }, max_bytes=16 * 1024)
    sys.stdout.write("PG16 diagnostic supervisor started; raw pytest output is discarded.\n")
    sys.stdout.flush()

    proc: subprocess.Popen[bytes] | None = None
    stack_ring = BoundedRing()
    progress_offset = 0
    stdout_drain_timed_out = False
    child_exit: int | None = None
    timed_out = False
    snapshot_result = "not_requested"
    group_cleanup: dict[str, Any] = {"group_empty": True, "leader_reaped": True}
    selector = selectors.DefaultSelector()
    try:
        proc = subprocess.Popen(
            command,
            cwd=Path.cwd(),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            start_new_session=True,
            pass_fds=(stack_write,),
        )
        os.close(stack_write)
        selector.register(proc.stdout, selectors.EVENT_READ, "pytest_output")
        selector.register(stack_read, selectors.EVENT_READ, "stack")
        deadline = start + timeout_seconds

        while proc.poll() is None:
            progress_offset = _emit_new_progress(progress_path, progress_offset)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _append_supervisor_event(diag_dir / "supervisor.jsonl", {
                    "event": "process_timeout",
                    "utc": _utc_now(),
                    "monotonic_ns": time.monotonic_ns(),
                })
                ready = diag_dir / "plugin.ready"
                if ready.is_file():
                    try:
                        os.kill(proc.pid, signal.SIGUSR1)
                    except ProcessLookupError:
                        pass
                    _, progress_offset = _drain_for(selector, stack_ring, progress_path, progress_offset, 0.25)
                snapshot_result = _run_snapshot_bounded(diag_dir, env, script_path)
                group_cleanup = _cleanup_group(proc.pid, proc)
                child_exit = 124
                break

            for key, _ in selector.select(min(0.1, max(0.0, remaining))):
                try:
                    chunk = os.read(key.fd, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    try:
                        selector.unregister(key.fileobj)
                    except Exception:
                        pass
                    if key.data == "pytest_output":
                        proc.stdout.close()
                    continue
                if key.data == "stack":
                    stack_ring.append(chunk)
                # Pytest output contains full parameter ids and exception details; discard it.

        if not timed_out:
            child_exit = proc.wait()
            progress_offset = _emit_new_progress(progress_path, progress_offset)
            stdout_drain_timed_out, progress_offset = _drain_for(
                selector, stack_ring, progress_path, progress_offset, PIPE_DRAIN_SECONDS
            )
            group_cleanup = _cleanup_group(proc.pid, proc)
            if not group_cleanup["group_empty"]:
                sys.stdout.write("PG16 diagnostic supervisor could not confirm an empty test process group.\n")
        if child_exit == 0 and (
            group_cleanup["term_sent"]
            or not group_cleanup["group_empty"]
            or not group_cleanup["leader_reaped"]
        ):
                child_exit = 125
    except Exception as exc:
        _append_supervisor_event(diag_dir / "supervisor.jsonl", {
            "event": "supervisor_error",
            "error_type": type(exc).__name__,
            "utc": _utc_now(),
        })
        if proc is not None:
            group_cleanup = _cleanup_group(proc.pid, proc)
        child_exit = child_exit if child_exit not in (None, 0) else 125
    finally:
        try:
            selector.close()
        finally:
            if proc is not None and proc.stdout is not None:
                try:
                    proc.stdout.close()
                except Exception:
                    pass
            for fd in (stack_read, stack_write):
                try:
                    os.close(fd)
                except OSError:
                    pass

    progress_offset = _emit_new_progress(progress_path, progress_offset)
    _write_stack_artifact(diag_dir, stack_ring)
    summary = {
        "event": "supervisor_finish",
        "utc": _utc_now(),
        "elapsed_ms": max(0, int((time.monotonic() - start) * 1000)),
        "test_exit_status": child_exit if child_exit is not None else 125,
        "timed_out": timed_out,
        "plugin_ready": (diag_dir / "plugin.ready").is_file(),
        "snapshot_result": snapshot_result,
        "stdout_drain_timed_out": stdout_drain_timed_out,
        "process_group": group_cleanup,
        "stack_truncated": stack_ring.truncated,
        "stack_bytes": len(stack_ring.bytes()),
        "raw_pytest_output": "discarded",
    }
    _write_json(diag_dir / "summary.json", summary, max_bytes=16 * 1024)
    sys.stdout.write(f"PG16 diagnostic supervisor finished with status {summary['test_exit_status']}.\n")
    sys.stdout.flush()
    return summary["test_exit_status"]


def _append_supervisor_event(path: Path, event: dict[str, Any]) -> None:
    journal = ProgressJournal(path)
    try:
        journal.write(event)
    finally:
        journal.close()


def _drain_for(
    selector: selectors.BaseSelector,
    stack_ring: BoundedRing,
    progress_path: Path,
    progress_offset: int,
    seconds: float,
) -> tuple[bool, int]:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        progress_offset = _emit_new_progress(progress_path, progress_offset)
        events = selector.select(min(0.05, max(0.0, deadline - time.monotonic())))
        for key, _ in events:
            try:
                chunk = os.read(key.fd, 65536)
            except OSError:
                chunk = b""
            if chunk and key.data == "stack":
                stack_ring.append(chunk)
            if not chunk:
                try:
                    selector.unregister(key.fileobj)
                except Exception:
                    pass
        if not selector.get_map():
            return False, progress_offset
    return bool(selector.get_map()), progress_offset


def _snapshot_mode() -> int:
    diag_dir = Path(os.environ["SOVITE_CI_DIAG_DIR"])
    return _capture_snapshot_to(diag_dir / "activity.json")


def _capture_snapshot_to(output_path: Path) -> int:
    try:
        import psycopg

        kwargs = _snapshot_connection_kwargs()
        with psycopg.connect(**kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(ACTIVITY_SQL)
                row = cursor.fetchone()
                result = row[0] if row else {}
        if not isinstance(result, dict):
            result = {"outcome": "unexpected_snapshot_result"}
        _write_json(output_path, {"outcome": "captured", **result})
        return 0
    except Exception as exc:  # never emit exception text, which could contain the URL
        _write_json(output_path, {"outcome": "unavailable", "error_type": type(exc).__name__})
        return 0


def _snapshot_connection_kwargs() -> dict[str, Any]:
    if os.environ.get("SOVITE_TEST_POSTGRES_CI") != "1":
        raise ValueError("ci_opt_in_required")
    if os.environ.get("SOVITE_WORK_PROJECTION_REQUIRE_PG") != "1":
        raise ValueError("pg_required_flag_required")
    if any(key.startswith("PG") for key in os.environ):
        raise ValueError("inherited_libpq_environment_refused")
    from sqlalchemy.engine import make_url

    url = make_url(os.environ.get("SOVITE_TEST_POSTGRES_URL", ""))
    if (
        url.drivername != "postgresql+psycopg"
        or url.host != "127.0.0.1"
        or url.port != 5432
        or url.username != "sovite_resource_test"
        or url.database != "sovite_resource_test"
        or url.query
        or url.password is None
    ):
        raise ValueError("non_owned_service_url_refused")
    return {
        "host": url.host,
        "port": url.port,
        "dbname": url.database,
        "user": url.username,
        "password": url.password,
        "connect_timeout": 3,
        "options": "-c statement_timeout=1000",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--diagnostics-dir")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TEST_TIMEOUT_SECONDS)
    parser.add_argument("--snapshot", action="store_true")
    args = parser.parse_args(argv)
    if args.snapshot:
        return _snapshot_mode()
    if not args.diagnostics_dir or args.timeout_seconds <= 0 or args.timeout_seconds > DEFAULT_TEST_TIMEOUT_SECONDS:
        parser.error("--diagnostics-dir and --timeout-seconds in (0, 480] are required")
    diag_dir = Path(args.diagnostics_dir)
    diag_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.environ["SOVITE_CI_DIAG_DIR"] = str(diag_dir)
    return _supervise(diag_dir, args.timeout_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
