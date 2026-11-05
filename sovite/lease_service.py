from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import re
import time
import uuid
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Text, and_, case, cast, func, inspect, literal, or_, select, text

from .adapters import RuntimeFailure
from .authority import check_execution_binding, issue_execution, refresh_execution, revoke_execution, sweep_terminal_authority
from .db import ExecutionAuthority, Job, MetadataRow, WorkerExecutionLease, WorkerExecutionOperation, WorkerHeartbeat
from .service import Platform, PlatformError, TERMINAL


class _HandBackOwnerUnavailable(RuntimeError):
    """A complete Job/Work owner snapshot could not be established."""


class _RecoverySweepIncomplete(RuntimeError):
    """Bounded recovery pass has more candidates."""


class _FreshReservationNeedsRecovery(RuntimeError):
    """Internal transaction boundary before a fresh reservation."""


class FinishRefused(RuntimeError):
    """A finish owner decision was unavailable; execution must not be reclassified."""


def _native_activation_preentry_matches(job, run, attempt, fence: str, execution_deadline) -> bool:
    """Check the persisted native activation descriptor against this exact attempt."""
    activation = attempt.get("activation") if type(attempt) is dict else None
    expected_fields = {
        "schema_version", "job_id", "attempt_id", "workspace_id", "repository_id",
        "principal_id", "generation", "fence_sha256", "packet_sha256", "deadline",
    }
    if (type(activation) is not dict or set(activation) != expected_fields
            or activation.get("schema_version") != "sovite-native-activation/v1"
            or type(fence) is not str or not fence):
        return False
    fence_digest = hashlib.sha256(fence.encode("utf-8")).hexdigest()
    packet_digest = attempt.get("packet_sha256")
    deadline = activation.get("deadline")
    if (type(packet_digest) is not str or re.fullmatch(r"[0-9a-f]{64}", packet_digest) is None
            or type(deadline) not in {int, float} or isinstance(deadline, bool)
            or not math.isfinite(deadline)
            or type(execution_deadline) not in {int, float} or isinstance(execution_deadline, bool)
            or not math.isfinite(execution_deadline)):
        return False
    try:
        run_deadline_value = datetime.fromisoformat(run["deadline"].replace("Z", "+00:00"))
        if run_deadline_value.tzinfo is None or run_deadline_value.utcoffset() is None:
            return False
        run_deadline = run_deadline_value.timestamp()
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return False
    if (not math.isfinite(run_deadline)
            or any(type(value) is not str or not value for value in (
                job.id, job.caller, job.generation, run.get("workspace_id"),
                run.get("repository_id"), attempt.get("id"),
            ))):
        return False
    return (
        activation["job_id"] == job.id
        and activation["attempt_id"] == attempt.get("id") == f"{job.id}-{job.attempts}"
        and activation["workspace_id"] == run.get("workspace_id")
        and activation["repository_id"] == run.get("repository_id")
        and activation["principal_id"] == job.caller == run.get("principal_id")
        and activation["generation"] == job.generation
        and activation["fence_sha256"] == fence_digest == attempt.get("fence_sha256")
        and activation["packet_sha256"] == packet_digest
        and deadline <= execution_deadline
        and deadline <= run_deadline
    )


# Arbitrary constant naming the PostgreSQL advisory lock that serializes claims.
CLAIM_LOCK = 0x50617669746531


def replay_safe(resolved: dict) -> bool:
    # A repository edit may have reached an external provider or tool before a
    # worker disappeared. Preserve the workspace/evidence and require an
    # explicit owner reconciliation; never replay it as an ordinary task.
    if resolved.get("repository_run") is not None:
        return False
    harness = resolved["harness"]
    return harness["kind"] in {"demo", "tool-agent"} and all(t.get("read_only", t["kind"] == "kubernetes-read") for t in resolved.get("tools", [])) or harness.get("idempotent", False)


def _recovery_replay_safe(resolved: dict) -> bool:
    """Validate stored replay flags before they can authorize a retry."""
    if resolved.get("repository_run") is not None:
        return False
    harness = resolved.get("harness")
    tools = resolved.get("tools", [])
    if type(harness) is not dict or type(tools) is not list:
        raise _HandBackOwnerUnavailable()
    if harness.get("kind") not in {"demo", "tool-agent", "native-codex", "native-claude", "native-opencode", "agent-http"}:
        raise _HandBackOwnerUnavailable()
    if "idempotent" in harness and type(harness["idempotent"]) is not bool:
        raise _HandBackOwnerUnavailable()
    for tool in tools:
        if (type(tool) is not dict or tool.get("kind") not in {"http-json", "kubernetes-read"}
                or ("read_only" in tool and type(tool["read_only"]) is not bool)):
            raise _HandBackOwnerUnavailable()
    return bool(replay_safe(resolved))


def _validate_recovery_retry_recipe(resolved: dict) -> None:
    """Reject malformed stored fields required when a recovered Job is retried."""
    model, grant, harness, agent = (resolved.get(key) for key in ("model", "grant", "harness", "agent"))
    tools = resolved.get("tools", [])
    if (type(model) is not dict or type(model.get("id")) is not str
            or ("connection" in model and type(model["connection"]) is not dict)
            or ("max_concurrency" in model and
                (type(model["max_concurrency"]) is not int or not 1 <= model["max_concurrency"] <= 64))
            or type(grant) is not dict or type(grant.get("input_class")) is not str
            or type(agent) is not dict or type(agent.get("id")) is not str or not agent["id"]
            or type(harness) is not dict or type(harness.get("kind")) is not str
            or type(tools) is not list or any(type(tool) is not dict for tool in tools)
            or any(resolved.get(key) is not None and type(resolved[key]) is not dict
                   for key in ("previous_attempt", "workflow_binding"))):
        raise _HandBackOwnerUnavailable()
    if (harness["kind"] not in {"demo", "tool-agent", "native-codex", "native-claude", "native-opencode", "agent-http"}
            or ("idempotent" in harness and type(harness["idempotent"]) is not bool)
            or any(type(tool.get("kind")) is not str or tool["kind"] not in {"http-json", "kubernetes-read"}
                   or ("read_only" in tool and type(tool["read_only"]) is not bool)
                   for tool in tools)
            or ("max_attempts" in grant and
                (type(grant["max_attempts"]) is not int or not 1 <= grant["max_attempts"] <= 10))
            or ("timeout_seconds" in grant and
                (type(grant["timeout_seconds"]) is not int or not 1 <= grant["timeout_seconds"] <= 3600))):
        raise _HandBackOwnerUnavailable()
    prior = resolved.get("previous_attempt")
    if prior is not None and any(type(prior.get(field)) is not str or not prior[field]
                                 for field in ("receipt", "job_id", "submission_role")):
        raise _HandBackOwnerUnavailable()
    if any(resolved.get(field) is not None and
           (type(resolved[field]) is not str or not resolved[field])
           for field in ("source_release", "transfer_id")):
        raise _HandBackOwnerUnavailable()
    run = resolved.get("repository_run")
    if run is not None:
        from .repository_runs import RepositoryRunError, _timestamp
        if type(run) is not dict:
            raise _HandBackOwnerUnavailable()
        try:
            deadline = _timestamp(run.get("deadline"), "deadline").timestamp()
        except (RepositoryRunError, TypeError, ValueError, OverflowError, OSError):
            raise _HandBackOwnerUnavailable() from None
        if not math.isfinite(deadline):
            raise _HandBackOwnerUnavailable()
        repair = run.get("repair")
        if repair is not None:
            repair_fields = {"parent_run_id", "root_run_id", "repair_number", "max_repairs",
                             "candidate_sha256", "candidate_tip_sha", "candidate_release_id",
                             "feedback_release_id", "feedback_sha256", "feedback_contract_ids",
                             "input_key", "input_sha256"}
            string_fields = repair_fields - {"repair_number", "max_repairs", "feedback_contract_ids"}
            if (type(repair) is not dict or set(repair) != repair_fields
                    or any(type(repair[field]) is not str or not repair[field] for field in string_fields)
                    or type(repair["repair_number"]) is not int
                    or type(repair["max_repairs"]) is not int
                    or not 1 <= repair["repair_number"] <= repair["max_repairs"] <= 3
                    or type(repair["feedback_contract_ids"]) is not list
                    or not repair["feedback_contract_ids"]
                    or any(type(item) is not str or not item for item in repair["feedback_contract_ids"])):
                raise _HandBackOwnerUnavailable()
    binding = resolved.get("workflow_binding")
    if binding is None and (resolved.get("source_release") is not None or resolved.get("transfer_id") is not None):
        raise _HandBackOwnerUnavailable()
    if binding is not None:
        from .workflows import (API_VERSION, WorkflowError, _digest as workflow_digest,
                                delivery_role_names, validate_revision_ref, validate_workflow)
        binding_fields = {"apiVersion", "kind", "workflow_revision", "definition", "installation_id",
                          "bundle_generation", "input_class", "transfers", "roles", "requirements_ref",
                          "session_policy_ref", "dispatch_authorized", "binding_digest"}
        try:
            if (type(binding) is not dict or set(binding) != binding_fields
                    or binding["apiVersion"] != API_VERSION or binding["kind"] != "DeliveryWorkflowBinding"
                    or binding["dispatch_authorized"] is not False
                    or any(type(binding[field]) is not str or not binding[field] for field in (
                        "workflow_revision", "installation_id", "bundle_generation", "input_class", "binding_digest"))
                    or type(binding["roles"]) is not dict
                    or type(binding["transfers"]) is not dict
                    or type(resolved.get("workflow_role")) is not str):
                raise _HandBackOwnerUnavailable()
            definition = validate_workflow(binding["definition"])
            validate_revision_ref(binding["requirements_ref"], "requirements_ref")
            validate_revision_ref(binding["session_policy_ref"], "session_policy_ref")
            role = binding["roles"].get(resolved["workflow_role"])
            source_release, transfer_id = resolved.get("source_release"), resolved.get("transfer_id")
            if source_release is None and transfer_id is not None:
                raise _HandBackOwnerUnavailable()
            if source_release is not None:
                transfer = binding["transfers"].get(transfer_id)
                if (type(transfer) is not dict
                        or transfer.get("target") != {"kind": "role", "role": resolved["workflow_role"]}
                        or type(transfer.get("contract_id")) is not str or not transfer["contract_id"]):
                    raise _HandBackOwnerUnavailable()
            if (resolved["workflow_role"] not in delivery_role_names(definition)
                    or type(role) is not dict
                    or any(type(role.get(field)) is not str for field in ("grant_id", "input_class", "model_destination"))
                    or type(role.get("requires_output_release")) is not bool
                    or binding["workflow_revision"] != workflow_digest(definition)
                    or binding["binding_digest"] != workflow_digest(
                        {key: value for key, value in binding.items() if key != "binding_digest"})):
                raise _HandBackOwnerUnavailable()
        except (WorkflowError, KeyError, TypeError, ValueError, OverflowError, RecursionError):
            raise _HandBackOwnerUnavailable() from None


def _validate_recovery_recipe_generation(resolved: dict, raw_bundle: str, generation: str, grant_id: str,
                                         hosting_envelope_path: str | None = None) -> None:
    """Check stored retry components against their recorded generation."""
    from .hosting import hosted_generation, load_envelope
    from .policy import PolicyError, resolve_recipe

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate bundle key")
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError("nonfinite bundle value")

    try:
        if type(raw_bundle) is not str:
            raise _HandBackOwnerUnavailable()
        parsed = json.loads(raw_bundle, object_pairs_hook=unique_pairs, parse_constant=reject_constant)
        if type(parsed) is not dict:
            raise _HandBackOwnerUnavailable()
        bundle = parsed
        envelope = load_envelope(hosting_envelope_path) if hosting_envelope_path else None
        if hosted_generation(bundle, envelope) != generation:
            raise _HandBackOwnerUnavailable()
        producer = resolve_recipe(bundle, grant_id)
        producer["generation"] = generation
        if any(resolved.get(key) != value for key, value in producer.items()):
            raise _HandBackOwnerUnavailable()
        if any(key in resolved and key not in producer for key in ("restricted_class", "connector_roles")):
            raise _HandBackOwnerUnavailable()
    except (PolicyError, KeyError, TypeError, ValueError, UnicodeError,
            AttributeError, OverflowError, RecursionError):
        raise _HandBackOwnerUnavailable() from None


@dataclass(frozen=True)
class ExecutionPrincipal:
    workload_id: str
    compute_id: str


@dataclass(frozen=True)
class ExecutionContext:
    instance_id: str
    operation_id: str
    job_id: str
    generation: str
    fence: str


_OP_DIGEST_SCHEMA = "sovite-worker-execution-operation/v1"
_OWNER_KEY_SCHEMA = "sovite-worker-execution-owner/v1"
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_INSTANCE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_OPERATION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_WORKLOAD_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_COMPUTE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_FENCE_RE = re.compile(r"^[0-9a-f]{32}$")
_GENERATION_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _fail(code: str, status: int, message: str):
    raise PlatformError(code, message, status)


def _request_error():
    _fail("invalid_request", 422, "Execution owner request is invalid")


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, OverflowError):
        raise ValueError("noncanonical value") from None


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _validate_principal(principal: ExecutionPrincipal) -> None:
    if (not isinstance(principal, ExecutionPrincipal)
        or not isinstance(principal.workload_id, str) or _WORKLOAD_RE.fullmatch(principal.workload_id) is None
        or not isinstance(principal.compute_id, str) or _COMPUTE_RE.fullmatch(principal.compute_id) is None):
        _request_error()


def _validate_instance(value: Any) -> None:
    if not isinstance(value, str) or _INSTANCE_RE.fullmatch(value) is None:
        _request_error()


def _validate_operation(value: Any) -> None:
    if not isinstance(value, str) or _OPERATION_RE.fullmatch(value) is None:
        _request_error()


def operation_request_digest(*, operation_kind: str, principal: ExecutionPrincipal, request: Mapping) -> str:
    """New internal owner digest; never redefines existing authored/audit bytes."""
    _validate_principal(principal)
    if operation_kind not in {"pull", "start"} or not isinstance(request, Mapping):
        _request_error()
    value = dict(request)
    fields = {"instance_id", "operation_id"} if operation_kind == "pull" else {"instance_id", "operation_id", "job_id", "generation", "fence"}
    profile = value.pop("profile", None)
    if set(value) != fields or (profile is not None and profile != "agent-http-v1"):
        _request_error()
    _validate_instance(value["instance_id"])
    _validate_operation(value["operation_id"])
    if operation_kind == "start":
        if (not isinstance(value["job_id"], str) or not value["job_id"] or len(value["job_id"]) > 64
            or not isinstance(value["generation"], str) or _GENERATION_RE.fullmatch(value["generation"]) is None
            or not isinstance(value["fence"], str) or _FENCE_RE.fullmatch(value["fence"]) is None):
            _request_error()
    try:
        return _digest({"schema_version": _OP_DIGEST_SCHEMA, "operation_kind": operation_kind,
                        "workload_id": principal.workload_id, "compute_id": principal.compute_id,
                        "request": {**value, **({"profile": profile} if profile is not None else {})}})
    except ValueError:
        _request_error()


