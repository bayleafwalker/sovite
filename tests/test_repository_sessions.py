from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sovite import repository_sessions as sessions


def git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.stdout.strip()


def _repo(tmp_path: Path, name: str) -> tuple[Path, Path, str]:
    bare = tmp_path / f"{name}.git"
    source = tmp_path / name
    git("init", "--bare", str(bare))
    git("init", str(source))
    git("config", "user.name", "Repository session test", cwd=source)
    git("config", "user.email", "repo-session@example.invalid", cwd=source)
    (source / "src").mkdir()
    (source / "src" / "old.txt").write_text("old\n")
    (source / "src" / "delete.txt").write_text("delete\n")
    (source / "README.md").write_text("initial\n")
    git("add", ".", cwd=source)
    git("commit", "-m", "initial", cwd=source)
    base = git("rev-parse", "HEAD", cwd=source)
    git("remote", "add", "origin", str(bare), cwd=source)
    git("push", "origin", "HEAD", cwd=source)
    git("--git-dir", str(bare), "symbolic-ref", "HEAD", "refs/heads/main")
    git("--git-dir", str(bare), "update-ref", "refs/heads/main", base)
    return bare, source, base


class Resolver:
    def __init__(self, binding: dict):
        self.binding = binding
        self.calls = []

    def resolve_repository_binding(self, binding_id: str):
        self.calls.append(binding_id)
        if binding_id != self.binding["binding_id"]:
            raise KeyError(binding_id)
        return dict(self.binding)


def _binding(bare: Path, base: str, **overrides) -> dict:
    binding = {
        "schema_version": sessions.PACKET_SCHEMA,
        "binding_id": "binding-01",
        "job_id": "job-01",
        "workspace_id": "workspace-01",
        "repository_id": "repo-01",
        "base_sha": base,
        "allowed_areas": ["src"],
        "deadline": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "attempt_id": "attempt-01",
        "generation": "sha256:" + "a" * 64,
        "fence": "b" * 32,
        "mirror_root": str(bare),
    }
    binding.update(overrides)
    return binding


def _packet(binding: dict) -> dict:
    return {field: binding[field] for field in sessions._PACKET_FIELDS}


def _commit(worktree: Path, message: str) -> str:
    git("config", "user.name", "Repository session test", cwd=worktree)
    git("config", "user.email", "repo-session@example.invalid", cwd=worktree)
    git("add", "-A", cwd=worktree)
    git("commit", "-m", message, cwd=worktree)
    return git("rev-parse", "HEAD", cwd=worktree)


def test_closed_packet_rejects_paths_commands_urls_and_credential_payloads(tmp_path):
    bare, _, base = _repo(tmp_path, "closed")
    value = _packet(_binding(bare, base))
    assert sessions.validate_packet(value).base_sha == base
    for field, bad in (("mirror_root", str(bare)), ("command", ["sh", "-c", "echo bad"]),
                       ("repository_url", "https://example.invalid/repo"),
                       ("credentials", {"token": "must-not-be-here"})):
        candidate = dict(value, **{field: bad})
        with pytest.raises(sessions.RepositorySessionError, match="fields"):
            sessions.validate_packet(candidate)
    for area in ("../escape", "/absolute", ".git/config", "src/../escape", "src/*"):
        with pytest.raises(sessions.RepositorySessionError):
            sessions.validate_packet(dict(value, allowed_areas=[area]))


def test_owner_binding_must_match_packet_and_bare_mirror_must_contain_base(tmp_path):
    bare, _, base = _repo(tmp_path, "binding")
    binding = _binding(bare, base)
    manager = sessions.RepositorySessionManager(tmp_path / "state", Resolver(binding))
    wrong = dict(_packet(binding), repository_id="some-other-repo")
    with pytest.raises(sessions.RepositorySessionError, match="does not match"):
        manager.prepare(wrong)
    wrong_base = "0" * 40
    binding["base_sha"] = wrong_base
    manager = sessions.RepositorySessionManager(tmp_path / "state2", Resolver(binding))
    with pytest.raises(sessions.RepositorySessionError) as error:
        manager.prepare(_packet(binding))
    assert error.value.code == "git_invalid"


