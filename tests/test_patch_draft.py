from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

from sovite.adapters import RuntimeFailure
from sovite.patch_draft import (
    PATCH_SCHEMA,
    BoundedPatchDraftExecutor,
    NativeAdapterAnswerOnlyCall,
    PatchDraftError,
    build_context,
    parse_draft,
)
from sovite import patch_draft as patch_draft_module
from sovite.repository_sessions import PACKET_SCHEMA, RepositorySessionManager
from sovite.repository_runs import RepositoryRunBudget


def git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.stdout.strip()


def repo(tmp_path: Path, *, symlink: bool = False):
    bare = tmp_path / "mirror.git"
    source = tmp_path / "source"
    git("init", "--bare", str(bare))
    git("init", str(source))
    git("config", "user.name", "Patch draft test", cwd=source)
    git("config", "user.email", "patch-draft@example.invalid", cwd=source)
    (source / "src").mkdir()
    (source / "src" / "main.py").write_text("print('before')\n")
    (source / "src" / "oracle.txt").write_text("immutable oracle\n")
    if symlink:
        (source / "src" / "link.py").symlink_to("main.py")
    git("add", ".", cwd=source)
    git("commit", "-m", "base", cwd=source)
    base = git("rev-parse", "HEAD", cwd=source)
    git("remote", "add", "origin", str(bare), cwd=source)
    git("push", "origin", "HEAD", cwd=source)
    git("--git-dir", str(bare), "symbolic-ref", "HEAD", "refs/heads/main")
    git("--git-dir", str(bare), "update-ref", "refs/heads/main", base)
    packet = {
        "schema_version": PACKET_SCHEMA,
        "binding_id": "binding-1", "job_id": "job-1", "workspace_id": "workspace-1",
        "repository_id": "repo-1", "base_sha": base, "allowed_areas": ["src"],
        "deadline": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "attempt_id": "attempt-1", "generation": "sha256:" + "a" * 64, "fence": "b" * 32,
    }
    binding = {**packet, "mirror_root": str(bare)}

    class Resolver:
        def resolve_repository_binding(self, binding_id):
            if binding_id != packet["binding_id"]:
                raise KeyError(binding_id)
            return binding

    manager = RepositorySessionManager(tmp_path / "state", Resolver())
    workspace = manager.prepare(packet)
    return bare, source, packet, manager, workspace


class Budget:
    def __init__(self, maximum=1.0):
        self.maximum_usd = maximum
        self.used_usd = 0.0
        self.reservations = []
        self.active_slots = []
        self.peak_concurrency = 0
        self.state = {"budget_reservations": [], "usage_usd": 0.0,
                      "draft_calls": [], "usage_status": "known"}

    def reserve_cost(self, call_id, amount):
        existing = next((row for row in self.state["budget_reservations"]
                         if row["call_id"] == call_id), None)
        if existing:
            if existing["reserved_usd"] == amount and existing["state"] == "reserved":
                return
            raise RuntimeFailure("repository_budget_operation_conflict")
        self.reservations.append({"call_id": call_id, "reserved_usd": amount, "state": "reserved"})
        self.state["budget_reservations"].append({"call_id": call_id, "reserved_usd": amount,
                                                   "state": "reserved", "actual_usd": None})

    def settle_cost(self, call_id, amount):
        self.used_usd += amount
        self.state["usage_usd"] += amount
        for row in self.state["budget_reservations"]:
            if row["call_id"] == call_id:
                row.update(state="settled", actual_usd=amount)
        if amount > next(row["reserved_usd"] for row in self.reservations if row["call_id"] == call_id):
            raise RuntimeFailure("external_execution_uncertain")

    def persist(self, mutate):
        mutate(self.state)


class FakeCall:
    supports_hard_cost_ceiling = True

    def __init__(self, answer, usage=0.2, error=None, provider_request_id="provider-request-1"):
        self.answer, self.usage, self.error = answer, usage, error
        self.provider_request_id = provider_request_id
        self.calls = []

    async def __call__(self, prompt, *, request_id, max_cost_usd):
        self.calls.append((prompt, request_id, max_cost_usd))
        if self.error:
            raise self.error
        response = {"answer": self.answer, "provider_request_id": self.provider_request_id}
        if self.usage is not None:
            response["usage_usd"] = self.usage
        return response


def draft_json(packet, manager, workspace, content):
    source = (workspace.worktree / "src" / "main.py").read_bytes()
    document = {"schema_version": PATCH_SCHEMA, "files": [{
        "path": "src/main.py", "base_sha256": hashlib.sha256(source).hexdigest(),
        "content": content,
    }]}
    return json.dumps(document)


