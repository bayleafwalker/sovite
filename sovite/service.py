from __future__ import annotations

import secrets
import time
import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from . import policy
from .db import BundleRevision, Database, GrantState, Job, MetadataRow, canonical, digest
from .qualification import is_qualified
from .settings import Settings
from .storage import ArtifactStore


TERMINAL = {"completed", "failed", "cancelled", "blocked"}
PUBLIC_STATUS = {"queued": "pending", "retry_wait": "pending", "running": "working", "completed": "available", "failed": "unavailable", "cancelled": "unavailable", "blocked": "unavailable"}


class PlatformError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class Platform:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.database_url, schema=settings.database_schema)
        self.store = ArtifactStore(settings)

    def initialize(self):
        self.db.initialize(self.settings.database_migrate)
        with self.db.sessions() as session:
            active = session.get(MetadataRow, "active_generation")
        # API replicas do not silently revert a revision loaded through the CLI.
        if active is None:
            self.reload(self.settings.bundle_path, initial=True)
        elif not self.settings.require_qualification:
            bundle, _ = self.active()
            if bundle.get("mode") == "enforced":
                raise PlatformError("qualification_cannot_be_disabled", "Enforced deployments require trusted qualification", 400)

    def reload(self, path: str, initial: bool = False) -> str:
        bundle, generation = policy.load_bundle(path)
        if bundle.get("mode") == "enforced" and not self.settings.require_qualification:
            raise PlatformError("qualification_cannot_be_disabled", "Enforced deployments require trusted qualification", 400)
        with self.db.sessions.begin() as session:
            if session.get(BundleRevision, generation) is None:
                session.add(BundleRevision(generation=generation, bundle=bundle))
            row = session.get(MetadataRow, "active_generation")
            if row:
                old = row.value.get("generation")
                row.value = {"generation": generation}
            else:
                old = None
                session.add(MetadataRow(key="active_generation", value={"generation": generation}))
            for grant in bundle.get("grants", []):
                if session.get(GrantState, grant["id"]) is None:
                    session.add(GrantState(id=grant["id"], inhibited=False))
            if old != generation:
                # Sessions are pinned. A config change cannot silently move an old session
                # into a new route or resurrect authorization removed in another revision.
                session.execute(update(Job).where(Job.status.in_(["queued", "retry_wait"]), Job.generation != generation).values(status="blocked", error_code="revision_superseded", finished_at=time.time(), updated_at=time.time()))
                session.execute(update(Job).where(Job.status == "running", Job.generation != generation).values(cancel_requested=True))
            self.db.audit(session, "operator", "configuration.initialized" if initial else "configuration.activated", generation, {"previous_generation": old, "mode": bundle.get("mode", "development")})
        return generation

    def active(self, session=None) -> tuple[dict, str]:
        if session is None:
            with self.db.sessions() as own:
                return self.active(own)
        active = session.get(MetadataRow, "active_generation")
        if not active:
            raise PlatformError("not_initialized", "The platform has no active configuration", 503)
        generation = active.value["generation"]
        row = session.get(BundleRevision, generation)
        if row is None:
            raise PlatformError("configuration_missing", "The active configuration revision is unavailable", 503)
        return row.bundle, generation

    def authorize(self, session, grant_id: str, caller: str, input_class: str | None = None, generation: str | None = None, is_operator: bool = False) -> dict:
        bundle, active_generation = self.active(session)
        if generation is not None and generation != active_generation:
            raise PlatformError("revision_superseded", "The task's configuration revision is no longer active")
        grant = next((g for g in bundle.get("grants", []) if g["id"] == grant_id), None)
        if not grant:
            raise PlatformError("grant_unavailable", "The requested grant is unavailable", 403)
        try:
            recipe = policy.authorize_job(bundle, grant_id, caller, input_class or grant["input_class"], is_operator=is_operator)
        except policy.PolicyError as exc:
            raise PlatformError(getattr(exc, "code", "policy_denied"), "The current flow contract does not permit this task", 403) from exc
        state = session.get(GrantState, grant_id)
        if state is not None and state.inhibited:
            raise PlatformError("grant_inhibited", "The grant is operationally inhibited", 403)
        if not is_qualified(session, self.settings, active_generation, bundle.get("default_profile", "local")):
            raise PlatformError("qualification_required", "The active deployment has no valid trusted qualification", 403)
        return recipe

    def submit(self, grant_id: str, task: str, caller: str, idempotency_key: str, input_class: str | None = None, is_operator: bool = False) -> Job:
        if not task.strip() or len(task.encode()) > self.settings.max_request_bytes:
            raise PlatformError("invalid_task", "Task content is empty or exceeds the configured limit", 422)
        if not idempotency_key or len(idempotency_key) > 160:
            raise PlatformError("invalid_idempotency_key", "An idempotency key of 1..160 characters is required", 422)
        request_digest = digest({"grant_id": grant_id, "task": task, "input_class": input_class})
        key = digest({"caller": caller, "key": idempotency_key})
        try:
            with self.db.sessions.begin() as session:
                recipe = self.authorize(session, grant_id, caller, input_class, is_operator=is_operator)
                recipe["submission_role"] = "operator" if is_operator else "caller"
                existing = session.scalar(select(Job).where(Job.idempotency_key == key))
                if existing:
                    if existing.request_digest != request_digest:
                        raise PlatformError("idempotency_conflict", "This idempotency key was already used for a different request")
                    return existing
                row = Job(id=uuid.uuid4().hex, receipt="rcpt_" + secrets.token_urlsafe(24), idempotency_key=key, request_digest=request_digest, caller=caller, grant_id=grant_id, generation=recipe["generation"], resolved=recipe, task=task, status="queued", attempts=0, next_attempt_at=time.time())
                session.add(row)
                self.db.audit(session, caller, "task.accepted", row.id, {"grant_id": grant_id, "generation": row.generation})
                session.flush()
                return row
        except IntegrityError:
            with self.db.sessions() as session:
                existing = session.scalar(select(Job).where(Job.idempotency_key == key))
                if existing and existing.request_digest == request_digest:
                    return existing
            raise PlatformError("idempotency_conflict", "This idempotency key was already used")

    def get_job(self, job_id: str, session=None) -> Job:
        if session is None:
            with self.db.sessions() as own:
                return self.get_job(job_id, own)
        row = session.get(Job, job_id)
        if not row:
            raise PlatformError("not_found", "Task not found", 404)
        return row

    def public_status(self, receipt: str, caller: str, *, audit: bool = True) -> dict:
        with self.db.sessions.begin() as session:
            row = session.scalar(select(Job).where(Job.receipt == receipt, Job.caller == caller))
            if row is None:
                raise PlatformError("not_found", "Receipt not found", 404)
            try:
                self.authorize(session, row.grant_id, caller, generation=row.generation)
            except PlatformError:
                # Never return a formerly permitted result after current authority changes.
                return {"receipt": row.receipt, "status": "unavailable"}
            projection = {"receipt": row.receipt, "status": PUBLIC_STATUS[row.status]}
            if row.status == "completed" and row.public_projection is None:
                projection["status"] = "unavailable"
            if audit:
                self.db.audit(session, caller, "receipt.observed", row.id, {"status": projection["status"]})
            return projection

    def inhibit(self, grant_id: str, reason: str, inhibit: bool = True) -> dict:
        if not reason.strip() or len(reason) > 500:
            raise PlatformError("invalid_reason", "Provide an operational reason of 1..500 characters", 422)
        with self.db.sessions.begin() as session:
            bundle, generation = self.active(session)
            grant = next((g for g in bundle.get("grants", []) if g["id"] == grant_id), None)
            if not grant:
                raise PlatformError("not_found", "Grant not found", 404)
            if not inhibit:
                if not grant.get("enabled", True):
                    raise PlatformError("grant_disabled", "The desired configuration disables this grant", 403)
                if not is_qualified(session, self.settings, generation, bundle.get("default_profile", "local")):
                    raise PlatformError("qualification_required", "Re-enablement requires current qualification", 403)
            row = session.get(GrantState, grant_id)
            if row is None:
                row = GrantState(id=grant_id)
                session.add(row)
            row.inhibited, row.reason, row.updated_at = inhibit, reason, time.time()
            if inhibit:
                session.execute(update(Job).where(Job.grant_id == grant_id, Job.status == "running").values(cancel_requested=True, updated_at=time.time()))
                session.execute(update(Job).where(Job.grant_id == grant_id, Job.status.in_(["queued", "retry_wait"])).values(status="blocked", error_code="grant_inhibited", finished_at=time.time(), updated_at=time.time()))
            self.db.audit(session, "operator", "grant.inhibited" if inhibit else "grant.resumed", grant_id, {"reason": reason, "generation": generation})
            return {"id": grant_id, "inhibited": inhibit, "reason": reason, "updated_at": row.updated_at}

    def cancel(self, job_id: str) -> Job:
        with self.db.sessions.begin() as session:
            row = self.get_job(job_id, session)
            if row.status not in TERMINAL:
                row.cancel_requested, row.updated_at = True, time.time()
                if row.status != "running":
                    row.status, row.finished_at = "cancelled", time.time()
                self.db.audit(session, "operator", "task.cancel_requested", row.id)
            return row


def job_dict(row: Job, detail: bool = False) -> dict[str, Any]:
    fields = ("id", "receipt", "grant_id", "caller", "generation", "status", "created_at", "updated_at", "started_at", "finished_at", "attempts", "next_attempt_at", "cancel_requested", "error_code", "released_at")
    result = {name: getattr(row, name) for name in fields}
    result["artifact_available"] = bool(row.artifact_key)
    if detail:
        result["task"], result["resolved"] = row.task, row.resolved
    return result
