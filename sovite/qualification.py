from __future__ import annotations

import base64
import hashlib
import json
import math
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
from .hosting import compile_bundle, envelope_binding, load_envelope, load_json
from .policy import load_bundle


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
    if settings.database_schema is not None:
        binding["database"]["schema"] = settings.database_schema
    envelope = load_envelope(settings.hosting_envelope_path)
    if envelope is not None:
        binding["hosting"] = envelope_binding(envelope)
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


ROLE_CHECKS = {
    "platform": {"runtime-identity", "network-isolation", "recovery"},
    "tenant": {"binding-identity", "retention", "artifact-placement", "receipt-boundary", "task-competence", "recovery"},
}


def key_digest(public_key: str) -> str:
    key = serialization.load_pem_public_key(public_key.encode())
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("Only Ed25519 qualification keys are supported")
    return "sha256:" + hashlib.sha256(key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).hexdigest()


def validate_issuers(settings: Settings):
    if not isinstance(settings.qualification_issuers, list) or len(settings.qualification_issuers) > 100:
        raise ValueError("Qualification issuers must be a bounded array")
    identities = set()
    for issuer in settings.qualification_issuers:
        if not isinstance(issuer, dict) or set(issuer) - {"path", "role", "workspace", "not_before", "expires_at"} or not {"path", "role", "expires_at"} <= set(issuer):
            raise ValueError("Qualification issuers require path, role and expires_at")
        if issuer["role"] not in {"platform", "tenant", "operator"} or not isinstance(issuer["path"], str) or not 1 <= len(issuer["path"]) <= 4096:
            raise ValueError("Invalid qualification issuer role or path")
        if issuer["role"] == "tenant":
            if not isinstance(issuer.get("workspace"), str) or not 1 <= len(issuer["workspace"]) <= 96:
                raise ValueError("Tenant qualification keys require a workspace")
        elif "workspace" in issuer:
            raise ValueError("Only tenant keys carry a workspace scope")
        if any(type(issuer.get(field, 0)) not in {int, float} or not math.isfinite(issuer.get(field, 0)) for field in ("not_before", "expires_at")) or issuer["expires_at"] <= issuer.get("not_before", 0):
            raise ValueError("Invalid qualification key validity interval")
        identity = key_digest(Path(issuer["path"]).read_text())
        if identity in identities:
            raise ValueError("Qualification key must have exactly one issuer entry; duplicate keys cannot serve multiple roles")
        identities.add(identity)
    revoked = settings.qualification_revoked_keys
    if not isinstance(revoked, list) or len(revoked) > 1000 or any(not isinstance(value, str) or len(value) != 71 or not value.startswith("sha256:") or any(c not in "0123456789abcdef" for c in value[7:]) for value in revoked):
        raise ValueError("Revoked keys must be SHA-256 public key digests")


def trusted_key(settings: Settings, public_key: str, role: str = "operator", workspace: str | None = None, now: float | None = None, *, created_at: float | None = None) -> bool:
    now = time.time() if now is None else now
    try:
        validate_issuers(settings)
        candidate = key_digest(public_key)
        if candidate in settings.qualification_revoked_keys:
            return False
        entries = settings.qualification_issuers
        matching = None
        for entry in entries:
            identity = key_digest(Path(entry["path"]).read_text())
            if identity == candidate:
                matching = entry
        if matching is not None:
            return (matching["role"] == role and (role != "tenant" or matching["workspace"] == workspace)
                    and matching.get("not_before", 0) <= now < matching["expires_at"]
                    and (created_at is None or matching.get("not_before", 0) <= created_at < matching["expires_at"]))
        if settings.hosting_envelope_path or role != "operator":
            return False
        for path in settings.qualification_public_keys:
            try:
                if key_digest(Path(path).read_text()) == candidate:
                    return True
            except (OSError, ValueError):
                continue
    except (OSError, ValueError, TypeError):
        pass
    return False


def _hosted_payload(payload: dict, settings: Settings) -> bool:
    envelope = load_envelope(settings.hosting_envelope_path)
    if envelope is None:
        return True
    role = payload.get("role")
    evidence = payload.get("evidence", {})
    if not isinstance(evidence, dict) or not isinstance(evidence.get("checks"), list):
        return False
    checks = evidence["checks"]
    if any(not isinstance(check, dict) or not isinstance(check.get("name"), str) or check.get("status") != "passed" for check in checks):
        return False
    names = [check["name"] for check in checks]
    return (payload.get("schema") == "sovite.qualification/v2" and role in ROLE_CHECKS
            and payload.get("hosting") == envelope_binding(envelope)
            and payload.get("workspace") == envelope["workspace"]
            and evidence.get("evidence_kind") == "deployment"
            and evidence.get("generation") == payload.get("generation")
            and evidence.get("profile") in {payload.get("profile"), "all"}
            and evidence.get("runtime_binding") == payload.get("runtime_binding")
            and isinstance(evidence.get("scope"), str) and bool(evidence["scope"].strip())
            and len(names) == len(set(names)) and ROLE_CHECKS[role] <= set(names))


