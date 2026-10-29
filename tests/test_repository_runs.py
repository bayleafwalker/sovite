from __future__ import annotations

import asyncio
import copy
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator, ValidationError
from fastapi.testclient import TestClient

from sovite import policy
from sovite.api import create_app
from sovite.db import Job
from sovite.adapters import RuntimeFailure
from sovite.repository_runs import (
    RUN_SCHEMA,
    RepositoryRunAttemptExecutor,
    RepositoryRunError,
    RepositoryRunOwner,
    canonical_packet_digest,
    expected_preview_id,
    validate_run_packet,
)
from sovite.service import Platform, PlatformError
from sovite.settings import Settings
from sovite.worker import Worker


ROOT = Path(__file__).resolve().parents[1]
CALLER = "repo-caller-token-000000000000000000"
OTHER = "repo-other-token-00000000000000000"


def git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.stdout.strip()


def make_repo(tmp_path: Path) -> tuple[Path, str]:
    bare = tmp_path / "project.git"
    source = tmp_path / "project"
    git("init", "--bare", str(bare))
    git("init", str(source))
    git("config", "user.name", "Repository run test", cwd=source)
    git("config", "user.email", "repo-run@example.invalid", cwd=source)
    (source / "src").mkdir()
    (source / "src" / "main.txt").write_text("base\n")
    git("add", ".", cwd=source)
    git("commit", "-m", "base", cwd=source)
    base = git("rev-parse", "HEAD", cwd=source)
    git("remote", "add", "origin", str(bare), cwd=source)
    git("push", "origin", "HEAD", cwd=source)
    git("--git-dir", str(bare), "symbolic-ref", "HEAD", "refs/heads/main")
    git("--git-dir", str(bare), "update-ref", "refs/heads/main", base)
    return bare, base


@pytest.fixture
def setup(tmp_path):
    bundle, _ = policy.load_bundle(ROOT / "config/platform.local.yaml")
    config_path = tmp_path / "bundle.yaml"
    config_path.write_text(yaml.safe_dump(bundle))
    settings = Settings(database_url=f"sqlite:///{tmp_path}/control.db", bundle_path=str(config_path),
        artifact_path=str(tmp_path / "artifacts"), operator_token="operator-repo-test-token-0000000000",
        caller_tokens={"cloud-codex": CALLER, "other-user": OTHER}, public_origin="http://testserver",
        secure_cookies=False, require_qualification=False,
        qualification_private_key=str(tmp_path / "private.pem"),
        qualification_public_keys=[str(tmp_path / "public.pem")])
    platform = Platform(settings)
    platform.initialize()
    return settings, platform


class BindingResolver:
    def __init__(self, mirror: Path, base: str, generation: str):
        self.mirror, self.base, self.generation = mirror, base, generation
        self.grant_id = "incident-forensics"
        self.input_class = None
        self.attempt_mirror = None
        self.attempt_binding_id = None
        self.attempt_schema = "sovite-repository-session/v1"
        self.attempt_packets: list[dict] = []
        self.deadline = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        self.latest_deadline = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()
        self.qualification_expires_at = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        self.qualification_ref = "repo-profile-qualification-8"
        self.qualification_sha256 = "e" * 64
        self.max_cost_usd = 5.0
        self.max_concurrency = 2

    def resolve_dispatch_binding(self, workspace_id: str, repository_id: str, principal_id: str):
        return {
            "binding_id": "owner-binding-1", "workspace_id": "workspace-1",
            "repository_id": "repo-1", "principal_id": "cloud-codex",
            "mirror_root": str(self.mirror), "base_sha": self.base,
            "allowed_areas": ["src"], "grant_id": self.grant_id,
            "input_class": self.input_class, "generation": self.generation,
            "executor_id": "repo-executor-1", "publication_revision": "rev-8",
            "max_cost_usd": self.max_cost_usd, "max_concurrency": self.max_concurrency,
            "latest_deadline": self.latest_deadline,
            "qualification_ref": self.qualification_ref,
            "qualification_sha256": self.qualification_sha256,
            "qualification_expires_at": self.qualification_expires_at,
        }

    def list_dispatch_bindings(self, workspace_id: str, principal_id: str):
        if workspace_id != "workspace-1" or principal_id != "cloud-codex":
            return []
        return [self.resolve_dispatch_binding(workspace_id, "repo-1", principal_id)]

    def issue_attempt_packet(self, binding_id: str, *, job_id: str, attempt_id: str,
                             generation: str, fence: str, deadline: str):
        packet = {
            "schema_version": self.attempt_schema,
            "binding_id": self.attempt_binding_id or binding_id,
            "job_id": job_id, "workspace_id": "workspace-1", "repository_id": "repo-1",
            "base_sha": self.base, "allowed_areas": ["src"],
            "deadline": deadline,
            "attempt_id": attempt_id, "generation": generation, "fence": fence,
            "mirror_root": str(self.attempt_mirror or self.mirror),
        }
        self.attempt_packets.append(packet)
        return packet


