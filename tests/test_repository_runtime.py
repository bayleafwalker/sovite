"""Protected installation composition; the injected driver is explicitly synthetic."""
import asyncio
import copy
import json

import pytest
from fastapi.testclient import TestClient

from sovite import api, cli
from sovite.repository_runtime import (compose_repository_runtime,
                                       protected_bytes)
from sovite.worker import Worker
from test_repository_runs import (BindingResolver, make_owner, make_repo, packet,
                                  setup, submit, git, CALLER)


def save(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


@pytest.fixture
def installed(setup, tmp_path):
    settings, platform = setup
    mirror, base = make_repo(tmp_path)
    old = BindingResolver(mirror, base, platform.active()[1])
    profile = make_owner(platform, old).test_profile
    config = {"schema_version": "sovite-repository-runtime/v1",
              "state_root": str(tmp_path / "protected-state"),
              "bindings": [old.resolve_dispatch_binding("workspace-1", "repo-1", "cloud-codex")],
              "driver": {"synthetic_test_only": True}, "retention": None}
    path = tmp_path / "installed.json"
    save(path, config)
    settings.repository_runtime_config = str(path)
    class SyntheticDriver:
        def __init__(self):
            self.profile = copy.deepcopy(profile)
        def execution_profile(self):
            return copy.deepcopy(self.profile)
        async def __call__(self, worktree, goal, deadline, budget, authorization_check):
            authorization_check()
            (worktree / "src" / "main.txt").write_text("synthetic candidate\n")
            git("add", "-A", cwd=worktree)
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "commit", "-m", "synthetic candidate", cwd=worktree)
    driver = SyntheticDriver()
    runtime = compose_repository_runtime(settings, platform, driver_factory=lambda _: driver)
    assert runtime.diagnostic == "repository_runtime_composed"
    return settings, platform, path, config, runtime, base, driver


def test_absent_invalid_and_missing_builtin_do_not_enable_execution(setup, tmp_path):
    settings, platform = setup
    assert compose_repository_runtime(settings, platform).owner is None
    path = tmp_path / "bad.json"
    save(path, {"schema_version": "made-up"})
    settings.repository_runtime_config = str(path)
    runtime = compose_repository_runtime(settings, platform)
    assert runtime.owner is None and runtime.execution is None
    assert runtime.diagnostic == "repository_runtime_unavailable"
    app = api.create_app(settings, platform)
    assert app.state.repository_run_owner is None
    with TestClient(app) as client:
        response = client.get("/api/repository-runs/v1/capabilities?workspace_id=workspace-1",
                              headers={"Authorization": "Bearer " + CALLER})
        assert response.status_code == 503


def test_invalid_file_permissions_and_symlinks_fail_closed(installed, tmp_path):
    settings, platform, path, config, runtime, _, driver = installed
    assert compose_repository_runtime(settings, platform).owner is None
    path.chmod(0o644)
    assert compose_repository_runtime(settings, platform, driver_factory=lambda _: driver).owner is None
    path.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    settings.repository_runtime_config = str(link)
    assert compose_repository_runtime(settings, platform, driver_factory=lambda _: driver).owner is None
    directory_link = tmp_path / "link-parent"
    directory_link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OSError):
        protected_bytes(directory_link / path.name)
    path.unlink()
    path.write_text('{"schema_version":"a","schema_version":"b"}')
    path.chmod(0o600)
    settings.repository_runtime_config = str(path)
    assert compose_repository_runtime(settings, platform, driver_factory=lambda _: driver).owner is None


