import asyncio
from types import SimpleNamespace
import time

import pytest

from model_support import authority_env
from sovite.authority import provision_enrollment
from sovite.db import HelperSession, Qualification
from sovite.helper_admin_api import (
    HOST_CLASS_SCHEMA,
    _bounded_timestamp,
    _expected_host_class,
    _validate_host_class,
)
from sovite.service import PlatformError
from test_connector_helper import certificates
from test_migrations import database  # noqa: F401
from test_policy_v1alpha2 import bundle as v2_bundle  # noqa: F401


def test_readiness_is_operator_only_bound_to_current_owner_evidence_and_non_authoritative(
    authority_env, monkeypatch, tmp_path
):
    env = authority_env
    env.settings.installation_id = "readiness-installation"
    monkeypatch.setattr("sovite.helper_admin_api.verify_qualification", lambda *_args, **_kwargs: True)

    cert_dir = tmp_path / "readiness-cert"
    cert_dir.mkdir()
    certificates(cert_dir)
    provision_enrollment(
        env.platform,
        enrollment_id="readiness-enrollment",
        binding_id=env.model["id"],
        installation_id=env.settings.installation_id,
        role=env.role["id"],
        certificate=(cert_dir / "helper.pem").read_bytes(),
    )

    with env.platform.db.sessions() as session:
        bundle, generation = env.platform.active(session)
    expected, error = _expected_host_class(env.model, bundle)
    assert error is None
    now = time.time()
    record = {
        "schema": HOST_CLASS_SCHEMA,
        "id": "readiness-host-class",
        **expected,
        "observed_at": now - 30,
        "expires_at": now + 3600,
        "evidence_refs": ["inventory://host-class/1"],
    }
    with env.platform.db.sessions.begin() as session:
        session.add(Qualification(
            id="readiness-qualification",
            generation=generation,
            profile=bundle.get("default_profile", "local"),
            created_at=now - 5,
            expires_at=now + 7200,
            passed=True,
            issuer="test-only",
            payload={"evidence": {"host_classes": [record]}},
            signature="test-only",
            public_key="test-only",
        ))
        session.add(HelperSession(
            id="readiness-session",
            binding_id=env.model["id"],
            enrollment_id="readiness-enrollment",
            gateway_id="readiness-gateway",
            generation=generation,
            challenge="c" * 64,
            active=True,
            identity={
                "binding_id": env.model["id"],
                "installation_id": env.settings.installation_id,
                "enrollment_id": "readiness-enrollment",
                "enrollment_role": env.role["id"],
                "session_id": "readiness-session",
                "host_instance_id": "host-instance-1",
                "helper_image_digest": expected["helper_image_digest"],
                "engine_image_digest": expected["engine_image_digest"],
                "weights_manifest_digest": expected["weights_manifest_digest"],
            },
            last_seen=now,
        ))

    routes = {route.path: route for route in env.app.routes if hasattr(route, "dependant")}
    route = routes["/api/helpers/readiness"]
    operator_dependency = route.dependant.dependencies[0]
    assert operator_dependency.call.__name__ == "operator"
    assert operator_dependency.dependencies[0].call.__name__ == "identity"
    with pytest.raises(PlatformError) as caller_denied:
        asyncio.run(operator_dependency.call(("operations", "caller")))
    assert caller_denied.value.status == 403

    result = route.endpoint(auth=("operator", "operator"))
    assert result["projection"] == "offline_source_local_provisional"
    assert result["dispatch_authority"] is False
    item = result["items"][0]
    assert item["qualification_evidence"]["host_class_match"] is True
    assert item["qualification_evidence"]["evidence_records_are_authority"] is False
    assert item["readiness"] == "provisional_evidence_complete"
    assert item["sessions"][0]["host_instance_id"] == "host-instance-1"


def test_host_class_timestamp_bounds_reject_huge_integers_and_out_of_range_values():
    now = time.time()
    expected = {
        "enrollment_role": "role",
        "helper_image_digest": "sha256:" + "1" * 64,
        "engine_image_digest": "sha256:" + "2" * 64,
        "engine_version": "engine-v1",
        "weights_manifest_digest": "sha256:" + "3" * 64,
        "destination": "inference",
        "trust_tier": "operator-owned",
        "retention": {"prompt_logging": False},
        "cancel_bound_seconds": 6,
    }
    qualification = SimpleNamespace(created_at=now - 20, expires_at=now + 3600)
    record = {
        "schema": HOST_CLASS_SCHEMA,
        "id": "bounded-time-record",
        **expected,
        "observed_at": now - 30,
        "expires_at": now + 120,
        "evidence_refs": ["inventory://bounded"],
    }
    assert _validate_host_class(record, expected, qualification, now) is None
    assert _bounded_timestamp(10**10000) is None
    record["observed_at"] = 10**10000
    assert _validate_host_class(record, expected, qualification, now) == "host_class_record_time_invalid_or_stale"
