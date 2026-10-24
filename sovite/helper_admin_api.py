"""Operator-only lifecycle controls for enrolled remote inference helpers."""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .authority import provision_enrollment, reconcile_helper_demand, revoke_enrollment, rotate_enrollment
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


def install_routes(app, platform, operator):
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
