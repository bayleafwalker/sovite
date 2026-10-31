"""Protected host composition for the existing repository-run owner.

Installed files narrow existing platform grants; they cannot create grants or
qualify a driver. Candidate processes must run outside this host UID and files.
No configuration value selects a Python module or an executable plugin.
"""
from __future__ import annotations

import copy
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
import stat
from typing import Any

from .db import Job, WorkerExecutionLease
from .repository_runs import (RepositoryRunAttemptExecutor, RepositoryRunBinding,
                              RepositoryRunOwner)
from .repository_sessions import (_canonical_json, _unique_pairs, validate_packet)


SCHEMA = "sovite-repository-runtime/v1"
_LIMIT = 1024 * 1024


def _need(value):
    if not value:
        raise ValueError("repository_runtime_unavailable")


def protected_bytes(value: str | Path, *, limit: int = _LIMIT) -> bytes:
    """Open every path component without following links, including parents."""
    path = Path(value)
    _need(path.is_absolute() and path == Path(os.path.normpath(path)))
    parts = path.parts[1:]
    _need(bool(parts))
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
            info = os.fstat(directory)
            # Root-owned sticky temporary roots are useful for isolated tests;
            # the installed file's immediate parent must still be protected.
            sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            _need(info.st_uid in {0, os.getuid()} and
                  (not info.st_mode & 0o022 or sticky_root))
        parent = os.fstat(directory)
        _need(not parent.st_mode & 0o022)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
        try:
            info = os.fstat(fd)
            _need(stat.S_ISREG(info.st_mode) and info.st_uid in {0, os.getuid()}
                  and info.st_nlink == 1 and not info.st_mode & 0o077
                  and info.st_size <= limit)
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(limit + 1)
            _need(len(raw) <= limit)
            return raw
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def protected_directory(value: str | Path, *, create: bool = False) -> Path:
    """Validate the full private-state path through pinned, no-follow directory FDs.

    A protected leaf alone is insufficient: an unrelated writer of a parent can
    rename that leaf and substitute its pathname. Every ancestor must therefore
    be root/service-owned and non-writable by other identities. Root-owned
    sticky ancestors (not the leaf) preserve ordinary /tmp test installations:
    unrelated users cannot rename the service-owned entry there. Root and the
    service UID remain trusted; this is not a same-UID or privileged-owner fence.
    """
    path = Path(value)
    _need(path.is_absolute() and path == Path(os.path.normpath(path)))
    parts = path.parts[1:]
    _need(bool(parts))
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for index, part in enumerate(parts):
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=directory)
            except FileNotFoundError:
                _need(create)
                try:
                    os.mkdir(part, mode=0o700, dir_fd=directory)
                except FileExistsError:
                    # A concurrent trusted creator may have installed it. Open
                    # without following links and validate its actual identity.
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=directory)
            try:
                info = os.fstat(child)
                leaf = index == len(parts) - 1
                if leaf:
                    _need(info.st_uid == os.getuid() and not info.st_mode & 0o077)
                else:
                    sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
                    _need(info.st_uid in {0, os.getuid()} and
                          (not info.st_mode & 0o022 or sticky_root))
            except BaseException:
                os.close(child)
                raise
            os.close(directory)
            directory = child
        return path
    finally:
        os.close(directory)


def _read_config(path):
    data = json.loads(protected_bytes(path), object_pairs_hook=_unique_pairs,
                      parse_constant=lambda _: _need(False))
    required = {"schema_version", "state_root", "bindings", "driver", "retention"}
    _need(type(data) is dict and set(data) in (required, required | {"repair"}))
    _need(data["schema_version"] == SCHEMA and type(data["bindings"]) is list
          and 1 <= len(data["bindings"]) <= 256 and type(data["driver"]) is dict)
    _need(type(data["state_root"]) is str and Path(data["state_root"]).is_absolute())
    bindings = [RepositoryRunBinding.from_mapping(row) for row in data["bindings"]]
    for binding in bindings:
        # Reuse the owner session scope validator before exposing launch.
        checked = validate_packet({"schema_version": "sovite-repository-session/v1",
            "binding_id": binding.binding_id, "job_id": "configuration-check",
            "workspace_id": binding.workspace_id, "repository_id": binding.repository_id,
            "base_sha": binding.base_sha, "allowed_areas": list(binding.allowed_areas),
            "deadline": binding.latest_deadline, "attempt_id": "configuration-check",
            "generation": binding.generation, "fence": "0" * 32})
        _need(checked.allowed_areas == binding.allowed_areas)
    _need(len({row.binding_id for row in bindings}) == len(bindings))
    _need(len({(row.workspace_id, row.repository_id, row.principal_id)
               for row in bindings}) == len(bindings))
    return data