def test_checkout_filters_configured_in_mirror_are_rejected_before_worktree_add(tmp_path):
    bare, source, _ = _repo(tmp_path, "filter")
    (source / ".gitattributes").write_text("*.txt filter=probe\n")
    git("add", ".gitattributes", cwd=source)
    git("commit", "-m", "declare filter", cwd=source)
    base = git("rev-parse", "HEAD", cwd=source)
    git("push", "origin", "HEAD", cwd=source)
    git("--git-dir", str(bare), "update-ref", "refs/heads/main", base)
    marker = tmp_path / "filter-ran"
    git("--git-dir", str(bare), "config", "filter.probe.smudge", f"touch {marker}; cat")
    binding = _binding(bare, base)
    manager = sessions.RepositorySessionManager(tmp_path / "state", Resolver(binding))
    with pytest.raises(sessions.RepositorySessionError) as error:
        manager.prepare(_packet(binding))
    assert error.value.code == "checkout_filter_denied"
    assert not marker.exists()


def test_prepare_is_idempotent_across_manager_restart_and_does_not_change_source(tmp_path):
    bare, source, base = _repo(tmp_path, "idempotent")
    sibling_bare, sibling_source, sibling_base = _repo(tmp_path, "sibling")
    binding = _binding(bare, base)
    packet = _packet(binding)
    source_tree = git("rev-parse", "HEAD^{tree}", cwd=source)
    sibling_tree = git("rev-parse", "HEAD^{tree}", cwd=sibling_source)
    manager = sessions.RepositorySessionManager(tmp_path / "state", Resolver(binding))
    first = manager.prepare(packet)
    (first.worktree / "src" / "dirty.txt").write_text("keep on recovery\n")
    recovered = sessions.RepositorySessionManager(tmp_path / "state", Resolver(binding)).prepare(packet)
    assert recovered.worktree == first.worktree
    assert recovered.state == "prepared"
    assert (recovered.worktree / "src" / "dirty.txt").read_text() == "keep on recovery\n"
    assert git("rev-parse", "HEAD^{tree}", cwd=source) == source_tree
    assert git("--git-dir", str(bare), "rev-parse", "refs/heads/main") == base
    assert git("--git-dir", str(sibling_bare), "rev-parse", "refs/heads/main") == sibling_base
    assert git("rev-parse", "HEAD^{tree}", cwd=sibling_source) == sibling_tree


def test_crash_after_worktree_creation_recovers_same_attempt(tmp_path, monkeypatch):
    bare, _, base = _repo(tmp_path, "crash-window")
    binding = _binding(bare, base)
    packet = _packet(binding)
    manager = sessions.RepositorySessionManager(tmp_path / "state", Resolver(binding))
    original = sessions._atomic_write
    writes = 0

    def crash_on_prepared(path, data, *, exclusive=False):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("simulated crash after git worktree add")
        return original(path, data, exclusive=exclusive)

    monkeypatch.setattr(sessions, "_atomic_write", crash_on_prepared)
    with pytest.raises(OSError):
        manager.prepare(packet)
    monkeypatch.setattr(sessions, "_atomic_write", original)
    restarted = sessions.RepositorySessionManager(tmp_path / "state", Resolver(binding)).prepare(packet)
    assert restarted.state == "prepared"
    assert git("-C", str(restarted.worktree), "rev-parse", "HEAD") == base


def test_wrong_base_or_reused_identity_is_rejected_without_resetting_worktree(tmp_path):
    bare, _, base = _repo(tmp_path, "wrong-base")
    binding = _binding(bare, base)
    manager = sessions.RepositorySessionManager(tmp_path / "state", Resolver(binding))
    prepared = manager.prepare(_packet(binding))
    (prepared.worktree / "src" / "local.txt").write_text("preserve\n")
    _commit(prepared.worktree, "unexpected base movement")
    # A prepared attempt that has left its exact base cannot be silently reset.
    with pytest.raises(sessions.RepositorySessionError) as error:
        manager.prepare(_packet(binding))
    assert error.value.code == "base_mismatch"
    assert (prepared.worktree / "src" / "local.txt").exists()

    # Same attempt reference cannot be reused with another fence or base.
    changed_fence = dict(_packet(binding), fence="c" * 32)
    with pytest.raises(sessions.RepositorySessionError):
        manager.prepare(changed_fence)