def test_live_scope_checks_platform_revocation_and_installed_replacement(installed):
    settings, platform, path, config, runtime, base, _ = installed
    resolver = runtime.execution.resolver
    assert resolver.list_dispatch_bindings("workspace-1", "cloud-codex")
    assert resolver.list_dispatch_bindings("workspace-1", "other-user") == []
    with pytest.raises(ValueError):
        resolver.resolve_dispatch_binding("workspace-1", "repo-1", "other-user")
    platform.inhibit("incident-forensics", "test current revocation")
    assert resolver.list_dispatch_bindings("workspace-1", "cloud-codex") == []
    with pytest.raises(Exception):
        resolver.resolve_dispatch_binding("workspace-1", "repo-1", "cloud-codex")
    config["state_root"] += "-replacement"
    save(path, config)
    with pytest.raises(ValueError):
        resolver.list_dispatch_bindings("workspace-1", "cloud-codex")


def test_live_source_removal_and_generation_change_revoke_scope(installed):
    settings, platform, path, config, runtime, base, _ = installed
    config["bindings"][0]["principal_id"] = "other-user"
    save(path, config)
    with pytest.raises(Exception):
        runtime.execution.resolver.resolve_dispatch_binding("workspace-1", "repo-1", "other-user")
    config["bindings"][0]["principal_id"] = "cloud-codex"
    config["bindings"][0]["generation"] = "sha256:" + "0" * 64
    save(path, config)
    with pytest.raises(Exception):
        runtime.execution.resolver.resolve_dispatch_binding("workspace-1", "repo-1", "cloud-codex")


def test_real_owner_worker_same_composition_and_exact_attempt_packet(installed):
    settings, platform, path, config, runtime, base, driver = installed
    assert runtime.owner.executor is runtime.execution
    assert runtime.owner.resolver is runtime.execution.resolver
    assert runtime.execution.executor is driver
    created = submit(runtime.owner, packet(base))
    worker = Worker(platform, repository_run_executor=runtime.execution)
    job = worker.claim()
    assert job is not None and job.id == created["id"]
    arguments = dict(job_id=job.id, attempt_id=f"{job.id}-{job.attempts}",
                     generation=job.generation, fence=job.execution_fence,
                     deadline=job.resolved["repository_run"]["deadline"])
    raw = runtime.execution.resolver.issue_attempt_packet("owner-binding-1", **arguments)
    assert raw["job_id"] == job.id and raw["fence"] == job.execution_fence
    for field, value in (("attempt_id", "foreign-attempt"), ("generation", "sha256:" + "0" * 64),
                         ("fence", "0" * 32), ("deadline", config["bindings"][0]["latest_deadline"])):
        with pytest.raises(ValueError):
            runtime.execution.resolver.issue_attempt_packet("owner-binding-1", **{**arguments, field: value})
    asyncio.run(worker.execute(job))
    assert platform.get_job(job.id).status == "completed"
    assert platform.get_job(job.id).resolved["repository_run"]["attempts"][-1]["state"] == "completed"
    with pytest.raises(ValueError):
        runtime.execution.resolver.issue_attempt_packet("owner-binding-1", **arguments)


def test_queued_run_cannot_borrow_changed_binding(installed):
    settings, platform, path, config, runtime, base, _ = installed
    created = submit(runtime.owner, packet(base))
    config["bindings"][0]["allowed_areas"] = ["different-area"]
    save(path, config)
    assert asyncio.run(Worker(platform, repository_run_executor=runtime.execution).run_once())
    job = platform.get_job(created["id"])
    assert job.status != "completed"
    assert not job.resolved["repository_run"]["attempts"]


def test_api_embedded_worker_keeps_exact_owner_execution(installed, monkeypatch):
    settings, platform, path, config, runtime, base, _ = installed
    settings.embedded_worker = True
    captured = []
    monkeypatch.setattr("sovite.repository_runtime.compose_repository_runtime", lambda *_: runtime)
    real_worker = api.Worker
    def worker_factory(*args, **kwargs):
        captured.append(kwargs["repository_run_executor"])
        return real_worker(*args, **kwargs)
    monkeypatch.setattr(api, "Worker", worker_factory)
    app = api.create_app(settings, platform)
    assert app.state.repository_run_owner is runtime.owner
    with TestClient(app):
        assert captured == [runtime.execution]


