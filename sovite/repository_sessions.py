"""Pre-launch repository-session workspace and candidate evidence primitive.

This module is deliberately not wired to job admission, the worker, a provider,
or the released ``agent-http-v1`` contract. A trusted owner resolver supplies
the repository binding; untrusted packet fields never select a path, command,
URL, or credential.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import selectors
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


PACKET_SCHEMA = "sovite-repository-session/v1"
EVIDENCE_SCHEMA = "sovite-candidate-evidence/v1"
_PACKET_FIELDS = {
    "schema_version", "binding_id", "job_id", "workspace_id", "repository_id",
    "base_sha", "allowed_areas", "deadline", "attempt_id", "generation", "fence",
}
_OWNER_FIELDS = _PACKET_FIELDS | {"mirror_root"}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SHA256_REF = re.compile(r"^sha256:[0-9a-f]{64}$")
_FENCE = re.compile(r"^[0-9a-f]{32}$")
_AREA_FORBIDDEN = set("*?[]{}!\\\x00")
_MAX_GIT_OUTPUT = 64 * 1024 * 1024
_MAX_PATCH_BYTES = 256 * 1024 * 1024


class RepositorySessionError(ValueError):
    """A repository binding or local workspace failed a closed validation."""

    def __init__(self, code: str, message: str, *, violations: tuple[str, ...] = ()):
        self.code = code
        self.violations = violations
        super().__init__(message)


@dataclass(frozen=True)
class RepositorySessionPacket:
    schema_version: str
    binding_id: str
    job_id: str
    workspace_id: str
    repository_id: str
    base_sha: str
    allowed_areas: tuple[str, ...]
    deadline: str
    attempt_id: str
    generation: str
    fence: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "binding_id": self.binding_id,
            "job_id": self.job_id,
            "workspace_id": self.workspace_id,
            "repository_id": self.repository_id,
            "base_sha": self.base_sha,
            "allowed_areas": list(self.allowed_areas),
            "deadline": self.deadline,
            "attempt_id": self.attempt_id,
            "generation": self.generation,
            "fence": self.fence,
        }


@dataclass(frozen=True)
class RepositoryWorkspace:
    packet: RepositorySessionPacket
    packet_sha256: str
    mirror_root: Path
    worktree: Path
    state: str


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise RepositorySessionError("invalid_packet", "Repository-session data is not canonical JSON") from None


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise RepositorySessionError("invalid_packet", f"{field} is invalid")
    return value


def _area(value: Any) -> str:
    if not isinstance(value, str) or not value or value.startswith("/") or any(c in _AREA_FORBIDDEN for c in value):
        raise RepositorySessionError("invalid_packet", "allowed_areas contains an invalid path")
    # Use a deliberately small, platform-independent relative-path grammar.
    parts = value.split("/")
    if any(part in {"", ".", "..", ".git"} for part in parts):
        raise RepositorySessionError("invalid_packet", "allowed_areas contains an invalid path")
    if PurePosixPath(value).as_posix() != value:
        raise RepositorySessionError("invalid_packet", "allowed_areas contains a non-canonical path")
    return value


def _deadline(value: Any) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise RepositorySessionError("invalid_packet", "deadline must be a timezone-aware ISO timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise RepositorySessionError("invalid_packet", "deadline must be a timezone-aware ISO timestamp") from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise RepositorySessionError("invalid_packet", "deadline must be a timezone-aware ISO timestamp")
    return result.astimezone(timezone.utc)


def validate_packet(value: Mapping[str, Any]) -> RepositorySessionPacket:
    """Validate the closed, versioned packet schema without resolving authority."""
    if not isinstance(value, Mapping) or set(value) != _PACKET_FIELDS:
        raise RepositorySessionError("invalid_packet", "Repository-session packet fields are invalid")
    if value["schema_version"] != PACKET_SCHEMA:
        raise RepositorySessionError("invalid_packet", "Repository-session packet version is unsupported")
    for field in ("binding_id", "job_id", "workspace_id", "repository_id", "attempt_id"):
        _identifier(value[field], field)
    if not isinstance(value["base_sha"], str) or _SHA1.fullmatch(value["base_sha"]) is None:
        raise RepositorySessionError("invalid_packet", "base_sha must be a full lowercase commit SHA")
    if not isinstance(value["generation"], str) or _SHA256_REF.fullmatch(value["generation"]) is None:
        raise RepositorySessionError("invalid_packet", "generation is invalid")
    if not isinstance(value["fence"], str) or _FENCE.fullmatch(value["fence"]) is None:
        raise RepositorySessionError("invalid_packet", "fence is invalid")
    _deadline(value["deadline"])
    areas = value["allowed_areas"]
    if not isinstance(areas, list) or len(areas) > 256:
        raise RepositorySessionError("invalid_packet", "allowed_areas must be a bounded list")
    normalized = tuple(_area(item) for item in areas)
    if len(set(normalized)) != len(normalized):
        raise RepositorySessionError("invalid_packet", "allowed_areas contains duplicates")
    packet = RepositorySessionPacket(
        schema_version=PACKET_SCHEMA,
        binding_id=value["binding_id"],
        job_id=value["job_id"],
        workspace_id=value["workspace_id"],
        repository_id=value["repository_id"],
        base_sha=value["base_sha"],
        allowed_areas=tuple(sorted(normalized)),
        deadline=value["deadline"],
        attempt_id=value["attempt_id"],
        generation=value["generation"],
        fence=value["fence"],
    )
    _canonical_json(packet.as_dict())
    return packet


def _safe_path(raw: Any, field: str) -> Path:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise RepositorySessionError("binding_unavailable", f"Trusted {field} is unavailable")
    path = Path(raw)
    try:
        if (not path.is_absolute() or str(path) != raw
                or path.resolve(strict=True) != path or not path.is_dir()):
            raise RepositorySessionError("binding_unavailable", f"Trusted {field} is not a canonical directory")
    except OSError:
        raise RepositorySessionError("binding_unavailable", f"Trusted {field} is unavailable") from None
    return path


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith("GIT_CONFIG_KEY_") or name.startswith("GIT_CONFIG_VALUE_"):
            env.pop(name, None)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                 "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_PREFIX",
                 "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
                 "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS", "GIT_EXTERNAL_DIFF"):
        env.pop(name, None)
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"})
    return env


def _consume_process(process: subprocess.Popen, *, max_bytes: int, timeout: float,
                     digest_only: bool = False) -> tuple[int, bytes | str]:
    """Read one subprocess stdout stream with an output cap and wall timeout."""
    if process.stdout is None:
        raise RepositorySessionError("git_unavailable", "Local Git output stream is unavailable")
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    consumed = 0
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    process.kill()
                    process.wait()
                    raise RepositorySessionError("git_timeout", "Local Git operation timed out")
                chunk = os.read(process.stdout.fileno(), 64 * 1024)
                if not chunk:
                    break
                consumed += len(chunk)
                if consumed > max_bytes:
                    process.kill()
                    process.wait()
                    raise RepositorySessionError("candidate_too_large", "Git subprocess output exceeds its size limit")
                if digest_only:
                    digest.update(chunk)
                else:
                    chunks.append(chunk)
        process.wait(timeout=max(0.01, deadline - time.monotonic()))
        return process.returncode, digest.hexdigest() if digest_only else b"".join(chunks)
    except RepositorySessionError:
        if process.poll() is None:
            process.kill()
            process.wait()
        raise
    except subprocess.TimeoutExpired:
        if process.poll() is None:
            process.kill()
            process.wait()
        raise RepositorySessionError("git_timeout", "Local Git operation timed out") from None
    except OSError:
        if process.poll() is None:
            process.kill()
            process.wait()
        raise RepositorySessionError("git_unavailable", "Local Git operation failed") from None
    finally:
        process.stdout.close()


def _run_git(*args: str, cwd: Path | None = None, max_bytes: int = _MAX_GIT_OUTPUT,
             timeout: float = 30, digest_only: bool = False) -> tuple[int, bytes | str]:
    try:
        process = subprocess.Popen(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd,
                                   env=_git_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL)
    except OSError:
        raise RepositorySessionError("git_unavailable", "Local Git operation failed") from None
    return _consume_process(process, max_bytes=max_bytes, timeout=timeout, digest_only=digest_only)


def _git(*args: str, cwd: Path | None = None) -> bytes:
    returncode, output = _run_git(*args, cwd=cwd)
    if returncode:
        raise RepositorySessionError("git_invalid", "Local Git repository operation was rejected")
    assert isinstance(output, bytes)
    return output


def _git_sha256(*args: str, cwd: Path | None = None, max_bytes: int = _MAX_PATCH_BYTES) -> str:
    """Hash a potentially large Git patch without buffering it in memory."""
    returncode, output = _run_git(*args, cwd=cwd, max_bytes=max_bytes, digest_only=True)
    if returncode:
        raise RepositorySessionError("git_invalid", "Local Git repository operation was rejected")
    assert isinstance(output, str)
    return output


def _mirror_git(mirror: Path, *args: str) -> bytes:
    return _git(f"--git-dir={mirror}", *args)


def _reject_checkout_filters(mirror: Path) -> None:
    """Refuse repository-configured smudge/process filters before checkout."""
    returncode, output = _run_git(f"--git-dir={mirror}", "config", "--get-regexp",
                                  r"^filter\..*\.(smudge|process)$", max_bytes=1024 * 1024,
                                  timeout=10)
    assert isinstance(output, bytes)
    if returncode == 0 and output:
        raise RepositorySessionError("checkout_filter_denied", "Repository mirror has configured checkout filters")
    if returncode != 1:
        raise RepositorySessionError("binding_unavailable", "Repository mirror configuration could not be checked")


def _canonical_root(path: Path, *, create: bool) -> Path:
    if not path.is_absolute():
        raise RepositorySessionError("state_root_invalid", "Workspace state root must be absolute")
    try:
        if create:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        root = path.resolve(strict=True)
        if root != path or not root.is_dir():
            raise RepositorySessionError("state_root_invalid", "Workspace state root must be a canonical directory")
        info = root.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise RepositorySessionError("state_root_invalid", "Workspace state root ownership or permissions are unsafe")
    except OSError:
        raise RepositorySessionError("state_root_invalid", "Workspace state root is unavailable") from None
    return root


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes, *, exclusive: bool = False) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if exclusive:
        # Called under the per-attempt flock. Publication is atomic for
        # cooperating writers; same-UID malicious writers are outside this
        # local foundation's trust boundary.
        for stale in path.parent.glob(f".{path.name}.tmp-*"):
            try:
                stale.unlink()
            except OSError:
                raise RepositorySessionError("state_corrupt", "Stale candidate receipt temporary cannot be removed") from None
        if path.exists() or path.is_symlink():
            raise RepositorySessionError("immutable_receipt_exists", "An immutable candidate receipt already exists")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        offset = 0
        while offset < len(view):
            written = os.write(fd, view[offset:])
            if written <= 0:
                raise OSError("short repository-session state write")
            offset += written
        os.fsync(fd)
        os.close(fd)
        fd = -1
        if exclusive and (path.exists() or path.is_symlink()):
            raise RepositorySessionError("immutable_receipt_exists", "An immutable candidate receipt already exists")
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    finally:
        if fd >= 0:
            os.close(fd)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077:
            raise RepositorySessionError("state_corrupt", "Repository-session state has unsafe ownership or permissions")
        if info.st_size > 256 * 1024:
            raise RepositorySessionError("state_corrupt", "Repository-session state exceeds its size limit")
        value = json.loads(path.read_text("utf-8"), object_pairs_hook=_unique_pairs,
                           parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except RepositorySessionError:
        raise
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        raise RepositorySessionError("state_corrupt", "Repository-session state is unavailable") from None


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


class RepositorySessionManager:
    """Manage isolated Git worktrees for trusted, owner-issued packets.

    ``binding_resolver`` is a trusted caller interface. It may be a callable
    or expose ``resolve_repository_binding(binding_id)``. It returns the exact
    packet fields plus a canonical local bare mirror path. No packet field can
    select a command, URL, credential, or local path.
    """

    def __init__(self, state_root: str | os.PathLike, binding_resolver: Any):
        self.state_root = _canonical_root(Path(state_root), create=True)
        self.binding_resolver = binding_resolver
        self.records_root = self.state_root / "records"
        self.worktrees_root = self.state_root / "worktrees"
        self.receipts_root = self.state_root / "receipts"
        for directory in (self.records_root, self.worktrees_root, self.receipts_root):
            directory.mkdir(mode=0o700, exist_ok=True)
            info = directory.stat()
            if (directory.resolve(strict=True) != directory or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077):
                raise RepositorySessionError("state_root_invalid", "Workspace state subdirectory is unsafe")

    def _resolve_binding(self, packet: RepositorySessionPacket, *, now: float | None = None,
                         check_deadline: bool = True) -> tuple[dict[str, Any], Path]:
        resolver = self.binding_resolver
        try:
            owner_value = (resolver.resolve_repository_binding(packet.binding_id)
                           if hasattr(resolver, "resolve_repository_binding") else resolver(packet.binding_id))
        except Exception:
            raise RepositorySessionError("binding_unavailable", "Owner repository binding could not be resolved") from None
        if not isinstance(owner_value, Mapping) or set(owner_value) != _OWNER_FIELDS:
            raise RepositorySessionError("binding_unavailable", "Owner repository binding has an invalid shape")
        owner_packet_value = {key: owner_value[key] for key in _PACKET_FIELDS}
        owner_packet = validate_packet(owner_packet_value)
        if owner_packet.as_dict() != packet.as_dict():
            raise RepositorySessionError("binding_mismatch", "Packet does not match the owner-issued repository binding")
        current = time.time() if now is None else now
        if (type(current) not in {int, float} or not math.isfinite(current)
                or (check_deadline and current >= _deadline(packet.deadline).timestamp())):
            raise RepositorySessionError("binding_expired", "Repository-session deadline has expired")
        mirror = _safe_path(owner_value["mirror_root"], "mirror root")
        bare = _mirror_git(mirror, "rev-parse", "--is-bare-repository").strip()
        if bare != b"true":
            raise RepositorySessionError("binding_unavailable", "Trusted repository root is not a bare Git mirror")
        _reject_checkout_filters(mirror)
        _mirror_git(mirror, "cat-file", "-e", f"{packet.base_sha}^{{commit}}")
        return owner_packet.as_dict(), mirror

    @staticmethod
    def _packet_key(packet: RepositorySessionPacket) -> str:
        return hashlib.sha256(_canonical_json(packet.as_dict())).hexdigest()

    @staticmethod
    def _attempt_key(packet: RepositorySessionPacket) -> str:
        identity = {"schema_version": PACKET_SCHEMA, "binding_id": packet.binding_id,
                    "workspace_id": packet.workspace_id, "repository_id": packet.repository_id,
                    "job_id": packet.job_id, "attempt_id": packet.attempt_id}
        return hashlib.sha256(_canonical_json(identity)).hexdigest()

    def _lock(self, key: str):
        lock_path = self.records_root / f"{key}.lock"
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except OSError:
            raise RepositorySessionError("state_corrupt", "Repository-session lock is unavailable") from None
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
            os.close(fd)
            raise RepositorySessionError("state_corrupt", "Repository-session lock is unsafe")
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def _record_path(self, key: str) -> Path:
        return self.records_root / f"{key}.json"

    def _worktree_path(self, key: str) -> Path:
        return self.worktrees_root / key[:2] / key

    def _verify_worktree(self, path: Path, mirror: Path) -> None:
        try:
            if path.resolve(strict=True) != path or not path.is_dir():
                raise RepositorySessionError("worktree_mismatch", "Attempt worktree path is unsafe")
        except OSError:
            raise RepositorySessionError("worktree_missing", "Attempt worktree is unavailable") from None
        top = Path(_git("-C", str(path), "rev-parse", "--show-toplevel").decode().strip())
        common = Path(_git("-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip())
        if top.resolve(strict=True) != path or common.resolve(strict=True) != mirror:
            raise RepositorySessionError("worktree_mismatch", "Attempt worktree is not attached to its trusted repository mirror")

    def _read_record(self, path: Path) -> dict[str, Any] | None:
        if not path.exists() and not path.is_symlink():
            return None
        return _read_json(path)

    def prepare(self, packet_value: Mapping[str, Any], *, now: float | None = None) -> RepositoryWorkspace:
        packet = validate_packet(packet_value)
        _, mirror = self._resolve_binding(packet, now=now)
        attempt_key = self._attempt_key(packet)
        packet_sha256 = self._packet_key(packet)
        worktree = self._worktree_path(attempt_key)
        record_path = self._record_path(attempt_key)
        fd = self._lock(attempt_key)
        try:
            record = self._read_record(record_path)
            identity = {"schema_version": PACKET_SCHEMA, "packet": packet.as_dict(),
                        "packet_sha256": packet_sha256, "attempt_key": attempt_key,
                        "mirror_root": str(mirror),
                        "worktree": str(worktree)}
            if record is None:
                if worktree.exists() or worktree.is_symlink():
                    raise RepositorySessionError("orphan_worktree", "Unrecorded attempt worktree already exists")
                record = {**identity, "state": "creating", "candidate": None}
                _atomic_write(record_path, _canonical_json(record))
            else:
                for name, expected in identity.items():
                    if record.get(name) != expected:
                        raise RepositorySessionError("attempt_identity_conflict", "Attempt identity is already bound to different workspace state")
                if record.get("state") not in {"creating", "prepared", "candidate"}:
                    raise RepositorySessionError("state_corrupt", "Attempt workspace state is invalid")

            if record["state"] == "creating":
                if not worktree.exists():
                    worktree.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    parent_info = worktree.parent.stat()
                    if (worktree.parent.resolve(strict=True) != worktree.parent
                            or parent_info.st_uid != os.getuid()
                            or stat.S_IMODE(parent_info.st_mode) & 0o077):
                        raise RepositorySessionError("worktree_mismatch", "Attempt worktree parent is unsafe")
                    _git(f"--git-dir={mirror}", "worktree", "add", "--detach", str(worktree), packet.base_sha)
                self._verify_worktree(worktree, mirror)
                head = _git("-C", str(worktree), "rev-parse", "HEAD").decode().strip()
                if head != packet.base_sha:
                    raise RepositorySessionError("base_mismatch", "Attempt worktree does not start at its authorized base")
                record["state"] = "prepared"
                _atomic_write(record_path, _canonical_json(record))
            else:
                self._verify_worktree(worktree, mirror)
                head = _git("-C", str(worktree), "rev-parse", "HEAD").decode().strip()
                if record["state"] == "prepared" and head != packet.base_sha:
                    receipt_path = self.receipts_root / f"{attempt_key}.json"
                    receipt = self._read_record(receipt_path)
                    if receipt is None:
                        raise RepositorySessionError("base_mismatch", "Prepared attempt worktree base changed")
                    current = self._candidate_snapshot(packet, worktree, mirror, now=now)
                    if self._candidate_core(receipt) != self._candidate_core(current):
                        raise RepositorySessionError("candidate_changed", "Candidate evidence recovery did not match the worktree")
                    record["state"], record["candidate"] = "candidate", receipt
                    _atomic_write(record_path, _canonical_json(record))
                if record["state"] == "candidate":
                    candidate = record.get("candidate")
                    current = self._candidate_snapshot(packet, worktree, mirror, now=now)
                    if not isinstance(candidate, dict) or self._candidate_core(candidate) != self._candidate_core(current):
                        raise RepositorySessionError("candidate_changed", "Recorded candidate evidence no longer matches the worktree")
            return RepositoryWorkspace(packet, packet_sha256, mirror, worktree, record["state"])
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @staticmethod
    def _within_area(path: str, areas: tuple[str, ...]) -> bool:
        return any(path == area or path.startswith(area + "/") for area in areas)

    @staticmethod
    def _git_paths(data: bytes) -> list[str]:
        if not data:
            return []
        try:
            return [part.decode("utf-8", "strict") for part in data.split(b"\0") if part]
        except UnicodeDecodeError:
            raise RepositorySessionError("invalid_git_path", "Candidate contains a path outside the supported UTF-8 path set") from None

    def _dirty_paths(self, worktree: Path) -> tuple[list[str], list[str]]:
        tracked = self._git_paths(_git("-C", str(worktree), "diff", "--no-renames", "--name-only", "-z", "HEAD"))
        untracked = self._git_paths(_git("-C", str(worktree), "ls-files", "--others", "--exclude-standard", "-z"))
        ignored = self._git_paths(_git("-C", str(worktree), "ls-files", "--others", "--ignored", "--exclude-standard", "-z"))
        return sorted(set(tracked)), sorted(set(untracked + ignored))

    def _symlink_violations(self, worktree: Path, tip: str, paths: list[str]) -> list[str]:
        violations = []
        entries = _git("-C", str(worktree), "ls-tree", "-r", "-z", tip).split(b"\0")
        tracked_symlinks = set()
        try:
            for entry in entries:
                if not entry:
                    continue
                metadata, raw_path = entry.split(b"\t", 1)
                mode = metadata.split(b" ", 1)[0]
                if mode == b"120000":
                    tracked_symlinks.add(raw_path.decode("utf-8", "strict"))
        except (ValueError, UnicodeDecodeError):
            raise RepositorySessionError("invalid_git_path", "Candidate tree has an unsupported path encoding") from None
        for path in paths:
            parts = path.split("/")
            for index in range(1, len(parts) + 1):
                prefix = "/".join(parts[:index])
                fs_path = worktree.joinpath(*parts[:index])
                try:
                    if prefix in tracked_symlinks or fs_path.is_symlink():
                        violations.append(f"symlink_path:{prefix}")
                except OSError:
                    violations.append(f"path_unavailable:{prefix}")
        return sorted(set(violations))

    @staticmethod
    def _candidate_core(value: Mapping[str, Any]) -> dict[str, Any]:
        return {key: value[key] for key in (
            "schema_version", "packet_sha256", "job_id", "workspace_id", "repository_id",
            "attempt_id", "generation", "fence", "base_sha", "tip_sha", "patch_sha256",
            "changed_paths", "working_tree_clean", "scope_violations", "within_deadline",
            "publication_eligible",
        )}

    def _candidate_snapshot(self, packet: RepositorySessionPacket, worktree: Path,
                            mirror: Path, *, now: float | None = None) -> dict[str, Any]:
        self._verify_worktree(worktree, mirror)
        base = packet.base_sha
        tip = _git("-C", str(worktree), "rev-parse", "HEAD").decode().strip()
        # The command has no output; retain its exit status to record a
        # non-descendant candidate as evidence rather than launching anything.
        ancestry_status, ancestry_output = _run_git("-C", str(worktree), "merge-base", "--is-ancestor", base, tip,
                                                    max_bytes=1024)
        assert isinstance(ancestry_output, bytes)
        violations: list[str] = []
        if ancestry_status != 0:
            violations.append("candidate_not_descendant_of_base")
        committed = self._git_paths(_git("-C", str(worktree), "diff", "--no-renames", "--name-only", "-z", base, tip, "--"))
        dirty, loose = self._dirty_paths(worktree)
        paths = sorted(set(committed + dirty + loose))
        for path in paths:
            if not self._within_area(path, packet.allowed_areas):
                violations.append(f"out_of_scope:{path}")
        for path in dirty:
            violations.append(f"uncommitted_change:{path}")
        for path in loose:
            violations.append(f"untracked_or_ignored:{path}")
        violations.extend(self._symlink_violations(worktree, tip, paths))
        patch_sha256 = _git_sha256("-C", str(worktree), "diff", "--no-ext-diff", "--no-textconv", "--binary",
                                   "--no-renames", base, tip, "--")
        now_value = time.time() if now is None else now
        if type(now_value) not in {int, float} or not math.isfinite(now_value):
            raise RepositorySessionError("invalid_time", "Candidate observation time is invalid")
        within_deadline = now_value <= _deadline(packet.deadline).timestamp()
        if not within_deadline:
            violations.append("deadline_expired_before_candidate_capture")
        clean = not dirty and not loose
        violations = sorted(set(violations))
        receipt = {
            "schema_version": EVIDENCE_SCHEMA,
            "packet_sha256": self._packet_key(packet),
            "job_id": packet.job_id,
            "workspace_id": packet.workspace_id,
            "repository_id": packet.repository_id,
            "attempt_id": packet.attempt_id,
            "generation": packet.generation,
            "fence": packet.fence,
            "base_sha": base,
            "tip_sha": tip,
            "patch_sha256": patch_sha256,
            "changed_paths": paths,
            "working_tree_clean": clean,
            "scope_violations": violations,
            "within_deadline": within_deadline,
            "publication_eligible": clean and within_deadline and not violations,
        }
        return receipt

    def capture_candidate(self, packet_value: Mapping[str, Any], *, now: float | None = None) -> dict[str, Any]:
        packet = validate_packet(packet_value)
        _, mirror = self._resolve_binding(packet, now=None, check_deadline=False)
        attempt_key = self._attempt_key(packet)
        packet_sha256 = self._packet_key(packet)
        fd = self._lock(attempt_key)
        try:
            record_path = self._record_path(attempt_key)
            record = self._read_record(record_path)
            worktree = self._worktree_path(attempt_key)
            if (not record or record.get("packet_sha256") != packet_sha256
                    or record.get("attempt_key") != attempt_key or record.get("mirror_root") != str(mirror)):
                raise RepositorySessionError("attempt_unavailable", "Prepared attempt workspace is unavailable")
            if record.get("state") not in {"prepared", "candidate"}:
                raise RepositorySessionError("attempt_unavailable", "Attempt workspace is not prepared")
            candidate = self._candidate_snapshot(packet, worktree, mirror, now=now)
            receipt_path = self.receipts_root / f"{attempt_key}.json"
            previous = self._read_record(receipt_path)
            if previous is not None:
                if self._candidate_core(previous) != self._candidate_core(candidate):
                    raise RepositorySessionError("candidate_changed", "Candidate differs from its immutable recorded evidence",
                                                 violations=tuple(candidate["scope_violations"]))
                if record.get("state") == "prepared":
                    record["state"], record["candidate"] = "candidate", previous
                    _atomic_write(record_path, _canonical_json(record))
                return previous
            candidate["captured_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            _atomic_write(receipt_path, _canonical_json(candidate), exclusive=True)
            record["state"] = "candidate"
            record["candidate"] = candidate
            _atomic_write(record_path, _canonical_json(record))
            return candidate
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