def test_patch_context_is_exact_base_bounded_and_omits_immutable_oracles(tmp_path):
    bare, source, packet, manager, workspace = repo(tmp_path)
    context = build_context(manager, workspace, packet, protected_paths=("src/oracle.txt",))
    assert [row.path for row in context] == ["src/main.py"]
    assert context[0].content == "print('before')\n"
    assert git("rev-parse", "HEAD", cwd=source) == packet["base_sha"]
    assert git("--git-dir", str(bare), "rev-parse", "refs/heads/main") == packet["base_sha"]


@pytest.mark.parametrize("path", ["../escape", ".git/config", "src/.git/config", "/absolute", "src\\escape"])
def test_patch_paths_reject_escape_git_and_noncanonical_paths(tmp_path, path):
    _, _, packet, manager, workspace = repo(tmp_path)
    context = build_context(manager, workspace, packet)
    draft = json.dumps({"schema_version": PATCH_SCHEMA, "files": [{
        "path": path, "base_sha256": context[0].sha256, "content": "bad\n",
    }]})
    with pytest.raises(PatchDraftError):
        parse_draft(draft, context)


def test_patch_draft_rejects_oracle_duplicate_hash_precondition_and_commands(tmp_path):
    _, _, packet, manager, workspace = repo(tmp_path)
    context = build_context(manager, workspace, packet, protected_paths=("src/oracle.txt",))
    for document in (
        {"schema_version": PATCH_SCHEMA, "files": [{"path": "src/oracle.txt", "base_sha256": "0" * 64, "content": "x"}]},
        {"schema_version": PATCH_SCHEMA, "files": [{"path": "src/main.py", "base_sha256": "0" * 64, "content": "x"}]},
        {"schema_version": PATCH_SCHEMA, "files": [{"path": "src/main.py", "base_sha256": context[0].sha256, "content": "x"}], "command": ["sh"]},
    ):
        with pytest.raises(PatchDraftError):
            parse_draft(json.dumps(document), context, protected_paths=("src/oracle.txt",))


def test_versioned_patch_contract_matches_strict_runtime_shape(tmp_path):
    _, _, packet, manager, workspace = repo(tmp_path)
    context = build_context(manager, workspace, packet)
    document = {"schema_version": PATCH_SCHEMA, "files": [{
        "path": context[0].path, "base_sha256": context[0].sha256, "content": "updated\n",
    }]}
    contract = json.loads((Path(__file__).resolve().parents[1]
                           / "sovite/execution_contract/patch-draft-v1/contract.json").read_text())
    validator = Draft202012Validator(contract)
    validator.validate(document)
    with pytest.raises(ValidationError):
        validator.validate(document | {"command": ["sh", "-c", "bad"]})


def test_context_rejects_symlinks_inside_allowed_source_area(tmp_path):
    _, _, packet, manager, workspace = repo(tmp_path, symlink=True)
    with pytest.raises(PatchDraftError, match="symlink"):
        build_context(manager, workspace, packet)


def test_context_rejects_assume_unchanged_worktree_divergence(tmp_path):
    _, _, packet, manager, workspace = repo(tmp_path)
    git("update-index", "--assume-unchanged", "src/main.py", cwd=workspace.worktree)
    (workspace.worktree / "src" / "main.py").write_text("changed behind Git status\n")
    assert git("status", "--porcelain", cwd=workspace.worktree) == ""
    with pytest.raises(PatchDraftError, match="differs from the authorized Git base"):
        build_context(manager, workspace, packet)


def test_filesystem_without_no_follow_support_fails_closed(tmp_path, monkeypatch):
    _, _, packet, manager, workspace = repo(tmp_path)
    context = build_context(manager, workspace, packet)
    monkeypatch.delattr(patch_draft_module.os, "O_NOFOLLOW")
    with pytest.raises(PatchDraftError, match="POSIX directory-relative no-follow"):
        patch_draft_module._read_regular_file(workspace.worktree, context[0].path, 4096)


def test_bounded_patch_draft_applies_only_context_file_and_captures_receipt(tmp_path):
    bare, source, packet, manager, workspace = repo(tmp_path)
    call = FakeCall(draft_json(packet, manager, workspace, "print('after')\n"))
    executor = BoundedPatchDraftExecutor(manager, packet, call)
    budget = Budget()
    result = asyncio.run(executor.execute("Change the output", budget=budget, authorization_check=lambda: None))
    assert result["base_sha"] == packet["base_sha"]
    assert result["changed_paths"] == ["src/main.py"]
    assert result["tip_sha"] != packet["base_sha"]
    assert len(result["evidence_sha256"]) == 64
    assert (workspace.worktree / "src" / "main.py").read_text() == "print('after')\n"
    assert (workspace.worktree / "src" / "oracle.txt").read_text() == "immutable oracle\n"
    assert git("rev-parse", "HEAD", cwd=source) == packet["base_sha"]
    assert git("--git-dir", str(bare), "rev-parse", "refs/heads/main") == packet["base_sha"]
    assert budget.used_usd == 0.2
    assert budget.state["draft_calls"][0]["state"] == "settled"


