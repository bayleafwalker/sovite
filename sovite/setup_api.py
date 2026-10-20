"""Opt-in, installation-bound setup proposals. These credentials cannot execute work."""
from __future__ import annotations

import copy
import hashlib
import secrets
import time
import uuid

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from . import policy
from .db import SetupDraft, SetupPrincipal, digest
from .service import PlatformError


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Envelope(Body):
    # Exact approved model templates prevent endpoint, credential and parameter widening.
    models: list[dict] = Field(min_length=1, max_length=8)
    harnesses: list[str] = Field(min_length=1, max_length=8)
    grants: list[str] = Field(min_length=1, max_length=8)
    max_steps: int = Field(ge=1, le=32, default=8)


class NewPrincipal(Body):
    name: str = Field(min_length=1, max_length=160)
    ttl_seconds: int = Field(ge=60, le=86400, default=3600)
    envelope: Envelope


class Proposal(Body):
    idempotency_key: str = Field(min_length=1, max_length=160)
    base_generation: str = Field(min_length=1, max_length=80)
    model: dict
    agent_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,79}$")
    agent_name: str = Field(min_length=1, max_length=160)
    harness: str = Field(min_length=1, max_length=160)
    instructions: str = Field(min_length=1, max_length=8000)
    max_steps: int = Field(ge=1, le=32)
    grant: str = Field(min_length=1, max_length=160)


class Activation(Body):
    generation: str = Field(min_length=1, max_length=80)


