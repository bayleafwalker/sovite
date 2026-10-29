"""Owner-side admission and observation for versioned repository runs.

This is a dormant integration seam. The released configuration does not
install an owner resolver or mutable execution profile. Worker execution only
uses this path when an explicit, injected profile is present.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol

from .adapters import RuntimeFailure
from .db import Job, WorkerExecutionLease
from .repository_sessions import RepositorySessionManager, RepositoryWorkspace
from .service import Platform, PlatformError


RUN_SCHEMA = "sovite-repository-run/v1"
PORTABLE_PACKET_SCHEMA = "portable-work/v1"
PACKET_FIELDS = {
    "schema_version", "workspace_id", "repository_id", "executor_id",
    "base_sha", "goal", "max_cost_usd", "deadline", "concurrency",
}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_OPERATION_KEY = re.compile(r"^[A-Za-z0-9_.-]{16,128}$")
_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SHA256_REF = re.compile(r"^sha256:[0-9a-f]{64}$")


class RepositoryRunError(ValueError):
    def __init__(self, code: str, message: str, status: int = 409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise RepositoryRunError("invalid_request", f"{field} must be a timezone-aware ISO timestamp", 422)
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise RepositoryRunError("invalid_request", f"{field} must be a timezone-aware ISO timestamp", 422) from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise RepositoryRunError("invalid_request", f"{field} must be a timezone-aware ISO timestamp", 422)
    return result.astimezone(timezone.utc)


def validate_run_packet(value: Any) -> dict[str, Any]:
    """Validate the immutable dispatch request; authority comes from resolver."""
    if not isinstance(value, dict) or set(value) != PACKET_FIELDS:
        raise RepositoryRunError("invalid_request", "Repository-run fields do not match the versioned contract", 422)
    if value["schema_version"] != PORTABLE_PACKET_SCHEMA:
        raise RepositoryRunError("unsupported_contract", "Repository-run contract version is unsupported", 422)
    for field in ("workspace_id", "repository_id", "executor_id"):
        if not isinstance(value[field], str) or not _ID.fullmatch(value[field]):
            raise RepositoryRunError("invalid_request", f"{field} is invalid", 422)
    if not isinstance(value["base_sha"], str) or not _SHA1.fullmatch(value["base_sha"]):
        raise RepositoryRunError("invalid_request", "base_sha must be a full lowercase commit SHA", 422)
    goal = value["goal"]
    # Keep the portable-work packet bounds identical to Vuoro's source schema.
    valid_goal = isinstance(goal, str) and bool(goal.strip()) and len(goal) <= 8_000
    if not valid_goal:
        raise RepositoryRunError("invalid_request", "goal must contain 1 to 8000 characters", 422)
    cost = value["max_cost_usd"]
    if type(cost) not in {int, float} or not math.isfinite(cost) or not 0 <= cost <= 100_000:
        raise RepositoryRunError("invalid_request", "max_cost_usd must be finite and between 0 and 100000", 422)
    if type(value["concurrency"]) is not int or not 1 <= value["concurrency"] <= 20:
        raise RepositoryRunError("invalid_request", "concurrency must be an integer from 1 to 20", 422)
    _timestamp(value["deadline"], "deadline")
    # Canonical serialization rejects nested or unsupported Python values.
    json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    return dict(value)


def canonical_packet_digest(packet: Mapping[str, Any]) -> str:
    data = json.dumps(packet, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def expected_preview_id(packet: Mapping[str, Any], principal_id: str) -> str:
    """Match Cloud's stable preview ID without normalizing the source packet."""
    data = json.dumps({"packet": packet, "principal": principal_id},
                      sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class RepositoryRunBinding:
    """Trusted repository and grant binding returned by the owner resolver."""

    binding_id: str
    workspace_id: str
    repository_id: str
    principal_id: str
    mirror_root: str
    base_sha: str
    allowed_areas: tuple[str, ...]
    grant_id: str
    input_class: str | None
    generation: str
    executor_id: str
    publication_revision: str
    max_cost_usd: float
    max_concurrency: int
    latest_deadline: str
    qualification_ref: str
    qualification_sha256: str
    qualification_expires_at: str

    @classmethod
    def from_mapping(cls, value: Any) -> "RepositoryRunBinding":
        fields = set(cls.__dataclass_fields__)
        if not isinstance(value, Mapping) or set(value) != fields:
            raise RepositoryRunError("binding_unavailable", "Trusted repository-run binding is unavailable", 503)
        if any(not isinstance(value[name], str) or not _ID.fullmatch(value[name])
               for name in ("binding_id", "workspace_id", "repository_id", "principal_id", "grant_id", "executor_id")):
            raise RepositoryRunError("binding_unavailable", "Trusted repository-run binding is invalid", 503)
        if not isinstance(value["mirror_root"], str) or not Path(value["mirror_root"]).is_absolute():
            raise RepositoryRunError("binding_unavailable", "Trusted repository-run mirror is invalid", 503)
        if not isinstance(value["base_sha"], str) or not _SHA1.fullmatch(value["base_sha"]):
            raise RepositoryRunError("binding_unavailable", "Trusted repository-run base is invalid", 503)
        if not isinstance(value["allowed_areas"], (tuple, list)) or len(value["allowed_areas"]) > 256:
            raise RepositoryRunError("binding_unavailable", "Trusted repository-run areas are invalid", 503)
        if any(not isinstance(area, str) for area in value["allowed_areas"]):
            raise RepositoryRunError("binding_unavailable", "Trusted repository-run areas are invalid", 503)
        if not isinstance(value["generation"], str) or not _SHA256_REF.fullmatch(value["generation"]):
            raise RepositoryRunError("binding_unavailable", "Trusted repository-run generation is invalid", 503)
        if (type(value["max_cost_usd"]) not in {int, float} or not math.isfinite(value["max_cost_usd"])
                or value["max_cost_usd"] < 0 or type(value["max_concurrency"]) is not int
                or not 1 <= value["max_concurrency"] <= 20):
            raise RepositoryRunError("binding_unavailable", "Trusted repository-run limits are invalid", 503)
        _timestamp(value["latest_deadline"], "latest_deadline")
        if not isinstance(value["qualification_ref"], str) or not _ID.fullmatch(value["qualification_ref"]):
            raise RepositoryRunError("binding_unavailable", "Mutable-profile qualification reference is unavailable", 503)
        if not isinstance(value["qualification_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["qualification_sha256"]):
            raise RepositoryRunError("binding_unavailable", "Mutable-profile qualification digest is invalid", 503)
        _timestamp(value["qualification_expires_at"], "qualification_expires_at")
        if not isinstance(value["publication_revision"], str) or not value["publication_revision"]:
            raise RepositoryRunError("binding_unavailable", "Trusted executor publication is unavailable", 503)
        if value["input_class"] is not None and not isinstance(value["input_class"], str):
            raise RepositoryRunError("binding_unavailable", "Trusted repository-run input class is invalid", 503)
        return cls(**{**value, "allowed_areas": tuple(value["allowed_areas"])})


class RepositoryRunResolver(Protocol):
    def list_dispatch_bindings(self, workspace_id: str,
                               principal_id: str) -> list[Mapping[str, Any]]: ...

    def resolve_dispatch_binding(self, workspace_id: str, repository_id: str,
                                 principal_id: str) -> Mapping[str, Any]: ...

    def issue_attempt_packet(self, binding_id: str, *, job_id: str, attempt_id: str,
                             generation: str, fence: str, deadline: str) -> Mapping[str, Any]: ...


_PROFILE_REQUIREMENTS = {
    "per_attempt_isolation", "credential_containment", "exact_binding",
    "durable_checkpoint_and_candidate", "predecessor_reconciliation",
    "bounded_usage",
}
_PROFILE_FIELDS = _PROFILE_REQUIREMENTS | {
    "schema_version", "mode", "executor_id", "publication_revision",
    "qualification_ref", "qualification_sha256", "qualification_expires_at",
}


def _profile_issue(provider: Any, binding: RepositoryRunBinding) -> str | None:
    """Check current trusted composition claims, not physical enforcement."""
    try:
        profile = provider() if callable(provider) else None
        if not isinstance(profile, Mapping) or set(profile) != _PROFILE_FIELDS:
            return "execution_profile_unavailable"
        if (profile["schema_version"] != "sovite-repository-execution-profile/v1"
                or profile["mode"] != "mutable-native"
                or any(profile[name] is not True for name in _PROFILE_REQUIREMENTS)):
            return "execution_profile_requirements_missing"
        if (profile["executor_id"] != binding.executor_id
                or profile["publication_revision"] != binding.publication_revision
                or profile["qualification_ref"] != binding.qualification_ref
                or profile["qualification_sha256"] != binding.qualification_sha256
                or profile["qualification_expires_at"] != binding.qualification_expires_at):
            return "execution_profile_binding_mismatch"
        if _timestamp(binding.qualification_expires_at, "qualification_expires_at") <= datetime.now(timezone.utc):
            return "execution_profile_expired"
        return None
    except Exception:
        return "execution_profile_unavailable"


class RepositoryRunOwner:
    """Authenticated owner adapter; disabled unless explicitly composed."""

    def __init__(self, platform: Platform, resolver: RepositoryRunResolver, *,
                 profile_enabled: bool = False, stub_or_qualified_executor: Any = None,
                 execution_profile: Any = None):
        self.platform = platform
        self.resolver = resolver
        self.profile_enabled = profile_enabled is True and stub_or_qualified_executor is not None
        self.executor = stub_or_qualified_executor
        self.execution_profile = execution_profile

    def capabilities(self, workspace_id: str, principal_id: str) -> dict[str, Any]:
        try:
            raw_bindings = self.resolver.list_dispatch_bindings(workspace_id, principal_id)
            if not isinstance(raw_bindings, list) or len(raw_bindings) > 1000:
                raise ValueError
            bindings = [RepositoryRunBinding.from_mapping(item) for item in raw_bindings]
            if any(binding.workspace_id != workspace_id or binding.principal_id != principal_id
                   for binding in bindings):
                raise ValueError
        except Exception:
            return {"schema_version": RUN_SCHEMA, "workspace_id": workspace_id,
                    "executors": [], "availability": "unavailable",
                    "blockers": [{"code": "repository_binding_unavailable",
                                  "message": "Repository bindings could not be verified for this caller."}]}
        now = datetime.now(timezone.utc)
        grouped: dict[str, list[RepositoryRunBinding]] = {}
        for binding in bindings:
            grouped.setdefault(binding.executor_id, []).append(binding)
        executors = []
        for executor_id, rows in sorted(grouped.items()):
            issues = [(_profile_issue(self.execution_profile, row)
                       or ("repository_deadline_expired"
                           if _timestamp(row.latest_deadline, "latest_deadline") <= now else None))
                      for row in rows]
            eligible = [row for row, issue in zip(rows, issues) if issue is None]
            launchable = self.profile_enabled and bool(eligible)
            issue = next((item for item in issues if item), "execution_profile_unavailable")
            # An available entry describes only bindings that admission can use.
            # An unavailable entry retains the resolver's IDs for diagnostics.
            displayed_rows = eligible if launchable else rows
            blockers = ([] if launchable else [{
                "code": issue if self.profile_enabled else "execution_profile_unavailable",
                "message": ("The repository owner deadline has expired." if self.profile_enabled and issue == "repository_deadline_expired"
                            else "A current qualified mutable execution profile is required for this binding."),
            }])
            executors.append({
                "id": executor_id,
                "workspace_id": workspace_id,
                "label": executor_id,
                "owner": "Sovite",
                "publication_revision": displayed_rows[0].publication_revision,
                "observed_at": now.isoformat(),
                "launch_supported": launchable,
                "availability": "available" if launchable else "unavailable",
                "blockers": blockers,
                "repository_ids": sorted({row.repository_id for row in displayed_rows}),
                "limits": {
                    "max_cost_usd": min(100_000, *(row.max_cost_usd for row in displayed_rows)),
                    "max_concurrency": min(row.max_concurrency for row in displayed_rows),
                    "latest_deadline": min((deadline for row in displayed_rows
                                            for deadline in (row.latest_deadline, row.qualification_expires_at)),
                                           key=lambda value: _timestamp(value, "deadline")),
                },
            })
        aggregate = [] if any(item["launch_supported"] for item in executors) else [{
            "code": "mutable_profile_unqualified",
            "message": "No qualified mutable repository executor is available for this caller.",
        }]
        return {"schema_version": RUN_SCHEMA, "workspace_id": workspace_id,
                "executors": executors,
                "availability": "available" if any(item["launch_supported"] for item in executors) else "unavailable",
                "blockers": aggregate}

    def _binding(self, packet: dict[str, Any], principal_id: str) -> RepositoryRunBinding:
        try:
            raw = self.resolver.resolve_dispatch_binding(packet["workspace_id"],
                                                        packet["repository_id"], principal_id)
        except Exception:
            raise RepositoryRunError("binding_unavailable", "Trusted repository binding could not be resolved", 503) from None
        binding = RepositoryRunBinding.from_mapping(raw)
        if (binding.workspace_id != packet["workspace_id"]
                or binding.repository_id != packet["repository_id"]
                or binding.principal_id != principal_id):
            raise RepositoryRunError("binding_mismatch", "Repository binding does not match the authenticated principal", 403)
        if binding.executor_id != packet["executor_id"] or binding.base_sha != packet["base_sha"]:
            raise RepositoryRunError("binding_mismatch", "Packet does not match the owner-issued executor and base", 409)
        deadline = _timestamp(packet["deadline"], "deadline")
        qualification_expires = _timestamp(binding.qualification_expires_at, "qualification_expires_at")
        if not self.profile_enabled or _profile_issue(self.execution_profile, binding) is not None:
            raise RepositoryRunError("executor_unavailable", "No qualified mutable repository executor is installed", 409)
        if deadline > qualification_expires:
            raise RepositoryRunError("deadline_outside_qualification", "Run deadline exceeds the executor qualification", 409)
        cost, concurrency = packet["max_cost_usd"], packet["concurrency"]
        latest = _timestamp(binding.latest_deadline, "latest_deadline")
        now = datetime.now(timezone.utc)
        if deadline <= now or deadline > latest:
            raise RepositoryRunError("deadline_outside_owner_limit", "Run deadline is expired or exceeds the owner limit", 409)
        if cost > binding.max_cost_usd or concurrency > binding.max_concurrency:
            raise RepositoryRunError("limits_exceeded", "Run exceeds the owner's published limits", 409)
        return binding

    def submit(self, packet_value: Any, *, preview_id: str, principal_id: str,
               idempotency_key: str) -> dict[str, Any]:
        packet = validate_run_packet(packet_value)
        if not isinstance(principal_id, str) or not _ID.fullmatch(principal_id):
            raise RepositoryRunError("unauthorized", "Repository-run principal is invalid", 401)
        if not isinstance(idempotency_key, str) or not _OPERATION_KEY.fullmatch(idempotency_key):
            raise RepositoryRunError("invalid_idempotency_key", "Idempotency-Key must contain 16 to 128 safe characters", 422)
        if (not isinstance(preview_id, str) or not re.fullmatch(r"[0-9a-f]{64}", preview_id)
                or preview_id != expected_preview_id(packet, principal_id)):
            raise RepositoryRunError("preview_mismatch", "Preview does not bind this exact packet and principal", 409)
        binding = self._binding(packet, principal_id)
        internal = {
            "schema_version": RUN_SCHEMA,
            "packet_sha256": canonical_packet_digest(packet),
            "preview_id": preview_id,
            "idempotency_key": idempotency_key,
            "workspace_id": binding.workspace_id,
            "repository_id": binding.repository_id,
            "principal_id": binding.principal_id,
            "binding_id": binding.binding_id,
            "mirror_root": binding.mirror_root,
            "grant_id": binding.grant_id,
            "input_class": binding.input_class,
            "base_sha": binding.base_sha,
            "allowed_areas": list(binding.allowed_areas),
            "executor_id": binding.executor_id,
            "publication_revision": binding.publication_revision,
            "qualification_ref": binding.qualification_ref,
            "qualification_sha256": binding.qualification_sha256,
            "qualification_expires_at": binding.qualification_expires_at,
            "generation": binding.generation,
            "max_cost_usd": packet["max_cost_usd"],
            "deadline": packet["deadline"],
            "concurrency": packet["concurrency"],
            "usage_usd": 0.0,
            "budget_reservations": [],
            "active_slots": [],
            "peak_concurrency": 0,
            "attempts": [],
            "evidence": [],
        }
        job = self.platform.submit(binding.grant_id, packet["goal"], principal_id,
                                   idempotency_key, binding.input_class,
                                   expected_generation=binding.generation,
                                   repository_run=internal)
        return self.project(job)

    @staticmethod
    def project(job: Job) -> dict[str, Any]:
        internal = job.resolved.get("repository_run")
        if not isinstance(internal, dict) or internal.get("schema_version") != RUN_SCHEMA:
            raise RepositoryRunError("run_unavailable", "Repository-run state is unavailable", 503)
        attempts = []
        for stored in internal.get("attempts", []):
            if not isinstance(stored, dict):
                continue
            state = stored.get("state", "unknown")
            if job.status == "blocked" and job.error_code == "execution_uncertain" and state in {"preparing", "running", "interrupted"}:
                state = "uncertain"
            elif job.status == "cancelled" and state in {"preparing", "running", "interrupted"}:
                state = "cancelled"
            elif job.status in {"failed", "blocked"} and state in {"preparing", "running", "interrupted"}:
                state = "failed"
            projected = {"id": stored.get("id", "unknown"), "state": state,
                         "provider": "sovite", "model": internal["executor_id"]}
            for field in ("started_at", "finished_at", "checkpoint_sha"):
                if stored.get(field) is not None:
                    projected[field] = stored[field]
            attempts.append(projected)
        return {
            "schema_version": RUN_SCHEMA,
            "id": job.id,
            "workspace_id": internal["workspace_id"],
            "repository_id": internal["repository_id"],
            "executor_id": internal["executor_id"],
            "preview_id": internal["preview_id"],
            "idempotency_key": internal["idempotency_key"],
            "state": job.status,
            "source": "sovite-owner-v1",
            "submitted_at": datetime.fromtimestamp(job.created_at, timezone.utc).isoformat(),
            "observed_at": datetime.fromtimestamp(job.updated_at, timezone.utc).isoformat(),
            "attempts": attempts,
            "evidence": list(internal.get("evidence", [])),
        }

    def get(self, run_id: str, *, principal_id: str, workspace_id: str | None = None) -> dict[str, Any]:
        if not isinstance(run_id, str) or not _ID.fullmatch(run_id):
            raise RepositoryRunError("run_not_found", "Repository run was not found", 404)
        with self.platform.db.sessions() as session:
            job = session.get(Job, run_id)
            if (job is None or job.caller != principal_id
                    or not isinstance(job.resolved.get("repository_run"), dict)
                    or workspace_id is not None and job.resolved["repository_run"].get("workspace_id") != workspace_id):
                raise RepositoryRunError("run_not_found", "Repository run was not found", 404)
            return self.project(job)

    def cancel(self, run_id: str, *, principal_id: str, workspace_id: str) -> dict[str, Any]:
        # Platform.cancel owns the same queue, lease and cancellation path used
        # by existing jobs. This wrapper only narrows authority to the current
        # repository-run principal and workspace.
        current = self.get(run_id, principal_id=principal_id, workspace_id=workspace_id)
        job = self.platform.cancel(run_id, actor=principal_id, require_owner=True)
        return self.project(job)

    def list(self, *, principal_id: str, workspace_id: str, limit: int = 100) -> list[dict[str, Any]]:
        from sqlalchemy import select
        with self.platform.db.sessions() as session:
            rows = session.scalars(select(Job).where(Job.caller == principal_id)
                                   .order_by(Job.created_at.desc()).limit(max(1, min(limit, 500)))).all()
            return [self.project(job) for job in rows
                    if isinstance(job.resolved.get("repository_run"), dict)
                    and job.resolved["repository_run"].get("workspace_id") == workspace_id]


class _AttemptBindingResolver:
    """Expose one resolver-issued attempt packet to the workspace utility."""

    def __init__(self, packet: Mapping[str, Any]):
        self.packet = dict(packet)

    def resolve_repository_binding(self, binding_id: str) -> Mapping[str, Any]:
        if binding_id != self.packet.get("binding_id"):
            raise KeyError(binding_id)
        return self.packet


class RepositoryRunBudget:
    """Persist reservation-before-call cost and concurrency under the attempt fence.

    A qualified executor must reserve cost and acquire a slot before issuing a
    paid/provider or parallel operation, then settle/release after it is
    reconciled. Ambiguous outcomes retain their reservation and block replay.
    """

    def __init__(self, maximum_usd: float, maximum_concurrency: int, *, used_usd: float,
                 reservations: list[dict], active_slots: list[str], peak_concurrency: int,
                 authorization_check, persist):
        self.maximum_usd = maximum_usd
        self.maximum_concurrency = maximum_concurrency
        self.used_usd = used_usd
        self.reservations = copy.deepcopy(reservations)
        self.active_slots = list(active_slots)
        self.peak_concurrency = peak_concurrency
        self.authorization_check, self.persist = authorization_check, persist

    @staticmethod
    def _call_id(value: Any) -> str:
        if not isinstance(value, str) or not _ID.fullmatch(value):
            raise RuntimeFailure("repository_budget_operation_invalid")
        return value

    def reserve_cost(self, call_id: str, amount_usd: float) -> None:
        call_id = self._call_id(call_id)
        if type(amount_usd) not in {int, float} or not math.isfinite(amount_usd) or amount_usd <= 0:
            raise RuntimeFailure("repository_cost_reservation_invalid")
        self.authorization_check()
        existing = next((row for row in self.reservations if row["call_id"] == call_id), None)
        if existing:
            if existing["reserved_usd"] == float(amount_usd) and existing["state"] == "reserved":
                return
            raise RuntimeFailure("repository_budget_operation_conflict")
        reserved = sum(row["reserved_usd"] for row in self.reservations if row["state"] == "reserved")
        if self.used_usd + reserved + float(amount_usd) > self.maximum_usd:
            raise RuntimeFailure("repository_cost_limit_exceeded")
        record = {"call_id": call_id, "reserved_usd": float(amount_usd),
                  "state": "reserved", "actual_usd": None}
        self.persist(lambda run: run["budget_reservations"].append(record))
        self.reservations.append(record)

    def settle_cost(self, call_id: str, actual_usd: float) -> None:
        call_id = self._call_id(call_id)
        if type(actual_usd) not in {int, float} or not math.isfinite(actual_usd) or actual_usd < 0:
            raise RuntimeFailure("repository_cost_observation_invalid")
        record = next((row for row in self.reservations if row["call_id"] == call_id), None)
        if record is None or record["state"] != "reserved":
            raise RuntimeFailure("repository_budget_operation_conflict")
        actual = float(actual_usd)
        record = {**record, "state": "settled", "actual_usd": actual}
        def settle(run):
            for index, row in enumerate(run["budget_reservations"]):
                if row["call_id"] == call_id and row["state"] == "reserved":
                    run["budget_reservations"][index] = record
                    run["usage_usd"] += actual
                    break
            else:
                raise RepositoryRunError("budget_state_conflict", "Cost reservation is no longer current", 409)
        self.persist(settle)
        self.reservations = [record if row["call_id"] == call_id else row for row in self.reservations]
        self.used_usd += actual
        if actual > record["reserved_usd"] or self.used_usd > self.maximum_usd:
            raise RuntimeFailure("external_execution_uncertain")

    def acquire_slot(self, slot_id: str) -> None:
        slot_id = self._call_id(slot_id)
        self.authorization_check()
        if slot_id in self.active_slots:
            return
        if len(self.active_slots) >= self.maximum_concurrency:
            raise RuntimeFailure("repository_concurrency_limit_exceeded")
        active = [*self.active_slots, slot_id]
        peak = max(self.peak_concurrency, len(active))
        self.persist(lambda run: run.update(active_slots=active, peak_concurrency=peak))
        self.active_slots, self.peak_concurrency = active, peak

    def release_slot(self, slot_id: str) -> None:
        slot_id = self._call_id(slot_id)
        if slot_id not in self.active_slots:
            raise RuntimeFailure("repository_budget_operation_conflict")
        active = [value for value in self.active_slots if value != slot_id]
        self.persist(lambda run: run.update(active_slots=active))
        self.active_slots = active


class RepositoryRunAttemptExecutor:
    """Fenced, bounded lifecycle around an injected executor implementation.

    Production native execution is deliberately not supplied here. Tests inject
    a deterministic local stub; a real executor must separately qualify its
    mutable process boundary before being composed.
    """

    def __init__(self, platform: Platform, resolver: RepositoryRunResolver,
                 state_root: str | Path, executor: Any, *, timeout_seconds: int = 300,
                 execution_profile: Any = None):
        if not callable(executor) or type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3600:
            raise ValueError("An executor and a bounded timeout are required")
        self.platform, self.resolver = platform, resolver
        self.state_root, self.executor, self.timeout_seconds = Path(state_root), executor, timeout_seconds
        self.execution_profile = execution_profile

    def _persist(self, job_id: str, fence: str, mutate) -> None:
        with self.platform.db.sessions.begin() as session:
            job = session.get(Job, job_id, with_for_update=True)
            lease = session.get(WorkerExecutionLease, job_id, with_for_update=True)
            if (job is None or lease is None or job.status != "running"
                    or lease.phase != "started" or lease.fence != fence
                    or job.lease_until != lease.lease_until):
                raise RepositoryRunError("execution_fenced", "Repository attempt is no longer current", 409)
            resolved = copy.deepcopy(job.resolved)
            run = resolved.get("repository_run", {})
            mutate(run)
            resolved["repository_run"] = run
            job.resolved = resolved
            job.updated_at = time.time()

    def effect_started(self, job_id: str) -> bool:
        """Conservatively treat a running mutable attempt as potentially effectful."""
        with self.platform.db.sessions() as session:
            job = session.get(Job, job_id)
            if job is None:
                return True
            record = job.resolved.get("repository_run")
            attempts = record.get("attempts", []) if isinstance(record, dict) else []
            return bool(attempts and attempts[-1].get("state") in {"running", "interrupted", "uncertain"})

    def check_authorization(self, job: Job) -> None:
        """Recheck the live repository binding and qualification during a run."""
        record = job.resolved.get("repository_run")
        if not isinstance(record, dict) or record.get("schema_version") != RUN_SCHEMA:
            raise RuntimeFailure("repository_run_invalid")
        try:
            current = RepositoryRunBinding.from_mapping(self.resolver.resolve_dispatch_binding(
                record["workspace_id"], record["repository_id"], record["principal_id"]))
            deadline = _timestamp(record["deadline"], "deadline")
            if (_profile_issue(self.execution_profile, current) is not None
                    or current.workspace_id != record["workspace_id"]
                    or current.repository_id != record["repository_id"]
                    or current.principal_id != record["principal_id"]
                    or current.binding_id != record["binding_id"]
                    or current.mirror_root != record["mirror_root"]
                    or current.grant_id != record["grant_id"]
                    or current.input_class != record["input_class"]
                    or current.base_sha != record["base_sha"]
                    or list(current.allowed_areas) != record["allowed_areas"]
                    or current.executor_id != record["executor_id"]
                    or current.publication_revision != record["publication_revision"]
                    or current.generation != record["generation"]
                    or current.qualification_ref != record["qualification_ref"]
                    or current.qualification_sha256 != record["qualification_sha256"]
                    or current.qualification_expires_at != record["qualification_expires_at"]
                    or current.max_cost_usd < record["max_cost_usd"]
                    or current.max_concurrency < record["concurrency"]
                    or deadline > _timestamp(current.latest_deadline, "latest_deadline")
                    or deadline > _timestamp(current.qualification_expires_at, "qualification_expires_at")
                    or deadline <= datetime.now(timezone.utc)):
                raise ValueError
        except Exception:
            raise RuntimeFailure("repository_binding_revoked") from None

    def _mark_attempt(self, job_id: str, fence: str, attempt_id: str,
                      state: str, error_code: str | None = None) -> None:
        def update_attempt(run):
            attempts = run.get("attempts", [])
            if attempts and attempts[-1].get("id") == attempt_id:
                attempts[-1]["state"] = state
                attempts[-1]["finished_at"] = datetime.now(timezone.utc).isoformat()
                if error_code:
                    attempts[-1]["error_code"] = error_code
        try:
            self._persist(job_id, fence, update_attempt)
        except RepositoryRunError:
            # A revoked or expired fence must not mutate a later attempt's
            # durable observation. Recovery projects the old one as uncertain.
            return

    async def run(self, job: Job, *, fence: str, authorization_check) -> dict[str, Any]:
        record = job.resolved.get("repository_run")
        if not isinstance(record, dict) or record.get("schema_version") != RUN_SCHEMA:
            raise RuntimeFailure("repository_run_invalid")
        attempt_id = f"{job.id}-{job.attempts}"
        self.check_authorization(job)
        try:
            raw = self.resolver.issue_attempt_packet(record["binding_id"], job_id=job.id,
                attempt_id=attempt_id, generation=job.generation, fence=fence,
                deadline=record["deadline"])
        except Exception:
            raise RuntimeFailure("repository_binding_unavailable") from None
        try:
            packet_value = {key: raw[key] for key in (
                "schema_version", "binding_id", "job_id", "workspace_id",
                "repository_id", "base_sha", "allowed_areas", "deadline",
                "attempt_id", "generation", "fence",
            )}
            if packet_value.get("job_id") != job.id or packet_value.get("attempt_id") != attempt_id or packet_value.get("generation") != job.generation or packet_value.get("fence") != fence:
                raise ValueError
            if (packet_value.get("binding_id") != record.get("binding_id")
                    or raw.get("mirror_root") != record.get("mirror_root")
                    or packet_value.get("workspace_id") != record.get("workspace_id")
                    or packet_value.get("repository_id") != record.get("repository_id")
                    or packet_value.get("base_sha") != record.get("base_sha")
                    or sorted(packet_value.get("allowed_areas", [])) != sorted(record.get("allowed_areas", []))
                    or packet_value.get("deadline") != record.get("deadline")):
                raise ValueError
            from .repository_sessions import validate_packet
            packet = validate_packet(packet_value)
        except Exception:
            raise RuntimeFailure("repository_binding_mismatch") from None
        digest = hashlib.sha256(json.dumps(packet.as_dict(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        attempt = {"id": attempt_id, "state": "preparing", "fence_sha256": hashlib.sha256(fence.encode()).hexdigest(),
                   "packet_sha256": digest, "base_sha": packet.base_sha, "started_at": datetime.now(timezone.utc).isoformat()}
        self._persist(job.id, fence, lambda run: run["attempts"].append(dict(attempt)))
        executor_started = False
        try:
            manager = RepositorySessionManager(self.state_root, _AttemptBindingResolver(raw))
            workspace = manager.prepare(packet.as_dict())
            self._persist(job.id, fence, lambda run: run["attempts"][-1].update({"state": "running"}))
            run_record = job.resolved["repository_run"]
            goal = job.task
            deadline = min(time.time() + self.timeout_seconds, _timestamp(run_record["deadline"], "deadline").timestamp())
            remaining = deadline - time.time()
            if remaining <= 0:
                raise RuntimeFailure("repository_run_deadline")
            if workspace.state != "candidate":
                def persist_budget(mutate):
                    self._persist(job.id, fence, mutate)
                budget = RepositoryRunBudget(
                    run_record["max_cost_usd"], run_record["concurrency"],
                    used_usd=run_record.get("usage_usd", 0.0),
                    reservations=run_record.get("budget_reservations", []),
                    active_slots=run_record.get("active_slots", []),
                    peak_concurrency=run_record.get("peak_concurrency", 0),
                    authorization_check=authorization_check, persist=persist_budget)
                authorization_check()
                executor_started = True
                await asyncio.wait_for(
                    self.executor(workspace.worktree, goal, deadline, budget, authorization_check),
                    timeout=remaining)
            authorization_check()
            evidence = manager.capture_candidate(packet.as_dict())
            evidence_path = manager.receipts_root / f"{manager._attempt_key(packet)}.json"
            evidence_digest = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
            evidence_projection = {"label": "candidate", "uri": f"sovite://repository-runs/{job.id}/{attempt_id}/candidate",
                                  "sha256": evidence_digest}
            attempt_result = {"state": "completed" if evidence["publication_eligible"] else "ineligible",
                              "finished_at": datetime.now(timezone.utc).isoformat(),
                              "checkpoint_sha": evidence["tip_sha"] if _SHA1.fullmatch(evidence["tip_sha"]) else None}
            def finish_attempt(run):
                attempts = run["attempts"]
                if not attempts or attempts[-1]["id"] != attempt_id or attempts[-1]["fence_sha256"] != attempt["fence_sha256"]:
                    raise RepositoryRunError("execution_fenced", "Repository attempt record changed", 409)
                attempts[-1].update(attempt_result)
                run["evidence"] = [evidence_projection]
            self._persist(job.id, fence, finish_attempt)
            if not evidence["publication_eligible"]:
                raise RuntimeFailure("repository_candidate_ineligible")
            return {"kind": "repository-candidate", "base_sha": evidence["base_sha"],
                    "tip_sha": evidence["tip_sha"], "patch_sha256": evidence["patch_sha256"],
                    "changed_paths": evidence["changed_paths"], "evidence_sha256": evidence_projection["sha256"]}
        except asyncio.CancelledError:
            self._mark_attempt(job.id, fence, attempt_id, "interrupted")
            raise
        except asyncio.TimeoutError:
            if executor_started:
                self._mark_attempt(job.id, fence, attempt_id, "uncertain", "execution_uncertain")
                raise RuntimeFailure("external_execution_uncertain") from None
            self._mark_attempt(job.id, fence, attempt_id, "failed", "repository_run_timeout")
            raise RuntimeFailure("repository_run_timeout") from None
        except RuntimeFailure as exc:
            if exc.code != "repository_candidate_ineligible":
                if executor_started:
                    self._mark_attempt(job.id, fence, attempt_id, "uncertain", "execution_uncertain")
                    raise RuntimeFailure("external_execution_uncertain") from None
                self._mark_attempt(job.id, fence, attempt_id, "failed", exc.code)
            raise
        except Exception as exc:
            code = getattr(exc, "code", "repository_executor_failed")
            if executor_started:
                self._mark_attempt(job.id, fence, attempt_id, "uncertain", "execution_uncertain")
                raise RuntimeFailure("external_execution_uncertain") from None
            self._mark_attempt(job.id, fence, attempt_id, "failed", code)
            raise RuntimeFailure(code) from None