def test_candidate_evidence_tracks_add_rename_delete_and_is_immutable(tmp_path):
    bare, source, base = _repo(tmp_path, "candidate")
    binding = _binding(bare, base)
    packet = _packet(binding)
    manager = sessions.RepositorySessionManager(tmp_path / "state", Resolver(binding))
    workspace = manager.prepare(packet)
    (workspace.worktree / "src" / "new.txt").write_text("new\n")
    git("-C", str(workspace.worktree), "mv", "src/old.txt", "src/renamed.txt")
    git("-C", str(workspace.worktree), "rm", "src/delete.txt")
    tip = _commit(workspace.worktree, "candidate edits")
    receipt = manager.capture_candidate(packet)
    assert receipt["base_sha"] == base
    assert receipt["tip_sha"] == tip
    assert receipt["patch_sha256"] == hashlib.sha256(
        subprocess.run(["git", "-C", str(workspace.worktree), "diff", "--no-ext-diff", "--no-textconv", "--binary",
                        "--no-renames", base, tip, "--"], check=True, stdout=subprocess.PIPE).stdout
    ).hexdigest()
    assert receipt["changed_paths"] == ["src/delete.txt", "src/new.txt", "src/old.txt", "src/renamed.txt"]
    assert receipt["publication_eligible"] is True
    assert sessions.RepositorySessionManager(tmp_path / "state", Resolver(binding)).capture_candidate(packet) == receipt
    (workspace.worktree / "src" / "new.txt").write_text("mutated after receipt\n")
    with pytest.raises(sessions.RepositorySessionError) as error:
        manager.capture_candidate(packet)
    assert error.value.code == "candidate_changed"
    assert git("rev-parse", "HEAD", cwd=source) == base


def test_candidate_receipt_write_crash_is_recovered_without_replacing_evidence(tmp_path, monkeypatch):
    bare, _, base = _repo(tmp_path, "receipt-crash")
    binding = _binding(bare, base)
    packet = _packet(binding)
    manager = sessions.RepositorySessionManager(tmp_path / "state", Resolver(binding))
    workspace = manager.prepare(packet)
    (workspace.worktree / "src" / "new.txt").write_text("candidate\n")
    tip = _commit(workspace.worktree, "candidate")
    original = sessions._atomic_write
    writes = 0

    def crash_after_receipt(path, data, *, exclusive=False):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("simulated crash after immutable receipt write")
        return original(path, data, exclusive=exclusive)

    monkeypatch.setattr(sessions, "_atomic_write", crash_after_receipt)
    with pytest.raises(OSError):
        manager.capture_candidate(packet)
    monkeypatch.setattr(sessions, "_atomic_write", original)
    restarted = sessions.RepositorySessionManager(tmp_path / "state", Resolver(binding))
    recovered = restarted.prepare(packet)
    assert recovered.state == "candidate"
    receipt = restarted.capture_candidate(packet)
    assert receipt["tip_sha"] == tip
    assert receipt["changed_paths"] == ["src/new.txt"]


