import asyncio

from sqlalchemy import select
import pytest
from pydantic import ValidationError

from model_support import authority_env, v2_bundle
from sovite.authority import _enrollment_certificate, provision_enrollment
from sovite.db import AuditEvent, HelperEnrollment
from sovite.helper_admin_api import CertificateRotation
from test_connector_helper import certificates
from test_migrations import database
from sovite.service import PlatformError


def test_operator_helper_lifecycle_routes_and_transactional_audit(authority_env, tmp_path):
    env = authority_env
    old_dir = tmp_path / "old"
    old_dir.mkdir()
    certificates(old_dir)
    provision_enrollment(env.platform, enrollment_id="admin-enrollment", binding_id=env.model["id"],
                         installation_id="admin-installation", role=env.role["id"],
                         certificate=(old_dir / "helper.pem").read_bytes())
    wrong_dir = tmp_path / "wrong-role"
    wrong_dir.mkdir()
    certificates(wrong_dir)
    wrong_certificate = (wrong_dir / "helper.pem").read_bytes()
    expires_at, certificate_digest, public_key = _enrollment_certificate(wrong_certificate)
    with env.platform.db.sessions.begin() as session:
        session.add(HelperEnrollment(id="wrong-role-enrollment", binding_id=env.model["id"],
                                     installation_id="admin-installation", role="retired-role",
                                     expires_at=expires_at, certificate_digest=certificate_digest,
                                     public_key=public_key, revoked=False))
    new_dir = tmp_path / "new"
    new_dir.mkdir()
    certificates(new_dir)

    routes = {route.path: route for route in env.app.routes if hasattr(route, "dependant")}
    rotate_route = routes["/api/helpers/enrollments/{enrollment_id}/rotate"]
    revoke_route = routes["/api/helpers/enrollments/{enrollment_id}/revoke"]
    reconcile_route = routes["/api/helpers/reconcile"]
    for route in (rotate_route, revoke_route, reconcile_route):
        assert route.dependant.dependencies
        operator_dependency = route.dependant.dependencies[0]
        assert operator_dependency.call.__name__ == "operator"
        assert operator_dependency.dependencies[0].call.__name__ == "identity"
    operator_gate = rotate_route.dependant.dependencies[0].call
    with pytest.raises(PlatformError) as caller_denied:
        asyncio.run(operator_gate(("operations", "caller")))
    assert caller_denied.value.status == 403

    with pytest.raises(ValidationError):
        CertificateRotation(certificate_pem="x" * 16_385)
    with pytest.raises(ValidationError):
        CertificateRotation(certificate_pem="x", extra="not-accepted")

    rotated = rotate_route.endpoint("admin-enrollment", CertificateRotation(
        certificate_pem=(new_dir / "helper.pem").read_text()), auth=("operator", "operator"))
    assert rotated["enrollment_id"] == "admin-enrollment"
    assert "certificate_pem" not in rotated
    assert "certificate" not in str(rotated).lower()

    reconciled = reconcile_route.endpoint(auth=("operator", "operator"))
    assert reconciled["certificate_issuance"] == "external_ca"
    assert reconciled["items"][0]["eligible_enrollments"] == 1

    revoked = revoke_route.endpoint("admin-enrollment", auth=("operator", "operator"))
    assert revoked == {"enrollment_id": "admin-enrollment", "revoked": True}

    with pytest.raises(PlatformError) as failure:
        rotate_route.endpoint("admin-enrollment", CertificateRotation(certificate_pem="CERTIFICATE-SECRET-SENTINEL"),
                             auth=("operator", "operator"))
    assert failure.value.code == "invalid_enrollment_certificate"
    assert "CERTIFICATE-SECRET-SENTINEL" not in str(failure.value)

    with env.platform.db.sessions() as session:
        enrollment = session.get(HelperEnrollment, "admin-enrollment")
        assert enrollment.revoked
        events = list(session.scalars(select(AuditEvent).order_by(AuditEvent.sequence)))
        actions = [event.action for event in events]
        assert actions[-3:] == ["helper.enrollment_rotated", "helper.demand_reconciled", "helper.enrollment_revoked"]
        assert all(event.actor == "operator" for event in events[-3:])
    assert env.platform.db.verify_audit()["valid"]