def packet(base: str, **overrides):
    value = {
        "schema_version": "portable-work/v1", "workspace_id": "workspace-1",
        "repository_id": "repo-1", "executor_id": "repo-executor-1",
        "base_sha": base, "goal": "Update the source file",
        "max_cost_usd": 2.0,
        "deadline": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "concurrency": 1,
    }
    value.update(overrides)
    return value


def make_owner(platform, resolver, executor=None, enabled=True):
    generation = platform.active()[1]
    if resolver.generation != generation:
        resolver.generation = generation
    profile = {
        "schema_version": "sovite-repository-execution-profile/v1", "mode": "mutable-native",
        "executor_id": "repo-executor-1", "publication_revision": "rev-8",
        "qualification_ref": resolver.qualification_ref,
        "qualification_sha256": resolver.qualification_sha256,
        "qualification_expires_at": resolver.qualification_expires_at,
        "per_attempt_isolation": True, "credential_containment": True,
        "exact_binding": True, "durable_checkpoint_and_candidate": True,
        "predecessor_reconciliation": True, "bounded_usage": True,
    }
    provider = lambda: profile
    if isinstance(executor, RepositoryRunAttemptExecutor):
        executor.execution_profile = provider
    owner = RepositoryRunOwner(platform, resolver, profile_enabled=enabled,
                               stub_or_qualified_executor=executor, execution_profile=provider)
    owner.test_profile = profile
    return owner


def submit(owner, value, *, principal_id="cloud-codex", idempotency_key="test-key-00000000"):
    return owner.submit(value, preview_id=expected_preview_id(value, principal_id),
                        principal_id=principal_id, idempotency_key=idempotency_key)


def test_closed_packet_rejects_commands_paths_credentials_and_bad_limits():
    valid = packet("a" * 40)
    assert validate_run_packet(valid) == valid
    for name, value in (("command", ["sh", "-c", "bad"]),
                        ("mirror_root", "/tmp/project.git"),
                        ("credentials", {"token": "private"}),
                        ("repository_url", "https://example.invalid/project")):
        with pytest.raises(RepositoryRunError, match="fields"):
            validate_run_packet(valid | {name: value})
    for invalid in (valid | {"concurrency": True}, valid | {"max_cost_usd": float("inf")},
                    valid | {"base_sha": "short"}, valid | {"deadline": "tomorrow"}):
        with pytest.raises(RepositoryRunError):
            validate_run_packet(invalid)
    with pytest.raises(RepositoryRunError, match="8000"):
        validate_run_packet(valid | {"goal": "x" * 8_001})
    with pytest.raises(RepositoryRunError, match="100000"):
        validate_run_packet(valid | {"max_cost_usd": 100_001})


def test_shipped_contract_matches_owner_envelope_and_exact_cloud_packet():
    schema = json.loads((ROOT / "sovite/execution_contract/repository-run-v1/contract.json").read_text())
    value = packet("a" * 40)
    envelope = {"schema_version": RUN_SCHEMA,
                "preview_id": expected_preview_id(value, "cloud-codex"), "packet": value}
    validator = Draft202012Validator(schema)
    validator.validate(envelope)
    with pytest.raises(ValidationError):
        validator.validate(envelope | {"command": ["sh"]})
    with pytest.raises(ValidationError):
        validator.validate({**envelope, "packet": value | {"mirror_root": "/tmp/repo"}})


def test_default_owner_is_disabled_and_cannot_admit_a_queue_item(setup, tmp_path):
    _, platform = setup
    mirror, base = make_repo(tmp_path)
    resolver = BindingResolver(mirror, base, platform.active()[1])
    owner = make_owner(platform, resolver, enabled=False)
    capabilities = owner.capabilities("workspace-1", "cloud-codex")
    assert capabilities["availability"] == "unavailable"
    assert capabilities["executors"][0]["launch_supported"] is False
    with pytest.raises(RepositoryRunError, match="qualified") as error:
        submit(owner, packet(base), principal_id="cloud-codex", idempotency_key="not-enabled-000000")
    assert error.value.code == "executor_unavailable"
    with platform.db.sessions() as session:
        assert session.query(Job).count() == 0


