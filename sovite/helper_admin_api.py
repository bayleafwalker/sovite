"""Operator-only lifecycle controls for enrolled remote inference helpers."""
from __future__ import annotations

import time
from typing import Annotated

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select

from .authority import provision_enrollment, reconcile_helper_demand, revoke_enrollment, rotate_enrollment
from .connector.protocol import FRESHNESS_SECONDS
from .db import BindingState, HelperEnrollment, HelperSession, Qualification
from .qualification import verify_qualification
from .service import PlatformError


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CertificateRotation(Body):
    # The CA remains external. This endpoint accepts only an already-issued client cert.
    certificate_pem: SecretStr = Field(min_length=1, max_length=16_384)


class EnrollmentProvision(Body):
    binding_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")
    installation_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")
    role: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")
    certificate_pem: SecretStr = Field(min_length=1, max_length=16_384)


EnrollmentId = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")]


def helper_readiness(platform):
    """Build a read-only, provisional evidence view; this is never dispatch authority."""
    now = time.time()
    with platform.db.sessions() as session:
        bundle, generation = platform.active(session)
        qualifications = list(session.scalars(select(Qualification).where(
            Qualification.generation == generation).order_by(Qualification.created_at.desc())))
        trusted = [row for row in qualifications if verify_qualification(row, platform.settings, now)]
        projections = []
        for binding in bundle.get("models", []):
            connection = binding.get("connection", {})
            if connection.get("kind") != "model-gateway":
                continue
            binding_id, role = binding["id"], connection.get("enrollment_role")
            runtime = session.get(BindingState, binding_id)
            enrollments = list(session.scalars(select(HelperEnrollment).where(
                HelperEnrollment.binding_id == binding_id).order_by(HelperEnrollment.id)))
            sessions = list(session.scalars(select(HelperSession).where(
                HelperSession.binding_id == binding_id).order_by(HelperSession.last_seen.desc())))
            current = [row for row in trusted if row.profile in {bundle.get("default_profile", "local"), "all"}]
            host_class_evidence = []
            for row in current:
                evidence = row.payload.get("evidence", {})
                classes = evidence.get("host_classes", []) if isinstance(evidence, dict) else []
                if isinstance(classes, list):
                    host_class_evidence.extend(item for item in classes if isinstance(item, dict))
            destination = next((item for item in bundle.get("destinations", [])
                                if item.get("id") == binding.get("destination")), {})
            connector_role = next((item for item in bundle.get("connector_roles", [])
                                   if item.get("id") == role), {})
            pins = binding.get("identity", {})
            expected_host_class = {
                "enrollment_role": role,
                "helper_image_digest": connector_role.get("helper_image_digest"),
                "engine_image_digest": connector_role.get("engine_image_digest"),
                "weights_manifest_digest": pins.get("weights_manifest_digest"),
                "destination": binding.get("destination"),
                "trust_tier": destination.get("trust_tier"),
                "retention": destination.get("retention"),
                "cancel_bound_seconds": binding.get("cancel_bound_seconds"),
            }

            enrollment_views = [{
                "enrollment_id": row.id,
                "binding_match": row.binding_id == binding_id,
                "installation_match": row.installation_id == platform.settings.installation_id,
                "role_match": row.role == role,
                "valid": not row.revoked and row.expires_at > now,
                "revoked": bool(row.revoked),
                "expires_at": row.expires_at,
            } for row in enrollments]
            session_views = []
            for helper in sessions:
                enrollment = session.get(HelperEnrollment, helper.enrollment_id)
                age = max(0.0, now - helper.last_seen)
                bound = (enrollment is not None and enrollment.binding_id == binding_id
                         and enrollment.installation_id == platform.settings.installation_id
                         and enrollment.role == role and not enrollment.revoked and enrollment.expires_at > now)
                active = bool(helper.active and helper.generation == generation and age <= FRESHNESS_SECONDS)
                assertion = helper.identity if isinstance(helper.identity, dict) else {}
                identity_match = bool(assertion and assertion.get("binding_id") == binding_id
                                      and assertion.get("installation_id") == platform.settings.installation_id
                                      and assertion.get("enrollment_id") == helper.enrollment_id
                                      and assertion.get("enrollment_role") == role
                                      and assertion.get("session_id") == helper.id
                                      and assertion.get("helper_image_digest") == connector_role.get("helper_image_digest")
                                      and assertion.get("engine_image_digest") == connector_role.get("engine_image_digest")
                                      and assertion.get("weights_manifest_digest") == pins.get("weights_manifest_digest"))
                session_views.append({
                    "session_id": helper.id, "enrollment_id": helper.enrollment_id,
                    "active": active, "fresh": age <= FRESHNESS_SECONDS,
                    "observation_age_seconds": age, "generation_match": helper.generation == generation,
                    "enrollment_bound": bound, "identity_bound": identity_match,
                    "host_instance_id": assertion.get("host_instance_id"),
                    "helper_image_digest": assertion.get("helper_image_digest"),
                    "engine_image_digest": assertion.get("engine_image_digest"),
                    "weights_manifest_digest": assertion.get("weights_manifest_digest"),
                })
            active_sessions = [item for item in session_views if item["active"] and item["enrollment_bound"]
                               and item["identity_bound"]]
            matching_class = any(all(item.get(key) == value for key, value in expected_host_class.items())
                                 for item in host_class_evidence)
            blockers = []
            if not binding.get("enabled", True): blockers.append("binding_disabled")
            if runtime is not None and runtime.inhibited: blockers.append("binding_inhibited")
            if not enrollment_views: blockers.append("enrollment_missing")
            if not any(item["valid"] and item["role_match"] and item["installation_match"] for item in enrollment_views):
                blockers.append("eligible_enrollment_missing")
            if not active_sessions: blockers.append("fresh_bound_session_missing")
            if not current: blockers.append("current_trusted_qualification_missing")
            if not matching_class: blockers.append("host_class_qualification_evidence_missing")
            projections.append({
                "binding_id": binding_id, "generation": generation, "role": role,
                "enabled": bool(binding.get("enabled", True)),
                "inhibited": bool(runtime.inhibited) if runtime else False,
                "enrollments": enrollment_views, "sessions": session_views,
                "qualification_evidence": {
                    "trusted_current_records": len(current),
                    "expected_host_class": expected_host_class,
                    "host_class_match": matching_class,
                    "evidence_records_are_authority": False,
                },
                "blockers": blockers,
                "readiness": "blocked" if blockers else "provisional_evidence_complete",
            })
        return {"generation": generation, "observed_at": now,
                "projection": "offline_source_local_provisional",
                "dispatch_authority": False, "items": projections}