def verify_qualification(row: Qualification, settings: Settings, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    try:
        if not row.passed or not all(type(value) in {int, float} and math.isfinite(value) for value in (row.expires_at, row.created_at)) or row.expires_at <= now or row.created_at > now + 60 or row.expires_at <= row.created_at:
            return False
        if not isinstance(row.payload, dict):
            return False
        if settings.hosting_envelope_path and row.expires_at - row.created_at > 90 * 86400:
            return False
        role = row.payload.get("role", "operator")
        if not trusted_key(settings, row.public_key, role, row.payload.get("workspace"), now, created_at=row.created_at):
            return False
        if row.payload.get("runtime_binding") != runtime_settings_fingerprint(settings) or not _hosted_payload(row.payload, settings):
            return False
        if any(row.payload.get(field) != getattr(row, field) for field in ("id", "generation", "profile", "created_at", "expires_at", "passed", "issuer")):
            return False
        key = serialization.load_pem_public_key(row.public_key.encode())
        key.verify(base64.b64decode(row.signature, validate=True), canonical(row.payload))
        return True
    except (OSError, ValueError, InvalidSignature, TypeError, KeyError, AttributeError):
        return False


def is_qualified(session, settings: Settings, generation: str, profile: str | None = None) -> bool:
    if not settings.require_qualification and not settings.hosting_envelope_path:
        revision = session.get(BundleRevision, generation)
        return not (revision and revision.bundle.get("mode") == "enforced")
    return current_qualification(session, settings, generation, profile) is not None


def current_qualification(session, settings: Settings, generation: str, profile: str | None = None) -> Qualification | None:
    """Latest trusted record; hosted pairs report their earlier expiry."""
    revision = session.get(BundleRevision, generation)
    enforced = bool(revision and revision.bundle.get("mode") == "enforced")
    if settings.hosting_envelope_path:
        try:
            if revision is None or compile_bundle(revision.bundle, load_envelope(settings.hosting_envelope_path))[1] != generation:
                return None
        except (OSError, ValueError):
            return None
        profile = profile or revision.bundle.get("default_profile", "local")
    roles = {}
    rows = session.scalars(select(Qualification).where(Qualification.generation == generation, Qualification.passed == True, Qualification.expires_at > time.time()).order_by(Qualification.expires_at.desc())).all()
    for row in rows:
        if (profile is not None and row.profile not in {profile, "all"}) or not verify_qualification(row, settings):
            continue
        if settings.hosting_envelope_path:
            roles.setdefault(row.payload["role"], row)
            if set(roles) == {"platform", "tenant"}:
                return min(roles.values(), key=lambda item: item.expires_at)
            continue
        if enforced:
            evidence = row.payload.get("evidence", {})
            checks = {check.get("name"): check.get("status") for check in evidence.get("checks", [])}
            required = {"runtime-identity", "network-isolation", "receipt-boundary", "recovery", "task-competence"}
            if evidence.get("evidence_kind") != "deployment" or not all(checks.get(name) == "passed" for name in required):
                continue
        return row
    return None


def issue_qualification(db: Database | None, settings: Settings, generation: str, profile: str, evidence: dict, valid_days: int = 30, issuer: str = "operator", role: str = "operator") -> Qualification:
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
    if db is not None:
        with db.sessions() as session:
            revision = session.get(BundleRevision, generation)
            bundle = revision.bundle if revision else None
    else:
        bundle = load_bundle(settings.bundle_path)[0]
    if bundle and bundle.get("mode") == "enforced" and evidence.get("runtime_binding") != binding:
        raise ValueError("Enforced qualification evidence must bind the runtime placement settings")
    if not 1 <= valid_days <= 90:
        raise ValueError("Qualification validity must be 1..90 days")
    private = serialization.load_pem_private_key(Path(settings.qualification_private_key).read_bytes(), password=None)
    if not isinstance(private, Ed25519PrivateKey):
        raise ValueError("Only Ed25519 qualification keys are supported")
    public = private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    envelope = load_envelope(settings.hosting_envelope_path)
    if envelope is None and role != "operator":
        raise ValueError("Non-hosted signing uses the operator role")
    if not trusted_key(settings, public, role, envelope["workspace"] if envelope else None):
        raise ValueError("Signing key is not configured as a trusted qualification issuer")
    now = time.time()
    passed = all(check["status"] == "passed" for check in checks)
    payload = {"schema": "sovite.qualification/v1", "id": uuid.uuid4().hex, "generation": generation, "runtime_binding": binding, "profile": profile, "created_at": now, "expires_at": now + valid_days * 86400, "passed": passed, "issuer": issuer, "evidence": evidence}
    if envelope is not None:
        payload.update(schema="sovite.qualification/v2", role=role, workspace=envelope["workspace"], hosting=envelope_binding(envelope))
        if not _hosted_payload(payload, settings):
            raise ValueError("Hosted qualification requires complete passing deployment evidence for its role")
        if db is None:
            expected = compile_bundle(load_bundle(settings.bundle_path)[0], envelope)[1]
        else:
            with db.sessions() as session:
                revision = session.get(BundleRevision, generation)
                expected = compile_bundle(revision.bundle, envelope)[1] if revision else None
        if expected != generation:
            raise ValueError("Qualification generation does not match the current hosting envelope")
    row = Qualification(id=payload["id"], generation=generation, profile=profile, created_at=now, expires_at=payload["expires_at"], passed=passed, issuer=issuer, payload=payload, signature=base64.b64encode(private.sign(canonical(payload))).decode(), public_key=public)
    if db is not None:
        with db.sessions.begin() as session:
            session.add(row)
            db.audit(session, issuer, "qualification.issued", row.id, {"generation": generation, "profile": profile, "passed": passed, "scope": evidence["scope"][:500]})
    return row


def export_qualification(db: Database, qualification_id: str, path: str):
    with db.sessions() as session:
        row = session.get(Qualification, qualification_id)
        if row is None:
            raise ValueError("Qualification not found")
        write_qualification(row, path)


def write_qualification(row: Qualification, path: str):
    document = {"payload": row.payload, "signature": row.signature, "public_key": row.public_key, "public_key_id": key_digest(row.public_key)}
    Path(path).write_text(json.dumps(document, indent=2, allow_nan=False) + "\n")


def import_qualification(db: Database, settings: Settings, generation: str, path: str) -> Qualification:
    document = load_json(path, 2_000_000)
    if not isinstance(document, dict) or set(document) != {"payload", "signature", "public_key", "public_key_id"}:
        raise ValueError("Invalid qualification document")
    payload = document["payload"]
    fields = {"schema", "id", "generation", "runtime_binding", "profile", "created_at", "expires_at", "passed", "issuer", "evidence"}
    if settings.hosting_envelope_path:
        fields |= {"role", "workspace", "hosting"}
    if not isinstance(payload, dict) or set(payload) != fields:
        raise ValueError("Invalid qualification payload")
    if payload["schema"] != ("sovite.qualification/v2" if settings.hosting_envelope_path else "sovite.qualification/v1"):
        raise ValueError("Unsupported qualification schema")
    if any(not isinstance(payload.get(field), str) or not 1 <= len(payload[field]) <= limit for field, limit in (("id", 64), ("generation", 80), ("runtime_binding", 80), ("profile", 160), ("issuer", 160))):
        raise ValueError("Invalid qualification identity")
    if payload["generation"] != generation or type(payload["passed"]) is not bool or not isinstance(payload["evidence"], dict):
        raise ValueError("Qualification does not match the requested generation")
    checks = payload["evidence"].get("checks")
    if not isinstance(checks, list) or not checks or any(not isinstance(check, dict) or not isinstance(check.get("name"), str) or not check["name"] or check.get("status") not in {"passed", "failed", "not_run"} for check in checks):
        raise ValueError("Invalid qualification evidence")
    if not all(isinstance(document[field], str) for field in ("signature", "public_key", "public_key_id")) or document["public_key_id"] != key_digest(document["public_key"]):
        raise ValueError("Qualification public key id differs")
    row = Qualification(**{field: payload[field] for field in ("id", "generation", "profile", "created_at", "expires_at", "passed", "issuer")}, payload=payload, signature=document["signature"], public_key=document["public_key"])
    if not verify_qualification(row, settings):
        raise ValueError("Qualification signature, scope, validity or evidence is not trusted")
    with db.sessions.begin() as session:
        revision = session.get(BundleRevision, generation)
        if revision is None or compile_bundle(revision.bundle, load_envelope(settings.hosting_envelope_path))[1] != generation:
            raise ValueError("Qualification generation is not compiled for this installation")
        if row.profile not in {revision.bundle.get("default_profile", "local"), "all"}:
            raise ValueError("Qualification profile differs from this installation")
        if session.get(Qualification, row.id) is not None:
            raise ValueError("Qualification already imported or issued")
        session.add(row)
        db.audit(session, row.issuer, "qualification.imported", row.id, {"generation": generation, "public_key_id": document["public_key_id"]})
    return row