def _fixed(data):
    return _canonical_json({key: value for key, value in data.items() if key != "bindings"})


class InstalledRepositoryResolver:
    """Live installed scope over current platform authorization, never a grant store."""
    def __init__(self, platform, path, config):
        self.platform, self.path = platform, Path(path)
        self.fixed = _fixed(config)

    def _source(self):
        data = _read_config(self.path)
        # Changing the driver, state location or disclosure target needs a new
        # composition; do not keep an old live driver under a changed file.
        _need(_fixed(data) == self.fixed)
        return data

    def _current(self, row, session):
        binding = RepositoryRunBinding.from_mapping(row)
        self.platform.authorize(session, binding.grant_id, binding.principal_id,
                                binding.input_class, generation=binding.generation)
        return binding

    def resolve_dispatch_binding(self, workspace_id, repository_id, principal_id):
        data = self._source()
        row = next((row for row in data["bindings"] if
                    (row["workspace_id"], row["repository_id"], row["principal_id"]) ==
                    (workspace_id, repository_id, principal_id)), None)
        _need(row is not None)
        with self.platform.db.sessions() as session:
            return asdict(self._current(row, session))

    def list_dispatch_bindings(self, workspace_id, principal_id):
        data = self._source()
        result = []
        with self.platform.db.sessions() as session:
            for row in data["bindings"]:
                if row["workspace_id"] == workspace_id and row["principal_id"] == principal_id:
                    try:
                        result.append(asdict(self._current(row, session)))
                    except Exception:
                        # A disabled grant is absent from discoverable launch scope.
                        continue
        return result

    def issue_attempt_packet(self, binding_id, *, job_id, attempt_id,
                             generation, fence, deadline):
        data = self._source()
        with self.platform.db.sessions() as session:
            job = session.get(Job, job_id)
            lease = session.get(WorkerExecutionLease, job_id)
            _need(job is not None and lease is not None and job.status == "running"
                  and not job.cancel_requested and lease.phase == "started"
                  and lease.fence == fence and job.lease_until == lease.lease_until)
            import time
            _need(job.lease_until is not None and job.lease_until > time.time()
                  and lease.generation == generation)
            run = job.resolved.get("repository_run")
            _need(isinstance(run, dict) and run.get("binding_id") == binding_id
                  and job.caller == run.get("principal_id")
                  and attempt_id == f"{job.id}-{job.attempts}"
                  and generation == job.generation == run.get("generation")
                  and deadline == run.get("deadline"))
            row = next((row for row in data["bindings"] if row["binding_id"] == binding_id), None)
            _need(row is not None)
            current = self._current(row, session)
            self.platform.authorize_existing_job(session, job)
            for field in ("workspace_id", "repository_id", "principal_id", "mirror_root",
                          "base_sha", "grant_id", "input_class", "generation", "executor_id",
                          "publication_revision", "qualification_ref", "qualification_sha256",
                          "qualification_expires_at"):
                _need(getattr(current, field) == run.get(field))
            _need(list(current.allowed_areas) == run.get("allowed_areas")
                  and current.max_cost_usd >= run["max_cost_usd"]
                  and current.max_concurrency >= run["concurrency"])
            from .repository_runs import _timestamp
            _need(_timestamp(deadline, "deadline") <= _timestamp(current.latest_deadline, "latest_deadline"))
            packet = {"schema_version": "sovite-repository-session/v1",
                      "binding_id": binding_id, "job_id": job.id,
                      "workspace_id": current.workspace_id, "repository_id": current.repository_id,
                      "base_sha": current.base_sha, "allowed_areas": list(current.allowed_areas),
                      "deadline": deadline, "attempt_id": attempt_id,
                      "generation": generation, "fence": fence}
            return {**validate_packet(packet).as_dict(), "mirror_root": current.mirror_root}