def test_executor_object_alone_does_not_advertise_or_admit_mutable_execution(setup, tmp_path):
    _, platform = setup
    mirror, base = make_repo(tmp_path)
    resolver = BindingResolver(mirror, base, platform.active()[1])
    owner = RepositoryRunOwner(platform, resolver, profile_enabled=True,
                               stub_or_qualified_executor=lambda *_: None)
    assert owner.capabilities("workspace-1", "cloud-codex")["availability"] == "unavailable"
    with pytest.raises(RepositoryRunError, match="qualified"):
        submit(owner, packet(base), idempotency_key="missing-profile-000000")


@pytest.mark.parametrize("field,value,code", [
    ("mode", "answer-only", "execution_profile_requirements_missing"),
    ("mode", "read-only", "execution_profile_requirements_missing"),
    ("per_attempt_isolation", False, "execution_profile_requirements_missing"),
    ("credential_containment", False, "execution_profile_requirements_missing"),
    ("exact_binding", False, "execution_profile_requirements_missing"),
    ("durable_checkpoint_and_candidate", False, "execution_profile_requirements_missing"),
    ("predecessor_reconciliation", False, "execution_profile_requirements_missing"),
    ("bounded_usage", False, "execution_profile_requirements_missing"),
    ("executor_id", "other", "execution_profile_binding_mismatch"),
    ("publication_revision", "other", "execution_profile_binding_mismatch"),
    ("qualification_ref", "other", "execution_profile_binding_mismatch"),
    ("qualification_sha256", "f" * 64, "execution_profile_binding_mismatch"),
    ("qualification_expires_at", "2000-01-01T00:00:00+00:00", "execution_profile_binding_mismatch"),
])
def test_profile_must_match_qualified_mutable_requirements(setup, tmp_path, field, value, code):
    _, platform = setup
    mirror, base = make_repo(tmp_path)
    resolver = BindingResolver(mirror, base, platform.active()[1])
    owner = make_owner(platform, resolver, executor=lambda *_: None)
    owner.test_profile[field] = value
    capability = owner.capabilities("workspace-1", "cloud-codex")["executors"][0]
    assert not capability["launch_supported"]
    assert capability["blockers"][0]["code"] == code
    with pytest.raises(RepositoryRunError, match="qualified"):
        submit(owner, packet(base), idempotency_key="invalid-profile-000000")


def test_profile_missing_field_or_provider_failure_is_unavailable(setup, tmp_path):
    _, platform = setup
    mirror, base = make_repo(tmp_path)
    resolver = BindingResolver(mirror, base, platform.active()[1])
    owner = make_owner(platform, resolver, executor=lambda *_: None)
    del owner.test_profile["credential_containment"]
    capability = owner.capabilities("workspace-1", "cloud-codex")["executors"][0]
    assert capability["blockers"][0]["code"] == "execution_profile_unavailable"
    def unavailable():
        raise RuntimeError("unavailable")
    owner.execution_profile = unavailable
    assert owner.capabilities("workspace-1", "cloud-codex")["availability"] == "unavailable"
    with pytest.raises(RepositoryRunError, match="qualified"):
        submit(owner, packet(base), idempotency_key="provider-down-000000")


def test_expired_profile_cannot_advertise_or_admit(setup, tmp_path):
    _, platform = setup
    mirror, base = make_repo(tmp_path)
    resolver = BindingResolver(mirror, base, platform.active()[1])
    resolver.qualification_expires_at = "2000-01-01T00:00:00+00:00"
    owner = make_owner(platform, resolver, executor=lambda *_: None)
    capability = owner.capabilities("workspace-1", "cloud-codex")["executors"][0]
    assert capability["blockers"][0]["code"] == "execution_profile_expired"
    with pytest.raises(RepositoryRunError, match="qualified"):
        submit(owner, packet(base), idempotency_key="expired-profile-000000")


