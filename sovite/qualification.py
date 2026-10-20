from __future__ import annotations

import base64
import os
import time
import uuid
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from sqlalchemy import select
from sqlalchemy.engine import make_url

from .db import BundleRevision, Database, Qualification, canonical, digest
from .settings import Settings


def runtime_settings_fingerprint(settings: Settings) -> str:
    """Bind nonsecret placement settings; rotating secrets does not revoke recipes."""
    database = make_url(settings.database_url)
    db_name = database.database
    if database.drivername.startswith("sqlite") and db_name and db_name != ":memory:":
        db_name = str(Path(db_name).resolve())
    binding = {
        "database": {"driver": database.drivername, "host": database.host, "port": database.port, "database": db_name, "username": database.username},
        "artifacts": {"backend": settings.artifact_backend, "endpoint": settings.s3_endpoint, "bucket": settings.s3_bucket, "region": settings.s3_region, "path": str(Path(settings.artifact_path).resolve()) if settings.artifact_backend == "filesystem" else None},
        "public_origin": settings.public_origin, "secure_cookies": settings.secure_cookies,
        "require_qualification": settings.require_qualification, "deployment_binding": settings.deployment_binding,
    }
    return "sha256:" + digest(binding)


def initialize_keys(settings: Settings) -> tuple[str, str]:
    private_path = Path(settings.qualification_private_key)
    public_path = Path(settings.qualification_public_keys[0])
    if private_path.exists() or public_path.exists():
        raise ValueError("Key files already exist; refusing to replace signing identity")
    private_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    public_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    private = Ed25519PrivateKey.generate()
    raw = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
    public_path.write_bytes(private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    return str(private_path), str(public_path)


def trusted_key(settings: Settings, public_key: str) -> bool:
    try:
        candidate = serialization.load_pem_public_key(public_key.encode())
        if not isinstance(candidate, Ed25519PublicKey):
            return False
        raw = candidate.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        for path in settings.qualification_public_keys:
            try:
                value = serialization.load_pem_public_key(Path(path).read_bytes())
                if isinstance(value, Ed25519PublicKey) and value.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw) == raw:
                    return True
            except (OSError, ValueError):
                continue
    except (ValueError, TypeError):
        pass
    return False


def verify_qualification(row: Qualification, settings: Settings, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    if not row.passed or row.expires_at <= now or row.created_at > now + 60:
        return False
    if not trusted_key(settings, row.public_key):
        return False
    if row.payload.get("runtime_binding") != runtime_settings_fingerprint(settings):
        return False
    if any(row.payload.get(field) != getattr(row, field) for field in ("id", "generation", "profile", "created_at", "expires_at", "passed", "issuer")):
        return False
    try:
        key = serialization.load_pem_public_key(row.public_key.encode())
        key.verify(base64.b64decode(row.signature, validate=True), canonical(row.payload))
        return True
    except (ValueError, InvalidSignature, TypeError):
        return False


def is_qualified(session, settings: Settings, generation: str, profile: str | None = None) -> bool:
    if not settings.require_qualification:
        revision = session.get(BundleRevision, generation)
        return not (revision and revision.bundle.get("mode") == "enforced")
    return current_qualification(session, settings, generation, profile) is not None


def current_qualification(session, settings: Settings, generation: str, profile: str | None = None) -> Qualification | None:
    """The trusted qualification for this generation that expires last, if any."""
    revision = session.get(BundleRevision, generation)
    enforced = bool(revision and revision.bundle.get("mode") == "enforced")
    rows = session.scalars(select(Qualification).where(Qualification.generation == generation, Qualification.passed == True, Qualification.expires_at > time.time()).order_by(Qualification.expires_at.desc())).all()
    for row in rows:
        if (profile is not None and row.profile not in {profile, "all"}) or not verify_qualification(row, settings):
            continue
        if enforced:
            evidence = row.payload.get("evidence", {})
            checks = {check.get("name"): check.get("status") for check in evidence.get("checks", [])}
            required = {"runtime-identity", "network-isolation", "receipt-boundary", "recovery", "task-competence"}
            if evidence.get("evidence_kind") != "deployment" or not all(checks.get(name) == "passed" for name in required):
                continue
        return row
    return None


def issue_qualification(db: Database, settings: Settings, generation: str, profile: str, evidence: dict, valid_days: int = 30, issuer: str = "operator") -> Qualification:
    checks = evidence.get("checks")
    if not isinstance(checks, list) or not checks or any(not isinstance(c, dict) or not c.get("name") or c.get("status") not in {"passed", "failed", "not_run"} for c in checks):
        raise ValueError("Evidence needs a nonempty checks array with name and status=passed|failed|not_run")
    if evidence.get("generation") != generation:
        raise ValueError("Evidence generation must exactly match the active configuration generation")
    if evidence.get("profile") not in {profile, "all"}:
        raise ValueError("Evidence profile must match requested qualification profile")
    if not evidence.get("scope"):
        raise ValueError("Evidence must state its scope and limitations")
    binding = runtime_settings_fingerprint(settings)
    if evidence.get("runtime_binding") not in {None, binding}:
        raise ValueError("Evidence runtime binding differs from this deployment")
    with db.sessions() as session:
        revision = session.get(BundleRevision, generation)
        if revision and revision.bundle.get("mode") == "enforced" and evidence.get("runtime_binding") != binding:
            raise ValueError("Enforced qualification evidence must bind the runtime placement settings")
    if not 1 <= valid_days <= 90:
        raise ValueError("Qualification validity must be 1..90 days")
    private = serialization.load_pem_private_key(Path(settings.qualification_private_key).read_bytes(), password=None)
    if not isinstance(private, Ed25519PrivateKey):
        raise ValueError("Only Ed25519 qualification keys are supported")
    public = private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    if not trusted_key(settings, public):
        raise ValueError("Signing key is not configured as a trusted qualification issuer")
    now = time.time()
    passed = all(check["status"] == "passed" for check in checks)
    payload = {"schema": "sovite.qualification/v1", "id": uuid.uuid4().hex, "generation": generation, "runtime_binding": binding, "profile": profile, "created_at": now, "expires_at": now + valid_days * 86400, "passed": passed, "issuer": issuer, "evidence": evidence}
    row = Qualification(id=payload["id"], generation=generation, profile=profile, created_at=now, expires_at=payload["expires_at"], passed=passed, issuer=issuer, payload=payload, signature=base64.b64encode(private.sign(canonical(payload))).decode(), public_key=public)
    with db.sessions.begin() as session:
        session.add(row)
        db.audit(session, issuer, "qualification.issued", row.id, {"generation": generation, "profile": profile, "passed": passed, "scope": evidence["scope"][:500]})
    return row
