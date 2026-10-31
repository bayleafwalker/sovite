from __future__ import annotations

import copy

import secrets
import json
import time
import uuid
from contextvars import ContextVar
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from . import policy
from . import repository_currentness as currentness
from .db import BundleRevision, Database, GrantState, Job, MetadataRow, canonical, digest
from .hosting import compile_bundle, load_envelope
from .qualification import is_qualified, validate_issuers
from .settings import Settings
from .storage import ArtifactStore


TERMINAL = {"completed", "failed", "cancelled", "blocked"}
PUBLIC_STATUS = {"queued": "pending", "retry_wait": "pending", "running": "working", "completed": "available", "failed": "unavailable", "cancelled": "unavailable", "blocked": "unavailable"}
_REAUTHORIZING = ContextVar("sovite_job_reauthorization", default=frozenset())
_AUTHORIZATION_STARTED = ContextVar("sovite_authorization_started", default=None)


class PlatformError(Exception):
    def __init__(self, code: str, message: str, status: int = 409, *, receipt: str | None = None):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
        self.receipt = receipt


class Platform:
    def __init__(self, settings: Settings, *, repository_currentness_authorizer=None):
        self.settings = settings
        self.repository_currentness_authorizer = repository_currentness_authorizer
        self.db = Database(settings.database_url, schema=settings.database_schema)
        self.store = ArtifactStore(settings)

    def initialize(self):
        validate_issuers(self.settings)
        self.db.initialize(self.settings.database_migrate)
        with self.db.sessions() as session:
            active = session.get(MetadataRow, "active_generation")
        # API replicas do not silently revert a revision loaded through the CLI.
        if active is None and not self.settings.hosting_envelope_path:
            self.reload(self.settings.bundle_path, initial=True)
        elif not self.settings.require_qualification:
            bundle, _ = self.active()
            if bundle.get("mode") == "enforced":
                raise PlatformError("qualification_cannot_be_disabled", "Enforced deployments require trusted qualification", 400)

    def compile(self, bundle: dict, role: str = "operator") -> str:
        self.policy_role(role, "author")
        bundle, generation = compile_bundle(bundle, load_envelope(self.settings.hosting_envelope_path))
        with self.db.sessions.begin() as session:
            if session.get(BundleRevision, generation) is None:
                session.add(BundleRevision(generation=generation, bundle=bundle))
            self.db.audit(session, role, "configuration.compiled", generation)
        return generation

    def policy_role(self, role: str, required: str, *, api: bool = False):
        if role != required and not (role == "operator" and not api and not self.settings.hosting_envelope_path):
            raise PlatformError("forbidden", f"{required.capitalize()} access is required", 403)

    def reload(self, path: str, initial: bool = False, role: str = "operator") -> str:
        if self.settings.hosting_envelope_path:
            raise PlatformError("hosted_reload_forbidden", "Hosted policy requires compile by an author, then activate <generation> by an activator", 403)
        self.policy_role(role, "activator")
        bundle, generation = compile_bundle(policy.load_bundle(path)[0], load_envelope(self.settings.hosting_envelope_path))
        if bundle.get("mode") == "enforced" and not self.settings.require_qualification:
            raise PlatformError("qualification_cannot_be_disabled", "Enforced deployments require trusted qualification", 400)
        with self.db.sessions.begin() as session:
            if session.get(BundleRevision, generation) is None:
                session.add(BundleRevision(generation=generation, bundle=bundle))
        return self.activate(generation, role, initial)

    def activate(self, generation: str, role: str = "operator", initial: bool = False) -> str:
        self.policy_role(role, "activator")
        with self.db.sessions() as session:
            revision = session.get(BundleRevision, generation)
            if revision is None:
                raise PlatformError("configuration_missing", "Compile the configuration before activation", 404)
            bundle, expected = compile_bundle(revision.bundle, load_envelope(self.settings.hosting_envelope_path))
        if generation != expected:
            raise PlatformError("hosting_changed", "Recompile against the current hosting envelope", 409)
        if bundle.get("mode") == "enforced" and not self.settings.require_qualification:
            raise PlatformError("qualification_cannot_be_disabled", "Enforced deployments require trusted qualification", 400)
        with self.db.sessions.begin() as session:
            self.db.authority_lock(session)
            self.activate_bundle(session, bundle, generation, role=role, initial=initial)
        return generation

    def activate_bundle(self, session, bundle: dict, generation: str, *, role: str = "operator", initial: bool = False):
        """Activate a validated bundle while holding the database authority lock."""
        self.policy_role(role, "activator")
        bundle, expected = compile_bundle(bundle, load_envelope(self.settings.hosting_envelope_path))
        if generation != expected:
            raise PlatformError("hosting_changed", "Recompile against the current hosting envelope", 409)
        if bundle.get("mode") == "enforced" and not self.settings.require_qualification:
            raise PlatformError("qualification_cannot_be_disabled", "Enforced deployments require trusted qualification", 400)
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
        self.db.audit(session, role, "configuration.initialized" if initial else "configuration.activated", generation, {"previous_generation": old, "mode": bundle.get("mode", "development")})

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
        if self.settings.hosting_envelope_path:
            try:
                _, expected = compile_bundle(row.bundle, load_envelope(self.settings.hosting_envelope_path))
                if expected != generation or not self.settings.require_qualification:
                    raise ValueError("Hosted binding changed")
            except (ValueError, OSError) as exc:
                raise PlatformError("hosting_changed", "The active configuration requires current hosting policy and qualification", 403) from exc
        return row.bundle, generation

    def authorize(self, session, grant_id: str, caller: str, input_class: str | None = None, generation: str | None = None, is_operator: bool = False, *, workflow_bound: bool = False) -> dict:
        bundle, active_generation = self.active(session)
        if generation is not None and generation != active_generation:
            raise PlatformError("revision_superseded", "The task's configuration revision is no longer active")
        grant = next((g for g in bundle.get("grants", []) if g["id"] == grant_id), None)
        if not grant:
            raise PlatformError("grant_unavailable", "The requested grant is unavailable", 403)
        try:
            recipe = policy.authorize_job(bundle, grant_id, caller, input_class, is_operator=is_operator, workflow_bound=workflow_bound)
        except policy.PolicyError as exc:
            raise PlatformError(getattr(exc, "code", "policy_denied"), "The current flow contract does not permit this task", 403) from exc
        state = session.get(GrantState, grant_id)
        if state is not None and state.inhibited:
            raise PlatformError("grant_inhibited", "The grant is operationally inhibited", 403)
        if not is_qualified(session, self.settings, active_generation, bundle.get("default_profile", "local")):
            raise PlatformError("qualification_required", "The active deployment has no valid trusted qualification", 403)
        recipe["generation"] = active_generation
        return recipe

    def authorize_existing_job(self, session, row: Job, *, purpose: str = "active") -> dict:
        """Recheck the job and any approved source chain at every owner boundary."""
        if purpose not in {"active", "observe"}:
            raise PlatformError("invalid_authorization_purpose", "Authorization purpose is invalid", 500)
        seen = _REAUTHORIZING.get()
        if row.id in seen or len(seen) >= 128:
            raise PlatformError("invalid_source_chain", "Output source chain is cyclic or exceeds its bound", 409)
        token = _REAUTHORIZING.set(seen | {row.id})
        started_token = None
        started = _AUTHORIZATION_STARTED.get()
        if started is None:
            started = time.monotonic()
            started_token = _AUTHORIZATION_STARTED.set(started)
        try:
            prior = row.resolved.get("previous_attempt")
            if prior is not None:
                from .attempts import previous
                if previous(self, session, prior.get("receipt"), row.caller,
                            role=row.resolved.get("submission_role", "caller"), recorded=prior) != prior:
                    raise PlatformError("previous_attempt_unavailable", "Previous attempt no longer matches its recorded identity", 409)
            binding = row.resolved.get("workflow_binding")
            selected = None
            if binding is not None:
                selected = self._check_workflow_binding(session, binding, row.resolved.get("workflow_role"))
                if selected["grant_id"] != row.grant_id or selected["input_class"] != row.resolved.get("input_class"):
                    raise PlatformError("workflow_binding_mismatch", "The job no longer matches its workflow role")
            recipe = self.authorize(session, row.grant_id, row.caller,
                input_class=row.resolved.get("input_class", row.resolved["grant"]["input_class"]),
                generation=row.generation, is_operator=row.resolved.get("submission_role") == "operator",
                workflow_bound=selected is not None)
            if purpose == "active":
                currentness.check_job(self, row, recipe, authorization_started_at=started)
            source_release = row.resolved.get("source_release")
            if selected is not None and source_release:
                from .output_contracts import transfer_content
                expected = transfer_content(self, session, binding, row.resolved["workflow_role"],
                    row.resolved.get("transfer_id"), source_release)
                if row.task != expected:
                    raise PlatformError("transfer_mismatch", "The job content differs from its approved output transfer")
            elif selected is not None and selected["requires_output_release"]:
                raise PlatformError("output_release_required", "The role has no approved output transfer", 403)
            from .repository_repairs import recheck
            recheck(self, session, row, recipe)
            return recipe
        except currentness.CurrentnessError as exc:
            raise PlatformError(exc.code, exc.message, exc.status) from None
        finally:
            _REAUTHORIZING.reset(token)
            if started_token is not None:
                _AUTHORIZATION_STARTED.reset(started_token)

    def workflow_installation_id(self) -> str:
        envelope = load_envelope(self.settings.hosting_envelope_path)
        installed = envelope["installation_id"] if envelope else self.settings.installation_id
        if envelope and self.settings.installation_id and installed != self.settings.installation_id:
            raise PlatformError("installation_mismatch", "Installation identity differs from the hosting envelope", 409)
        if not installed:
            raise PlatformError("installation_identity_required", "Set SOVITE_INSTALLATION_ID before binding workflows", 409)
        return installed

    def bind_workflow(self, document: dict, requirements_ref: dict, session_policy_ref: dict,
                      expected_generation: str, *, input_class: str | None = None,
                      actor: str = "operator") -> dict:
        from .workflows import compile_workflow
        self.policy_role(actor, "author")
        with self.db.sessions.begin() as session:
            self.db.authority_lock(session)
            bundle, generation = self.active(session)
            if expected_generation != generation:
                raise PlatformError("revision_superseded", "The expected installation generation is no longer active")
            binding = compile_workflow(document, bundle, requirements_ref=requirements_ref,
                session_policy_ref=session_policy_ref, installation_id=self.workflow_installation_id(),
                generation=generation, input_class=input_class)
            # A hash establishes integrity, not the owner's selection.
            key = "workflow_" + binding["binding_digest"].removeprefix("sha256:")
            existing = session.get(MetadataRow, key)
            if existing is None:
                session.add(MetadataRow(key=key, value={"binding": binding, "actor": actor,
                                                       "bound_at": time.time()}))
                self.db.audit(session, actor, "workflow.bound", binding["binding_digest"],
                    {"workflow_revision": binding["workflow_revision"], "generation": generation,
                     "input_class": binding["input_class"]})
            elif existing.value.get("binding") != binding:
                raise PlatformError("workflow_binding_mismatch", "The stored workflow binding failed integrity verification")
            return binding

    def _check_workflow_binding(self, session, binding: dict, role: str) -> dict:
        from .workflows import compile_workflow, delivery_role_names, WorkflowError
        bundle, generation = self.active(session)
        if binding.get("bundle_generation") != generation:
            raise PlatformError("revision_superseded", "The workflow installation generation is no longer active")
        try:
            rebuilt = compile_workflow(binding["definition"], bundle,
                requirements_ref=binding["requirements_ref"], session_policy_ref=binding["session_policy_ref"],
                installation_id=self.workflow_installation_id(), generation=generation,
                input_class=binding["input_class"])
            if rebuilt != binding or role not in delivery_role_names(rebuilt["definition"]):
                raise ValueError("Binding mismatch or external authoring role")
        except (KeyError, TypeError, ValueError, WorkflowError) as exc:
            raise PlatformError("workflow_binding_mismatch", "The workflow binding or delivery role does not match the installed definition", 409) from exc
        key = "workflow_" + rebuilt["binding_digest"].removeprefix("sha256:")
        installed = session.get(MetadataRow, key)
        if installed is None or installed.value.get("binding") != rebuilt:
            raise PlatformError("workflow_not_bound", "An author must bind this exact workflow before role submission", 403)
        return rebuilt["roles"][role]

    def submit_workflow_role(self, binding: dict, role: str, task: str, caller: str,
                             idempotency_key: str, *, is_operator: bool = False,
                             source_release: str | None = None, transfer_id: str | None = None,
                             previous_attempt: str | None = None, selection_reason: str | None = None) -> Job:
        try:
            grant = binding["roles"][role]["grant_id"]
            input_class = binding["roles"][role]["input_class"]
        except (KeyError, TypeError) as exc:
            raise PlatformError("workflow_binding_mismatch", "Unknown workflow role", 422) from exc
        return self.submit(grant, task, caller, idempotency_key, input_class, is_operator,
            expected_generation=binding.get("bundle_generation"), workflow_binding=binding,
            workflow_role=role, source_release=source_release, transfer_id=transfer_id,
            previous_attempt=previous_attempt, selection_reason=selection_reason)

    def submit(self, grant_id: str, task: str, caller: str, idempotency_key: str, input_class: str | None = None, is_operator: bool = False,
               *, expected_generation: str | None = None, workflow_binding: dict | None = None,
               workflow_role: str | None = None, source_release: str | None = None,
               transfer_id: str | None = None, previous_attempt: str | None = None,
               selection_reason: str | None = None,
               repository_run: dict | None = None) -> Job:
        from .attempts import previous, validate_metadata
        validate_metadata(previous_attempt, selection_reason)
        try:
            valid_task = (isinstance(task, str) and (bool(task.strip()) or source_release is not None)
                          and len(task.encode("utf-8")) <= self.settings.max_request_bytes)
        except UnicodeError:
            valid_task = False
        if not valid_task:
            raise PlatformError("invalid_task", "Task content must be valid UTF-8, nonempty and within the configured limit", 422)
        try:
            valid_key = (isinstance(idempotency_key, str) and 1 <= len(idempotency_key) <= 160
                         and bool(idempotency_key.encode("utf-8")))
        except UnicodeError:
            valid_key = False
        if not valid_key:
            raise PlatformError("invalid_idempotency_key", "A valid UTF-8 idempotency key of 1..160 characters is required", 422)
        request = {"grant_id": grant_id, "task": task, "input_class": input_class}
        if previous_attempt is not None:
            request["previous_attempt"] = previous_attempt
        if selection_reason is not None:
            request["selection_reason"] = selection_reason
        if workflow_binding is not None:
            request.update(workflow_binding=workflow_binding, workflow_role=workflow_role,
                           source_release=source_release, transfer_id=transfer_id)
        if repository_run is not None:
            # Only the owner repository-run adapter supplies this closed,
            # resolver-authored binding. Including it in the request digest
            # makes idempotency cover the repository, base and run limits.
            required_repository_fields = {
                "schema_version", "packet_sha256", "preview_id", "workspace_id", "repository_id",
                "idempotency_key", "principal_id", "binding_id", "mirror_root", "grant_id", "input_class", "base_sha", "allowed_areas",
                "executor_id", "publication_revision", "generation",
                "qualification_ref", "qualification_sha256", "qualification_expires_at",
                "max_cost_usd", "deadline", "concurrency", "usage_usd", "peak_concurrency",
                "budget_reservations", "active_slots", "attempts", "evidence",
            }
            if (not isinstance(repository_run, dict)
                    or set(repository_run) not in (required_repository_fields, required_repository_fields | {"repair"})
                    or repository_run.get("schema_version") != "sovite-repository-run/v1"
                    or repository_run.get("principal_id") != caller
                    or repository_run.get("grant_id") != grant_id
                    or repository_run.get("input_class") != input_class):
                raise PlatformError("invalid_repository_run", "Repository-run owner binding is invalid", 422)
            request["repository_run"] = repository_run
        if expected_generation is not None:
            request["expected_generation"] = expected_generation
        try:
            request_digest = digest(request)
            key = digest({"caller": caller, "key": idempotency_key})
        except (ValueError, TypeError, RecursionError):
            raise PlatformError("invalid_request", "The task request must contain valid UTF-8 values", 422) from None
        try:
            with self.db.sessions.begin() as session:
                self.db.authority_lock(session)
                existing = session.scalar(select(Job).where(Job.idempotency_key == key))
                if existing:
                    same_role = existing.resolved.get("submission_role", "caller") == ("operator" if is_operator else "caller")
                    if not same_role or existing.request_digest != request_digest:
                        raise PlatformError("idempotency_conflict", "This idempotency key was already used for a different request",
                                            receipt=existing.receipt if same_role else None)
                    # Receipt recovery never re-admits or executes work. The result
                    # projection independently rechecks current release authority.
                    return existing
                if workflow_binding is not None:
                    selected = self._check_workflow_binding(session, workflow_binding, workflow_role)
                    if selected["grant_id"] != grant_id or selected["input_class"] != input_class:
                        raise PlatformError("workflow_binding_mismatch", "Role input and grant differ from the binding")
                    if source_release is not None:
                        if task.strip():
                            raise PlatformError("unclassified_addition", "A released transfer must be submitted without additional task content", 422)
                        from .output_contracts import transfer_content
                        task = transfer_content(self, session, workflow_binding, workflow_role, transfer_id, source_release)
                    elif selected["requires_output_release"]:
                        raise PlatformError("output_release_required", "This role requires an approved output transfer", 403)
                    elif transfer_id is not None:
                        raise PlatformError("output_release_required", "A transfer requires its approved output release", 422)
                    else:
                        task = json.dumps({"role": workflow_role,
                            "workflow_revision": workflow_binding["workflow_revision"],
                            "requirements_ref": workflow_binding["requirements_ref"],
                            "session_policy_ref": workflow_binding["session_policy_ref"],
                            "instructions": workflow_binding["definition"], "input": task},
                            ensure_ascii=False, separators=(",", ":"))
                elif source_release is not None or transfer_id is not None:
                    raise PlatformError("workflow_binding_required", "Output transfers require a bound workflow role", 422)
                recipe = self.authorize(session, grant_id, caller, input_class, generation=expected_generation, is_operator=is_operator,
                                        workflow_bound=workflow_binding is not None)
                prior = previous(self, session, previous_attempt, caller, role="operator" if is_operator else "caller")
                if prior is not None:
                    recipe["previous_attempt"] = prior
                recipe["selection"] = {"grant_id": grant_id, "model_id": recipe["model"]["id"],
                    "harness_id": recipe["harness"]["id"], "generation": recipe["generation"],
                    "reason": selection_reason, "reason_source": "caller" if selection_reason is not None else "not_supplied"}
                if repository_run is not None:
                    if repository_run["generation"] != recipe["generation"]:
                        raise PlatformError("revision_superseded", "Repository-run binding generation is no longer active", 409)
                    if "repair" in repository_run:
                        from .repository_repairs import admit
                        admit(self, session, repository_run, caller, recipe, task)
                    recipe["repository_run"] = copy.deepcopy(repository_run)
                admission = currentness.admit(self, session, repository_run=repository_run,
                    task=task, caller=caller, recipe=recipe)
                if len(task.encode()) > self.settings.max_request_bytes:
                    raise PlatformError("invalid_task", "Task and workflow context exceed the configured limit", 422)
                if workflow_binding is not None:
                    recipe.update(workflow_binding=workflow_binding, workflow_role=workflow_role,
                                  source_release=source_release, transfer_id=transfer_id)
                recipe["submission_role"] = "operator" if is_operator else "caller"
                row = Job(id=uuid.uuid4().hex, receipt="rcpt_" + secrets.token_urlsafe(24), idempotency_key=key, request_digest=request_digest, caller=caller, grant_id=grant_id, generation=recipe["generation"], resolved=recipe, task=task, status="queued", attempts=0, next_attempt_at=time.time())
                currentness.bind_job(self, row, admission)
                session.add(row)
                self.db.audit(session, caller, "task.accepted", row.id, {"grant_id": grant_id, "generation": row.generation})
                session.flush()
                return row
        except currentness.CurrentnessError as exc:
            raise PlatformError(exc.code, exc.message, exc.status) from None
        except IntegrityError:
            with self.db.sessions() as session:
                existing = session.scalar(select(Job).where(Job.idempotency_key == key))
                same_role = existing is not None and existing.resolved.get("submission_role", "caller") == ("operator" if is_operator else "caller")
                if same_role and existing.request_digest == request_digest:
                    return existing
                raise PlatformError("idempotency_conflict", "This idempotency key was already used",
                                    receipt=existing.receipt if same_role else None)

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
                self.authorize_existing_job(session, row, purpose="observe")
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
            self.db.authority_lock(session)
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

    def inhibit_binding(self, binding_id: str, reason: str, inhibit: bool = True) -> dict:
        from .authority import Authority
        from .db import BindingState, HelperSession
        if not reason.strip() or len(reason) > 500:
            raise PlatformError("invalid_reason", "Provide an operational reason of 1..500 characters", 422)
        with self.db.sessions.begin() as session:
            self.db.authority_lock(session)
            bundle, generation = self.active(session)
            binding = next((m for m in bundle["models"] if m["id"] == binding_id), None)
            if binding is None:
                raise PlatformError("not_found", "Binding not found", 404)
            if not inhibit:
                if not binding.get("enabled", True):
                    raise PlatformError("binding_disabled", "The desired configuration disables this binding", 403)
                if not is_qualified(session, self.settings, generation, bundle.get("default_profile", "local")):
                    raise PlatformError("qualification_required", "Re-enablement requires current qualification", 403)
            else:
                authority = Authority(self)
                for helper in session.scalars(select(HelperSession).where(HelperSession.binding_id == binding_id, HelperSession.active == True)):
                    authority._close(session, helper)
            state = session.get(BindingState, binding_id)
            if state is None:
                state = BindingState(id=binding_id)
                session.add(state)
            state.inhibited, state.reason, state.updated_at = inhibit, reason, time.time()
            self.db.audit(session, "operator", "binding.inhibited" if inhibit else "binding.resumed", binding_id, {"reason": reason, "generation": generation})
            return {"id": binding_id, "inhibited": inhibit, "reason": reason, "updated_at": state.updated_at}

    def cancel(self, job_id: str, *, actor: str = "operator", require_owner: bool = False) -> Job:
        with self.db.sessions.begin() as session:
            self.db.authority_lock(session)
            row = self.get_job(job_id, session)
            if require_owner and row.caller != actor:
                raise PlatformError("not_found", "Task not found", 404)
            if row.status not in TERMINAL:
                row.cancel_requested, row.updated_at = True, time.time()
                if row.status != "running":
                    row.status, row.finished_at = "cancelled", time.time()
                self.db.audit(session, actor, "task.cancel_requested", row.id)
            return row


def job_dict(row: Job, detail: bool = False) -> dict[str, Any]:
    fields = ("id", "receipt", "grant_id", "caller", "generation", "status", "created_at", "updated_at", "started_at", "finished_at", "attempts", "next_attempt_at", "cancel_requested", "error_code", "released_at")
    result = {name: getattr(row, name) for name in fields}
    result["artifact_available"] = bool(row.artifact_key)
    if detail:
        result["task"], result["resolved"] = row.task, row.resolved
    return result