def test_expired_owner_deadline_is_not_advertised_or_admitted(setup, tmp_path):
    _, platform = setup
    mirror, base = make_repo(tmp_path)
    resolver = BindingResolver(mirror, base, platform.active()[1])
    resolver.latest_deadline = "2000-01-01T00:00:00+00:00"
    owner = make_owner(platform, resolver, executor=lambda *_: None)
    capability = owner.capabilities("workspace-1", "cloud-codex")["executors"][0]
    assert not capability["launch_supported"]
    assert capability["blockers"][0]["code"] == "repository_deadline_expired"
    with pytest.raises(RepositoryRunError, match="deadline"):
        submit(owner, packet(base), idempotency_key="expired-owner-limit-000000")


@pytest.mark.parametrize("bad", [None, "", "2029-01-01T00:00:00", "not-a-date"])
def test_invalid_owner_deadline_fails_closed(setup, tmp_path, bad):
    _, platform = setup
    mirror, base = make_repo(tmp_path)
    resolver = BindingResolver(mirror, base, platform.active()[1])
    resolver.latest_deadline = bad
    owner = make_owner(platform, resolver, executor=lambda *_: None)
    assert owner.capabilities("workspace-1", "cloud-codex")["availability"] == "unavailable"
    with pytest.raises(RepositoryRunError):
        submit(owner, packet(base), idempotency_key="invalid-owner-limit-000000")


def test_attempt_cannot_continue_past_admitted_deadline(setup, tmp_path, monkeypatch):
    import sovite.repository_runs as runs
    _, platform = setup
    mirror, base = make_repo(tmp_path)
    resolver = BindingResolver(mirror, base, platform.active()[1])
    async def stub(*_args):
        pass
    lifecycle = RepositoryRunAttemptExecutor(platform, resolver, tmp_path / "deadline-state", stub)
    owner = make_owner(platform, resolver, executor=lifecycle)
    deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
    created = submit(owner, packet(base, deadline=deadline.isoformat()),
                     idempotency_key="run-deadline-000000")
    class AfterDeadline(datetime):
        @classmethod
        def now(cls, tz=None):
            return (deadline + timedelta(seconds=1)).astimezone(tz)
    monkeypatch.setattr(runs, "datetime", AfterDeadline)
    with platform.db.sessions() as session:
        job = session.get(Job, created["id"])
        with pytest.raises(RuntimeFailure, match="repository_binding_revoked"):
            lifecycle.check_authorization(job)


def test_discovery_filters_expired_binding_but_keeps_current_binding(setup, tmp_path):
    _, platform = setup
    mirror, base = make_repo(tmp_path)
    resolver = BindingResolver(mirror, base, platform.active()[1])
    def bindings(workspace_id, principal_id):
        current = resolver.resolve_dispatch_binding(workspace_id, "repo-1", principal_id)
        return [current | {"repository_id": "expired-repo", "latest_deadline": "2000-01-01T00:00:00+00:00"}, current]
    resolver.list_dispatch_bindings = bindings
    owner = make_owner(platform, resolver, executor=lambda *_: None)
    capability = owner.capabilities("workspace-1", "cloud-codex")["executors"][0]
    assert capability["launch_supported"]
    assert capability["repository_ids"] == ["repo-1"]
    assert capability["limits"]["latest_deadline"] == resolver.latest_deadline
    assert submit(owner, packet(base), idempotency_key="current-owner-limit-000000")["id"]


def test_discovery_deadline_uses_instant_not_iso_lexical_order(setup, tmp_path):
    _, platform = setup
    mirror, base = make_repo(tmp_path)
    resolver = BindingResolver(mirror, base, platform.active()[1])
    now = datetime.now(timezone.utc)
    earlier = (now + timedelta(hours=2)).astimezone(timezone(timedelta(hours=14))).isoformat()
    later = (now + timedelta(hours=3)).astimezone(timezone(timedelta(hours=-12))).isoformat()
    assert later < earlier
    def bindings(workspace_id, principal_id):
        current = resolver.resolve_dispatch_binding(workspace_id, "repo-1", principal_id)
        return [current | {"repository_id": "repo-1", "latest_deadline": earlier},
                current | {"repository_id": "repo-2", "latest_deadline": later}]
    resolver.list_dispatch_bindings = bindings
    owner = make_owner(platform, resolver, executor=lambda *_: None)
    capability = owner.capabilities("workspace-1", "cloud-codex")["executors"][0]
    assert capability["launch_supported"]
    assert capability["limits"]["latest_deadline"] == earlier