def install_routes(app, platform, operator):
    @app.get("/api/helpers/readiness")
    def readiness(auth=Depends(operator)):
        return helper_readiness(platform)

    @app.post("/api/helpers/enrollments/{enrollment_id}", status_code=201)
    def provision(enrollment_id: EnrollmentId, body: EnrollmentProvision, auth=Depends(operator)):
        try:
            provision_enrollment(platform, enrollment_id=enrollment_id, binding_id=body.binding_id,
                                 installation_id=body.installation_id, role=body.role,
                                 certificate=body.certificate_pem.get_secret_value().encode("ascii"), actor=auth[0])
        except (ValueError, UnicodeError):
            raise PlatformError("invalid_enrollment_certificate", "A valid helper client certificate is required", 422) from None
        return {"enrollment_id": enrollment_id, "provisioned": True}

    @app.post("/api/helpers/enrollments/{enrollment_id}/rotate")
    def rotate(enrollment_id: EnrollmentId, body: CertificateRotation, auth=Depends(operator)):
        try:
            result = rotate_enrollment(platform, enrollment_id, body.certificate_pem.get_secret_value().encode("ascii"),
                                       actor=auth[0])
        except (ValueError, UnicodeError):
            raise PlatformError("invalid_enrollment_certificate", "A valid helper client certificate is required", 422) from None
        return result

    @app.post("/api/helpers/enrollments/{enrollment_id}/revoke")
    def revoke(enrollment_id: EnrollmentId, auth=Depends(operator)):
        return revoke_enrollment(platform, enrollment_id, actor=auth[0])

    @app.post("/api/helpers/reconcile")
    def reconcile(auth=Depends(operator)):
        return {"items": reconcile_helper_demand(platform, actor=auth[0]),
                "certificate_issuance": "external_ca"}