def execution_owner_key(*, workload_id: str, instance_id: str) -> str:
    """Bounded owner key; full scope remains separately stored in lease rows."""
    if not isinstance(workload_id, str) or _WORKLOAD_RE.fullmatch(workload_id) is None:
        _request_error()
    _validate_instance(instance_id)
    return "execution:" + _digest({"schema_version": _OWNER_KEY_SCHEMA, "workload_id": workload_id, "instance_id": instance_id})


class LeaseService:
    """Owner-side lease transition service for the Worker execution loop.

    This is an internal extraction, not an independent authority: every transition
    runs inside the owning Platform's database sessions and authority checks. It
    cannot run without database authority.
    """

    def __init__(self, platform: Platform, worker_id: str, execution_fences: dict,
                 *, repository_run_profile: bool = False, repository_worker_only: bool = False):
        # execution_fences is the Worker's own dict, shared by reference so the
        # execution loop and lease transitions see the same fence state.
        self.platform, self.id, self.execution_fences = platform, worker_id, execution_fences
        self.repository_run_profile = repository_run_profile is True
        self.repository_worker_only = repository_worker_only is True
        self._trusted_worker_binding: tuple[ExecutionPrincipal, str] | None = None

    def _eligible_job(self, job):
        if not self.repository_worker_only:
            return True
        from .repository_runs import RUN_SCHEMA
        run=job.resolved.get("repository_run")
        return type(run)is dict and run.get("schema_version")==RUN_SCHEMA

    def _eligible_query(self, query):
        if self.repository_worker_only:
            from .repository_runs import RUN_SCHEMA
            query=query.where(Job.resolved["repository_run"]["schema_version"].as_string()==RUN_SCHEMA)
        return query

    def bind_trusted_worker(self, *, principal: ExecutionPrincipal, instance_id: str) -> None:
        """Internal adapter only: instance must hash this service's complete Worker.id."""
        _validate_principal(principal)
        _validate_instance(instance_id)
        expected = hashlib.sha256(self.id.encode("utf-8")).hexdigest()
        if instance_id != expected or principal.compute_id != self.platform.settings.compute_id:
            _fail("execution_scope_denied", 403, "Execution owner scope is not permitted")
        self._trusted_worker_binding = (principal, instance_id)

    def _owner_for(self, principal: ExecutionPrincipal, instance_id: str) -> str:
        binding = self._trusted_worker_binding
        if binding is not None:
            if binding != (principal, instance_id):
                _fail("execution_scope_denied", 403, "Execution owner scope is not permitted")
            return self.id
        return execution_owner_key(workload_id=principal.workload_id, instance_id=instance_id)

    def _authorize_current(self, session, job: Job, now: float) -> dict:
        if not self._eligible_job(job):
            _fail("execution_scope_denied", 403, "Execution owner scope is not permitted")
        if job.cancel_requested:
            _fail("cancelled", 409, "Execution was cancelled")
        repository_run = job.resolved.get("repository_run")
        if repository_run is not None:
            try:
                raw_deadline = repository_run["deadline"]
                deadline = datetime.fromisoformat(raw_deadline.replace("Z", "+00:00")).timestamp()
            except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
                _fail("owner_unavailable", 503, "Repository-run owner state is unavailable")
            if not math.isfinite(deadline) or deadline <= now:
                _fail("repository_run_deadline", 409, "Repository-run deadline has expired")
        recipe = self.platform.authorize_existing_job(session, job)
        check_execution_binding(self.platform, session, job)
        return recipe

    def _journal(self, session, principal, instance_id, operation_id, *, kind, digest, now):
        key = (principal.workload_id, instance_id, operation_id)
        row = session.scalar(select(WorkerExecutionOperation).where(
            WorkerExecutionOperation.workload_id == key[0],
            WorkerExecutionOperation.instance_id == key[1],
            WorkerExecutionOperation.operation_id == key[2],
        ).with_for_update())
        if row is not None and (row.operation_kind != kind or row.request_digest != digest):
            _fail("idempotency_conflict", 409, "Execution operation key was already used")
        return row

    @staticmethod
    def _private_hashes(job: Job) -> tuple[str, str]:
        try:
            return hashlib.sha256(job.task.encode("utf-8")).hexdigest(), _digest(job.resolved)
        except (AttributeError, UnicodeEncodeError, ValueError):
            _fail("owner_unavailable", 503, "Execution owner state is unavailable")

    @staticmethod
    def _check_lease_finite(job: Job, lease: WorkerExecutionLease) -> None:
        if (lease.phase not in {"reserved", "started", "finished"}
            or not isinstance(lease.fence, str) or _FENCE_RE.fullmatch(lease.fence) is None
            or not isinstance(lease.generation, str) or not _GENERATION_RE.fullmatch(lease.generation)
            or not isinstance(lease.workload_id, str) or _WORKLOAD_RE.fullmatch(lease.workload_id) is None
            or not isinstance(lease.instance_id, str) or _INSTANCE_RE.fullmatch(lease.instance_id) is None
            or isinstance(lease.created_at, bool) or not isinstance(lease.created_at, (int, float)) or not math.isfinite(lease.created_at)
            or isinstance(lease.updated_at, bool) or not isinstance(lease.updated_at, (int, float)) or not math.isfinite(lease.updated_at)
            or isinstance(lease.lease_until, bool) or not isinstance(lease.lease_until, (int, float))
            or not math.isfinite(lease.lease_until)
            or (lease.execution_deadline is not None and
                (isinstance(lease.execution_deadline, bool) or not isinstance(lease.execution_deadline, (int, float)) or not math.isfinite(lease.execution_deadline)))
            or lease.generation != job.generation):
            _fail("owner_unavailable", 503, "Execution owner state is unavailable")

    def _same_owner(self, job: Job, lease: WorkerExecutionLease, principal: ExecutionPrincipal,
                    instance_id: str, owner: str) -> bool:
        return (lease.workload_id == principal.workload_id and lease.instance_id == instance_id
            and job.lease_owner == owner and lease.generation == job.generation)

    def _agent_http_recipe(self, job: Job) -> dict:
        """Project only the first admitted external recipe from owner state."""
        resolved = job.resolved
        try:
            harness = resolved["harness"]
            agent_id = resolved["agent"]["id"]
            connection = resolved.get("model", {}).get("connection", {})
            timeout = resolved.get("grant", {}).get("timeout_seconds", self.platform.settings.job_timeout_seconds)
            endpoint = harness.get("endpoint")
            cancel_endpoint = harness.get("cancel_endpoint")
            credential_env = harness.get("api_key_env")
            idempotent = harness.get("idempotent", False)
        except (AttributeError, KeyError, TypeError):
            _fail("owner_unavailable", 503, "Execution owner state is unavailable")
        if (harness.get("kind") != "agent-http" or connection.get("kind") == "model-gateway"
            or not isinstance(agent_id, str) or not 1 <= len(agent_id) <= 96
            or not isinstance(endpoint, str) or not 1 <= len(endpoint) <= 16000
            or (cancel_endpoint is not None and (not isinstance(cancel_endpoint, str) or not 1 <= len(cancel_endpoint) <= 16000))
            or (credential_env is not None and (not isinstance(credential_env, str)
                or re.fullmatch(r"[A-Z][A-Z0-9_]{1,95}", credential_env) is None))
            or type(idempotent) is not bool
            or type(timeout) is not int or not 1 <= timeout <= 2_147_483_647):
            _fail("execution_profile_unavailable", 409, "Execution profile is not available to this worker")
        recipe = {"profile": "agent-http-v1", "agent_id": agent_id, "endpoint": endpoint,
            "idempotent": idempotent, "timeout_seconds": timeout}
        if cancel_endpoint is not None:
            recipe["cancel_endpoint"] = cancel_endpoint
        if credential_env is not None:
            recipe["credential_env"] = credential_env
        return recipe

    def _external_reservation_response(self, job: Job, lease: WorkerExecutionLease) -> dict:
        return {"state": "reserved", "job": {"job_id": job.id, "generation": job.generation,
            "fence": lease.fence, "lease_until": lease.lease_until, "task": job.task,
            "recipe": self._agent_http_recipe(job)}}

    def _recovery_scope(self, session, query, compute_id):
        """Exclude only an explicitly foreign JSON string; unknown types stay in scope."""
        value = Job.resolved["compute"]["id"]
        dialect = session.get_bind().dialect.name
        if dialect == "sqlite":
            root_members = func.json_each(Job.resolved).table_valued("key", "value")
            compute_members = func.json_each(Job.resolved, "$.compute").table_valued("key", "value")
            root_count = select(func.count()).select_from(root_members).where(
                root_members.c.key == "compute").correlate(Job).scalar_subquery()
            id_count = select(func.count()).select_from(compute_members).where(
                compute_members.c.key == "id").correlate(Job).scalar_subquery()
            kind = func.json_type(Job.resolved, "$.compute.id")
            identifier = case((kind == "text", value.as_string()), else_="")
            # Use the bundle producer's ID contract, not the distinct execution
            # principal syntax. A malformed string is unknown, not foreign.
            valid_identifier = and_(identifier.regexp_match(r"^[A-Za-z][A-Za-z0-9_.-]{0,95}$"),
                                    ~identifier.contains("\n"))
            in_scope = or_(root_count != 1, id_count != 1, kind.is_(None),
                           kind != "text", ~valid_identifier, identifier == compute_id)
            if self.repository_worker_only:
                from .repository_runs import RUN_SCHEMA
                in_scope = and_(Job.resolved["repository_run"]["schema_version"].as_string() == RUN_SCHEMA,
                                in_scope)
            # SQLite CASE is lazy; malformed JSON reaches candidate validation.
            return query.where(case((func.json_valid(Job.resolved) == 0, True), else_=in_scope))
        if dialect == "postgresql":
            empty_object = cast(literal("{}"), JSON)
            root_object = case((func.json_typeof(Job.resolved) == "object", Job.resolved), else_=empty_object)
            compute_object = case((func.json_typeof(Job.resolved["compute"]) == "object",
                                   Job.resolved["compute"]), else_=empty_object)
            root_members = func.json_each(root_object).table_valued("key", "value")
            compute_members = func.json_each(compute_object).table_valued("key", "value")
            root_count = select(func.count()).select_from(root_members).where(
                root_members.c.key == "compute").correlate(Job).scalar_subquery()
            id_count = select(func.count()).select_from(compute_members).where(
                compute_members.c.key == "id").correlate(Job).scalar_subquery()
            kind = func.json_typeof(value)
            identifier = case((kind == "string", value.as_string()), else_="")
            valid_identifier = and_(identifier.regexp_match(r"^[A-Za-z][A-Za-z0-9_.-]{0,95}$"),
                                    ~identifier.contains("\n"))
            in_scope = or_(root_count != 1, id_count != 1, kind.is_(None),
                           kind != "string", ~valid_identifier, identifier == compute_id)
            if self.repository_worker_only:
                from .repository_runs import RUN_SCHEMA
                in_scope = and_(Job.resolved["repository_run"]["schema_version"].as_string() == RUN_SCHEMA,
                                in_scope)
            # PostgreSQL json can store escapes which text JSON operators reject.
            # On PG16+, jsonb input validation distinguishes bad escapes from
            # valid pairs and literal backslashes without decoding the JSON.
            raw = cast(Job.resolved, Text)
            version = session.get_bind().dialect.server_version_info or ()
            if version >= (16,):
                unsafe_escape = ~func.pg_input_is_valid(raw, "jsonb")
            else:
                # Older servers lack pg_input_is_valid: conservatively include
                # suspicious escape text for candidate-local validation.
                unsafe_escape = raw.regexp_match(r"\\u(0000|[dD][89aAbBcCdDeEfF][0-9a-fA-F]{2})")
            return query.where(case((unsafe_escape, True), else_=in_scope))
        raise RuntimeError("unsupported recovery database")

    def _guard_fresh_reservation(self, session, compute_id, *, request_recovery=False):
        from .recovery_cursor import peek_cursor
        try:
            cursor = peek_cursor(session, compute_id, self.repository_worker_only)
            if cursor is not None and cursor.phase != "idle":
                if request_recovery:
                    raise _FreshReservationNeedsRecovery()
                _fail("owner_unavailable", 503, "Execution owner state is unavailable")
            now = time.time()
            if cursor is not None and cursor.covered_through is not None and cursor.covered_through > now:
                _fail("owner_unavailable", 503, "Execution owner state is unavailable")
            query = self._recovery_scope(session, select(Job.id).where(
                Job.status == "running", Job.lease_until < now), compute_id)
            if not request_recovery and cursor is not None and cursor.covered_through is not None:
                query = query.where(or_(Job.created_at >= cursor.covered_through,
                                        Job.lease_until >= cursor.covered_through))
            if session.execute(query.limit(1)).scalar_one_or_none() is not None:
                if request_recovery:
                    raise _FreshReservationNeedsRecovery()
                _fail("owner_unavailable", 503, "Execution owner state is unavailable")
        except _FreshReservationNeedsRecovery:
            raise
        except PlatformError:
            raise
        except Exception:
            raise PlatformError("owner_unavailable", "Execution owner state is unavailable", 503) from None

    def reserve(self, *, principal: ExecutionPrincipal, instance_id: str, operation_id: str,
                external_profile: str | None = None) -> dict:
        """Replay a prior reservation or run bounded recovery before a fresh one."""
        try:
            return self._reserve_once(principal=principal, instance_id=instance_id,
                                      operation_id=operation_id, external_profile=external_profile,
                                      recovery_done=False)
        except _FreshReservationNeedsRecovery:
            try:
                self.recover_expired()
            except _RecoverySweepIncomplete:
                raise PlatformError("owner_unavailable", "Execution owner state is unavailable", 503) from None
            return self._reserve_once(principal=principal, instance_id=instance_id,
                                      operation_id=operation_id, external_profile=external_profile,
                                      recovery_done=True)

    def _reserve_once(self, *, principal: ExecutionPrincipal, instance_id: str, operation_id: str,
                      external_profile: str | None, recovery_done: bool) -> dict:
        """One caller-scoped reservation; no attempt, bearer or execution deadline."""
        _validate_principal(principal); _validate_instance(instance_id); _validate_operation(operation_id)
        if external_profile not in {None, "agent-http-v1"}:
            _request_error()
        owner = self._owner_for(principal, instance_id)
        request = {"instance_id": instance_id, "operation_id": operation_id}
        if external_profile is not None:
            request["profile"] = external_profile
        request_digest = operation_request_digest(operation_kind="pull", principal=principal, request=request)
        with self.platform.db.sessions.begin() as session:
            self.platform.db.authority_lock(session)
            if self.platform.db.engine.dialect.name == "postgresql":
                session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": CLAIM_LOCK})
            # Decision time belongs to the serialized owner transaction, not
            # the caller's arrival before it waited for the locks above.
            now = time.time()
            # Prove the owner/control plane is available even for an empty queue.
            self.platform.active(session)
            prior = self._journal(session, principal, instance_id, operation_id,
                kind="pull", digest=request_digest, now=now)
            if prior is not None:
                if not isinstance(prior.result, dict):
                    _fail("owner_unavailable", 503, "Execution owner state is unavailable")
                if prior.result == {"state": "idle"} and prior.job_id is None and prior.fence is None:
                    return {"state": "idle"}
                if prior.result.get("state") != "reserved" or not prior.job_id or not prior.fence:
                    _fail("execution_fenced", 409, "Execution reservation is no longer current")
                job = session.execute(select(Job).where(Job.id == prior.job_id).with_for_update()).scalar_one_or_none()
                lease = session.get(WorkerExecutionLease, prior.job_id, with_for_update=True)
                now = time.time()
                if job is None or lease is None:
                    _fail("execution_fenced", 409, "Execution reservation is no longer current")
                self._check_lease_finite(job, lease)
                if (not self._same_owner(job, lease, principal, instance_id, owner)
                    or job.status != "running" or lease.phase != "reserved"
                    or lease.fence != prior.fence or job.lease_until is None
                    or not math.isfinite(job.lease_until) or job.lease_until <= now
                    or lease.lease_until != job.lease_until or lease.execution_deadline is not None):
                    _fail("execution_fenced", 409, "Execution reservation is no longer current")
                self._authorize_current(session, job, now)
                task_hash, resolved_hash = self._private_hashes(job)
                expected_result = {"state": "reserved", "job_id": job.id, "generation": job.generation,
                    "fence": lease.fence, "lease_until": job.lease_until,
                    "task_sha256": task_hash, "resolved_sha256": resolved_hash}
                if prior.result != expected_result:
                    _fail("execution_fenced", 409, "Execution reservation is no longer current")
                response = (self._external_reservation_response(job, lease) if external_profile is not None
                    else self._reservation_response(job, lease))
                self._check_response_size(response)
                return copy.deepcopy(response)

            if principal.compute_id != self.platform.settings.compute_id:
                _fail("execution_scope_denied", 403, "Execution owner scope is not permitted")
            if not recovery_done:
                self._guard_fresh_reservation(session, principal.compute_id, request_recovery=True)

            # Exclude provably foreign compute pools before the bounded window.
            # Unknown/malformed selectors remain in scope for owner validation.
            candidate_query = select(Job).where(
                Job.status.in_(("queued", "retry_wait")), Job.next_attempt_at <= now,
                Job.cancel_requested.is_(False),
            )
            if self.repository_worker_only:
                # Do not send unknown JSON through repository eligibility: an
                # unsafe row may be ordinary work and cannot be denied here.
                # It remains queued until its owning path can inspect it.
                from .repository_runs import RUN_SCHEMA
                raw = cast(Job.resolved, Text)
                dialect = session.get_bind().dialect
                if dialect.name == "sqlite":
                    unsafe = func.json_valid(Job.resolved) == 0
                    root_members = func.json_each(Job.resolved).table_valued("key", "value")
                    run_members = func.json_each(Job.resolved, "$.repository_run").table_valued("key", "value")
                elif dialect.name == "postgresql":
                    version = dialect.server_version_info or ()
                    unsafe = (~func.pg_input_is_valid(raw, "jsonb") if version >= (16,)
                              else raw.regexp_match(r"\\u(0000|[dD][89aAbBcCdDeEfF][0-9a-fA-F]{2})"))
                    empty = cast(literal("{}"), JSON)
                    root = case((func.json_typeof(Job.resolved) == "object", Job.resolved), else_=empty)
                    run = case((func.json_typeof(Job.resolved["repository_run"]) == "object",
                                Job.resolved["repository_run"]), else_=empty)
                    root_members = func.json_each(root).table_valued("key", "value")
                    run_members = func.json_each(run).table_valued("key", "value")
                else:
                    raise RuntimeError("unsupported dispatch database")
                root_count = select(func.count()).select_from(root_members).where(
                    root_members.c.key == "repository_run").correlate(Job).scalar_subquery()
                schema_count = select(func.count()).select_from(run_members).where(
                    run_members.c.key == "schema_version").correlate(Job).scalar_subquery()
                is_repository = and_(root_count == 1, schema_count == 1,
                    Job.resolved["repository_run"]["schema_version"].as_string() == RUN_SCHEMA)
                candidate_query = candidate_query.where(case((unsafe, False), else_=is_repository))
                candidate_query = self._recovery_scope(session, candidate_query, principal.compute_id)
            else:
                candidate_query = self._recovery_scope(session, candidate_query, principal.compute_id)
            # Local worker classes share durable bounded scan progress. A retry
            # of an idle operation returns its journal before touching this cursor.
            # The external profile retains its existing denial/no-write contract.
            cursor_row = None
            cursor_value = None
            cursor_key = None
            if external_profile is None:
                scope = {"compute_id": principal.compute_id,
                         "repository_worker_only": self.repository_worker_only,
                         "repository_run_profile": self.repository_run_profile}
                cursor_key = "dispatch_fair:" + hashlib.sha256(
                    json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:48]
                cursor_record = session.execute(select(cast(MetadataRow.__table__.c.value, Text)).where(
                    MetadataRow.key == cursor_key).with_for_update()).one_or_none()
                if cursor_record is not None:
                    raw_cursor = cursor_record[0]
                    def unique_pairs(pairs):
                        result = {}
                        for key, value in pairs:
                            if key in result:
                                raise ValueError("duplicate cursor key")
                            result[key] = value
                        return result
                    try:
                        cursor_value = json.loads(raw_cursor, object_pairs_hook=unique_pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite cursor")))
                    except (TypeError, ValueError, RecursionError):
                        _fail("owner_unavailable", 503, "Dispatch progress is unavailable")
                    if (type(cursor_value) is not dict or set(cursor_value) != {"schema", "scope", "created_at", "id"}
                            or cursor_value["schema"] != "sovite-dispatch-fair/v1" or cursor_value["scope"] != scope
                            or type(cursor_value["created_at"]) not in (int, float)
                            or not math.isfinite(cursor_value["created_at"])
                            or type(cursor_value["id"]) is not str or not 1 <= len(cursor_value["id"]) <= 64):
                        _fail("owner_unavailable", 503, "Dispatch progress is unavailable")
                    cursor_row = session.get(MetadataRow, cursor_key)
            def save_progress(job):
                nonlocal cursor_row
                if cursor_key is None:
                    return
                if (type(job.created_at) not in (int, float) or not math.isfinite(job.created_at)
                        or type(job.id) is not str or not 1 <= len(job.id) <= 64):
                    _fail("owner_unavailable", 503, "Dispatch progress is unavailable")
                value = {"schema": "sovite-dispatch-fair/v1", "scope": scope,
                         "created_at": job.created_at, "id": job.id}
                if cursor_row is None:
                    cursor_row = MetadataRow(key=cursor_key, value=value)
                    session.add(cursor_row)
                else:
                    cursor_row.value = value
            ordered = candidate_query.order_by(Job.created_at, Job.id)
            if cursor_value is None:
                candidates = session.scalars(ordered.limit(100).with_for_update(skip_locked=True)).all()
            else:
                stamp, identifier = cursor_value["created_at"], cursor_value["id"]
                after = or_(Job.created_at > stamp, and_(Job.created_at == stamp, Job.id > identifier))
                before = or_(Job.created_at < stamp, and_(Job.created_at == stamp, Job.id <= identifier))
                candidates = session.scalars(ordered.where(after).limit(100).with_for_update(skip_locked=True)).all()
                if len(candidates) < 100:
                    candidates.extend(session.scalars(ordered.where(before).limit(
                        100 - len(candidates)).with_for_update(skip_locked=True)).all())
            # Candidate row locks can wait too; base each new reservation on
            # owner time observed after those locks have been acquired.
            now = time.time()
            from .work_projection import WorkProjectionError, _raw_job_object
            running = Counter()
            for active_raw in session.scalars(select(cast(Job.resolved, Text)).where(Job.status == "running")):
                try:
                    active_root = _raw_job_object(active_raw)
                except (WorkProjectionError, TypeError, ValueError, RecursionError):
                    continue
                active_model = active_root.get("model")
                model_id = active_model.get("id") if type(active_model) is dict else None
                if type(model_id) is str:
                    running[model_id] += 1
            incompatible_profile_job = False
            for job in candidates:
                if job.resolved.get("compute", {}).get("id") != principal.compute_id:
                    continue
                if job.resolved.get("repository_run") is not None:
                    # The released agent-http profile and ordinary local worker
                    # are answer-only. Repository mutation needs its separately
                    # injected owner profile; never hand it to a generic worker.
                    if external_profile is not None:
                        incompatible_profile_job = True
                    elif not self.repository_run_profile:
                        continue
                    if incompatible_profile_job:
                        continue
                existing_lease = session.get(WorkerExecutionLease, job.id, with_for_update=True)
                # This final row lock can block after candidate selection too.
                now = time.time()
                if job.next_attempt_at > now:
                    continue
                try:
                    self._authorize_current(session, job, now)
                except PlatformError as exc:
                    # Existing-job lineage can become ineligible after enqueue.
                    # These owner-declared history denials are distinct from an
                    # unavailable/corrupt current owner and may block dispatch.
                    lineage_denial = (exc.status, exc.code) in {
                        (404, "previous_attempt_unavailable"),
                        (409, "previous_attempt_unfinished"),
                        (409, "previous_effect_uncertain"),
                        (409, "repository_run_deadline"),
                        (409, "repository_currentness_revoked"),
                        (409, "repository_currentness_binding_invalid"),
                        (409, "repository_currentness_required"),
                    }
                    if exc.status != 403 and not lineage_denial:
                        raise
                    job.status, job.error_code, job.updated_at, job.finished_at = "blocked", exc.code, now, now
                    self.platform.db.audit(session, self.id, "task.dispatch_denied", job.id, {"code": exc.code})
                    continue
                if external_profile is not None:
                    try:
                        self._agent_http_recipe(job)
                    except PlatformError as exc:
                        if exc.code != "execution_profile_unavailable":
                            raise
                        # The HTTP pull endpoint admits only its server-selected
                        # profile. Leave incompatible work in the owner's queue
                        # for local or separately qualified workers.
                        incompatible_profile_job = True
                        continue
                model = job.resolved.get("model", {})
                limit = model.get("max_concurrency")
                if limit and running[model.get("id")] >= limit:
                    continue
                self._guard_fresh_reservation(session, principal.compute_id)
                now = time.time()
                fence = uuid.uuid4().hex
                lease_until = now + self.platform.settings.lease_seconds
                lease = WorkerExecutionLease(job_id=job.id, workload_id=principal.workload_id,
                    instance_id=instance_id, fence=fence, generation=job.generation, phase="reserved",
                    lease_until=lease_until, execution_deadline=None, created_at=now, updated_at=now)
                if existing_lease is not None:
                    for field in ("workload_id", "instance_id", "fence", "generation", "phase", "lease_until", "execution_deadline", "created_at", "updated_at"):
                        setattr(existing_lease, field, getattr(lease, field))
                    lease = existing_lease
                response = (self._external_reservation_response(job, lease) if external_profile is not None else
                    {"state": "reserved", "job": {"job_id": job.id, "generation": job.generation,
                    "fence": fence, "lease_until": lease_until, "task": job.task, "resolved": copy.deepcopy(job.resolved)}})
                self._check_response_size(response)
                task_hash, resolved_hash = self._private_hashes(job)
                job.status, job.lease_owner, job.lease_until, job.updated_at = "running", owner, lease_until, now
                session.add(lease)
                session.add(WorkerExecutionOperation(workload_id=principal.workload_id, instance_id=instance_id,
                    operation_id=operation_id, operation_kind="pull", request_digest=request_digest,
                    job_id=job.id, fence=fence, result={"state": "reserved", "job_id": job.id,
                    "generation": job.generation, "fence": fence, "lease_until": lease_until,
                    "task_sha256": task_hash, "resolved_sha256": resolved_hash}, created_at=now, updated_at=now))
                self.platform.db.audit(session, self.id, "task.execution_reserved", job.id,
                    {"generation": job.generation, "fence": fence, "workload_id": principal.workload_id, "instance_id": instance_id})
                save_progress(job)
                return copy.deepcopy(response)
            if incompatible_profile_job:
                _fail("execution_profile_unavailable", 409, "Execution profile is not available to this worker")
            if cursor_key is not None:
                if len(candidates) == 100:
                    save_progress(candidates[-1])
                elif cursor_row is not None:
                    session.delete(cursor_row)
            idle = {"state": "idle"}
            session.add(WorkerExecutionOperation(workload_id=principal.workload_id, instance_id=instance_id,
                operation_id=operation_id, operation_kind="pull", request_digest=request_digest,
                job_id=None, fence=None, result=idle, created_at=now, updated_at=now))
            self.platform.db.audit(session, self.id, "task.execution_idle", instance_id,
                {"workload_id": principal.workload_id, "compute_id": principal.compute_id})
            return idle

    @staticmethod
    def _reservation_response(job: Job, lease: WorkerExecutionLease) -> dict:
        return {"state": "reserved", "job": {"job_id": job.id, "generation": job.generation,
            "fence": lease.fence, "lease_until": lease.lease_until, "task": job.task,
            "resolved": copy.deepcopy(job.resolved)}}

    @staticmethod
    def _check_response_size(response: dict) -> None:
        try:
            size = len(_canonical_bytes(response))
        except ValueError:
            _fail("owner_unavailable", 503, "Execution owner state is unavailable")
        if size > _MAX_RESPONSE_BYTES:
            _fail("execution_payload_too_large", 413, "Execution payload exceeds the bounded response size")

    def start(self, *, principal: ExecutionPrincipal, context: ExecutionContext,
              external_profile: str | None = None) -> dict:
        """Linearize once; only a first committed start may return execution bearer."""
        _validate_principal(principal)
        if external_profile not in {None, "agent-http-v1"}:
            _request_error()
        if not isinstance(context, ExecutionContext):
            _request_error()
        _validate_instance(context.instance_id); _validate_operation(context.operation_id)
        if (not isinstance(context.job_id, str) or not context.job_id or len(context.job_id) > 64
            or not isinstance(context.generation, str) or _GENERATION_RE.fullmatch(context.generation) is None
            or not isinstance(context.fence, str) or _FENCE_RE.fullmatch(context.fence) is None):
            _request_error()
        owner = self._owner_for(principal, context.instance_id)
        request = {"instance_id": context.instance_id, "operation_id": context.operation_id,
            "job_id": context.job_id, "generation": context.generation, "fence": context.fence}
        if external_profile is not None:
            request["profile"] = external_profile
        request_digest = operation_request_digest(operation_kind="start", principal=principal, request=request)
        with self.platform.db.sessions.begin() as session:
            self.platform.db.authority_lock(session)
            self.platform.active(session)
            now = time.time()
            prior = self._journal(session, principal, context.instance_id, context.operation_id,
                kind="start", digest=request_digest, now=now)
            if principal.compute_id != self.platform.settings.compute_id:
                _fail("execution_scope_denied", 403, "Execution owner scope is not permitted")
            job = session.execute(select(Job).where(Job.id == context.job_id).with_for_update()).scalar_one_or_none()
            if job is None or not self._eligible_job(job):
                _fail("execution_scope_denied", 403, "Execution owner scope is not permitted")
            lease = session.get(WorkerExecutionLease, context.job_id, with_for_update=True)
            if lease is None:
                _fail("execution_fenced", 409, "Execution context is no longer current")
            # Job and lease row locks may themselves wait after the shared
            # authority lock. Sample only after all blocking locks are held.
            now = time.time()
            self._check_lease_finite(job, lease)
            if not self._same_owner(job, lease, principal, context.instance_id, owner):
                _fail("execution_scope_denied", 403, "Execution owner scope is not permitted")
            if context.generation != job.generation or context.fence != lease.fence:
                _fail("execution_fenced", 409, "Execution context is no longer current")
            if prior is not None and (prior.job_id != job.id or prior.fence != lease.fence):
                _fail("idempotency_conflict", 409, "Execution operation key was already used")
            if prior is not None and not isinstance(prior.result, dict):
                _fail("owner_unavailable", 503, "Execution owner state is unavailable")
            if job.cancel_requested:
                _fail("cancelled", 409, "Execution was cancelled")
            self._authorize_current(session, job, now)
            if external_profile is not None:
                self._agent_http_recipe(job)
            if job.status != "running" or job.lease_owner != owner or job.lease_until is None:
                _fail("execution_fenced", 409, "Execution context is no longer current")
            if isinstance(job.lease_until, bool) or not isinstance(job.lease_until, (int, float)) or not math.isfinite(job.lease_until) or job.lease_until <= now or lease.lease_until != job.lease_until:
                _fail("execution_fenced", 409, "Execution context is no longer current")
            if lease.phase == "started":
                if lease.execution_deadline is None or lease.execution_deadline <= now:
                    _fail("execution_fenced", 409, "Execution context is no longer current")
                already = {"state": "already_started", "job_id": job.id, "generation": job.generation, "fence": lease.fence}
                if prior is None:
                    session.add(WorkerExecutionOperation(workload_id=principal.workload_id, instance_id=context.instance_id,
                        operation_id=context.operation_id, operation_kind="start", request_digest=request_digest,
                        job_id=job.id, fence=lease.fence, result=already, created_at=now, updated_at=now))
                    self.platform.db.audit(session, self.id, "task.execution_start_replayed", job.id,
                        {"generation": job.generation, "fence": lease.fence})
                else:
                    recorded = prior.result
                    common = {"job_id": job.id, "generation": job.generation, "fence": lease.fence}
                    if recorded.get("state") == "started":
                        shape_ok = (set(recorded) == {"state", "job_id", "generation", "fence", "lease_until", "deadline"}
                            and isinstance(recorded.get("lease_until"), (int, float))
                            and not isinstance(recorded.get("lease_until"), bool)
                            and math.isfinite(recorded["lease_until"])
                            and isinstance(recorded.get("deadline"), (int, float))
                            and not isinstance(recorded.get("deadline"), bool)
                            and math.isfinite(recorded["deadline"]))
                    else:
                        shape_ok = recorded.get("state") == "already_started" and set(recorded) == {"state", "job_id", "generation", "fence"}
                    if not shape_ok or any(recorded.get(key) != value for key, value in common.items()):
                        _fail("execution_fenced", 409, "Execution context is no longer current")
                if job.resolved.get("model", {}).get("connection", {}).get("kind") == "model-gateway":
                    from .authority import execution_authority_current
                    if not execution_authority_current(self.platform, session, job, lease, principal.workload_id, now):
                        _fail("execution_fenced", 409, "Execution context is no longer current")
                return already
            if lease.phase != "reserved" or lease.execution_deadline is not None:
                _fail("owner_unavailable", 503, "Execution owner state is unavailable")
            if prior is not None:
                _fail("idempotency_conflict", 409, "Execution operation key was already used")
            authority = session.get(ExecutionAuthority, job.id)
            if job.resolved.get("model", {}).get("connection", {}).get("kind") == "model-gateway" and authority is not None and authority.capability_digest and authority.expires_at > now:
                _fail("owner_unavailable", 503, "Execution owner state is unavailable")
            grant_timeout = job.resolved.get("grant", {}).get("timeout_seconds", self.platform.settings.job_timeout_seconds)
            if isinstance(grant_timeout, bool) or not isinstance(grant_timeout, (int, float)) or not math.isfinite(grant_timeout) or grant_timeout <= 0:
                _fail("owner_unavailable", 503, "Execution owner state is unavailable")
            deadline = now + min(float(grant_timeout), float(self.platform.settings.job_timeout_seconds))
            repository_run = job.resolved.get("repository_run")
            if repository_run is not None:
                try:
                    repository_deadline = datetime.fromisoformat(
                        repository_run["deadline"].replace("Z", "+00:00")
                    ).timestamp()
                except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
                    _fail("owner_unavailable", 503, "Repository-run owner state is unavailable")
                deadline = min(deadline, repository_deadline)
            if not math.isfinite(deadline) or deadline <= now:
                _fail("owner_unavailable", 503, "Execution owner state is unavailable")
            job.attempts += 1
            job.started_at = job.started_at or now
            job.updated_at = now
            lease.phase, lease.execution_deadline, lease.updated_at = "started", deadline, now
            execution = issue_execution(self.platform, session, job, fence=lease.fence,
                workload=principal.workload_id, now=now, deadline=deadline)
            response = {"state": "started", "job_id": job.id, "generation": job.generation,
                "fence": lease.fence, "lease_until": job.lease_until, "deadline": deadline}
            if execution is not None:
                response["execution"] = execution
            self._check_response_size(response)
            journal_result = {k: v for k, v in response.items() if k != "execution"}
            session.add(WorkerExecutionOperation(workload_id=principal.workload_id, instance_id=context.instance_id,
                operation_id=context.operation_id, operation_kind="start", request_digest=request_digest,
                job_id=job.id, fence=lease.fence, result=journal_result, created_at=now, updated_at=now))
            self.platform.db.audit(session, self.id, "task.execution_started", job.id,
                {"generation": job.generation, "fence": lease.fence, "attempt": job.attempts, "workload_id": principal.workload_id})
            return copy.deepcopy(response)

    def heartbeat(self, job_id: str | None = None):
        with self.platform.db.sessions.begin() as session:
            row = session.get(WorkerHeartbeat, self.id)
            if not row:
                session.add(WorkerHeartbeat(id=self.id, updated_at=time.time(), job_id=job_id))
            else:
                row.updated_at, row.job_id = time.time(), job_id

    def recover_expired(self):
        """Recover expired owners; global failures prevent a subsequent claim."""
        try:
            return self._recover_expired_sweep()
        except _RecoverySweepIncomplete:
            raise _RecoverySweepIncomplete("recovery sweep incomplete") from None
        except Exception:
            try:
                logging.getLogger(__name__).warning("recovery_scan status=unavailable")
            except Exception:
                pass
            raise PlatformError("owner_unavailable", "Execution owner state is unavailable", 503) from None

    def _recover_expired_sweep(self):
        """Recover bounded expired owner snapshots without borrowing a live fence."""
        from .collection_transaction import owner_transaction
        from .db import Base, MIGRATIONS
        from .recovery_cursor import read_cursor, advance_cursor
        from .work_projection import _WorkProjectionBusy

        database = self.platform.db
        counts = {"examined": 0, "retired": 0, "changed": 0, "busy": 0, "invalid": 0}

        def coordinate(session):
            if database.engine.dialect.name == "postgresql":
                session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": CLAIM_LOCK})

        def scoped(session, query):
            return self._recovery_scope(session, query, self.platform.settings.compute_id)

        def eligible_after(session, cursor):
            statement = scoped(session, select(Job.id).where(
                Job.status == "running", Job.lease_until < cursor.scan_started_at))
            if cursor.phase == "forward":
                if cursor.forward_upper_id is None:
                    return None
                statement = statement.where(Job.id <= cursor.forward_upper_id)
                if cursor.fair_pointer_job_id is not None:
                    statement = statement.where(Job.id > cursor.fair_pointer_job_id)
            else:
                statement = statement.where(Job.id <= cursor.fair_pointer_job_id)
            if cursor.last_job_id is not None:
                statement = statement.where(Job.id > cursor.last_job_id)
            return session.execute(statement.order_by(Job.id).limit(1)).scalar_one_or_none()

        def save_cursor(session, cursor, **changes):
            values = {name: getattr(cursor, name) for name in (
                "phase", "last_job_id", "fair_pointer_job_id", "forward_upper_id",
                "scan_started_at", "covered_through")}
            values.update(changes)
            if not advance_cursor(session, cursor, **values):
                raise RuntimeError("recovery cursor changed")
            return read_cursor(session, cursor.compute_id, cursor.repository_worker_only)

        def complete_or_wrap(session, cursor):
            if cursor.phase == "forward" and cursor.fair_pointer_job_id is not None:
                save_cursor(session, cursor, phase="wrap", last_job_id=None)
                return False
            save_cursor(session, cursor, phase="idle", last_job_id=None,
                        fair_pointer_job_id=cursor.forward_upper_id,
                        forward_upper_id=None, covered_through=cursor.scan_started_at,
                        scan_started_at=None)
            return True

        # Global schema/audit/terminal authority failures must abort the claim.
        with owner_transaction(database) as session:
            coordinate(session)
            connection = session.connection()
            version = database._version(connection)
            if type(version) is not int or not 1 <= version <= database.schema_version:
                raise RuntimeError("recovery schema unavailable")
            expected = {table.name: table for migration, tables in MIGRATIONS
                        if migration <= version for table in tables}
            later = {table.name for migration, tables in MIGRATIONS if migration > version
                     for table in tables if table.name not in expected}
            names = set(inspect(connection).get_table_names(schema=database.schema))
            if set(expected) - names or names.intersection(later):
                raise RuntimeError("recovery schema unavailable")
            database._structure(connection, list(expected.values()))
            if not self.repository_worker_only:
                sweep_terminal_authority(self.platform, session)

        while counts["examined"] < 1000:
            cursor = None
            initial_cursor = None
            candidate_id = None
            retired_fence = None
            outcome = "changed"
            try:
                with owner_transaction(database) as session:
                    coordinate(session)
                    cursor = read_cursor(session, self.platform.settings.compute_id,
                                         self.repository_worker_only)
                    if cursor.phase == "idle":
                        initial_cursor = cursor
                        scan_started_at = time.time()
                        upper = session.execute(scoped(session, select(func.max(Job.id)))).scalar_one()
                        cursor = save_cursor(session, cursor, phase="forward", last_job_id=None,
                                             scan_started_at=scan_started_at, forward_upper_id=upper)
                    candidate_id = eligible_after(session, cursor)
                    if candidate_id is None:
                        outcome = "complete" if complete_or_wrap(session, cursor) else "wrap"
                    else:
                        counts["examined"] += 1
                        if self._close_owner(candidate_id, recovery_session=session):
                            locked_lease = session.get(WorkerExecutionLease, candidate_id)
                            retired_fence = locked_lease.fence if locked_lease is not None else None
                            outcome = "retired"
                        save_cursor(session, cursor, last_job_id=candidate_id)
                if candidate_id is None:
                    if outcome == "wrap":
                        outcome = "changed"
                        continue
                    try:
                        logging.getLogger(__name__).info("recovery_scan status=complete counts=%s", counts)
                    except Exception:
                        pass
                    return counts
                counts[outcome] += 1
                if outcome == "retired":
                    self._forget_fence(candidate_id, retired_fence)
            except (_WorkProjectionBusy, _HandBackOwnerUnavailable) as exc:
                # Candidate transaction rolled back; progress requires exact cursor CAS.
                if cursor is None or candidate_id is None:
                    raise
                with owner_transaction(database) as session:
                    coordinate(session)
                    latest = read_cursor(session, self.platform.settings.compute_id,
                                         self.repository_worker_only)
                    if initial_cursor is not None:
                        if latest.value != initial_cursor.value:
                            raise RuntimeError("recovery cursor changed") from None
                        latest = save_cursor(session, latest, phase=cursor.phase,
                                             last_job_id=cursor.last_job_id,
                                             fair_pointer_job_id=cursor.fair_pointer_job_id,
                                             forward_upper_id=cursor.forward_upper_id,
                                             scan_started_at=cursor.scan_started_at,
                                             covered_through=cursor.covered_through)
                    if latest.value != cursor.value:
                        raise RuntimeError("recovery cursor changed") from None
                    save_cursor(session, latest, last_job_id=candidate_id)
                counts["busy" if isinstance(exc, _WorkProjectionBusy) else "invalid"] += 1
        # Complete empty tail phases without spending a candidate budget.
        with owner_transaction(database) as session:
            coordinate(session)
            cursor = read_cursor(session, self.platform.settings.compute_id,
                                 self.repository_worker_only)
            while eligible_after(session, cursor) is None:
                if complete_or_wrap(session, cursor):
                    return counts
                cursor = read_cursor(session, cursor.compute_id, cursor.repository_worker_only)
        raise _RecoverySweepIncomplete("recovery sweep incomplete")

    def claim(self) -> Job | None:
        principal = ExecutionPrincipal(self.platform.settings.execution_workload_identity,
                                       self.platform.settings.compute_id)
        instance_id = hashlib.sha256(self.id.encode("utf-8")).hexdigest()
        self.bind_trusted_worker(principal=principal, instance_id=instance_id)
        reserved = self.reserve(principal=principal, instance_id=instance_id, operation_id=uuid.uuid4().hex)
        if reserved.get("state") != "reserved":
            return None
        view = reserved["job"]
        context = ExecutionContext(instance_id, uuid.uuid4().hex, view["job_id"],
                                   view["generation"], view["fence"])
        started = self.start(principal=principal, context=context)
        with self.platform.db.sessions() as session:
            row = session.get(Job, started["job_id"])
            if row is None:
                _fail("execution_fenced", 409, "Execution context is no longer current")
            row.execution_fence = started["fence"]
            row.execution_deadline = started["deadline"]
            if "execution" in started:
                row.execution_capability = started["execution"]["capability"]
            self.execution_fences[row.id] = started["fence"]
            return row

    def _forget_fence(self, job_id: str, fence: str | None) -> None:
        if fence is not None and self.execution_fences.get(job_id) == fence:
            self.execution_fences.pop(job_id, None)

    def renew(self, job_id: str, fence: str | None = None) -> bool:
        with self.platform.db.sessions.begin() as session:
            self.platform.db.authority_lock(session)
            row = session.execute(select(Job).where(Job.id == job_id).with_for_update()).scalar_one_or_none()
            lease = session.get(WorkerExecutionLease, job_id, with_for_update=True)
            if not row or lease is None or not self._eligible_job(row):
                return False
            now = time.time()
            try:
                self._check_lease_finite(row, lease)
            except PlatformError:
                return False
            selected_fence = fence if fence is not None else self.execution_fences.get(job_id)
            try:
                owner = self._owner_for(ExecutionPrincipal(lease.workload_id, self.platform.settings.compute_id), lease.instance_id)
            except PlatformError:
                return False
            if (lease.phase != "started" or not self._same_owner(row, lease,
                ExecutionPrincipal(lease.workload_id, self.platform.settings.compute_id), lease.instance_id, owner)
                or row.status != "running" or row.lease_owner != owner or row.cancel_requested
                or selected_fence != lease.fence or row.lease_until is None
                or not isinstance(row.lease_until, (int, float)) or isinstance(row.lease_until, bool)
                or not math.isfinite(row.lease_until) or row.lease_until <= now
                or lease.lease_until != row.lease_until or lease.execution_deadline is None
                or lease.execution_deadline <= now):
                return False
            try:
                self._authorize_current(session, row, now)
            except PlatformError:
                return False
            is_gateway = row.resolved.get("model", {}).get("connection", {}).get("kind") == "model-gateway"
            if is_gateway and not refresh_execution(self.platform, session, row, selected_fence, now):
                return False
            until = min(now + self.platform.settings.lease_seconds, lease.execution_deadline)
            if until <= now or not math.isfinite(until):
                return False
            row.lease_until = lease.lease_until = until
            row.updated_at = lease.updated_at = now
            return True

    def hand_back(self, job_id: str, fence: str | None = None):
        return self._close_owner(job_id, fence=fence)

    def _close_owner(self, job_id: str, fence: str | None = None, *, finish_outcome=None,
                     recovery_session=None):
        """Validate, lock, and rescan one complete Job/Work owner snapshot."""
        from contextlib import nullcontext
        from sqlalchemy import outerjoin

        from .collection_transaction import _owner_transaction_is_active, owner_transaction
        from .db import Base, MIGRATIONS, digest
        from .program_ledger import _mark_work_projection_unknown_in_transaction
        from .program_ledger import _is_lower_hex, _validate_locked_legacy_binding
        from .repository_runs import RUN_SCHEMA
        from .work_projection import (
            WorkProjectionError, _WorkProjectionBusy, _WorkProjectionUnavailable,
            _compare_actual_work_projection,
            _raw_job_object,
            lock_work_projection_set,
            work_projections_from_job,
        )

        database = self.platform.db
        if recovery_session is not None and not _owner_transaction_is_active(recovery_session):
            raise ValueError("recovery owner transaction unavailable")
        hand_back_attempt_states = {
            "preparing", "awaiting_activation", "running", "interrupted",
            "cancelled", "activation_cancelled",
        }
        owner_attempt_states = {
            "preparing", "awaiting_activation", "running", "interrupted",
            "cancelled", "activation_cancelled", "failed", "uncertain",
            "completed", "ineligible",
        }
        def legacy_baseline_payload(link):
            return {name: link[name] for name in (
                "scope_id", "batch_id", "link_id", "job_id", "original_attempt_id",
                "original_call_id", "original_slot_id", "subject_id", "call_id", "slot_id",
                "baseline_state", "baseline_slot_state", "baseline_reserved_usd_micro",
                "baseline_reserved_allowance_micro", "baseline_actual_usd_micro",
                "baseline_actual_allowance_micro",
            )}

        succeeded = False
        succeeded_fence = None
        try:
            transaction = (owner_transaction(database) if recovery_session is None
                           else nullcontext(recovery_session))
            with transaction as session:
                connection = session.connection()
                try:
                    version = database._version(connection)
                    if type(version) is not int or not 1 <= version <= database.schema_version:
                        raise _HandBackOwnerUnavailable()
                    expected_by_name = {
                        table.name: table for migration, migrated_tables in MIGRATIONS
                        if migration <= version for table in migrated_tables
                    }
                    expected_tables = list(expected_by_name.values())
                    later_table_names = {
                        table.name for migration, migrated_tables in MIGRATIONS
                        if migration > version for table in migrated_tables
                        if table.name not in expected_by_name
                    }
                    physical_names = set(inspect(connection).get_table_names(schema=database.schema))
                    if (any(table.name not in physical_names for table in expected_tables)
                            or physical_names.intersection(later_table_names)):
                        raise _HandBackOwnerUnavailable()
                    database._structure(connection, expected_tables)
                except _HandBackOwnerUnavailable:
                    if recovery_session is not None:
                        raise RuntimeError("recovery schema unavailable") from None
                    raise
                except Exception:
                    if recovery_session is not None:
                        raise RuntimeError("recovery schema unavailable") from None
                    raise _HandBackOwnerUnavailable() from None

                tables = Base.metadata.tables
                job_table = tables["jobs"]
                probe = session.execute(select(
                    Job.id, Job.status, Job.caller, Job.attempts, Job.generation, Job.grant_id,
                    Job.lease_owner, Job.lease_until, Job.cancel_requested,
                ).where(Job.id == job_id)).mappings().one_or_none()
                if probe is None:
                    if finish_outcome is not None:
                        raise _HandBackOwnerUnavailable()
                    return False
                lease_snapshot = None
                retry_bundle_snapshot = None
                if recovery_session is not None:
                    bundle_table = tables["bundle_revisions"]
                    retry_bundle_snapshot = session.execute(select(cast(bundle_table.c.bundle, Text)).where(
                        bundle_table.c.generation == probe["generation"])).scalar_one_or_none()
                    lease_snapshot = session.execute(select(WorkerExecutionLease.__table__).where(
                        WorkerExecutionLease.job_id == job_id)).mappings().one_or_none()
                raw = session.execute(select(cast(job_table.c.resolved, Text)).where(
                    job_table.c.id == job_id)).scalar_one_or_none()
                if type(raw) is not str:
                    raise _HandBackOwnerUnavailable()

                try:
                    root = _raw_job_object(raw)
                    if "program_ledger_projections_v1" in root:
                        raise WorkProjectionError("orphan top-level projection marker")
                    if "repository_run" in root:
                        run = root["repository_run"]
                        if type(run) is not dict or run.get("schema_version") != RUN_SCHEMA:
                            raise WorkProjectionError("repository-run marker is malformed")
                        projections_all = work_projections_from_job(raw, allow_absent=True)
                        attempts = run.get("attempts")
                        if type(attempts) is not list:
                            raise WorkProjectionError("repository-run attempts are malformed")
                        attempt_ids = []
                        for attempt in attempts:
                            if type(attempt) is not dict or type(attempt.get("id")) is not str:
                                raise WorkProjectionError("repository-run attempt identity is malformed")
                            attempt_ids.append(attempt["id"])
                        if len(set(attempt_ids)) != len(attempt_ids):
                            raise WorkProjectionError("repository-run attempt identities are duplicated")
                    else:
                        run, attempts, attempt_ids = None, [], []
                        projections_all = ()
                    if any(item.work_job_id != job_id for item in projections_all):
                        raise WorkProjectionError("projection belongs to another Job")
                except (WorkProjectionError, TypeError, ValueError, RecursionError):
                    raise _HandBackOwnerUnavailable() from None

                current_attempt = None
                current_attempt_row = None
                current_attempt_state = None
                preappend_finish = False
                recovery_reserved = (recovery_session is not None and lease_snapshot is not None
                                     and lease_snapshot["phase"] == "reserved")
                if recovery_reserved:
                    preappend_finish = True
                if run is not None and probe["status"] == "running" and not recovery_reserved:
                    if (type(probe["attempts"]) is not int or isinstance(probe["attempts"], bool)
                            or probe["attempts"] < 1):
                        raise _HandBackOwnerUnavailable()
                    expected_attempt = f"{job_id}-{probe['attempts']}"
                    if attempts and type(attempts[-1]) is dict and attempts[-1].get("id") == expected_attempt:
                        current_attempt_row = attempts[-1]
                        current_attempt = current_attempt_row.get("id")
                        current_attempt_state = current_attempt_row.get("state")
                        if current_attempt_state not in owner_attempt_states:
                            raise _HandBackOwnerUnavailable()
                    else:
                        error = finish_outcome[1] if finish_outcome is not None else None
                        preappend_codes = {
                            "repository_profile_unavailable",
                            "repository_binding_revoked",
                            "repository_binding_unavailable",
                            "repository_binding_mismatch",
                        }
                        preappend_cancel = (
                            finish_outcome is not None
                            and finish_outcome[0] is None
                            and finish_outcome[1] is None
                            and finish_outcome[2] is True
                            and finish_outcome[3] is False
                            and probe["cancel_requested"] is True
                        )
                        # A narrow pre-append finish is safe only when no record
                        # exists for this claimed attempt and either the producer
                        # failed before Work creation or an authorized cancellation
                        # request must retire the still-live lease.
                        attempt_numbers = []
                        prefix = f"{job_id}-"
                        prior_terminal = {
                            "interrupted", "cancelled", "activation_cancelled", "failed",
                            "uncertain", "completed", "ineligible", "settled",
                        }
                        for attempt in attempts:
                            attempt_id = attempt["id"]
                            suffix = attempt_id[len(prefix):] if attempt_id.startswith(prefix) else ""
                            if (not suffix.isascii() or not suffix.isdigit() or int(suffix) < 1
                                    or str(int(suffix)) != suffix):
                                raise _HandBackOwnerUnavailable()
                            if (attempt.get("state") not in prior_terminal
                                    or not _is_lower_hex(attempt.get("fence_sha256"), 64)):
                                raise _HandBackOwnerUnavailable()
                            attempt_numbers.append(int(suffix))
                        preappend_error = (
                            finish_outcome is not None
                            and finish_outcome[0] is None
                            and finish_outcome[1] is not None
                            and finish_outcome[2] is False
                            and finish_outcome[3] is False
                            and type(getattr(error, "code", None)) is str
                            and error.code in preappend_codes
                        )
                        recovery_preappend = recovery_session is not None
                        if (not (preappend_error or preappend_cancel or recovery_preappend)
                                or any(number >= probe["attempts"] for number in attempt_numbers)
                                or attempt_numbers != sorted(set(attempt_numbers))):
                            raise _HandBackOwnerUnavailable()
                        preappend_finish = True
                elif run is not None and type(probe["attempts"]) is not int:
                    raise _HandBackOwnerUnavailable()

                if run is not None:
                    known_attempt_ids = set(attempt_ids)
                    for projection in projections_all:
                        if (projection.attempt_id not in known_attempt_ids
                                or projection.attempt_id != f"{job_id}-{int(projection.attempt_id.rsplit('-', 1)[1])}"
                                or int(projection.attempt_id.rsplit("-", 1)[1]) > probe["attempts"]):
                            raise _HandBackOwnerUnavailable()

                schema_version = version
                work_intents = []
                work_bindings = []
                work_reservations = []
                work_slots = []
                legacy_links = []
                if schema_version >= 8:
                    reservation_t = tables.get("program_reservations")
                    subject_t = tables.get("program_budget_subjects")
                    slot_t = tables.get("program_slots")
                    if reservation_t is None or subject_t is None or slot_t is None:
                        raise _HandBackOwnerUnavailable()
                    work_reservations = session.execute(select(reservation_t).where(
                        reservation_t.c.subject_id == job_id)).mappings().all()
                    joined_slots = session.execute(select(
                        slot_t, subject_t.c.purpose.label("subject_purpose"),
                    ).select_from(outerjoin(slot_t, subject_t, and_(
                        slot_t.c.scope_id == subject_t.c.scope_id,
                        slot_t.c.subject_id == subject_t.c.subject_id,
                    ))).where(slot_t.c.subject_id == job_id)).mappings().all()
                    for slot_row in joined_slots:
                        purpose = slot_row["subject_purpose"]
                        if purpose not in {"work", "collection"}:
                            raise _HandBackOwnerUnavailable()
                        if purpose == "work":
                            work_slots.append(slot_row)
                    for reservation in work_reservations:
                        if reservation["purpose"] not in {"work", "collection"}:
                            raise _HandBackOwnerUnavailable()
                    work_reservations = [row for row in work_reservations if row["purpose"] == "work"]

                if schema_version >= 9:
                    intent_t = tables.get("program_call_intents")
                    legacy_t = tables.get("program_legacy_links")
                    if intent_t is None or legacy_t is None:
                        raise _HandBackOwnerUnavailable()
                    work_intents = session.execute(select(intent_t).where(or_(
                        intent_t.c.work_job_id == job_id,
                        and_(intent_t.c.subject_id == job_id,
                             intent_t.c.purpose == "work"),
                    ))).mappings().all()
                    legacy_links = session.execute(select(legacy_t).where(
                        or_(legacy_t.c.job_id == job_id,
                            legacy_t.c.subject_id == job_id))).mappings().all()
                    if any(row["purpose"] != "work" or row["subject_id"] != job_id
                           or row["work_job_id"] != job_id
                           or type(row["work_attempt_id"]) is not str for row in work_intents):
                        raise _HandBackOwnerUnavailable()

                if schema_version >= 10:
                    binding_t = tables.get("program_work_projection_bindings")
                    if binding_t is None:
                        raise _HandBackOwnerUnavailable()
                    work_bindings = session.execute(select(binding_t).where(or_(
                        binding_t.c.work_job_id == job_id,
                        and_(binding_t.c.subject_id == job_id,
                             binding_t.c.purpose == "work"),
                    ))).mappings().all()
                    if any(row["work_job_id"] != job_id or row["subject_id"] != job_id
                           or row["purpose"] != "work"
                           or type(row["attempt_id"]) is not str for row in work_bindings):
                        raise _HandBackOwnerUnavailable()

                legacy_batch_keys = {(link["scope_id"], link["batch_id"]) for link in legacy_links}
                historical_calls = []
                seen_source_calls, seen_source_slots, seen_target_calls, seen_target_slots = set(), set(), set(), set()
                for link in legacy_links:
                    if (link["job_id"] != job_id or link["subject_id"] != job_id
                            or not all(type(link[name]) is str and link[name] for name in (
                                "scope_id", "batch_id", "link_id", "call_id", "slot_id",
                                "original_attempt_id", "original_call_id", "original_slot_id"))):
                        raise _HandBackOwnerUnavailable()
                    source_call = (link["job_id"], link["original_call_id"])
                    source_slot = (link["job_id"], link["original_slot_id"])
                    target_call, target_slot = (link["scope_id"], link["call_id"]), (link["scope_id"], link["slot_id"])
                    if (source_call in seen_source_calls or source_slot in seen_source_slots
                            or target_call in seen_target_calls or target_slot in seen_target_slots):
                        raise _HandBackOwnerUnavailable()
                    seen_source_calls.add(source_call); seen_source_slots.add(source_slot)
                    seen_target_calls.add(target_call); seen_target_slots.add(target_slot)
                    historical_calls.append((link["scope_id"], link["call_id"], link["slot_id"],
                                             link["subject_id"], "work", job_id))

                projection_keys = {
                    (item.scope_id, item.call_id, item.attempt_id)
                    for item in projections_all
                }
                if len(projection_keys) != len(projections_all):
                    raise _HandBackOwnerUnavailable()
                if schema_version >= 10:
                    binding_keys = {
                        (row["scope_id"], row["call_id"], row["attempt_id"])
                        for row in work_bindings
                    }
                    intent_keys = {
                        (row["scope_id"], row["call_id"], row["work_attempt_id"])
                        for row in work_intents
                    }
                    historical_target_keys = {(item[0], item[1]) for item in historical_calls}
                    historical_intent_keys = {
                        (row["scope_id"], row["call_id"], row["work_attempt_id"]) for row in work_intents
                        if (row["scope_id"], row["call_id"]) in historical_target_keys
                    }
                    allowed_keys = projection_keys | historical_intent_keys
                    if (len(binding_keys) != len(work_bindings)
                            or len(intent_keys) != len(work_intents)
                            or binding_keys != projection_keys or intent_keys != allowed_keys):
                        raise _HandBackOwnerUnavailable()
                    original_by_target = {(item[0], item[1]): next(
                        link["original_attempt_id"] for link in legacy_links
                        if (link["scope_id"], link["call_id"]) == (item[0], item[1]))
                        for item in historical_calls}
                else:
                    history_intent_keys = {(row["scope_id"], row["call_id"]) for row in work_intents}
                    if (projections_all or work_bindings
                            or history_intent_keys - {(item[0], item[1]) for item in historical_calls}):
                        raise _HandBackOwnerUnavailable()
                original_by_target = {(item[0], item[1]): next(
                    link["original_attempt_id"] for link in legacy_links
                    if (link["scope_id"], link["call_id"]) == (item[0], item[1]))
                    for item in historical_calls}
                if any((row["scope_id"], row["call_id"]) in original_by_target
                       and row["work_attempt_id"] != original_by_target[(row["scope_id"], row["call_id"])]
                       for row in work_intents):
                    raise _HandBackOwnerUnavailable()

                if schema_version >= 8:
                    expected_calls = {(item.scope_id, item.call_id) for item in projections_all}
                    expected_slots = {(item.scope_id, item.slot_id, item.call_id) for item in projections_all}
                    legacy_call_keys = {(link["scope_id"], link["call_id"]) for link in legacy_links}
                    legacy_slot_keys = {(link["scope_id"], link["slot_id"], link["call_id"]) for link in legacy_links}
                    reservation_calls = {
                        (row["scope_id"], row["call_id"]) for row in work_reservations
                    }
                    slot_keys = {
                        (row["scope_id"], row["slot_id"], row["call_id"]) for row in work_slots
                    }
                    if (len(reservation_calls) != len(work_reservations)
                            or len(slot_keys) != len(work_slots)
                            or reservation_calls != expected_calls | legacy_call_keys
                            or slot_keys != expected_slots | legacy_slot_keys):
                        raise _HandBackOwnerUnavailable()

                current = tuple(
                    item for item in projections_all
                    if current_attempt is not None and item.attempt_id == current_attempt
                )
                if current_attempt is not None and probe["status"] == "running":
                    # A projection at the current attempt must be the complete
                    # current Work call set; historical attempts remain separate.
                    current_keys = {(item.scope_id, item.call_id) for item in current}
                    binding_current = {
                        (row["scope_id"], row["call_id"]) for row in work_bindings
                        if row["attempt_id"] == current_attempt
                    }
                    intent_current = {
                        (row["scope_id"], row["call_id"]) for row in work_intents
                        if row["work_attempt_id"] == current_attempt
                    }
                    if (len(binding_current) != sum(row["attempt_id"] == current_attempt for row in work_bindings)
                            or len(intent_current) != sum(row["work_attempt_id"] == current_attempt for row in work_intents)
                            or binding_current != current_keys or intent_current != current_keys):
                        raise _HandBackOwnerUnavailable()

                def snapshot_rows(rows):
                    return tuple(sorted((dict(item) for item in rows), key=lambda item: repr(sorted(item.items()))))

                reservation_snapshot = snapshot_rows(work_reservations)
                slot_snapshot = snapshot_rows(work_slots)
                intent_snapshot = snapshot_rows(work_intents)
                binding_snapshot = snapshot_rows(work_bindings)
                legacy_snapshot = snapshot_rows(legacy_links)

                if preappend_finish and (projections_all or legacy_links or legacy_batch_keys
                        or work_reservations or work_slots or work_intents or work_bindings):
                    raise _HandBackOwnerUnavailable()

                if projections_all or legacy_batch_keys or recovery_session is not None:
                    try:
                        lock_work_projection_set(session, projections_all,
                            additional_work_calls=tuple(historical_calls),
                            legacy_batch_keys=tuple(legacy_batch_keys), expected_job_id=job_id,
                            include_bindings=schema_version >= 10,
                            nonblocking_owner_ids=(job_id,) if recovery_session is not None else (),
                            allow_missing_owner_lease=(recovery_session is not None and lease_snapshot is None))
                    except _WorkProjectionBusy:
                        if recovery_session is not None:
                            raise
                        raise _HandBackOwnerUnavailable() from None
                    except _WorkProjectionUnavailable:
                        raise _HandBackOwnerUnavailable() from None
                    except Exception:
                        if recovery_session is not None:
                            raise
                        raise _HandBackOwnerUnavailable() from None
                row = session.execute(select(Job).where(Job.id == job_id).with_for_update()).scalar_one_or_none()
                lease = session.get(WorkerExecutionLease, job_id, with_for_update=True)
                locked_raw = session.execute(select(cast(job_table.c.resolved, Text)).where(
                    job_table.c.id == job_id).with_for_update()).scalar_one_or_none()
                if row is None:
                    if finish_outcome is not None:
                        raise _HandBackOwnerUnavailable()
                    return False
                if type(locked_raw) is not str:
                    raise _HandBackOwnerUnavailable()
                if schema_version >= 8:
                    fresh_reservations = session.execute(select(reservation_t).where(
                        reservation_t.c.subject_id == job_id)).mappings().all()
                    if any(item["purpose"] not in {"work", "collection"} for item in fresh_reservations):
                        raise _HandBackOwnerUnavailable()
                    fresh_reservations = [item for item in fresh_reservations if item["purpose"] == "work"]
                    fresh_slots = session.execute(select(
                        slot_t, subject_t.c.purpose.label("subject_purpose"),
                    ).select_from(outerjoin(slot_t, subject_t, and_(
                        slot_t.c.scope_id == subject_t.c.scope_id,
                        slot_t.c.subject_id == subject_t.c.subject_id,
                    ))).where(slot_t.c.subject_id == job_id)).mappings().all()
                    if any(item["subject_purpose"] not in {"work", "collection"} for item in fresh_slots):
                        raise _HandBackOwnerUnavailable()
                    fresh_slots = [item for item in fresh_slots if item["subject_purpose"] == "work"]
                    if (snapshot_rows(fresh_reservations) != reservation_snapshot
                            or snapshot_rows(fresh_slots) != slot_snapshot):
                        raise _HandBackOwnerUnavailable()
                if schema_version >= 9:
                    fresh_intents = session.execute(select(intent_t).where(or_(
                        intent_t.c.work_job_id == job_id,
                        and_(intent_t.c.subject_id == job_id,
                             intent_t.c.purpose == "work"),
                    ))).mappings().all()
                    fresh_legacy = session.execute(select(legacy_t).where(or_(
                        legacy_t.c.job_id == job_id,
                        legacy_t.c.subject_id == job_id,
                    ))).mappings().all()
                    if (snapshot_rows(fresh_intents) != intent_snapshot
                            or snapshot_rows(fresh_legacy) != legacy_snapshot):
                        raise _HandBackOwnerUnavailable()
                if schema_version >= 10:
                    fresh_bindings = session.execute(select(binding_t).where(or_(
                        binding_t.c.work_job_id == job_id,
                        and_(binding_t.c.subject_id == job_id,
                             binding_t.c.purpose == "work"),
                    ))).mappings().all()
                    if snapshot_rows(fresh_bindings) != binding_snapshot:
                        raise _HandBackOwnerUnavailable()
                if (locked_raw != raw or row.status != probe["status"]
                        or row.attempts != probe["attempts"]
                        or (recovery_session is not None and row.grant_id != probe["grant_id"])
                        or row.generation != probe["generation"]
                        or row.lease_owner != probe["lease_owner"]
                        or row.lease_until != probe["lease_until"]
                        or row.cancel_requested != probe["cancel_requested"]):
                    raise _HandBackOwnerUnavailable()
                if (not self._eligible_job(row) or row.status != "running"
                        or (recovery_session is None and row.lease_owner != self.id)):
                    if finish_outcome is not None:
                        raise _HandBackOwnerUnavailable()
                    return False

                try:
                    locked_root = _raw_job_object(locked_raw)
                    locked_all = work_projections_from_job(locked_raw, allow_absent=True)
                except (WorkProjectionError, TypeError, ValueError, RecursionError):
                    raise _HandBackOwnerUnavailable() from None
                if locked_all != projections_all:
                    raise _HandBackOwnerUnavailable()

                if legacy_links:
                    link_t = tables["program_legacy_links"]
                    batch_t = tables["program_census_batches"]
                    policy_t = tables["program_policy_revisions"]
                    registry_t = tables["program_subject_registry"]
                    reservation_t = tables["program_reservations"]
                    slot_t = tables["program_slots"]
                    if run is None or current_attempt is None:
                        raise _HandBackOwnerUnavailable()
                    source_attempts = run.get("attempts")
                    source_reservations = run.get("budget_reservations")
                    source_slots = run.get("active_slots")
                    if any(type(value) is not list for value in (source_attempts, source_reservations, source_slots)):
                        raise _HandBackOwnerUnavailable()
                    for field, rows, key in (("attempts", source_attempts, "id"),
                                             ("budget_reservations", source_reservations, "call_id")):
                        ids = [item.get(key) if type(item) is dict else None for item in rows]
                        if any(type(value) is not str for value in ids) or len(ids) != len(set(ids)):
                            raise _HandBackOwnerUnavailable()
                    if (any(type(value) is not str for value in source_slots)
                            or len(source_slots) != len(set(source_slots))):
                        raise _HandBackOwnerUnavailable()
                    attempt_map = {item["id"]: item for item in source_attempts}
                    attempt_positions = {item["id"]: index for index, item in enumerate(source_attempts)}
                    reservation_map = {item["call_id"]: item for item in source_reservations}
                    current_call_ids = {(item.scope_id, item.call_id) for item in current}
                    current_slot_ids = {(item.scope_id, item.slot_id) for item in current}
                    current_call_ids.update((row["scope_id"], row["call_id"]) for row in work_intents
                                            if row["work_attempt_id"] == current_attempt)
                    current_call_ids.update((row["scope_id"], row["call_id"]) for row in work_bindings
                                            if row["attempt_id"] == current_attempt)
                    current_slot_ids.update((row["scope_id"], row["slot_id"]) for row in work_bindings
                                            if row["attempt_id"] == current_attempt)
                    current_source_call_ids, current_source_slot_ids = set(), set()
                    for source_reservation in source_reservations:
                        if source_reservation.get("attempt_id") == current_attempt:
                            current_source_call_ids.add(source_reservation["call_id"])
                            if type(source_reservation.get("slot_id")) is str:
                                current_source_slot_ids.add(source_reservation["slot_id"])
                    source_keys, source_call_keys, source_slot_keys, target_calls, target_slots = set(), set(), set(), set(), set()
                    for batch_key in sorted(legacy_batch_keys):
                        batch = session.execute(select(batch_t).where(
                            batch_t.c.scope_id == batch_key[0], batch_t.c.batch_id == batch_key[1])).mappings().one_or_none()
                        links = session.execute(select(link_t).where(
                            link_t.c.scope_id == batch_key[0], link_t.c.batch_id == batch_key[1])).mappings().all()
                        links.sort(key=lambda item: item["link_id"].encode("utf-8"))
                        if (batch is None or batch["state"] != "complete"
                                or type(batch["source_count"]) is not int or batch["source_count"] != len(links)
                                or type(batch["mapped_count"]) is not int or batch["mapped_count"] != batch["source_count"]
                                or type(batch["unmatched_count"]) is not int or batch["unmatched_count"] != 0
                                or type(batch["policy_revision"]) is not int or batch["policy_revision"] <= 0
                                or not _is_lower_hex(batch["policy_digest"], 64)
                                or not _is_lower_hex(batch["census_generation"], 32)
                                or not _is_lower_hex(batch["snapshot_digest"], 64)
                                or not _is_lower_hex(batch["fence_digest"], 64)
                                or type(batch["owner_epoch"]) is not int or batch["owner_epoch"] < 0
                                or digest([legacy_baseline_payload(item) for item in links]) != batch["snapshot_digest"]):
                            raise _HandBackOwnerUnavailable()
                        policy = session.execute(select(policy_t).where(
                            policy_t.c.scope_id == batch_key[0],
                            policy_t.c.revision == batch["policy_revision"])).mappings().one_or_none()
                        if policy is None or policy["policy_digest"] != batch["policy_digest"]:
                            raise _HandBackOwnerUnavailable()
                        gate = session.execute(select(tables["program_gate_state"]).where(
                            tables["program_gate_state"].c.scope_id == batch_key[0])).mappings().one_or_none()
                        if (gate is None or gate["mode"] not in {"disabled", "census", "active", "inhibited"}
                                or type(gate["policy_revision"]) is not int or gate["policy_revision"] <= 0
                                or not _is_lower_hex(gate["policy_digest"], 64)):
                            raise _HandBackOwnerUnavailable()
                        gate_policy = session.execute(select(policy_t).where(
                            policy_t.c.scope_id == batch_key[0],
                            policy_t.c.revision == gate["policy_revision"])).mappings().one_or_none()
                        if gate_policy is None or gate_policy["policy_digest"] != gate["policy_digest"]:
                            raise _HandBackOwnerUnavailable()
                        scope = session.execute(select(tables["program_budget_scopes"]).where(
                            tables["program_budget_scopes"].c.id == batch_key[0])).mappings().one_or_none()
                        if (scope is None or type(scope["policy_revision"]) is not int
                                or scope["policy_revision"] != gate["policy_revision"]
                                or scope["policy_digest"] != gate["policy_digest"]):
                            raise _HandBackOwnerUnavailable()
                        audit_t = tables["audit_events"]
                        if (type(gate["revision"]) is not int or not 0 <= gate["revision"] <= (1 << 63) - 1
                                or type(gate["audit_sequence"]) is not int
                                or not 0 < gate["audit_sequence"] <= (1 << 31) - 1
                                or session.execute(select(audit_t.c.sequence).where(
                                    audit_t.c.sequence == gate["audit_sequence"])).scalar_one_or_none()
                                != gate["audit_sequence"]):
                            raise _HandBackOwnerUnavailable()
                        cutover = (gate["cutover_batch_id"], gate["cutover_digest"], gate["cutover_generation"],
                                   gate["cutover_fence_digest"], gate["cutover_owner_epoch"])
                        if any(value is None for value in cutover) and any(value is not None for value in cutover):
                            raise _HandBackOwnerUnavailable()
                        if gate["mode"] == "active" and cutover[0] is None:
                            raise _HandBackOwnerUnavailable()
                        if cutover[0] is not None and (not _is_lower_hex(cutover[1], 64)
                                or not _is_lower_hex(cutover[2], 32) or not _is_lower_hex(cutover[3], 64)
                                or type(cutover[0]) is not str or not 1 <= len(cutover[0]) <= 96
                                or any(not "!" <= char <= "~" for char in cutover[0])
                                or type(cutover[4]) is not int or cutover[4] < 0):
                            raise _HandBackOwnerUnavailable()
                        if cutover[0] == batch_key[1] and (
                                gate["policy_revision"] != batch["policy_revision"]
                                or gate["policy_digest"] != batch["policy_digest"]
                                or cutover[1] != batch["snapshot_digest"]
                                or cutover[2] != batch["census_generation"]
                                or cutover[3] != batch["fence_digest"]
                                or cutover[4] != batch["owner_epoch"]):
                            raise _HandBackOwnerUnavailable()
                        totals = {name: 0 for name in ("reserved_usd_micro", "settled_usd_micro", "unknown_usd_micro",
                            "reserved_allowance_micro", "settled_allowance_micro", "unknown_allowance_micro", "held_slots", "unknown_slots")}
                        for item in links:
                            if (any(type(item[name]) is not str or not 1 <= len(item[name]) <= (64 if name == "job_id" else 96)
                                    or any(not "!" <= char <= "~" for char in item[name])
                                    for name in ("scope_id", "batch_id", "link_id", "job_id", "original_attempt_id",
                                                 "original_call_id", "original_slot_id", "subject_id", "call_id", "slot_id"))
                                    or not _is_lower_hex(item["original_digest"], 64)
                                    or digest(legacy_baseline_payload(item)) != item["original_digest"]):
                                raise _HandBackOwnerUnavailable()
                            identity = (item["job_id"], item["original_attempt_id"], item["original_call_id"], item["original_slot_id"])
                            source_call = (item["job_id"], item["original_call_id"])
                            source_slot = (item["job_id"], item["original_slot_id"])
                            target_call = (item["scope_id"], item["call_id"])
                            target_slot = (item["scope_id"], item["slot_id"])
                            if (identity in source_keys or source_call in source_call_keys or source_slot in source_slot_keys
                                    or target_call in target_calls or target_slot in target_slots):
                                raise _HandBackOwnerUnavailable()
                            source_keys.add(identity); source_call_keys.add(source_call); source_slot_keys.add(source_slot)
                            target_calls.add(target_call); target_slots.add(target_slot)
                            reserve_usd, reserve_allowance = item["baseline_reserved_usd_micro"], item["baseline_reserved_allowance_micro"]
                            actual_usd, actual_allowance = item["baseline_actual_usd_micro"], item["baseline_actual_allowance_micro"]
                            if (type(reserve_usd) is not int or type(reserve_allowance) is not int
                                    or reserve_usd < 0 or reserve_allowance < 0
                                    or ((actual_usd is None) != (actual_allowance is None))
                                    or any(type(value) is not int or value < 0 for value in (actual_usd, actual_allowance) if value is not None)):
                                raise _HandBackOwnerUnavailable()
                            if (item["baseline_state"], item["baseline_slot_state"], actual_usd is None) == ("reserved", "held", True):
                                totals["reserved_usd_micro"] += reserve_usd
                                totals["reserved_allowance_micro"] += reserve_allowance
                                totals["held_slots"] += 1
                            elif (item["baseline_state"], item["baseline_slot_state"], actual_usd is None) == ("settled", "released", False):
                                totals["settled_usd_micro"] += actual_usd
                                totals["settled_allowance_micro"] += actual_allowance
                            elif item["baseline_state"] == "unknown" and item["baseline_slot_state"] == "unknown":
                                totals["unknown_usd_micro"] += reserve_usd if actual_usd is None else max(reserve_usd, actual_usd)
                                totals["unknown_allowance_micro"] += reserve_allowance if actual_allowance is None else max(reserve_allowance, actual_allowance)
                                totals["unknown_slots"] += 1
                            else:
                                raise _HandBackOwnerUnavailable()
                        if any(type(batch[name]) is not int or batch[name] != value for name, value in totals.items()):
                            raise _HandBackOwnerUnavailable()
                        if any((item["scope_id"], item["call_id"]) in {
                                (projection.scope_id, projection.call_id) for projection in current
                            } or (item["scope_id"], item["slot_id"]) in {
                                (projection.scope_id, projection.slot_id) for projection in current
                            } for item in links):
                            raise _HandBackOwnerUnavailable()
                        for link in (item for item in links if item["job_id"] == job_id):
                            source_attempt = attempt_map.get(link["original_attempt_id"])
                            source_reservation = reservation_map.get(link["original_call_id"])
                            if (source_attempt is None or source_attempt.get("id") != link["original_attempt_id"]
                                    or source_reservation is None
                                    or source_reservation.get("attempt_id") != link["original_attempt_id"]
                                    or source_reservation.get("slot_id") != link["original_slot_id"]
                                    or source_attempt.get("state") not in {"preparing", "running", "awaiting_activation", "interrupted", "uncertain", "failed", "cancelled", "activation_cancelled", "completed", "ineligible", "settled"}
                                    or link["original_attempt_id"] == current_attempt
                                    or attempt_positions.get(link["original_attempt_id"], len(attempt_positions))
                                    >= attempt_positions[current_attempt]
                                    or source_slots.count(link["original_slot_id"]) != 1
                                    or link["original_call_id"] in current_source_call_ids
                                    or link["original_slot_id"] in current_source_slot_ids
                                    or (link["scope_id"], link["call_id"]) in current_call_ids
                                    or (link["scope_id"], link["slot_id"]) in current_slot_ids
                                    or (link["scope_id"], link["call_id"]) in {
                                        (projection.scope_id, projection.call_id) for projection in current
                                    } or (link["scope_id"], link["slot_id"], link["call_id"]) in {
                                        (projection.scope_id, projection.slot_id, projection.call_id) for projection in current
                                    }):
                                raise _HandBackOwnerUnavailable()
                            registry = session.execute(select(registry_t).where(
                                registry_t.c.scope_id == link["scope_id"], registry_t.c.subject_id == job_id,
                                registry_t.c.purpose == "work")).mappings().one_or_none()
                            reservation = session.execute(select(reservation_t).where(
                                reservation_t.c.scope_id == link["scope_id"], reservation_t.c.call_id == link["call_id"])).mappings().one_or_none()
                            slot = session.execute(select(slot_t).where(
                                slot_t.c.scope_id == link["scope_id"], slot_t.c.slot_id == link["slot_id"])).mappings().one_or_none()
                            job_mapping = {column.name: getattr(row, column.name) for column in job_table.columns}
                            try:
                                _validate_locked_legacy_binding(link, batch, registry, job_mapping, reservation, slot)
                            except ValueError:
                                raise _HandBackOwnerUnavailable() from None

                if recovery_session is not None:
                    now = time.time()
                    legacy_no_lease = lease_snapshot is None
                    if legacy_no_lease:
                        # Migration 5 introduced leases without backfilling old
                        # running Jobs. Job and lease locks have now been taken;
                        # any newly appeared lease makes this snapshot stale.
                        if lease is not None or run is not None or projections_all or legacy_links:
                            raise _HandBackOwnerUnavailable()
                        if work_reservations or work_slots or work_intents or work_bindings:
                            raise _HandBackOwnerUnavailable()
                    else:
                        if lease is None or any(getattr(lease, col.name) != lease_snapshot[col.name]
                                                for col in WorkerExecutionLease.__table__.columns):
                            raise _HandBackOwnerUnavailable()
                        try:
                            self._check_lease_finite(row, lease)
                        except PlatformError as error:
                            if error.code != "owner_unavailable":
                                raise
                            raise _HandBackOwnerUnavailable() from None
                    compute = row.resolved.get("compute") if type(row.resolved) is dict else None
                    if (type(compute) is not dict or compute.get("id") != self.platform.settings.compute_id
                            or type(row.lease_until) not in (int, float)
                            or not math.isfinite(row.lease_until) or row.lease_until >= now
                            or type(row.attempts) is not int or row.attempts < (1 if legacy_no_lease else 0)
                            or type(row.lease_owner) is not str or not row.lease_owner):
                        raise _HandBackOwnerUnavailable()
                    if not legacy_no_lease:
                        if (row.lease_until != lease.lease_until
                                or lease.phase not in {"reserved", "started"}
                                or (lease.phase == "started" and row.attempts < 1)):
                            raise _HandBackOwnerUnavailable()
                        derived_owner = execution_owner_key(workload_id=lease.workload_id,
                                                            instance_id=lease.instance_id)
                        if (row.lease_owner != derived_owner and
                                hashlib.sha256(row.lease_owner.encode("utf-8")).hexdigest() != lease.instance_id):
                            raise _HandBackOwnerUnavailable()
                        if ((lease.phase == "reserved" and lease.execution_deadline is not None)
                                or (lease.phase == "started" and lease.execution_deadline is None)):
                            raise _HandBackOwnerUnavailable()
                    projected_history = bool(projections_all or legacy_links)
                    if not legacy_no_lease and lease.phase == "reserved" and projected_history:
                        raise _HandBackOwnerUnavailable()
                    if run is not None and not recovery_reserved and current_attempt_row is not None:
                        if hashlib.sha256(lease.fence.encode("utf-8")).hexdigest() != current_attempt_row.get("fence_sha256"):
                            raise _HandBackOwnerUnavailable()
                    elif projected_history:
                        raise _HandBackOwnerUnavailable()
                    recovery_states = frozenset(owner_attempt_states)
                    for projection in current:
                        try:
                            _compare_actual_work_projection(
                                session, scope_id=projection.scope_id, call_id=projection.call_id,
                                allow_cancel_requested=True,
                                recovery_attempt_states=recovery_states,
                                recovery_expected_owner=row.lease_owner,
                            )
                        except _WorkProjectionBusy:
                            raise
                        except _WorkProjectionUnavailable:
                            raise _HandBackOwnerUnavailable() from None
                    unresolved_work = False
                    next_projections = {}
                    explicit_uncertainty = current_attempt_state == "uncertain"
                    for projection in current:
                        state = (projection.intent_state, projection.reservation_state, projection.slot_state)
                        if state == ("known", "settled", "released") and not explicit_uncertainty:
                            continue
                        if state in {("committed", "reserved", "held"), ("unknown", "unknown", "unknown")}:
                            unresolved_work = True
                        elif state != ("known", "settled", "released"):
                            raise _HandBackOwnerUnavailable()
                        payload = _mark_work_projection_unknown_in_transaction(
                            database, session, projection, job_id=job_id, attempt_id=current_attempt)
                        if payload is not None:
                            next_projections[(projection.scope_id, projection.call_id)] = payload
                    if next_projections:
                        locked_root["repository_run"]["program_ledger_projections_v1"] = [
                            next_projections.get((entry.get("scope_id"), entry.get("call_id")), entry)
                            for entry in locked_root["repository_run"]["program_ledger_projections_v1"]
                        ]
                        row.resolved = locked_root
                    def validate_retry():
                        current_bundle = session.execute(select(cast(bundle_table.c.bundle, Text)).where(
                            bundle_table.c.generation == probe["generation"])).scalar_one_or_none()
                        if type(retry_bundle_snapshot) is not str or current_bundle != retry_bundle_snapshot:
                            raise _HandBackOwnerUnavailable()
                        _validate_recovery_retry_recipe(row.resolved)
                        _validate_recovery_recipe_generation(row.resolved, retry_bundle_snapshot,
                                                             probe["generation"], probe["grant_id"],
                                                             getattr(self.platform.settings, "hosting_envelope_path", None))

                    if unresolved_work or explicit_uncertainty:
                        row.status, row.error_code = "blocked", "execution_uncertain"
                    elif row.cancel_requested:
                        row.status, row.error_code = "cancelled", "cancelled"
                    elif not legacy_no_lease and lease.phase == "reserved":
                        validate_retry()
                        row.status, row.error_code, row.next_attempt_at = "retry_wait", "lease_expired", now
                    else:
                        try:
                            safe = False if projected_history else _recovery_replay_safe(row.resolved)
                            if safe:
                                grant = row.resolved["grant"]
                                if type(grant) is not dict:
                                    raise _HandBackOwnerUnavailable()
                                max_attempts = grant.get("max_attempts", self.platform.settings.max_attempts)
                                if (type(max_attempts) is not int or max_attempts < 1
                                        or ("max_attempts" in grant and max_attempts > 10)):
                                    raise _HandBackOwnerUnavailable()
                            else:
                                max_attempts = None
                        except (KeyError, TypeError, AttributeError, ValueError, OverflowError):
                            raise _HandBackOwnerUnavailable() from None
                        if not safe:
                            row.status, row.error_code = "blocked", "execution_uncertain"
                        elif row.attempts >= max_attempts:
                            row.status, row.error_code = "failed", "lease_recovery_exhausted"
                        else:
                            validate_retry()
                            row.status, row.error_code, row.next_attempt_at = "retry_wait", "lease_expired", now
                    row.lease_owner, row.lease_until, row.updated_at = None, None, now
                    if lease is not None:
                        lease.phase, lease.lease_until, lease.updated_at = "finished", 0, now
                    revoke_execution(session, row.id)
                    if row.status in TERMINAL:
                        row.finished_at = now
                    self.platform.db.audit(session, self.id, "task.lease_recovered", row.id,
                                           {"status": row.status, "error_code": row.error_code})
                    succeeded = True
                    succeeded_fence = lease.fence if lease is not None else None
                elif finish_outcome is None:
                    now = time.time()
                    projected_history = bool(projections_all or legacy_links)
                    if run is not None and current_attempt_state not in hand_back_attempt_states:
                        raise _HandBackOwnerUnavailable()
                    if lease is not None:
                        try:
                            self._check_lease_finite(row, lease)
                        except PlatformError as error:
                            if error.code == "owner_unavailable":
                                raise _HandBackOwnerUnavailable() from None
                            raise
                        expected_fence = fence if fence is not None else (
                            None if projected_history else self.execution_fences.get(job_id)
                        )
                        if projected_history and (type(fence) is not str or not fence):
                            _fail("execution_fenced", 409, "Execution reservation is no longer current")
                        if (lease.phase != "started" or lease.fence != expected_fence
                                or row.lease_until is None or lease.lease_until != row.lease_until
                                or lease.execution_deadline is None or lease.lease_until <= now
                                or lease.execution_deadline <= now):
                            if projected_history and fence is not None and lease.fence != fence:
                                _fail("execution_fenced", 409, "Execution reservation is no longer current")
                            return False
                    elif projected_history:
                        raise _HandBackOwnerUnavailable()
                    elif fence is not None:
                        return False

                    if projected_history:
                        if (current_attempt is None or lease is None
                                or current_attempt_row is None
                                or current_attempt_state not in hand_back_attempt_states):
                            raise _HandBackOwnerUnavailable()
                    if (run is not None and current_attempt_row is not None and lease is not None
                            and hashlib.sha256(lease.fence.encode("utf-8")).hexdigest()
                            != current_attempt_row.get("fence_sha256")):
                        raise _HandBackOwnerUnavailable()
                    if projected_history and current:
                        try:
                            for projection in current:
                                _compare_actual_work_projection(
                                    session, scope_id=projection.scope_id,
                                    call_id=projection.call_id, allow_cancel_requested=True,
                                    allow_interrupted_attempt=True,
                                    allow_preentry_cancelled_attempt=True,
                                )
                        except Exception:
                            raise _HandBackOwnerUnavailable() from None

                    next_projections = {}
                    unresolved_work = False
                    if current:
                        for projection in current:
                            state = (projection.intent_state, projection.reservation_state,
                                     projection.slot_state)
                            if state == ("known", "settled", "released"):
                                continue
                            if state not in {
                                ("committed", "reserved", "held"),
                                ("unknown", "unknown", "unknown"),
                            }:
                                raise _HandBackOwnerUnavailable()
                            unresolved_work = True
                            try:
                                payload = _mark_work_projection_unknown_in_transaction(
                                    database, session, projection,
                                    job_id=job_id, attempt_id=current_attempt,
                                )
                            except Exception:
                                raise _HandBackOwnerUnavailable() from None
                            if payload is not None:
                                next_projections[(projection.scope_id, projection.call_id)] = payload

                    if row.cancel_requested:
                        row.status, row.error_code = "cancelled", "cancelled"
                    elif unresolved_work or not replay_safe(row.resolved):
                        row.status, row.error_code = "blocked", "execution_uncertain"
                    else:
                        row.status, row.error_code, row.next_attempt_at = "retry_wait", "worker_shutdown", now
                        if not projected_history:
                            # The legacy decrement is allowed only after the exact
                            # schema/JSON/relational empty-set proof above.
                            row.attempts = max(0, row.attempts - 1)

                    if next_projections:
                        run_locked = locked_root.get("repository_run")
                        entries = run_locked.get("program_ledger_projections_v1") if type(run_locked) is dict else None
                        if type(entries) is not list:
                            raise _HandBackOwnerUnavailable()
                        run_locked["program_ledger_projections_v1"] = [
                            next_projections.get((entry.get("scope_id"), entry.get("call_id")), entry)
                            if type(entry) is dict else entry for entry in entries
                        ]
                        row.resolved = locked_root

                    row.lease_owner, row.lease_until, row.updated_at = None, None, now
                    if lease is not None:
                        lease.phase, lease.lease_until, lease.updated_at = "finished", 0, now
                    revoke_execution(session, row.id)
                    if row.status in TERMINAL:
                        row.finished_at = now
                    self.platform.db.audit(session, self.id, "task.handed_back", row.id, {
                        "status": row.status, "error_code": row.error_code,
                        "work_state": "unknown_held" if unresolved_work else "unchanged",
                    })
                    succeeded = True
                    succeeded_fence = lease.fence if lease is not None else None
                else:
                    artifact, error, cancelled, execution_completed = finish_outcome
                    now = time.time()
                    projected_history = bool(projections_all or legacy_links)
                    if preappend_finish and cancelled and row.cancel_requested is not True:
                        raise _HandBackOwnerUnavailable()
                    if lease is None:
                        raise _HandBackOwnerUnavailable()
                    try:
                        self._check_lease_finite(row, lease)
                    except PlatformError:
                        raise _HandBackOwnerUnavailable() from None
                    if (type(fence) is not str or not fence or lease.phase != "started"
                            or lease.fence != fence or row.lease_until is None
                            or lease.lease_until != row.lease_until
                            or lease.execution_deadline is None or lease.lease_until <= now
                            or lease.execution_deadline <= now):
                        raise _HandBackOwnerUnavailable()
                    if (not self._eligible_job(row) or row.status != "running"
                            or row.lease_owner != self.id):
                        raise _HandBackOwnerUnavailable()
                    awaiting_activation_persist_failure = False
                    if run is not None and not preappend_finish:
                        if current_attempt_row is None or current_attempt is None:
                            raise _HandBackOwnerUnavailable()
                        fence_digest = hashlib.sha256(fence.encode("utf-8")).hexdigest()
                        if current_attempt_row.get("fence_sha256") != fence_digest:
                            raise _HandBackOwnerUnavailable()
                        if artifact is not None:
                            accepted_attempt_states = frozenset({"completed"})
                        elif error is not None and error.code == "repository_candidate_ineligible":
                            accepted_attempt_states = frozenset({"ineligible"})
                        elif error is not None and error.code == "external_execution_uncertain":
                            accepted_attempt_states = frozenset({"uncertain", "interrupted"})
                        elif error is not None and error.code == "native_vm_activation_cancelled":
                            accepted_attempt_states = frozenset({"activation_cancelled"})
                        elif error is not None and error.code == "repository_binding_revoked":
                            accepted_attempt_states = frozenset({
                                "failed", "cancelled", "activation_cancelled",
                            })
                        elif (error is not None and artifact is None and not cancelled
                              and not execution_completed and not row.cancel_requested
                              and current_attempt_state == "awaiting_activation"
                              and _native_activation_preentry_matches(
                                  row, run, current_attempt_row, fence,
                                  lease.execution_deadline)):
                            # The executor persisted the native activation
                            # descriptor but a later pre-entry persistence
                            # failure prevented its terminal marker. Preserve
                            # the original error only for this exact attempt;
                            # this state does not authorize replay.
                            accepted_attempt_states = frozenset({"awaiting_activation"})
                            awaiting_activation_persist_failure = True
                        elif (error is not None and error.code == "execution_timeout"
                              and artifact is None and not cancelled
                              and not execution_completed and not row.cancel_requested
                              and current_attempt_state == "activation_cancelled"
                              and _native_activation_preentry_matches(
                                  row, run, current_attempt_row, fence,
                                  lease.execution_deadline)):
                            # A live timeout may pair with the executor's
                            # durable pre-entry cancellation marker. The exact
                            # lease/deadline checks above still gate closure.
                            accepted_attempt_states = frozenset({"activation_cancelled"})
                        elif error is not None and execution_completed is True:
                            accepted_attempt_states = frozenset({"completed"})
                        elif cancelled or row.cancel_requested:
                            accepted_attempt_states = frozenset({"cancelled", "activation_cancelled"})
                        else:
                            accepted_attempt_states = frozenset({"failed"})
                        if current_attempt_state not in accepted_attempt_states:
                            raise _HandBackOwnerUnavailable()
                    else:
                        accepted_attempt_states = None

                    if projected_history and current:
                        try:
                            for projection in current:
                                _compare_actual_work_projection(
                                    session, scope_id=projection.scope_id,
                                    call_id=projection.call_id, allow_cancel_requested=True,
                                    finish_attempt_states=accepted_attempt_states,
                                )
                        except Exception:
                            raise _HandBackOwnerUnavailable() from None

                    unresolved_work = False
                    explicit_uncertainty = bool(error and error.code == "external_execution_uncertain")
                    next_projections = {}
                    for projection in current:
                        state = (projection.intent_state, projection.reservation_state,
                                 projection.slot_state)
                        if state == ("known", "settled", "released"):
                            if not explicit_uncertainty:
                                continue
                        elif state in {("committed", "reserved", "held"),
                                       ("unknown", "unknown", "unknown")}:
                            unresolved_work = True
                        else:
                            raise _HandBackOwnerUnavailable()
                        try:
                            payload = _mark_work_projection_unknown_in_transaction(
                                database, session, projection,
                                job_id=job_id, attempt_id=current_attempt,
                            )
                        except Exception:
                            raise _HandBackOwnerUnavailable() from None
                        if payload is not None:
                            next_projections[(projection.scope_id, projection.call_id)] = payload

                    denied = None
                    try:
                        self.platform.authorize_existing_job(
                            session, row, purpose="active" if artifact else "observe")
                    except PlatformError as exc:
                        denied = exc.code
                    currentness_cancel_cleanup = (
                        denied == "repository_currentness_revoked"
                        and preappend_finish and cancelled
                        and row.cancel_requested is True
                    )
                    if ((cancelled and row.cancel_requested is not True)
                            or (denied == "repository_currentness_revoked"
                                and (cancelled or row.cancel_requested)
                                and not currentness_cancel_cleanup)):
                        # Currentness loss only permits the exact validated
                        # empty-preappend cancellation; hints or current attempts
                        # cannot expand that exception.
                        raise _HandBackOwnerUnavailable()
                    if row.resolved.get("model", {}).get("connection", {}).get("kind") == "model-gateway":
                        try:
                            check_execution_binding(self.platform, session, row)
                        except PlatformError:
                            raise _HandBackOwnerUnavailable() from None
                        from .authority import execution_authority_current
                        if not execution_authority_current(self.platform, session, row,
                                lease, lease.workload_id, now):
                            raise _HandBackOwnerUnavailable()

                    if artifact is not None:
                        row.artifact_key, row.artifact_sha256 = artifact
                    if next_projections:
                        run_locked = locked_root.get("repository_run")
                        entries = run_locked.get("program_ledger_projections_v1") if type(run_locked) is dict else None
                        if type(entries) is not list:
                            raise _HandBackOwnerUnavailable()
                        run_locked["program_ledger_projections_v1"] = [
                            next_projections.get((entry.get("scope_id"), entry.get("call_id")), entry)
                            if type(entry) is dict else entry for entry in entries
                        ]
                        row.resolved = locked_root

                    if explicit_uncertainty or unresolved_work:
                        row.status, row.error_code = "blocked", "execution_uncertain"
                    elif cancelled or row.cancel_requested:
                        # Only persisted caller intent may close as cancelled.
                        # The preappend path has already proved empty history.
                        if denied and denied != "repository_currentness_revoked":
                            row.status, row.error_code = "blocked", denied
                        else:
                            row.status, row.error_code = "cancelled", "cancelled"
                    elif denied:
                        row.status, row.error_code = "blocked", denied
                    elif execution_completed and run is not None:
                        # Do not replay a repository effect because storing its
                        # completed result failed; this outcome preserves the
                        # storage error without granting retry authority.
                        row.status, row.error_code = "blocked", error.code
                    elif awaiting_activation_persist_failure:
                        # Retained activation metadata is not evidence that
                        # retrying the repository run is safe, even when the
                        # grant normally queues retryable failures.
                        row.status, row.error_code = "blocked", error.code
                    elif artifact is not None:
                        row.status, row.error_code = "completed", None
                        row.public_projection = {"receipt": row.receipt, "status": "available"}
                        row.released_at = now
                        self.platform.db.audit(session, self.id, "result.release_authorized", row.id, {
                            "grant_id": row.grant_id, "generation": row.generation,
                            "artifact_sha256": row.artifact_sha256,
                        })
                    elif error and error.retryable and any(
                            not tool.get("read_only", tool["kind"] == "kubernetes-read")
                            for tool in row.resolved.get("tools", [])):
                        row.status, row.error_code = "blocked", "execution_uncertain"
                    elif (error and error.retryable
                          and row.resolved["grant"].get("on_outage", "queue") == "queue"
                          and row.attempts < int(row.resolved["grant"].get(
                              "max_attempts", self.platform.settings.max_attempts))):
                        row.status, row.error_code = "retry_wait", error.code
                        row.next_attempt_at = now + min(300, 2 ** row.attempts * 5)
                    else:
                        row.status, row.error_code = "failed", error.code if error else "execution_failed"

                    row.lease_owner, row.lease_until, row.updated_at = None, None, now
                    lease.phase, lease.lease_until, lease.updated_at = "finished", 0, now
                    revoke_execution(session, row.id)
                    if row.status in TERMINAL:
                        row.finished_at = now
                    self.platform.db.audit(session, self.id, "task.state_changed", row.id, {
                        "status": row.status, "error_code": row.error_code,
                        "work_state": "unknown_held" if unresolved_work else (
                            "uncertain_gate_inhibited" if explicit_uncertainty else "unchanged"),
                    })
                    succeeded = True
                    succeeded_fence = lease.fence

        except PlatformError:
            if recovery_session is not None:
                raise
            if finish_outcome is not None:
                raise FinishRefused("execution owner state unavailable") from None
            raise
        except _HandBackOwnerUnavailable:
            if recovery_session is not None:
                raise
            if finish_outcome is not None:
                raise FinishRefused("execution owner state unavailable") from None
            _fail("execution_owner_state_unavailable", 503,
                  "Execution owner state is unavailable; retry the request")
        except Exception:
            if recovery_session is not None:
                raise
            if finish_outcome is not None:
                raise FinishRefused("execution owner state unavailable") from None
            _fail("execution_owner_state_unavailable", 503,
                  "Execution owner state is unavailable; retry the request")

        if succeeded and recovery_session is None:
            self._forget_fence(job_id, succeeded_fence)
        return succeeded

    def finish(self, job_id: str, artifact: tuple[str, str] | None = None,
               error: RuntimeFailure | None = None, cancelled: bool = False,
               fence: str | None = None, execution_completed: bool = False):
        """Finish a lease using hand_back's owner closure.

        ``execution_completed`` only records that Worker received a result
        before artifact storage failed. Repository Work still requires the
        persisted current completed attempt, exact fence, and full closure.
        """
        if type(execution_completed) is not bool or (execution_completed and
                (artifact is not None or error is None or cancelled
                 or getattr(error, "code", None) in {"repository_candidate_ineligible",
                                                       "external_execution_uncertain",
                                                       "native_vm_activation_cancelled"})):
            raise FinishRefused("invalid finish outcome")
        return self._close_owner(
            job_id, fence=fence, finish_outcome=(artifact, error, cancelled, execution_completed))