def test_discovery_reports_only_profile_matching_repositories(setup, tmp_path):
    _, platform = setup
    mirror, base = make_repo(tmp_path)
    resolver = BindingResolver(mirror, base, platform.active()[1])
    original_list = resolver.list_dispatch_bindings
    def mixed_bindings(workspace_id, principal_id):
        current = original_list(workspace_id, principal_id)[0]
        other = current | {"repository_id": "repo-other", "qualification_ref": "other-profile",
                           "publication_revision": "old-revision", "max_cost_usd": 0.5}
        return [other, current]
    resolver.list_dispatch_bindings = mixed_bindings
    owner = make_owner(platform, resolver, executor=lambda *_: None)
    capability = owner.capabilities("workspace-1", "cloud-codex")["executors"][0]
    assert capability["launch_supported"]
    assert capability["repository_ids"] == ["repo-1"]
    assert capability["publication_revision"] == "rev-8"
    assert capability["limits"]["max_cost_usd"] == 5.0
    assert submit(owner, packet(base), idempotency_key="matched-profile-000000")["id"]


def test_binding_principal_repository_executor_limits_and_key_digest_are_enforced(setup, tmp_path):
    _, platform = setup
    mirror, base = make_repo(tmp_path)
    resolver = BindingResolver(mirror, base, platform.active()[1])
    owner = make_owner(platform, resolver, executor=lambda *_: None)
    request = packet(base)
    first = submit(owner, request, principal_id="cloud-codex", idempotency_key="same.key-00000000")
    assert first["workspace_id"] == "workspace-1"
    assert submit(owner, request, principal_id="cloud-codex", idempotency_key="same.key-00000000")["id"] == first["id"]
    assert first["attempts"] == [] and first["evidence"] == []
    assert canonical_packet_digest(request) != canonical_packet_digest(request | {"goal": "different"})
    with pytest.raises(PlatformError, match="different request"):
        submit(owner, request | {"goal": "different"}, principal_id="cloud-codex", idempotency_key="same.key-00000000")
    with pytest.raises(RepositoryRunError) as denied:
        submit(owner, request, principal_id="other-user", idempotency_key="other-key-00000000")
    assert denied.value.code == "binding_mismatch"
    with pytest.raises(RepositoryRunError) as limited:
        submit(owner, request | {"max_cost_usd": 6.0}, principal_id="cloud-codex", idempotency_key="over-limit-00000000")
    assert limited.value.code == "limits_exceeded"
    with pytest.raises(RepositoryRunError) as preview:
        owner.submit(request, preview_id="0" * 64, principal_id="cloud-codex",
                     idempotency_key="wrong-preview-0000000")
    assert preview.value.code == "preview_mismatch"


def test_authenticated_api_scopes_run_list_status_and_cancel(setup, tmp_path):
    settings, platform = setup
    mirror, base = make_repo(tmp_path)
    resolver = BindingResolver(mirror, base, platform.active()[1])
    owner = make_owner(platform, resolver, executor=lambda *_: None)
    with TestClient(create_app(settings, platform, owner)) as client:
        caps = client.get("/api/repository-runs/v1/capabilities?workspace_id=workspace-1",
                          headers={"Authorization": "Bearer " + CALLER})
        assert caps.status_code == 200
        assert caps.json()["schema_version"] == RUN_SCHEMA
        capability = caps.json()["executors"][0]
        assert capability["workspace_id"] == "workspace-1"
        assert capability["repository_ids"] == ["repo-1"]
        assert capability["launch_supported"] is True
        assert capability["limits"] == {
            "max_cost_usd": 5.0, "max_concurrency": 2,
            "latest_deadline": resolver.latest_deadline,
        }
        request_packet = packet(base)
        request_packet = packet(base)
        submitted = client.post("/api/repository-runs/v1/runs", headers={
            "Authorization": "Bearer " + CALLER, "Idempotency-Key": "api-once-00000000"},
            json={"schema_version": RUN_SCHEMA, "preview_id": expected_preview_id(request_packet, "cloud-codex"),
                  "packet": request_packet})
        assert submitted.status_code == 201
        run = submitted.json()["run"]
        listed = client.get("/api/repository-runs/v1/runs?workspace_id=workspace-1",
                            headers={"Authorization": "Bearer " + CALLER})
        assert [row["id"] for row in listed.json()["runs"]] == [run["id"]]
        assert client.get(f"/api/repository-runs/v1/runs/{run['id']}?workspace_id=other",
                          headers={"Authorization": "Bearer " + CALLER}).status_code == 404
        assert client.get(f"/api/repository-runs/v1/runs/{run['id']}?workspace_id=workspace-1",
                          headers={"Authorization": "Bearer " + OTHER}).status_code == 404
        cancelled = client.post(f"/api/repository-runs/v1/runs/{run['id']}/cancel?workspace_id=workspace-1",
                                headers={"Authorization": "Bearer " + OTHER})
        assert cancelled.status_code == 404
        cancelled = client.post(f"/api/repository-runs/v1/runs/{run['id']}/cancel?workspace_id=workspace-1",
                                headers={"Authorization": "Bearer " + CALLER})
        assert cancelled.status_code == 200
        assert cancelled.json()["run"]["state"] == "cancelled"