def test_mid_temp_receipt_write_crash_leaves_no_partial_receipt_and_recovers(tmp_path, monkeypatch):
    bare, _, base = _repo(tmp_path, "partial-receipt")
    binding = _binding(bare, base)
    packet = _packet(binding)
    manager = sessions.RepositorySessionManager(tmp_path / "state", Resolver(binding))
    workspace = manager.prepare(packet)
    (workspace.worktree / "src" / "new.txt").write_text("candidate\n")
    _commit(workspace.worktree, "candidate")
    attempt_key = manager._attempt_key(sessions.validate_packet(packet))
    receipt_path = manager.receipts_root / f"{attempt_key}.json"
    write = sessions.os.write
    calls = 0

    def interrupt_mid_write(fd, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            return write(fd, data[:max(1, len(data) // 2)])
        raise KeyboardInterrupt("simulated process interruption during temp-file write")

    monkeypatch.setattr(sessions.os, "write", interrupt_mid_write)
    with pytest.raises(KeyboardInterrupt):
        manager.capture_candidate(packet)
    assert not receipt_path.exists()
    assert list(manager.receipts_root.glob(f".{receipt_path.name}.tmp-*"))

    monkeypatch.setattr(sessions.os, "write", write)
    recovered = sessions.RepositorySessionManager(tmp_path / "state", Resolver(binding)).capture_candidate(packet)
    assert recovered["tip_sha"] == git("-C", str(workspace.worktree), "rev-parse", "HEAD")
    assert json.loads(receipt_path.read_text()) == recovered
    assert not list(manager.receipts_root.glob(f".{receipt_path.name}.tmp-*"))


def test_git_metadata_output_is_capped_and_slow_git_child_is_terminated(tmp_path):
    bare, _, base = _repo(tmp_path, "bounded-git")
    git("--git-dir", str(bare), "config", "test.large", "x" * 2048)
    with pytest.raises(sessions.RepositorySessionError) as error:
        sessions._run_git(f"--git-dir={bare}", "config", "--list", max_bytes=128)
    assert error.value.code == "candidate_too_large"

    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
    with pytest.raises(sessions.RepositorySessionError) as error:
        sessions._consume_process(process, max_bytes=128, timeout=0.05)
    assert error.value.code == "git_timeout"
    assert process.poll() is not None


def test_scope_symlink_and_untracked_diagnostics_are_bound_into_receipt(tmp_path):
    bare, _, base = _repo(tmp_path, "scope")
    outside = tmp_path / "outside"
    outside.mkdir()
    binding = _binding(bare, base)
    packet = _packet(binding)
    manager = sessions.RepositorySessionManager(tmp_path / "state", Resolver(binding))
    workspace = manager.prepare(packet)
    (workspace.worktree / "outside.txt").write_text("out of area\n")
    link = workspace.worktree / "src" / "escape"
    link.symlink_to(outside, target_is_directory=True)
    _commit(workspace.worktree, "unsafe candidate")
    (workspace.worktree / "outside-untracked.txt").write_text("not committed\n")
    receipt = manager.capture_candidate(packet)
    assert "out_of_scope:outside.txt" in receipt["scope_violations"]
    assert "symlink_path:src/escape" in receipt["scope_violations"]
    assert "untracked_or_ignored:outside-untracked.txt" in receipt["scope_violations"]
    assert receipt["publication_eligible"] is False
    assert "outside-untracked.txt" in receipt["changed_paths"]
    assert not (outside / "written-by-session").exists()


def test_deadline_expired_evidence_is_retained_but_not_publishable(tmp_path):
    bare, _, base = _repo(tmp_path, "expired")
    deadline = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    binding = _binding(bare, base, deadline=deadline)
    packet = _packet(binding)
    manager = sessions.RepositorySessionManager(tmp_path / "state", Resolver(binding))
    workspace = manager.prepare(packet)
    receipt = manager.capture_candidate(packet, now=datetime.now(timezone.utc).timestamp() + 10)
    assert receipt["within_deadline"] is False
    assert receipt["publication_eligible"] is False
    assert "deadline_expired_before_candidate_capture" in receipt["scope_violations"]


def test_time_passing_cannot_change_an_immutable_receipt_deadline_decision(tmp_path):
    bare, _, base = _repo(tmp_path, "receipt-deadline")
    deadline_dt = datetime.now(timezone.utc) + timedelta(hours=1)
    deadline = deadline_dt.isoformat().replace("+00:00", "Z")
    binding = _binding(bare, base, deadline=deadline)
    packet = _packet(binding)
    manager = sessions.RepositorySessionManager(tmp_path / "state", Resolver(binding))
    workspace = manager.prepare(packet)
    (workspace.worktree / "src" / "new.txt").write_text("candidate\n")
    _commit(workspace.worktree, "candidate")
    captured = manager.capture_candidate(packet, now=deadline_dt.timestamp() - 1)
    assert captured["within_deadline"] is True
    with pytest.raises(sessions.RepositorySessionError) as error:
        manager.capture_candidate(packet, now=deadline_dt.timestamp() + 1)
    assert error.value.code == "candidate_changed"
    attempt_key = manager._attempt_key(sessions.validate_packet(packet))
    stored = json.loads((manager.receipts_root / f"{attempt_key}.json").read_text())
    assert stored["within_deadline"] is True
    assert stored["publication_eligible"] is True