def test_standalone_worker_cli_uses_installed_composition(installed, monkeypatch):
    settings, platform, path, config, runtime, base, _ = installed
    captured = []
    monkeypatch.setattr("sys.argv", ["sovite", "worker"])
    monkeypatch.setattr(cli.Settings, "from_env", lambda **_: settings)
    monkeypatch.setattr(cli, "Platform", lambda _: platform)
    monkeypatch.setattr("sovite.repository_runtime.compose_repository_runtime", lambda *_: runtime)
    class ImmediateWorker:
        def __init__(self, _platform, *, repository_run_executor):
            captured.append(repository_run_executor)
            self.stop = asyncio.Event()
        async def run(self):
            pass
    monkeypatch.setattr(cli, "Worker", ImmediateWorker)
    cli.main()
    assert captured == [runtime.execution]


def test_profile_claims_are_driver_owned_and_can_revoke(installed):
    settings, platform, path, config, runtime, base, driver = installed
    assert runtime.owner.capabilities("workspace-1", "cloud-codex")["availability"] == "available"
    driver.profile = None
    assert runtime.owner.capabilities("workspace-1", "cloud-codex")["availability"] == "unavailable"
    with pytest.raises(Exception):
        submit(runtime.owner, packet(base))
    config["profile"] = {"per_attempt_isolation": True}
    save(path, config)
    assert compose_repository_runtime(settings, platform, driver_factory=lambda _: driver).owner is None


def test_enabled_runtime_path_changes_platform_qualification_fingerprint(installed):
    from sovite.qualification import runtime_settings_fingerprint
    settings, platform, path, config, runtime, base, driver = installed
    enabled = runtime_settings_fingerprint(settings)
    settings.repository_runtime_config = None
    assert runtime_settings_fingerprint(settings) != enabled


def test_retention_uses_real_client_profile_and_exact_owner(installed, tmp_path):
    # The project's test/retention extra pins the released owner client.
    from vuoro_client.profile import Profile
    settings, platform, path, config, runtime, base, driver = installed
    credential = tmp_path / "workspace-token"
    credential.write_text("synthetic-workspace-token-0000000000\n")
    credential.chmod(0o600)
    profile_path = tmp_path / "workspace-profile.json"
    profile = {"schema_version": "vuoro-client-profile/v1", "id": "retention-test",
               "revision": 1, "source_environment_id": "test",
               "target": {"environment_id": "workspace-test", "environment_class": "development",
                          "endpoint": "https://workspace.invalid"},
               "credential_ref": "file:" + str(credential),
               "required_authorities": ["knowledge:read", "knowledge:contribute"],
               "production_endpoint_denied": True}
    save(profile_path, profile)
    workspace = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    config["retention"] = {"root": str(tmp_path / "publication-intents"),
                           "profile_path": str(profile_path), "credential_file": str(credential),
                           "target": {"endpoint": "https://workspace.invalid", "workspace_id": workspace,
                                      "destination": "workspace-" + workspace, "allowed_classes": ["C3"]}}
    save(path, config)
    runtime = compose_repository_runtime(settings, platform, driver_factory=lambda _: driver)
    service = runtime.publication
    assert service is not None and isinstance(service.profile, Profile)
    assert service.exporter.platform is platform
    assert service.exporter.execution is runtime.execution is runtime.owner.executor
    assert service.credential_resolver(service.profile.credential_ref) == "synthetic-workspace-token-0000000000"
    with pytest.raises(ValueError):
        service.credential_resolver("file:/foreign/credential")
    profile["target"]["endpoint"] = "https://foreign.invalid"
    save(profile_path, profile)
    with pytest.raises(ValueError):
        service.credential_resolver(service.profile.credential_ref)
    assert compose_repository_runtime(settings, platform, driver_factory=lambda _: driver).owner is None