def _builtin_driver(config):
    # Fixed owner code only. An installation without the VM driver stays closed.
    from .repository_native_vm import create_driver
    return create_driver(config)


def _retention(exporter, config):
    from vuoro_client.profile import load_profile
    from .repository_candidate_retention import CandidatePublicationService, WorkspaceTarget
    _need(type(config) is dict and set(config) == {
        "root", "profile_path", "credential_file", "target"})
    raw_profile = protected_bytes(config["profile_path"])
    profile = load_profile(config["profile_path"])
    _need(protected_bytes(config["profile_path"]) == raw_profile)
    credential_path = str(Path(config["credential_file"]))
    _need(profile.credential_ref == "file:" + credential_path)
    protected_bytes(credential_path, limit=65536)
    target_config = config["target"]
    _need(type(target_config) is dict and set(target_config) == {
        "endpoint", "workspace_id", "destination", "allowed_classes"})
    _need(type(target_config["allowed_classes"]) is list)
    target = WorkspaceTarget(**{**target_config,
                              "allowed_classes": frozenset(target_config["allowed_classes"])})
    _need(profile.endpoint.rstrip("/") == target.endpoint.rstrip("/"))
    root = protected_directory(config["root"], create=True)
    def credential(ref):
        _need(ref == profile.credential_ref and
              protected_bytes(config["profile_path"]) == raw_profile)
        value = protected_bytes(credential_path, limit=65536).decode("utf-8").strip()
        _need(value and not any(ord(char) <= 32 or ord(char) == 127 for char in value))
        return value
    return CandidatePublicationService(exporter, root, target, profile, credential)


@dataclass(frozen=True)
class RepositoryRuntime:
    owner: Any = None
    execution: Any = None
    publication: Any = None
    diagnostic: str = "repository_runtime_not_configured"
    repair: Any = None


def compose_repository_runtime(settings, platform, *, driver_factory=None):
    """Compose the fixed builtin driver; tests may inject the same driver interface.

    Failure disables only optional repository execution. Fixed diagnostics never
    include private paths, upstream exceptions or credentials.
    """
    path = settings.repository_runtime_config
    if not path:
        return RepositoryRuntime()
    try:
        config = _read_config(path)
        protected_directory(config["state_root"], create=True)
        if config["retention"] is not None:
            _need(type(config["retention"]) is dict)
            protected_directory(config["retention"]["root"], create=True)
        resolver = InstalledRepositoryResolver(platform, path, config)
        driver = (driver_factory or _builtin_driver)(copy.deepcopy(config["driver"]))
        _need(callable(driver) and callable(driver.execution_profile))
        execution = RepositoryRunAttemptExecutor(
            platform, resolver, config["state_root"], driver,
            timeout_seconds=min(3600, settings.job_timeout_seconds),
            execution_profile=driver.execution_profile)
        owner = RepositoryRunOwner(platform, resolver, profile_enabled=True,
                                   stub_or_qualified_executor=execution,
                                   execution_profile=driver.execution_profile)
        from .repository_candidate_export import CandidateExportOwner
        exporter = CandidateExportOwner(platform, resolver, execution)
        publication = _retention(exporter, config["retention"]) if config["retention"] is not None else None
        repair = None
        if config.get("repair") is not None:
            from .repository_repairs import RepositoryRepairService, RepairPolicy
            selected = config["repair"]
            _need(type(selected) is dict and set(selected) == {"max_repairs", "feedback_contract_ids"}
                  and type(selected["feedback_contract_ids"]) is list
                  and 1 <= len(selected["feedback_contract_ids"]) <= 64
                  and publication is not None
                  and getattr(driver, "supports_repository_repair_context", False) is True)
            policy = RepairPolicy(selected["max_repairs"], tuple(selected["feedback_contract_ids"]))
            repair = RepositoryRepairService(owner, publication, policy)
        return RepositoryRuntime(owner, execution, publication, "repository_runtime_composed", repair)
    except Exception:
        return RepositoryRuntime(diagnostic="repository_runtime_unavailable")