def test_stub_executor_is_fenced_and_publishes_typed_candidate_evidence(setup, tmp_path):
    _, platform = setup
    mirror, base = make_repo(tmp_path)
    resolver = BindingResolver(mirror, base, platform.active()[1])
    start = asyncio.Event()

    async def stub(worktree, goal, deadline, budget, authorize):
        assert goal == "Update the source file"
        assert deadline > datetime.now(timezone.utc).timestamp()
        authorize()
        budget.acquire_slot("slot-one")
        budget.reserve_cost("provider-call-1", 0.5)
        budget.settle_cost("provider-call-1", 0.25)
        budget.release_slot("slot-one")
        (worktree / "src" / "main.txt").write_text("candidate\n")
        git("add", "-A", cwd=worktree)
        git("-c", "user.name=Run stub", "-c", "user.email=stub@example.invalid",
            "commit", "-m", "candidate", cwd=worktree)
        start.set()

    lifecycle = RepositoryRunAttemptExecutor(platform, resolver, tmp_path / "attempt-state", stub)
    owner = make_owner(platform, resolver, executor=lifecycle)
    request_packet = packet(base)
    created = submit(owner, request_packet, principal_id="cloud-codex", idempotency_key="execute-once-00000000")
    assert asyncio.run(Worker(platform, repository_run_executor=lifecycle).run_once())
    observed = owner.get(created["id"], principal_id="cloud-codex", workspace_id="workspace-1")
    with platform.db.sessions() as session:
        saved = session.get(Job, created["id"])
        error_code = saved.error_code
    assert observed["state"] == "completed", error_code
    assert observed["preview_id"] == expected_preview_id(request_packet, "cloud-codex")
    assert observed["idempotency_key"] == "execute-once-00000000"
    assert len(observed["attempts"]) == 1
    assert observed["attempts"][0]["state"] == "completed"
    assert set(observed["attempts"][0]) <= {"id", "state", "provider", "model", "started_at", "finished_at", "checkpoint_sha"}
    assert observed["attempts"][0]["checkpoint_sha"] != base
    assert len(observed["evidence"]) == 1
    assert observed["evidence"][0]["uri"].startswith("sovite://repository-runs/")
    assert len(observed["evidence"][0]["sha256"]) == 64
    assert resolver.attempt_packets[0]["fence"]
    assert start.is_set()
    with platform.db.sessions() as session:
        internal = session.get(Job, created["id"]).resolved["repository_run"]
    assert internal["usage_usd"] == 0.25
    assert internal["budget_reservations"] == [{"call_id": "provider-call-1", "reserved_usd": 0.5,
        "state": "settled", "actual_usd": 0.25}]
    assert internal["peak_concurrency"] == 1


def test_default_worker_never_routes_repository_run_into_answer_only_runner(setup, tmp_path):
    _, platform = setup
    mirror, base = make_repo(tmp_path)
    resolver = BindingResolver(mirror, base, platform.active()[1])
    owner = make_owner(platform, resolver, executor=lambda *_: None)
    run = submit(owner, packet(base), principal_id="cloud-codex", idempotency_key="leave-queued-00000000")
    assert not asyncio.run(Worker(platform).run_once())
    assert owner.get(run["id"], principal_id="cloud-codex")["state"] == "queued"