def test_bound_driver_context_comes_from_started_owner_not_worktree(installed):
    from dataclasses import FrozenInstanceError
    settings, platform, path, config, runtime, base, driver = installed
    contexts = []
    def bind(context):
        contexts.append(context)
        with pytest.raises(FrozenInstanceError):
            context.fence = "foreign"
        context.model["test-detached-copy"] = True
        return driver
    driver.for_attempt = bind
    created = submit(runtime.owner, packet(base))
    worker = Worker(platform, repository_run_executor=runtime.execution)
    assert asyncio.run(worker.run_once())
    job = platform.get_job(created["id"])
    assert job.status == "completed" and len(contexts) == 1
    context = contexts[0]
    assert context.job_id == job.id and context.attempt_id == f"{job.id}-{job.attempts}"
    assert context.workspace_id == "workspace-1" and context.repository_id == "repo-1"
    assert context.principal_id == job.caller and context.generation == job.generation
    assert context.execution is None and context.fence not in repr(context)
    assert "test-detached-copy" not in job.resolved["model"]


def test_gateway_bound_context_requires_actual_capability_and_current_fence(installed, tmp_path):
    from dataclasses import replace
    import time
    import yaml
    from sovite.adapters import RuntimeFailure
    from sovite.db import ExecutionAuthority, WorkerExecutionLease
    from sovite.service import Platform
    from test_policy_v1alpha2 import bundle as gateway_bundle_fixture
    # The real v1alpha2 policy fixture, not an injected authority response.
    bundle = gateway_bundle_fixture.__wrapped__()
    bundle["mode"] = "development"
    bundle["grants"][0]["callers"] = ["cloud-codex"]
    original_settings, _, path, config, _, base, driver = installed
    bundle_path = tmp_path / "gateway-bundle.yaml"
    bundle_path.write_text(yaml.safe_dump(bundle))
    settings = replace(original_settings, database_url=f"sqlite:///{tmp_path}/gateway.db",
                       bundle_path=str(bundle_path))
    platform = Platform(settings)
    platform.db.migrate(); platform.initialize()
    config["bindings"][0]["generation"] = platform.active()[1]
    save(path, config)
    runtime = compose_repository_runtime(settings, platform, driver_factory=lambda _: driver)
    created = submit(runtime.owner, packet(base))
    worker = Worker(platform, repository_run_executor=runtime.execution)
    job = worker.claim()
    assert job is not None and job.id == created["id"]
    args = dict(fence=job.execution_fence, attempt_id=f"{job.id}-{job.attempts}",
                deadline=time.time() + 1000)
    context = runtime.execution._attempt_context(job, **args)
    assert context.execution["capability"] == job.execution_capability
    assert context.execution["context"]["generation"] == job.generation
    assert context.execution["context"]["binding_id"] == job.resolved["model"]["id"]
    with platform.db.sessions() as session:
        authority = session.get(ExecutionAuthority, job.id)
        lease = session.get(WorkerExecutionLease, job.id)
        assert context.execution["context"] == authority.context
        assert context.deadline == lease.execution_deadline
    assert job.execution_capability not in repr(context)
    context.execution["context"]["caller"] = "foreign"
    assert runtime.execution._attempt_context(job, **args).execution["context"]["caller"] == job.caller
    for changes in ({"fence": "0" * 32}, {"attempt_id": "foreign-attempt"}):
        with pytest.raises(RuntimeFailure, match="repository_execution_context_unavailable"):
            runtime.execution._attempt_context(job, **{**args, **changes})
    actual = job.execution_capability
    job.execution_capability = "foreign-capability"
    with pytest.raises(RuntimeFailure, match="repository_execution_context_unavailable"):
        runtime.execution._attempt_context(job, **args)
    job.execution_capability = actual
    platform.inhibit("incident-forensics", "test context revocation")
    with pytest.raises(RuntimeFailure, match="repository_execution_context_unavailable"):
        runtime.execution._attempt_context(job, **args)