def install_routes(app, platform, operator):
    if not platform.settings.setup_api_enabled or platform.settings.hosting_envelope_path:
        return
    installation = platform.settings.setup_installation_id

    def current(session, token=None, principal_id=None):
        if token is not None:
            row = session.scalar(select(SetupPrincipal).where(
                SetupPrincipal.token_hash == hashlib.sha256(token.encode()).hexdigest()))
        else:
            row = session.get(SetupPrincipal, principal_id)
        if not row or row.installation_id != installation or row.revoked or row.expires_at <= time.time():
            raise PlatformError("setup_authority_unavailable", "Current setup authority is required", 403)
        return row

    def bearer(request):
        value = request.headers.get("authorization", "")
        if not value.startswith("Bearer ") or len(value) > 1024:
            raise PlatformError("unauthorized", "A setup bearer credential is required", 401)
        return value[7:]

    def projection(row, *, include_bundle=False):
        result = {"id": row.id, "author": row.author, "installation_id": row.installation_id,
                "base_generation": row.base_generation, "generation": row.generation,
                "state": row.state}
        if include_bundle:
            result["bundle"] = row.bundle
        return result

    @app.post("/api/setup/principals", status_code=201)
    def mint(body: NewPrincipal, auth=Depends(operator)):
        envelope = body.envelope.model_dump()
        with platform.db.sessions.begin() as session:
            platform.db.authority_lock(session)
            bundle, _ = platform.active(session)
            if not set(envelope["harnesses"]) <= {h["id"] for h in bundle["harnesses"]} or not set(envelope["grants"]) <= {g["id"] for g in bundle["grants"]}:
                raise PlatformError("invalid_envelope", "Envelope must reference existing harnesses and grants", 422)
            # Validate each exact approved model against the installation's destination policy.
            for model in envelope["models"]:
                trial = copy.deepcopy(bundle)
                if not isinstance(model.get("id"), str) or any(m["id"] == model["id"] for m in trial["models"]):
                    raise PlatformError("invalid_envelope", "Approved models must have new identifiers", 422)
                trial["models"].append(model)
                try:
                    policy.validate_bundle(trial)
                    policy.bundle_generation(trial)  # Reject non-finite JSON before durable storage.
                except (policy.PolicyError, ValueError):
                    raise PlatformError("invalid_envelope", "Approved model violates the installation policy", 422) from None
            token = "setup_" + secrets.token_urlsafe(32)
            row = SetupPrincipal(id=uuid.uuid4().hex, name=body.name, installation_id=installation,
                token_hash=hashlib.sha256(token.encode()).hexdigest(), envelope=envelope,
                expires_at=time.time() + body.ttl_seconds, revoked=False)
            session.add(row)
            platform.db.audit(session, auth[0], "setup.principal_created", row.id,
                              {"installation_id": installation, "expires_at": row.expires_at})
            return {"id": row.id, "name": row.name, "installation_id": installation,
                    "expires_at": row.expires_at, "token": token, "envelope": envelope}

    @app.get("/api/setup/principals")
    def principals(after: str = "", auth=Depends(operator)):
        with platform.db.sessions() as session:
            rows = list(session.scalars(select(SetupPrincipal).where(
                SetupPrincipal.installation_id == installation, SetupPrincipal.id > after
            ).order_by(SetupPrincipal.id).limit(100)))
            return {"items": [{"id": row.id, "name": row.name, "installation_id": row.installation_id,
                    "expires_at": row.expires_at, "revoked": row.revoked} for row in rows],
                    "next_cursor": rows[-1].id if len(rows) == 100 else None}

    @app.post("/api/setup/principals/{principal_id}/revoke")
    def revoke(principal_id: str, auth=Depends(operator)):
        with platform.db.sessions.begin() as session:
            platform.db.authority_lock(session)
            row = session.get(SetupPrincipal, principal_id)
            if not row or row.installation_id != installation:
                raise PlatformError("not_found", "Setup principal not found", 404)
            row.revoked = True
            platform.db.audit(session, auth[0], "setup.principal_revoked", row.id)
            return {"id": row.id, "revoked": True}

    @app.get("/api/setup/context")
    def context(request: Request):
        with platform.db.sessions.begin() as session:
            platform.db.authority_lock(session)
            principal = current(session, token=bearer(request))
            _, generation = platform.active(session)
            return {"principal_id": principal.id, "installation_id": installation,
                    "generation": generation, "expires_at": principal.expires_at, "envelope": principal.envelope}

    @app.post("/api/setup/drafts", status_code=201)
    def propose(body: Proposal, request: Request):
        with platform.db.sessions.begin() as session:
            platform.db.authority_lock(session)
            principal = current(session, token=bearer(request))
            request_digest = digest(body.model_dump())
            key = digest({"author": principal.id, "key": body.idempotency_key})
            existing = session.scalar(select(SetupDraft).where(SetupDraft.idempotency_key == key))
            if existing:
                if existing.request_digest != request_digest:
                    raise PlatformError("idempotency_conflict", "The key names a different proposal")
                return projection(existing)
            bundle, generation = platform.active(session)
            if body.base_generation != generation:
                raise PlatformError("revision_superseded", "Refresh the active setup context before proposing")
            bounds = principal.envelope
            if body.model not in bounds["models"] or body.harness not in bounds["harnesses"] or body.grant not in bounds["grants"] or body.max_steps > bounds["max_steps"]:
                raise PlatformError("setup_scope_denied", "Proposal exceeds the operator-approved setup envelope", 403)
            # No existing definitions, tools, callers, classification or grant authority can be edited.
            if any(m["id"] == body.model.get("id") for m in bundle["models"]) or any(a["id"] == body.agent_id for a in bundle["agents"]):
                raise PlatformError("definition_exists", "Setup proposals only create new model and agent definitions")
            proposed = copy.deepcopy(bundle)
            proposed["models"].append(body.model)
            proposed["agents"].append({"id": body.agent_id, "name": body.agent_name,
                "model": body.model["id"], "harness": body.harness, "tools": [],
                "instructions": body.instructions, "max_steps": body.max_steps, "version": "1.0.0"})
            next(g for g in proposed["grants"] if g["id"] == body.grant)["agent"] = body.agent_id
            try:
                proposed = policy.validate_bundle(proposed)
            except (policy.PolicyError, ValueError):
                raise PlatformError("invalid_proposal", "Proposal violates the installation policy", 422) from None
            row = SetupDraft(id=uuid.uuid4().hex, author=principal.id, installation_id=installation,
                idempotency_key=key, request_digest=request_digest, base_generation=generation,
                generation=policy.bundle_generation(proposed), bundle=proposed, state="draft")
            session.add(row)
            platform.db.audit(session, principal.id, "setup.draft_created", row.id,
                              {"base_generation": generation, "generation": row.generation})
            return projection(row)

    @app.get("/api/setup/my-drafts/{draft_id}")
    def own_draft(draft_id: str, request: Request):
        with platform.db.sessions.begin() as session:
            platform.db.authority_lock(session)
            principal = current(session, token=bearer(request))
            row = session.get(SetupDraft, draft_id)
            if not row or row.author != principal.id or row.installation_id != installation:
                raise PlatformError("not_found", "Setup draft not found", 404)
            return projection(row)

    @app.get("/api/setup/drafts/{draft_id}")
    def inspect_draft(draft_id: str, auth=Depends(operator)):
        with platform.db.sessions() as session:
            row = session.get(SetupDraft, draft_id)
            if not row or row.installation_id != installation:
                raise PlatformError("not_found", "Setup draft not found", 404)
            return projection(row, include_bundle=True)

    @app.post("/api/setup/drafts/{draft_id}/activate")
    def activate(draft_id: str, body: Activation, auth=Depends(operator)):
        with platform.db.sessions.begin() as session:
            platform.db.authority_lock(session)
            row = session.get(SetupDraft, draft_id)
            if not row or row.installation_id != installation:
                raise PlatformError("not_found", "Setup draft not found", 404)
            current(session, principal_id=row.author)
            _, generation = platform.active(session)
            if body.generation != row.generation:
                raise PlatformError("proposal_mismatch", "Approve the exact inspected generation")
            if row.state == "activated" and generation == row.generation:
                return projection(row)
            if row.state != "draft" or generation != row.base_generation:
                raise PlatformError("revision_superseded", "The proposal is stale; create a new proposal")
            platform.activate_bundle(session, row.bundle, row.generation)
            row.state = "activated"
            platform.db.audit(session, auth[0], "setup.draft_activated", row.id,
                              {"author": row.author, "generation": row.generation})
            return projection(row)