@pytest.mark.parametrize("overrun", ["cost", "concurrency"])
def test_owner_budget_reserves_before_calls_and_stops_overruns(setup, tmp_path, overrun):
    _, platform = setup
    mirror, base = make_repo(tmp_path)
    resolver = BindingResolver(mirror, base, platform.active()[1])

    async def stub(worktree, goal, deadline, budget, authorize):
        authorize()
        if overrun == "cost":
            budget.reserve_cost("call-one", 1.5)
            budget.settle_cost("call-one", 1.5)
            # This rejection happens before another paid operation can begin.
            budget.reserve_cost("call-two", 0.6)
        else:
            budget.acquire_slot("parallel-one")
            budget.acquire_slot("parallel-two")

    lifecycle = RepositoryRunAttemptExecutor(platform, resolver, tmp_path / "bounded-state", stub)
    owner = make_owner(platform, resolver, executor=lifecycle)
    created = submit(owner, packet(base), principal_id="cloud-codex",
                     idempotency_key=f"budget-{overrun}-00000000")
    assert asyncio.run(Worker(platform, repository_run_executor=lifecycle).run_once())
    observed = owner.get(created["id"], principal_id="cloud-codex")
    assert observed["state"] == "blocked"
    assert observed["attempts"][0]["state"] == "uncertain"
    with platform.db.sessions() as session:
        internal = session.get(Job, created["id"]).resolved["repository_run"]
    if overrun == "cost":
        assert internal["usage_usd"] == 1.5
        assert [row["call_id"] for row in internal["budget_reservations"]] == ["call-one"]
    else:
        assert internal["active_slots"] == ["parallel-one"]
        assert internal["peak_concurrency"] == 1


@pytest.mark.parametrize("revocation", ["qualification", "limit", "grant", "input_class", "mirror"])
def test_admitted_run_rechecks_live_owner_qualification_and_limits(setup, tmp_path, revocation):
    _, platform = setup
    mirror, base = make_repo(tmp_path)
    resolver = BindingResolver(mirror, base, platform.active()[1])
    invoked = []

    async def stub(*_args):
        invoked.append(True)

    lifecycle = RepositoryRunAttemptExecutor(platform, resolver, tmp_path / "reauth-state", stub)
    owner = make_owner(platform, resolver, executor=lifecycle)
    created = submit(owner, packet(base), principal_id="cloud-codex",
                     idempotency_key=f"reauth-{revocation}-0000000")
    if revocation == "qualification":
        resolver.qualification_sha256 = "f" * 64
    elif revocation == "limit":
        resolver.max_cost_usd = 1.0
    elif revocation == "grant":
        resolver.grant_id = "replacement-grant"
    elif revocation == "input_class":
        resolver.input_class = "replacement-class"
    else:
        resolver.mirror = tmp_path / "other.git"
    with platform.db.sessions() as session:
        job = session.get(Job, created["id"])
        with pytest.raises(RuntimeFailure, match="repository_binding_revoked"):
            lifecycle.check_authorization(job)
    assert invoked == []


@pytest.mark.parametrize("bad_packet", ["binding", "schema"])
def test_attempt_packet_cannot_replace_admitted_binding(setup, tmp_path, bad_packet):
    _, platform = setup
    mirror, base = make_repo(tmp_path)
    resolver = BindingResolver(mirror, base, platform.active()[1])
    invoked = []
    async def stub(*_args):
        invoked.append(True)
    lifecycle = RepositoryRunAttemptExecutor(platform, resolver, tmp_path / "binding-state", stub)
    owner = make_owner(platform, resolver, executor=lifecycle)
    created = submit(owner, packet(base), idempotency_key="binding-packet-000000")
    if bad_packet == "binding":
        resolver.attempt_binding_id = "different-owner-binding"
    else:
        resolver.attempt_schema = "sovite-repository-session/v2"
    assert asyncio.run(Worker(platform, repository_run_executor=lifecycle).run_once())
    observed = owner.get(created["id"], principal_id="cloud-codex")
    assert observed["state"] == "failed"
    assert observed["attempts"] == []
    assert invoked == []


def test_attempt_packet_cannot_redirect_admitted_mirror(setup, tmp_path):
    _, platform = setup
    mirror, base = make_repo(tmp_path)
    resolver = BindingResolver(mirror, base, platform.active()[1])
    invoked = []

    async def stub(*_args):
        invoked.append(True)

    lifecycle = RepositoryRunAttemptExecutor(platform, resolver, tmp_path / "packet-state", stub)
    owner = make_owner(platform, resolver, executor=lifecycle)
    created = submit(owner, packet(base), idempotency_key="mirror-packet-000000")
    resolver.attempt_mirror = tmp_path / "unadmitted.git"
    assert asyncio.run(Worker(platform, repository_run_executor=lifecycle).run_once())
    observed = owner.get(created["id"], principal_id="cloud-codex")
    assert observed["state"] == "failed"
    assert observed["attempts"] == []
    assert invoked == []


