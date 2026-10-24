"""Small process-local recovery journal for the opt-in remote worker.

The owner remains authoritative. This file records only one unresolved key and
its fence metadata so a restarted process cannot silently claim more work.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import re
import stat
import tempfile
from pathlib import Path


MAX_BYTES = 4096
MAX_DEPTH = 8
MAX_NODES = 100
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_GENERATION = re.compile(r"^sha256:[0-9a-f]{64}$")
_FENCE = re.compile(r"^[0-9a-f]{32}$")
_KINDS = {"pull", "start", "renew", "handback", "complete"}
_PHASES = {"pending_pull", "reserved", "pending_start", "started", "pending_renew",
           "pending_handback", "pending_complete", "manual"}
_RESULTS = {"idle", "reserved", "started", "already_started", "renewed", "handed_back",
            "completed", "failed", "cancelled", "retry_wait", "blocked"}
_FIELDS = {"schema_version", "instance_id", "phase", "operation_id", "operation_kind",
           "job_id", "generation", "fence", "expected_result_states", "lease_until", "deadline"}


class JournalError(RuntimeError):
    """The local recovery record cannot be safely used."""


def _valid_record(record):
    if not isinstance(record, dict) or set(record) != _FIELDS:
        return False
    if record["schema_version"] != 1 or type(record["schema_version"]) is not int:
        return False
    if not isinstance(record["instance_id"], str) or _ID.fullmatch(record["instance_id"]) is None:
        return False
    if (not isinstance(record["phase"], str) or record["phase"] not in _PHASES
            or not isinstance(record["operation_kind"], str) or record["operation_kind"] not in _KINDS):
        return False
    if not isinstance(record["operation_id"], str) or _ID.fullmatch(record["operation_id"]) is None:
        return False
    for field, pattern in (("job_id", _ID), ("generation", _GENERATION), ("fence", _FENCE)):
        value = record[field]
        if value is not None and (not isinstance(value, str) or pattern.fullmatch(value) is None):
            return False
    states = record["expected_result_states"]
    if (not isinstance(states, list) or len(states) > len(_RESULTS)
            or any(not isinstance(state, str) or state not in _RESULTS for state in states)
            or len(set(states)) != len(states)):
        return False
    for field in ("lease_until", "deadline"):
        value = record[field]
        if value is not None:
            if type(value) is int:
                if abs(value) > 1_000_000_000_000:
                    return False
            elif type(value) is float:
                if not math.isfinite(value) or abs(value) > 1_000_000_000_000:
                    return False
            else:
                return False
    if record["operation_kind"] == "pull":
        if record["phase"] == "pending_pull" and any(record[k] is not None for k in ("job_id", "generation", "fence")):
            return False
        if record["phase"] not in {"pending_pull", "manual"} and any(
                record[k] is None for k in ("job_id", "generation", "fence")):
            return False
    elif record["phase"] != "manual" and any(record[k] is None for k in ("job_id", "generation", "fence")):
        return False
    expected_kind = {
        "pending_pull": "pull", "reserved": "pull", "pending_start": "start",
        "pending_renew": "renew", "pending_handback": "handback",
        "pending_complete": "complete",
    }.get(record["phase"])
    if expected_kind is not None and record["operation_kind"] != expected_kind:
        return False
    return True


def _pairs_no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError


def _preflight(source: str):
    depth = nodes = index = 0
    while index < len(source):
        char = source[index]
        if char in " \t\r\n,:":
            index += 1
        elif char in "{[":
            depth += 1
            nodes += 1
            index += 1
            if depth > MAX_DEPTH:
                raise ValueError
        elif char in "}]":
            depth -= 1
            index += 1
            if depth < 0:
                raise ValueError
        elif char == '"':
            nodes += 1
            index += 1
            while index < len(source):
                current = source[index]
                if current == "\\":
                    index += 2
                    continue
                index += 1
                if current == '"':
                    break
            else:
                raise ValueError
        else:
            nodes += 1
            while index < len(source) and source[index] not in " \t\r\n,:{}[]":
                index += 1
        if nodes > MAX_NODES:
            raise ValueError
    if depth:
        raise ValueError


class ExecutionJournal:
    """Locked atomic single-record journal; never stores execution payloads."""

    def __init__(self, path: str | os.PathLike):
        raw = Path(path)
        if not raw.is_absolute():
            raise JournalError("Execution journal path must be absolute")
        parent = raw.parent
        try:
            if not parent.is_dir() or parent.resolve(strict=True) != parent:
                raise JournalError("Execution journal directory must be an existing non-symlink directory")
        except OSError:
            raise JournalError("Execution journal directory is unavailable") from None
        self.path = raw
        self._lock_path = Path(str(raw) + ".lock")
        self._lock_fd = self._open_lock()
        try:
            self._read()
        except Exception:
            self.close()
            raise

    @classmethod
    def inspect(cls, path: str | os.PathLike):
        """Read one stable journal snapshot without creating or changing files."""
        raw = Path(path)
        if not raw.is_absolute():
            raise JournalError("Execution journal path must be absolute")
        try:
            if not raw.parent.is_dir() or raw.parent.resolve(strict=True) != raw.parent:
                raise JournalError("Execution journal directory must be an existing non-symlink directory")
        except OSError:
            raise JournalError("Execution journal directory is unavailable") from None

        lock_path = Path(str(raw) + ".lock")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(lock_path, flags)
        except OSError:
            raise JournalError("Execution journal lock is unavailable for read-only inspection") from None
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1):
                raise JournalError("Execution journal lock file has unsafe ownership or permissions")
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                raise JournalError("Execution journal is active; inspect it after the worker stops") from None
            reader = object.__new__(cls)
            reader.path = raw
            return reader._read()
        finally:
            os.close(fd)

    def _open_lock(self):
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self._lock_path, flags, 0o600)
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1):
                os.close(fd)
                raise JournalError("Execution journal lock file has unsafe ownership or permissions")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                raise JournalError("Execution journal is already locked by another process") from None
            return fd
        except JournalError:
            raise
        except OSError:
            raise JournalError("Execution journal lock could not be opened safely") from None

    def close(self):
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    @staticmethod
    def _check_file(fd):
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1
                or info.st_size > MAX_BYTES):
            raise JournalError("Execution journal has unsafe ownership, permissions, or size")

    def _read(self):
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags | getattr(os, "O_NONBLOCK", 0))
        except FileNotFoundError:
            return None
        except OSError:
            raise JournalError("Execution journal could not be opened safely") from None
        try:
            self._check_file(fd)
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise JournalError("Execution journal exceeds its size limit")
            source = raw.decode("utf-8")
            _preflight(source)
            record = json.loads(source, object_pairs_hook=_pairs_no_duplicates,
                                parse_constant=_reject_constant)
            if not _valid_record(record):
                raise ValueError
            return record
        except (UnicodeDecodeError, ValueError, RecursionError, OverflowError):
            raise JournalError("Execution journal is invalid or exceeds structural limits") from None
        finally:
            os.close(fd)

    def load(self):
        return self._read()

    @staticmethod
    def _encode(record):
        if not _valid_record(record):
            raise JournalError("Execution journal record is invalid")
        try:
            raw = json.dumps(record, allow_nan=False, ensure_ascii=True,
                             separators=(",", ":")).encode("ascii")
        except (TypeError, ValueError, RecursionError):
            raise JournalError("Execution journal record is invalid") from None
        if len(raw) > MAX_BYTES:
            raise JournalError("Execution journal exceeds its size limit")
        return raw

    def write(self, record):
        raw = self._encode(record)
        # Recheck destination immediately before replacement; never replace an
        # unsafe file planted after process startup.
        try:
            current = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                              | getattr(os, "O_NONBLOCK", 0))
        except FileNotFoundError:
            current = None
        except OSError:
            raise JournalError("Execution journal destination is unsafe") from None
        if current is not None:
            try:
                self._check_file(current)
            finally:
                os.close(current)
        fd, temp_name = tempfile.mkstemp(prefix=".sovite-execution-", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.path)
            self._fsync_dir()
        except OSError:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise JournalError("Execution journal could not be written atomically") from None

    def clear(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_NONBLOCK", 0))
        except FileNotFoundError:
            return
        except OSError:
            raise JournalError("Execution journal destination is unsafe") from None
        try:
            self._check_file(fd)
        finally:
            os.close(fd)
        try:
            os.unlink(self.path)
            self._fsync_dir()
        except OSError:
            raise JournalError("Execution journal could not be cleared safely") from None

    def _fsync_dir(self):
        fd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