@pytest.mark.parametrize("areas", [["../escape"], ["src", "src"], ["tests", "src"]])
def test_installed_scope_must_be_valid_and_canonical_before_composition(installed, areas):
    settings, platform, path, config, runtime, base, driver = installed
    config["bindings"][0]["allowed_areas"] = areas
    save(path, config)
    assert compose_repository_runtime(settings, platform, driver_factory=lambda _: driver).owner is None


def test_world_writable_state_parent_cannot_enable_runtime(installed, tmp_path):
    settings, platform, path, config, runtime, base, driver = installed
    unsafe = tmp_path / "world-writable-parent"
    unsafe.mkdir(mode=0o700)
    unsafe.chmod(0o777)
    config["state_root"] = str(unsafe / "private-state")
    save(path, config)
    assert compose_repository_runtime(settings, platform, driver_factory=lambda _: driver).owner is None


@pytest.mark.parametrize("bad_layout", ["parent-link", "leaf-link", "leaf-permissions"])
def test_unsafe_state_directory_layout_disables_runtime(installed, tmp_path, bad_layout):
    settings, platform, path, config, runtime, base, driver = installed
    protected = tmp_path / "directory-root"
    protected.mkdir(mode=0o700)
    state = protected / "private-state"
    state.mkdir(mode=0o700)
    if bad_layout == "parent-link":
        link = tmp_path / "directory-link"
        link.symlink_to(protected, target_is_directory=True)
        state = link / "private-state"
    elif bad_layout == "leaf-link":
        link = protected / "state-link"
        link.symlink_to(state, target_is_directory=True)
        state = link
    else:
        state.chmod(0o750)
    config["state_root"] = str(state)
    save(path, config)
    closed = compose_repository_runtime(settings, platform, driver_factory=lambda _: driver)
    assert closed.owner is None and closed.execution is None
    assert closed.diagnostic == "repository_runtime_unavailable"


def test_private_directory_walk_validates_leaf_owner_and_sticky_ancestors(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace
    from sovite.repository_runtime import protected_directory
    state = tmp_path / "private-state"
    assert protected_directory(state, create=True) == state
    assert state.stat().st_mode & 0o777 == 0o700
    actual_stat = os.fstat
    leaf_identity = (state.stat().st_dev, state.stat().st_ino)
    def wrong_leaf_owner(fd):
        info = actual_stat(fd)
        if (info.st_dev, info.st_ino) == leaf_identity:
            return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1)
        return info
    monkeypatch.setattr("sovite.repository_runtime.os.fstat", wrong_leaf_owner)
    with pytest.raises(ValueError, match="repository_runtime_unavailable"):
        protected_directory(state)
    monkeypatch.setattr("sovite.repository_runtime.os.fstat", actual_stat)
    # A service-owned sticky directory is not a substitute for a root-owned
    # sticky ancestor. It must still be refused when writable by other users.
    if os.getuid() != 0:
        writable = tmp_path / "service-sticky"
        writable.mkdir(mode=0o700)
        writable.chmod(0o1777)
        with pytest.raises(ValueError):
            protected_directory(writable / "child", create=True)
        assert not (writable / "child").exists()