@pytest.mark.parametrize("revocation", ["qualification", "grant", "input_class", "mirror", "profile"])
def test_revocation_after_mutation_preserves_uncertain_attempt_and_candidate_workspace(setup, tmp_path, revocation):
    _, platform = setup
    mirror, base = make_repo(tmp_path)
    resolver = BindingResolver(mirror, base, platform.active()[1])

    async def stub(worktree, _goal, _deadline, budget, authorize):
        budget.acquire_slot("write")
        (worktree / "src" / "main.txt").write_text("partial candidate\n")
        git("add", "-A", cwd=worktree)
        git("-c", "user.name=Run stub", "-c", "user.email=stub@example.invalid",
            "commit", "-m", "partial", cwd=worktree)
        if revocation == "qualification":
            resolver.qualification_ref = "replacement-profile-qualification"
        elif revocation == "grant":
            resolver.grant_id = "replacement-grant"
        elif revocation == "input_class":
            resolver.input_class = "replacement-class"
        elif revocation == "mirror":
            resolver.mirror = tmp_path / "other.git"
        else:
            owner.test_profile["credential_containment"] = False
        authorize()

    lifecycle = RepositoryRunAttemptExecutor(platform, resolver, tmp_path / "uncertain-state", stub)
    owner = make_owner(platform, resolver, executor=lifecycle)
    created = submit(owner, packet(base), principal_id="cloud-codex",
                     idempotency_key="revoke-mid-run-0000000")
    assert asyncio.run(Worker(platform, repository_run_executor=lifecycle).run_once())
    observed = owner.get(created["id"], principal_id="cloud-codex")
    assert observed["state"] == "blocked"
    assert observed["attempts"][0]["state"] == "uncertain"
    assert observed["evidence"] == []


def test_cancel_during_mutable_attempt_becomes_uncertain_not_clean_cancel(setup, tmp_path):
    _, platform = setup
    platform.settings.lease_seconds = 1
    mirror, base = make_repo(tmp_path)
    resolver = BindingResolver(mirror, base, platform.active()[1])
    started = asyncio.Event()

    async def blocking_stub(worktree, _goal, _deadline, budget, authorize):
        authorize()
        budget.acquire_slot("writing")
        (worktree / "src" / "main.txt").write_text("partial write\n")
        started.set()
        await asyncio.Event().wait()

    lifecycle = RepositoryRunAttemptExecutor(platform, resolver, tmp_path / "cancel-state", blocking_stub)
    owner = make_owner(platform, resolver, executor=lifecycle)
    created = submit(owner, packet(base), principal_id="cloud-codex",
                     idempotency_key="cancel-mid-run-0000000")
    worker = Worker(platform, repository_run_executor=lifecycle)

    async def cancel_run():
        operation = asyncio.create_task(worker.run_once())
        await asyncio.wait_for(started.wait(), timeout=5)
        owner.cancel(created["id"], principal_id="cloud-codex", workspace_id="workspace-1")
        await asyncio.wait_for(operation, timeout=5)

    asyncio.run(cancel_run())
    observed = owner.get(created["id"], principal_id="cloud-codex")
    assert observed["state"] == "blocked"
    assert observed["attempts"][0]["state"] == "uncertain"


def test_expired_worker_fence_blocks_mutable_run_replay(setup, tmp_path):
    _, platform = setup
    mirror, base = make_repo(tmp_path)
    resolver = BindingResolver(mirror, base, platform.active()[1])
    owner = make_owner(platform, resolver, executor=lambda *_: None)
    run = submit(owner, packet(base), principal_id="cloud-codex", idempotency_key="recover-blocked-00000000")
    worker = Worker(platform, repository_run_executor=lambda *_: None)
    job = worker.claim()
    assert job is not None and job.id == run["id"]
    assert not __import__("sovite.lease_service", fromlist=["replay_safe"]).replay_safe(job.resolved)
    with platform.db.sessions.begin() as session:
        row = session.get(Job, job.id)
        row.lease_until = 0
        resolved = copy.deepcopy(row.resolved)
        resolved["repository_run"]["attempts"] = [{"id": "attempt-interrupted", "state": "interrupted"}]
        row.resolved = resolved
        lease = session.get(__import__("sovite.db", fromlist=["WorkerExecutionLease"]).WorkerExecutionLease, job.id)
        lease.lease_until = 0
    worker.recover_expired()
    observed = owner.get(run["id"], principal_id="cloud-codex")
    assert observed["state"] == "blocked"
    assert observed["attempts"][0]["state"] == "uncertain"