def test_native_answer_only_adapter_is_not_called_without_hard_cost_ceiling(tmp_path):
    _, _, packet, manager, _workspace = repo(tmp_path)

    class Runner:
        called = False

        async def run_native(self, *args, **kwargs):
            self.called = True
            return {"answer": "{}"}

    runner = Runner()
    call = NativeAdapterAnswerOnlyCall(runner, {}, "job-1")
    executor = BoundedPatchDraftExecutor(manager, packet, call)
    with pytest.raises(RuntimeFailure, match="cost_boundary_unqualified"):
        asyncio.run(executor.execute("Change output", budget=Budget(), authorization_check=lambda: None))
    assert runner.called is False


def test_missing_usage_receipt_is_recorded_unknown_and_blocks_attempt(tmp_path):
    _, _, packet, manager, workspace = repo(tmp_path)
    call = FakeCall(draft_json(packet, manager, workspace, "print('after')\n"), usage=None)
    budget = Budget()
    executor = BoundedPatchDraftExecutor(manager, packet, call)
    with pytest.raises(RuntimeFailure, match="external_execution_uncertain"):
        asyncio.run(executor.execute("Change output", budget=budget, authorization_check=lambda: None))
    assert call.calls
    assert budget.state["usage_status"] == "unknown"
    assert budget.state["draft_calls"][0]["state"] == "unknown"
    assert budget.state["budget_reservations"][0]["state"] == "reserved"
    assert git("rev-parse", "HEAD", cwd=workspace.worktree) == packet["base_sha"]
    with pytest.raises(RuntimeFailure, match="external_execution_uncertain"):
        asyncio.run(executor.execute("Change output", budget=budget, authorization_check=lambda: None))
    assert len(call.calls) == 1


def test_cancellation_after_call_started_records_unknown_and_propagates(tmp_path):
    _, _, packet, manager, workspace = repo(tmp_path)
    call = FakeCall("", error=asyncio.CancelledError())
    budget = Budget()
    executor = BoundedPatchDraftExecutor(manager, packet, call)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(executor.execute("Change output", budget=budget, authorization_check=lambda: None))
    assert budget.state["usage_status"] == "unknown"
    assert budget.state["draft_calls"][0]["state"] == "unknown"
    assert git("rev-parse", "HEAD", cwd=workspace.worktree) == packet["base_sha"]


def test_actual_cost_overage_is_persisted_and_blocks_candidate_publication(tmp_path):
    _, _, packet, manager, workspace = repo(tmp_path)
    call = FakeCall(draft_json(packet, manager, workspace, "print('after')\n"), usage=1.5)
    budget = Budget(maximum=1.0)
    executor = BoundedPatchDraftExecutor(manager, packet, call)
    with pytest.raises(RuntimeFailure, match="external_execution_uncertain"):
        asyncio.run(executor.execute("Change output", budget=budget, authorization_check=lambda: None))
    assert budget.used_usd == 1.5
    assert budget.state["budget_reservations"][0]["actual_usd"] == 1.5
    assert budget.state["draft_calls"][0]["state"] == "settled"
    assert git("rev-parse", "HEAD", cwd=workspace.worktree) == packet["base_sha"]


def test_known_charge_without_provider_receipt_binding_is_unknown_but_preserved(tmp_path):
    _, _, packet, manager, workspace = repo(tmp_path)
    call = FakeCall(draft_json(packet, manager, workspace, "print('after')\n"),
                    usage=0.2, provider_request_id=None)
    budget = Budget()
    executor = BoundedPatchDraftExecutor(manager, packet, call)
    with pytest.raises(RuntimeFailure, match="external_execution_uncertain"):
        asyncio.run(executor.execute("Change output", budget=budget, authorization_check=lambda: None))
    assert budget.used_usd == 0.2
    assert budget.state["budget_reservations"][0]["actual_usd"] == 0.2
    assert budget.state["usage_status"] == "unknown"
    assert budget.state["draft_calls"][0]["state"] == "unknown"
    assert budget.state["draft_calls"][0]["observed_usage_usd"] == 0.2
    assert git("rev-parse", "HEAD", cwd=workspace.worktree) == packet["base_sha"]