def test_retention_state_rejects_world_writable_parent(installed, tmp_path):
    settings, platform, path, config, runtime, base, driver = installed
    credential = tmp_path / "retention-token"
    credential.write_text("synthetic-workspace-token-0000000000\n")
    credential.chmod(0o600)
    profile_path = tmp_path / "retention-profile.json"
    save(profile_path, {"schema_version": "vuoro-client-profile/v1", "id": "retention-test",
        "revision": 1, "source_environment_id": "test",
        "target": {"environment_id": "workspace-test", "environment_class": "development",
                   "endpoint": "https://workspace.invalid"},
        "credential_ref": "file:" + str(credential),
        "required_authorities": ["knowledge:read", "knowledge:contribute"],
        "production_endpoint_denied": True})
    unsafe = tmp_path / "writable-publication-parent"
    unsafe.mkdir(mode=0o700)
    unsafe.chmod(0o777)
    target = {"endpoint": "https://workspace.invalid", "workspace_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
              "destination": "workspace-01ARZ3NDEKTSV4RRFFQ69G5FAV", "allowed_classes": ["C3"]}
    config["retention"] = {"root": str(unsafe / "publication-state"),
                           "profile_path": str(profile_path), "credential_file": str(credential),
                           "target": target}
    save(path, config)
    driver_calls = []
    def factory(value):
        driver_calls.append(value)
        return driver
    closed = compose_repository_runtime(settings, platform, driver_factory=factory)
    assert driver_calls == []
    assert closed.owner is None and closed.publication is None
    assert closed.diagnostic == "repository_runtime_unavailable"
    assert not (unsafe / "publication-state").exists()


def repair_installation(installed, tmp_path):
    settings, platform, path, config, runtime, base, driver = installed
    credential = tmp_path/'repair-reader'
    credential.write_text('synthetic-fixture-reader\n'); credential.chmod(0o600)
    profile_path = tmp_path/'repair-reader-profile.json'
    save(profile_path, {'schema_version':'vuoro-client-profile/v1', 'id':'repair-test',
        'revision':1, 'source_environment_id':'test',
        'target':{'environment_id':'test', 'environment_class':'development', 'endpoint':'https://workspace.invalid'},
        'credential_ref':'file:'+str(credential), 'required_authorities':['knowledge:read','knowledge:contribute'],
        'production_endpoint_denied':True})
    workspace = '01ARZ3NDEKTSV4RRFFQ69G5FAV'
    config['retention'] = {'root':str(tmp_path/'repair-publication'), 'profile_path':str(profile_path),
        'credential_file':str(credential), 'target':{'endpoint':'https://workspace.invalid',
        'workspace_id':workspace, 'destination':'workspace-'+workspace, 'allowed_classes':['C3']}}
    config['repair'] = {'max_repairs':1, 'feedback_contract_ids':['trusted-feedback']}
    driver.supports_repository_repair_context = True
    return settings, platform, path, config, runtime, base, driver


def test_repair_policy_is_opt_in_and_uses_same_owner_and_current_reader(installed, tmp_path, monkeypatch):
    from sovite import repository_runtime as module
    assert installed[4].repair is None
    settings, platform, path, config, _, _, driver = repair_installation(installed, tmp_path)
    save(path, config)
    runtime = compose_repository_runtime(settings, platform, driver_factory=lambda _:driver)
    assert runtime.repair is not None
    assert runtime.repair.owner is runtime.owner
    assert runtime.repair.retention is runtime.publication
    assert runtime.repair.policy.max_repairs == 1
    def installed_driver(_, actual_platform):
        assert actual_platform is platform
        return driver
    monkeypatch.setattr(module, '_builtin_driver', installed_driver)
    app = api.create_app(settings, platform)
    assert app.state.repository_repair_service is app.state.repository_runtime.repair
    assert app.state.repository_repair_service.owner is app.state.repository_run_owner


@pytest.mark.parametrize('fault', ['missing-retention','unsupported-driver','invalid-limit','unknown-field'])
def test_invalid_requested_repair_never_silently_enables_initial_execution(installed, tmp_path, fault):
    settings, platform, path, config, _, _, driver = repair_installation(installed, tmp_path)
    if fault == 'missing-retention': config['retention'] = None
    if fault == 'unsupported-driver': driver.supports_repository_repair_context = False
    if fault == 'invalid-limit': config['repair']['max_repairs'] = 4
    if fault == 'unknown-field': config['repair']['caller'] = 'made-up-authority'
    save(path, config)
    runtime = compose_repository_runtime(settings, platform, driver_factory=lambda _:driver)
    assert runtime.owner is None and runtime.repair is None
    assert runtime.diagnostic == 'repository_runtime_unavailable'
