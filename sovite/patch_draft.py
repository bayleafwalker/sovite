"""Dormant, bounded patch-draft primitive for an answer-only model adapter.

This module does not start a repository process, merge a branch, or enable a
production mutable profile. A model may return replacements for regular text
files included in a small exact-base context. The owner applies those bytes
with fixed Git operations inside an existing RepositorySessionManager
worktree, then asks that manager to capture the immutable candidate receipt.

Paid calls are refused unless the injected adapter explicitly supports a
server-side hard cost ceiling. The shipped AdapterRunner does not report
provider cost or enforce such a ceiling, so it is intentionally not a usable
production PatchDraftCall. A missing or ambiguous usage receipt is persisted
as unknown and blocks the attempt.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Protocol

from .adapters import AdapterRunner, RuntimeFailure
from .repository_sessions import (
    RepositorySessionError,
    RepositorySessionManager,
    RepositoryWorkspace,
    _git,
    validate_packet,
)


PATCH_SCHEMA = "sovite-patch-draft/v1"
MAX_CONTEXT_FILES = 24
MAX_CONTEXT_FILE_BYTES = 24 * 1024
MAX_CONTEXT_BYTES = 192 * 1024
MAX_DRAFT_BYTES = 256 * 1024
MAX_DRAFT_FILE_BYTES = 48 * 1024
_SHA256 = set("0123456789abcdef")


class PatchDraftError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class PatchDraftCall(Protocol):
    """Trusted adapter boundary, separate from the model-produced answer."""

    supports_hard_cost_ceiling: bool

    async def __call__(self, prompt: str, *, request_id: str,
                       max_cost_usd: float) -> Mapping[str, Any]: ...


class NativeAdapterAnswerOnlyCall:
    """Thin wrapper around the existing answer-only native adapter.

    It deliberately advertises no hard cost ceiling and returns no fabricated
    provider usage. BoundedPatchDraftExecutor therefore refuses to launch it.
    A future qualified adapter must replace this wrapper and provide real
    provider-side cost limits plus a trusted usage receipt.
    """

    supports_hard_cost_ceiling = False

    def __init__(self, runner: AdapterRunner, recipe: Mapping[str, Any], job_id: str):
        self.runner = runner
        self.recipe = dict(recipe)
        self.job_id = job_id

    async def __call__(self, prompt: str, *, request_id: str,
                       max_cost_usd: float) -> Mapping[str, Any]:
        result = await self.runner.run_native(self.recipe, prompt, self.job_id, isolated=True)
        answer = result.get("answer") if isinstance(result, Mapping) else None
        return {"answer": answer}


@dataclass(frozen=True)
class ContextFile:
    path: str
    sha256: str
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256, "content": self.content}


def _safe_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise PatchDraftError("patch_path_invalid", "Patch path is not a canonical repository path")
    path = PurePosixPath(value)
    if (path.is_absolute() or path.as_posix() != value
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
            or any(part in {"", ".", ".."} or part.lower() == ".git" for part in value.split("/"))):
        raise PatchDraftError("patch_path_invalid", "Patch path is not a canonical repository path")
    return value


def _within_area(path: str, areas: tuple[str, ...]) -> bool:
    return any(path == area or path.startswith(area + "/") for area in areas)


def _overlaps_protected(path: str, protected: tuple[str, ...]) -> bool:
    return any(path == item or path.startswith(item + "/") or item.startswith(path + "/")
               for item in protected)


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_regular_file(root: Path, relative: str, maximum: int) -> bytes:
    """Open each path component without following symlinks and read one blob."""
    parts = relative.split("/")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    directory_flags = flags | getattr(os, "O_DIRECTORY", 0)
    try:
        parent_fd = os.open(root, directory_flags)
        try:
            for part in parts[:-1]:
                next_fd = os.open(part, directory_flags, dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd = next_fd
            fd = os.open(parts[-1], flags | getattr(os, "O_NONBLOCK", 0), dir_fd=parent_fd)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
                    raise PatchDraftError("source_file_unsafe", "Source context contains a non-regular or oversized file")
                output = bytearray()
                while True:
                    chunk = os.read(fd, min(64 * 1024, maximum + 1 - len(output)))
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > maximum:
                        raise PatchDraftError("source_file_unsafe", "Source context file exceeds its size limit")
                return bytes(output)
            finally:
                os.close(fd)
        finally:
            os.close(parent_fd)
    except PatchDraftError:
        raise
    except OSError:
        raise PatchDraftError("source_file_unsafe", "Source context path contains a symlink or unavailable component") from None


def _write_regular_file(root: Path, relative: str, data: bytes) -> None:
    """Atomically replace an existing regular file without following links."""
    parts = relative.split("/")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        parent_fd = os.open(root, directory_flags)
        try:
            for part in parts[:-1]:
                next_fd = os.open(part, directory_flags, dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd = next_fd
            current_fd = os.open(parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                                 | getattr(os, "O_NONBLOCK", 0), dir_fd=parent_fd)
            try:
                info = os.fstat(current_fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise PatchDraftError("patch_target_unsafe", "Patch target is not a regular file")
                mode = stat.S_IMODE(info.st_mode) & 0o777
            finally:
                os.close(current_fd)
            temp = f".sovite-draft-{secrets.token_hex(16)}.tmp"
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                         mode or 0o600, dir_fd=parent_fd)
            try:
                view = memoryview(data)
                offset = 0
                while offset < len(view):
                    written = os.write(fd, view[offset:])
                    if written <= 0:
                        raise OSError("short draft file write")
                    offset += written
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temp, parts[-1], src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except PatchDraftError:
        raise
    except OSError:
        raise PatchDraftError("patch_target_unsafe", "Patch target or parent directory is unsafe") from None


def build_context(manager: RepositorySessionManager, workspace: RepositoryWorkspace,
                  packet_value: Mapping[str, Any], *, protected_paths: tuple[str, ...] = ()) -> tuple[ContextFile, ...]:
    """Build a bounded context from regular text blobs at the exact base tip."""
    packet = validate_packet(packet_value)
    if workspace.packet.as_dict() != packet.as_dict():
        raise PatchDraftError("attempt_binding_mismatch", "Patch context does not match the prepared repository attempt")
    try:
        manager._verify_worktree(workspace.worktree, workspace.mirror_root)
        head = _git("-C", str(workspace.worktree), "rev-parse", "HEAD").decode("ascii").strip()
        if head != packet.base_sha:
            raise PatchDraftError("attempt_base_changed", "Patch context requires the original attempt base")
        dirty = _git("-C", str(workspace.worktree), "status", "--porcelain=v1", "--untracked-files=all")
        if dirty:
            raise PatchDraftError("attempt_workspace_dirty", "Patch context requires a clean attempt worktree")
        tree = _git("-C", str(workspace.worktree), "ls-tree", "-r", "-z", "--full-tree", packet.base_sha)
    except RepositorySessionError as exc:
        raise PatchDraftError(exc.code, str(exc)) from None

    protected = tuple(_safe_relative_path(path) for path in protected_paths)
    files: list[ContextFile] = []
    total = 0
    for entry in tree.split(b"\x00"):
        if not entry:
            continue
        try:
            meta, raw_path = entry.split(b"\t", 1)
            mode, kind, object_id = meta.decode("ascii").split(" ", 2)
            path = _safe_relative_path(raw_path.decode("utf-8", "strict"))
        except (ValueError, UnicodeDecodeError):
            raise PatchDraftError("source_tree_invalid", "Source tree contains an unsupported path") from None
        if not _within_area(path, packet.allowed_areas) or _overlaps_protected(path, protected):
            continue
        if mode not in {"100644", "100755"} or kind != "blob":
            raise PatchDraftError("source_file_unsafe", "Allowed source area contains a symlink, submodule, or special Git entry")
        if len(files) >= MAX_CONTEXT_FILES:
            raise PatchDraftError("source_context_too_large", "Allowed source context exceeds its file count limit")
        try:
            content = _read_regular_file(workspace.worktree, path, MAX_CONTEXT_FILE_BYTES)
            text = content.decode("utf-8", "strict")
        except UnicodeDecodeError:
            # Binary source is omitted and cannot be selected by the model.
            continue
        if b"\x00" in content:
            continue
        total += len(content)
        if total > MAX_CONTEXT_BYTES:
            raise PatchDraftError("source_context_too_large", "Allowed source context exceeds its byte limit")
        files.append(ContextFile(path, _hash(content), text))
    if not files:
        raise PatchDraftError("source_context_empty", "No bounded regular text files are available to draft against")
    return tuple(files)


def parse_draft(raw: Any, context: tuple[ContextFile, ...], *, protected_paths: tuple[str, ...] = ()) -> list[tuple[str, str, bytes]]:
    if not isinstance(raw, str):
        raise PatchDraftError("draft_response_invalid", "Patch draft must be a JSON string")
    try:
        encoded = raw.encode("utf-8", "strict")
    except UnicodeEncodeError:
        raise PatchDraftError("draft_response_invalid", "Patch draft is not valid UTF-8") from None
    if len(encoded) > MAX_DRAFT_BYTES:
        raise PatchDraftError("draft_response_too_large", "Patch draft exceeds its byte limit")

    def unique_pairs(pairs):
        output = {}
        for key, value in pairs:
            if key in output:
                raise ValueError("duplicate field")
            output[key] = value
        return output

    try:
        document = json.loads(raw, object_pairs_hook=unique_pairs,
                              parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
    except (ValueError, RecursionError):
        raise PatchDraftError("draft_response_invalid", "Patch draft is not strict JSON") from None
    if not isinstance(document, dict) or set(document) != {"schema_version", "files"} or document["schema_version"] != PATCH_SCHEMA:
        raise PatchDraftError("draft_schema_invalid", "Patch draft does not match sovite-patch-draft/v1")
    rows = document["files"]
    if not isinstance(rows, list) or not rows or len(rows) > MAX_CONTEXT_FILES:
        raise PatchDraftError("draft_schema_invalid", "Patch draft must contain a bounded nonempty file list")
    context_by_path = {item.path: item for item in context}
    protected = tuple(_safe_relative_path(path) for path in protected_paths)
    seen: set[str] = set()
    validated: list[tuple[str, str, bytes]] = []
    total = 0
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"path", "base_sha256", "content"}:
            raise PatchDraftError("draft_schema_invalid", "Patch file fields do not match the strict schema")
        path = _safe_relative_path(row["path"])
        if path not in context_by_path or path in seen or _overlaps_protected(path, protected):
            raise PatchDraftError("draft_path_denied", "Draft path is outside the exact source context or immutable set")
        seen.add(path)
        expected = context_by_path[path].sha256
        supplied = row["base_sha256"]
        if (not isinstance(supplied, str) or len(supplied) != 64
                or any(char not in _SHA256 for char in supplied) or supplied != expected):
            raise PatchDraftError("draft_precondition_failed", "Draft does not bind the exact source file digest")
        content = row["content"]
        if not isinstance(content, str) or "\x00" in content:
            raise PatchDraftError("draft_content_invalid", "Draft content must be NUL-free UTF-8 text")
        try:
            blob = content.encode("utf-8", "strict")
        except UnicodeEncodeError:
            raise PatchDraftError("draft_content_invalid", "Draft content is not valid UTF-8") from None
        if len(blob) > MAX_DRAFT_FILE_BYTES:
            raise PatchDraftError("draft_content_too_large", "Draft file exceeds its byte limit")
        total += len(blob)
        if total > MAX_DRAFT_BYTES:
            raise PatchDraftError("draft_response_too_large", "Draft replacements exceed their total byte limit")
        validated.append((path, expected, blob))
    return validated


def _mark_call(budget: Any, call_id: str, state: str, **fields: Any) -> None:
    persist = getattr(budget, "persist", None)
    if not callable(persist):
        raise RuntimeFailure("repository_usage_state_unavailable")

    def mutate(run: dict[str, Any]) -> None:
        events = run.setdefault("draft_calls", [])
        existing = next((row for row in events if row.get("call_id") == call_id), None)
        if state == "calling" and existing is not None:
            raise RuntimeFailure("external_execution_uncertain")
        if existing is None:
            existing = {"call_id": call_id}
            events.append(existing)
        existing.update({"state": state, **fields})
        if state == "unknown":
            run["usage_status"] = "unknown"

    persist(mutate)


class BoundedPatchDraftExecutor:
    """One answer-only call, strict replacement data, fixed Git commit, receipt.

    ``call`` must be an owner-composed adapter which applies a provider-side
    hard cost cap and returns trusted actual usage. The ordinary native
    AdapterRunner has neither capability and therefore cannot be called by this
    executor. Tests inject an inert deterministic implementation.
    """

    def __init__(self, manager: RepositorySessionManager, packet: Mapping[str, Any],
                 call: PatchDraftCall, *, protected_paths: tuple[str, ...] = (),
                 ):
        self.manager = manager
        self.packet = validate_packet(packet)
        self.call = call
        self.protected_paths = tuple(_safe_relative_path(path) for path in protected_paths)

    async def execute(self, goal: str, *, budget: Any, authorization_check) -> dict[str, Any]:
        packet = self.packet
        authorization_check()
        workspace = self.manager.prepare(packet.as_dict())
        context = build_context(self.manager, workspace, packet.as_dict(), protected_paths=self.protected_paths)
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 8_000:
            raise PatchDraftError("goal_invalid", "Patch draft goal is invalid")
        if getattr(self.call, "supports_hard_cost_ceiling", False) is not True:
            raise RuntimeFailure("patch_draft_cost_boundary_unqualified")
        maximum = getattr(budget, "maximum_usd", None)
        if type(maximum) not in {int, float} or not math.isfinite(maximum) or maximum <= 0:
            raise RuntimeFailure("repository_cost_reservation_invalid")
        if not callable(getattr(budget, "persist", None)):
            raise RuntimeFailure("repository_usage_state_unavailable")
        call_id = "draft-" + hashlib.sha256(
            f"{packet.job_id}\0{packet.attempt_id}".encode("utf-8")).hexdigest()[:48]
        prompt = json.dumps({
            "instruction": (
                "Return only one JSON object with schema_version='sovite-patch-draft/v1' and files[]. "
                "Each file must replace one provided regular text file, preserving its exact path and base_sha256. "
                "Do not return commands, diffs, deletions, renames, binary data, or paths absent from context."
            ),
            "schema_version": PATCH_SCHEMA,
            "goal": goal,
            "source_files": [row.as_dict() for row in context],
        }, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        if len(prompt.encode("utf-8")) > MAX_CONTEXT_BYTES + 16_000:
            raise PatchDraftError("source_context_too_large", "Patch prompt exceeds its byte limit")

        budget.reserve_cost(call_id, float(maximum))
        _mark_call(budget, call_id, "calling", attempt_id=packet.attempt_id,
                   prompt_sha256=_hash(prompt.encode("utf-8")))
        authorization_check()
        try:
            response = await self.call(prompt, request_id=call_id, max_cost_usd=float(maximum))
        except BaseException as exc:
            _mark_call(budget, call_id, "unknown", reason="adapter_outcome_ambiguous")
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit, GeneratorExit)):
                raise
            if isinstance(exc, RuntimeFailure):
                raise RuntimeFailure("external_execution_uncertain") from None
            raise RuntimeFailure("external_execution_uncertain") from None
        if not isinstance(response, Mapping):
            _mark_call(budget, call_id, "unknown", reason="usage_receipt_missing")
            raise RuntimeFailure("external_execution_uncertain")
        answer = response.get("answer")
        actual = response.get("usage_usd")
        provider_request_id = response.get("provider_request_id")
        if type(actual) not in {int, float} or not math.isfinite(actual) or actual < 0:
            _mark_call(budget, call_id, "unknown", reason="usage_receipt_invalid")
            raise RuntimeFailure("external_execution_uncertain")
        try:
            budget.settle_cost(call_id, float(actual))
        except RuntimeFailure as exc:
            if exc.code == "external_execution_uncertain":
                # RepositoryRunBudget raises this only after persisting an
                # observed charge that crossed its reservation or ceiling.
                _mark_call(budget, call_id, "settled", actual_usd=float(actual),
                           provider_request_id=provider_request_id if isinstance(provider_request_id, str) else None)
                raise
            _mark_call(budget, call_id, "unknown", reason="cost_settlement_failed",
                       observed_usage_usd=float(actual),
                       provider_request_id=provider_request_id if isinstance(provider_request_id, str) else None)
            raise RuntimeFailure("external_execution_uncertain") from None
        _mark_call(budget, call_id, "settled", actual_usd=float(actual),
                   provider_request_id=provider_request_id if isinstance(provider_request_id, str) else None)
        if (not isinstance(answer, str) or not isinstance(provider_request_id, str)
                or not provider_request_id or len(provider_request_id) > 256):
            raise RuntimeFailure("external_execution_uncertain")
        authorization_check()
        replacements = parse_draft(answer, context, protected_paths=self.protected_paths)
        current = _git("-C", str(workspace.worktree), "rev-parse", "HEAD").decode("ascii").strip()
        if current != packet.base_sha:
            raise PatchDraftError("attempt_base_changed", "Attempt base changed before draft application")
        dirty = _git("-C", str(workspace.worktree), "status", "--porcelain=v1", "--untracked-files=all")
        if dirty:
            raise PatchDraftError("attempt_workspace_dirty", "Attempt worktree changed before draft application")
        for path, expected_sha256, content in replacements:
            current_content = _read_regular_file(workspace.worktree, path, MAX_CONTEXT_FILE_BYTES)
            if _hash(current_content) != expected_sha256:
                raise PatchDraftError("draft_precondition_failed", "Patch target changed while the model was drafting")
            _write_regular_file(workspace.worktree, path, content)
        paths = [path for path, _, _ in replacements]
        try:
            changed = set(self.manager._git_paths(_git("-C", str(workspace.worktree), "diff",
                "--no-renames", "--name-only", "-z", "HEAD")))
            loose = set(self.manager._git_paths(_git("-C", str(workspace.worktree), "ls-files",
                "--others", "--exclude-standard", "-z")))
            ignored = set(self.manager._git_paths(_git("-C", str(workspace.worktree), "ls-files",
                "--others", "--ignored", "--exclude-standard", "-z")))
            if changed != set(paths) or loose or ignored:
                raise PatchDraftError("attempt_workspace_changed", "Only the selected draft files may change in the attempt worktree")
            _git("-C", str(workspace.worktree), "add", "--", *paths)
            _git("-C", str(workspace.worktree), "-c", "user.name=Sovite patch draft",
                 "-c", "user.email=patch-draft@sovite.invalid", "-c", "commit.gpgsign=false",
                 "commit", "--no-verify", "--only", "-m", "Sovite bounded patch draft", "--", *paths)
            tip = _git("-C", str(workspace.worktree), "rev-parse", "HEAD").decode("ascii").strip()
            parent = _git("-C", str(workspace.worktree), "rev-parse", "HEAD^").decode("ascii").strip()
            if parent != packet.base_sha or tip == packet.base_sha:
                raise PatchDraftError("draft_commit_invalid", "Draft commit is not a direct child of the authorized base")
            committed = set(self.manager._git_paths(_git("-C", str(workspace.worktree), "diff",
                "--no-renames", "--name-only", "-z", packet.base_sha, tip, "--")))
            if committed != set(paths) or any(_overlaps_protected(path, self.protected_paths) for path in committed):
                raise PatchDraftError("draft_commit_invalid", "Draft commit contains a protected or undeclared path")
            evidence = self.manager.capture_candidate(packet.as_dict())
        except RepositorySessionError as exc:
            raise PatchDraftError(exc.code, str(exc)) from None
        if not evidence["publication_eligible"]:
            raise PatchDraftError("repository_candidate_ineligible", "Draft candidate violates repository scope")
        receipt_path = self.manager.receipts_root / f"{self.manager._attempt_key(packet)}.json"
        receipt_sha256 = _hash(receipt_path.read_bytes())
        return {"schema_version": "sovite-patch-draft-result/v1",
                "base_sha": evidence["base_sha"], "tip_sha": evidence["tip_sha"],
                "patch_sha256": evidence["patch_sha256"],
                "changed_paths": evidence["changed_paths"],
                "evidence_sha256": receipt_sha256}

