"""CURRENT_AUTHORITY: the transaction granting a permit is the linearization point."""
from __future__ import annotations

import hashlib
import math
import secrets
import time
import uuid
from contextlib import contextmanager

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.x509.oid import ExtendedKeyUsageOID
from sqlalchemy import delete, select

from .connector.canonical import ProtocolError
from .connector.identity import ExpectedIdentity, Verification, verify_binding, verify_connection, verify_preparation
from .connector.protocol import BODY_LIMIT, FRESHNESS_SECONDS, PERMIT_SECONDS
from .db import BindingState, ExecutionAuthority, HelperEnrollment, HelperSession, Job, MetadataRow, RequestLedger, WorkerExecutionLease
from .service import PlatformError

CAPABILITY_SECONDS = 60
RETENTION_BATCH = 500


def fail(code, status=403):
    raise PlatformError(code, "Inference authority denied", status)


def checksum(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def execution_context(job, workload):
    return {"generation": job.generation, "grant_id": job.grant_id, "caller": job.caller,
            "submission_role": job.resolved.get("submission_role"), "binding_id": job.resolved["model"]["id"],
            "lease_owner": job.lease_owner, "workload": workload}


def check_execution_binding(platform, session, job):
    """Revalidate a gateway binding from current owner state, without helper I/O."""
    model = job.resolved.get("model", {})
    if model.get("connection", {}).get("kind") != "model-gateway":
        return None
    binding_id = model.get("id")
    try:
        bundle, generation, binding, _ = Authority(platform)._binding(session, binding_id)
    except (KeyError, TypeError, StopIteration):
        fail("binding_context_denied", 503)
    if generation != job.generation or binding != model:
        fail("revision_superseded")
    return binding


def issue_execution(platform, session, job, *, fence: str, workload: str, now: float, deadline: float):
    if job.resolved.get("model", {}).get("connection", {}).get("kind") != "model-gateway":
        return None
    if not isinstance(fence, str) or len(fence) != 32 or any(c not in "0123456789abcdef" for c in fence):
        raise ValueError("owner fence is invalid")
    if not deadline > now:
        raise ValueError("execution deadline is invalid")
    capability = secrets.token_urlsafe(32)
    row = session.get(ExecutionAuthority, job.id)
    if row is None:
        row = ExecutionAuthority(job_id=job.id)
        session.add(row)
    row.fence, row.capability_digest = fence, checksum(capability)
    row.context = execution_context(job, workload)
    row.expires_at, row.deadline = min(deadline, now + CAPABILITY_SECONDS), deadline
    # Transient claim result only; job_dict and SQL never contain the bearer secret.
    job.execution_capability, job.execution_fence, job.execution_deadline = capability, fence, deadline
    return {"capability": capability, "context": row.context}


def revoke_execution(session, job_id):
    row = session.get(ExecutionAuthority, job_id)
    if row is not None:
        row.capability_digest, row.expires_at = "", 0


def refresh_execution(platform, session, job, fence, now):
    row = session.get(ExecutionAuthority, job.id)
    lease = session.get(WorkerExecutionLease, job.id)
    if (row is None or not isinstance(fence, str) or row.fence != fence
            or lease is None or lease.phase != "started" or lease.fence != fence
            or lease.execution_deadline is None or lease.execution_deadline != row.deadline
            or not isinstance(row.capability_digest, str) or not row.capability_digest
            or isinstance(row.expires_at, bool) or not isinstance(row.expires_at, (int, float))
            or isinstance(row.deadline, bool) or not isinstance(row.deadline, (int, float))
            or not math.isfinite(row.expires_at) or not math.isfinite(row.deadline)
            or not row.expires_at > now or not row.deadline > now
            or not isinstance(row.context, dict)
            or row.context != execution_context(job, lease.workload_id)):
        return False
    row.expires_at = min(row.deadline, now + CAPABILITY_SECONDS)
    return row.expires_at > now


def execution_authority_current(platform, session, job, lease, workload, now):
    """Return whether a started gateway context still matches its owner lease."""
    row = session.get(ExecutionAuthority, job.id)
    return bool(
        row is not None
        and lease is not None
        and lease.phase == "started"
        and lease.fence == row.fence
        and lease.workload_id == workload
        and lease.execution_deadline is not None
        and row.deadline == lease.execution_deadline
        and isinstance(row.capability_digest, str)
        and bool(row.capability_digest)
        and isinstance(row.expires_at, (int, float))
        and not isinstance(row.expires_at, bool)
        and math.isfinite(row.expires_at)
        and row.expires_at > now
        and isinstance(row.deadline, (int, float))
        and not isinstance(row.deadline, bool)
        and math.isfinite(row.deadline)
        and row.deadline > now
        and isinstance(row.context, dict)
        and row.context == execution_context(job, workload)
    )


def sweep_terminal_authority(platform, session):
    """Caller holds the authority lock; the hourly throttle is shared by workers."""
    now = time.time()
    marker = session.get(MetadataRow, "authority_retention_sweep")
    if marker is not None and now < marker.value["at"] + 3600:
        return
    cutoff = now - platform.settings.authority_retention_days * 86400
    requests = select(RequestLedger.id).where(RequestLedger.status.in_(["finished", "interrupted"]),
        RequestLedger.updated_at < cutoff).order_by(RequestLedger.updated_at, RequestLedger.id).limit(RETENTION_BATCH)
    session.execute(delete(RequestLedger).where(RequestLedger.id.in_(requests)).execution_options(synchronize_session=False))
    helpers = select(HelperSession.id).where(HelperSession.active == False, HelperSession.last_seen < cutoff,
        ~select(RequestLedger.id).where(RequestLedger.session_id == HelperSession.id).exists()
        ).order_by(HelperSession.last_seen, HelperSession.id).limit(RETENTION_BATCH)
    session.execute(delete(HelperSession).where(HelperSession.id.in_(helpers)).execution_options(synchronize_session=False))
    if marker is None:
        session.add(MetadataRow(key="authority_retention_sweep", value={"at": now}))
    else:
        marker.value = {"at": now}


def _enrollment_certificate(certificate: bytes):
    cert = x509.load_pem_x509_certificate(certificate)
    key = cert.public_key()
    if (cert.not_valid_before_utc.timestamp() > time.time() or cert.not_valid_after_utc.timestamp() <= time.time()
            or ExtendedKeyUsageOID.CLIENT_AUTH not in cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value):
        raise ValueError("valid helper client certificate required")
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("Ed25519 enrollment required")
    return (cert.not_valid_after_utc.timestamp(),
            hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest(),
            key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode())


def provision_enrollment(platform, *, enrollment_id, binding_id, installation_id, role, certificate: bytes):
    """Register a certificate issued by the separately managed helper CA."""
    expires_at, certificate_digest, public_key = _enrollment_certificate(certificate)
    with platform.db.sessions.begin() as session:
        platform.db.authority_lock(session)
        if session.get(HelperEnrollment, enrollment_id):
            fail("enrollment_exists")
        bundle, _ = platform.active(session)
        binding = next((m for m in bundle["models"] if m["id"] == binding_id), None)
        if binding is None or binding.get("connection", {}).get("enrollment_role") != role:
            fail("model_identity_mismatch")
        session.add(HelperEnrollment(id=enrollment_id, binding_id=binding_id, installation_id=installation_id, role=role,
                    expires_at=expires_at, certificate_digest=certificate_digest, public_key=public_key))


def rotate_enrollment(platform, enrollment_id: str, certificate: bytes):
    """Atomically replace an enrollment certificate; existing sessions are fenced."""
    expires_at, certificate_digest, public_key = _enrollment_certificate(certificate)
    with platform.db.sessions.begin() as session:
        platform.db.authority_lock(session)
        enrollment = session.get(HelperEnrollment, enrollment_id)
        if enrollment is None or enrollment.revoked or enrollment.expires_at <= time.time():
            fail("model_identity_mismatch")
        if certificate_digest == enrollment.certificate_digest:
            fail("enrollment_certificate_unchanged")
        enrollment.certificate_digest = certificate_digest
        enrollment.public_key = public_key
        enrollment.expires_at = expires_at
        for helper in session.scalars(select(HelperSession).where(
                HelperSession.enrollment_id == enrollment_id, HelperSession.active == True)):
            Authority(platform)._close(session, helper)
        return {"enrollment_id": enrollment_id, "expires_at": expires_at}


def revoke_enrollment(platform, enrollment_id: str):
    """Revoke an enrollment and terminate its current sessions in the same transaction."""
    authority = Authority(platform)
    with platform.db.sessions.begin() as session:
        platform.db.authority_lock(session)
        enrollment = session.get(HelperEnrollment, enrollment_id)
        if enrollment is None:
            fail("model_identity_mismatch")
        enrollment.revoked = True
        for helper in session.scalars(select(HelperSession).where(
                HelperSession.enrollment_id == enrollment_id, HelperSession.active == True)):
            authority._close(session, helper)
    return {"enrollment_id": enrollment_id, "revoked": True}


def reconcile_helper_demand(platform):
    """Return per-binding helper capacity and fence sessions with expired/revoked enrollment."""
    now = time.time()
    authority = Authority(platform)
    with platform.db.sessions.begin() as session:
        platform.db.authority_lock(session)
        bundle, generation = platform.active(session)
        demand = []
        for binding in bundle.get("models", []):
            if binding.get("connection", {}).get("kind") != "model-gateway":
                continue
            binding_id = binding["id"]
            role = binding.get("connection", {}).get("enrollment_role")
            runtime = session.get(BindingState, binding_id)
            enrollments = list(session.scalars(select(HelperEnrollment).where(HelperEnrollment.binding_id == binding_id)))
            for helper in session.scalars(select(HelperSession).where(
                    HelperSession.binding_id == binding_id, HelperSession.active == True)):
                enrollment = session.get(HelperEnrollment, helper.enrollment_id)
                if (enrollment is None or enrollment.revoked or enrollment.expires_at <= now
                        or not binding.get("enabled", True) or (runtime and runtime.inhibited)
                        or helper.generation != generation or enrollment.role != role):
                    authority._close(session, helper)
            eligible = [row for row in enrollments if not row.revoked and row.expires_at > now]
            active = session.scalar(select(HelperSession.id).where(
                HelperSession.binding_id == binding_id, HelperSession.active == True)) is not None
            demand.append({"binding_id": binding_id, "enabled": bool(binding.get("enabled", True)),
                           "eligible_enrollments": len(eligible), "active_session": active,
                           "demand": "none" if not binding.get("enabled", True) or active else
                                     ("replacement" if eligible else "enrollment")})
        return demand


class Authority:
    def __init__(self, platform):
        self.platform, self.db = platform, platform.db

    @contextmanager
    def _transaction(self):
        # Safety inhibits discovered while checking authority must survive denial.
        denied = None
        with self.db.sessions.begin() as session:
            self.db.authority_lock(session)
            try:
                yield session
            except PlatformError as error:
                if error.code != "binding_inhibited":
                    raise
                denied = error
        if denied is not None:
            raise denied

    def _gateway(self, session, gateway_id):
        row = session.get(MetadataRow, "active_gateway")
        if row is None or row.value != {"id": gateway_id}:
            fail("gateway_fenced")

    def _inhibit(self, session, binding_id):
        row = session.get(BindingState, binding_id)
        if row is None:
            row = BindingState(id=binding_id)
            session.add(row)
        row.inhibited, row.reason, row.updated_at = True, "model_cancel_unconfirmed", time.time()

    def _close(self, session, helper):
        helper.active, helper.last_seen = False, time.time()
        for row in session.scalars(select(RequestLedger).where(RequestLedger.session_id == helper.id, RequestLedger.status == "active")):
            row.status, row.outcome, row.updated_at = "interrupted", "model_cancel_unconfirmed", time.time()
            self._inhibit(session, row.binding_id)

    def start(self, gateway_id):
        with self.db.sessions.begin() as session:
            self.db.authority_lock(session)
            for helper in session.scalars(select(HelperSession).where(HelperSession.active == True)):
                self._close(session, helper)
            row = session.get(MetadataRow, "active_gateway")
            if row is None:
                session.add(MetadataRow(key="active_gateway", value={"id": gateway_id}))
            else:
                row.value = {"id": gateway_id}
        return {"gateway_id": gateway_id}

    def _binding(self, session, binding_id):
        bundle, generation = self.platform.active(session)
        binding = next((m for m in bundle["models"] if m["id"] == binding_id), None)
        if binding is None or not binding.get("enabled", True):
            fail("binding_disabled")
        state = session.get(BindingState, binding_id)
        if state and state.inhibited:
            fail("binding_inhibited")
        if binding.get("connection", {}).get("kind") != "model-gateway":
            fail("model_identity_mismatch")
        role = next(r for r in bundle["connector_roles"] if r["id"] == binding["connection"]["enrollment_role"])
        return bundle, generation, binding, role

    def _helper(self, session, gateway_id, session_id):
        self._gateway(session, gateway_id)
        helper = session.get(HelperSession, session_id)
        if helper is None or not helper.active or helper.gateway_id != gateway_id:
            fail("model_host_unavailable")
        enrollment = session.get(HelperEnrollment, helper.enrollment_id)
        if enrollment is None or enrollment.revoked or enrollment.expires_at <= time.time() or enrollment.binding_id != helper.binding_id:
            fail("model_identity_mismatch")
        bundle, generation, binding, role = self._binding(session, helper.binding_id)
        if generation != helper.generation or enrollment.role != role["id"]:
            fail("revision_superseded")
        expected = ExpectedIdentity(enrollment.installation_id, binding["id"], enrollment.id, role["id"], helper.id, helper.challenge)
        key = serialization.load_pem_public_key(enrollment.public_key.encode())
        return helper, bundle, binding, role, expected, key

    def open_session(self, gateway_id, certificate_digest):
        with self._transaction() as session:
            self._gateway(session, gateway_id)
            enrollment = session.scalar(select(HelperEnrollment).where(HelperEnrollment.certificate_digest == certificate_digest))
            if enrollment is None or enrollment.revoked or enrollment.expires_at <= time.time():
                fail("model_identity_mismatch")
            for helper in session.scalars(select(HelperSession).where(
                    HelperSession.binding_id == enrollment.binding_id, HelperSession.active == True)):
                if helper.last_seen + FRESHNESS_SECONDS <= time.time() or helper.gateway_id != gateway_id:
                    self._close(session, helper)
            bundle, generation, binding, role = self._binding(session, enrollment.binding_id)
            if role["id"] != enrollment.role:
                fail("model_identity_mismatch")
            if session.scalar(select(HelperSession.id).where(HelperSession.binding_id == binding["id"], HelperSession.active == True)):
                fail("binding_session_busy", 409)
            helper = HelperSession(id=uuid.uuid4().hex, binding_id=binding["id"], enrollment_id=enrollment.id, gateway_id=gateway_id,
                                   generation=generation, challenge=secrets.token_hex(32), active=True, last_seen=time.time())
            session.add(helper)
            return {"session_id": helper.id, "nonce": helper.challenge, "installation_id": enrollment.installation_id,
                    "binding_id": binding["id"], "enrollment_id": enrollment.id, "enrollment_role": enrollment.role,
                    "binding": binding, "role": role, "mode": bundle["mode"], "public_key": enrollment.public_key}

    def hello(self, gateway_id, session_id, identity):
        with self.db.sessions.begin() as session:
            self.db.authority_lock(session)
            helper, bundle, binding, role, expected, key = self._helper(session, gateway_id, session_id)
            if helper.identity is not None:
                fail("model_protocol_error")
            try:
                verified = verify_connection(identity, key, expected, now=int(time.time()))
                verify_binding(verified, expected, binding, role, mode=bundle["mode"])
            except ProtocolError:
                fail("model_identity_mismatch")
            helper.identity, helper.last_seen = verified.assertion, time.time()
        return {}

    def touch(self, gateway_id, session_id):
        with self.db.sessions.begin() as session:
            self.db.authority_lock(session)
            helper, *_ = self._helper(session, gateway_id, session_id)
            helper.last_seen = time.time()
        return {}

    def fence_session(self, gateway_id, session_id):
        with self.db.sessions.begin() as session:
            self.db.authority_lock(session)
            self._gateway(session, gateway_id)
            helper = session.get(HelperSession, session_id)
            if helper and helper.gateway_id == gateway_id:
                self._close(session, helper)
        return {}

    def _checks(self, session, gateway_id, request, capability, workload, *, existing=None):
        now = time.time()
        job = session.get(Job, request["job_id"])
        execution = session.get(ExecutionAuthority, request["job_id"])
        # 1: capability and authenticated worker/harness identity (forwarded by trusted gateway).
        if execution is None or not secrets.compare_digest(execution.capability_digest, checksum(capability)) or execution.expires_at <= now:
            fail("execution_capability_invalid")
        if execution.context["workload"] != workload:
            fail("workload_denied")
        # 2-3: LEASE_FENCED; expiry never revives an old attempt.
        if (job is None or execution.fence != request["fence"] or job.status != "running"
                or not job.lease_until or job.lease_until <= now or execution.deadline <= now
                or execution.context != execution_context(job, workload)):
            fail("execution_fenced")
        if job.cancel_requested:
            fail("cancelled")
        # 4: grant/caller/generation/qualification are current, never a cached recipe.
        recipe = self.platform.authorize_existing_job(session, job)
        # 5: recompile context clearance as well as binding runtime inhibit.
        from .policy import PolicyError, validate_bundle
        bundle, _ = self.platform.active(session)
        try:
            validate_bundle(bundle)
        except PolicyError:
            fail("binding_context_denied")
        binding_id = execution.context["binding_id"]
        _, _, binding, _ = self._binding(session, binding_id)
        # An expired permit with no stop report is uncertain, never silently busy.
        abandoned = session.scalars(select(RequestLedger).where(RequestLedger.binding_id == binding_id,
            RequestLedger.status == "active", RequestLedger.permit_until + binding["cancel_bound_seconds"] <= now)).all()
        if abandoned:
            for row in abandoned:
                row.status, row.outcome, row.updated_at = "interrupted", "model_cancel_unconfirmed", now
            self._inhibit(session, binding_id)
            fail("binding_inhibited")
        if recipe["model"] != job.resolved["model"] or recipe["model"]["id"] != binding_id:
            fail("model_identity_mismatch")
        # 6: EXACT_BINDING, SESSION_IDENTITY, enrolled public key and qualified pins.
        selected = session.get(HelperSession, request["session_id"])
        if selected is None or selected.binding_id != binding_id:
            fail("model_identity_mismatch")
        helper, bundle, binding, role, expected, key = self._helper(session, gateway_id, request["session_id"])
        if helper.binding_id != binding_id or helper.identity is None or helper.last_seen + FRESHNESS_SECONDS <= now:
            fail("model_identity_mismatch")
        try:
            verify_binding(Verification(helper.identity, "connection-only"), expected, binding, role, mode=bundle["mode"])
        except ProtocolError:
            fail("model_identity_mismatch")
        # 7: operation/size/digest/deadline/concurrency. Body semantics checked by gateway before this call.
        if (request["operation"] != binding["wire_api"] or not 0 < request["body_size"] <= BODY_LIMIT
                or not now < request["deadline"] <= min(execution.deadline, execution.expires_at, now + binding["timeout_seconds"], now + 600)):
            fail("inference_request_denied")
        active = session.scalar(select(RequestLedger.id).where(RequestLedger.binding_id == binding_id, RequestLedger.status == "active"))
        if active is not None and (existing is None or active != existing.id):
            fail("binding_busy", 409)
        return execution, helper, bundle, binding, role, expected, key

    def _permit(self, session, execution, request):
        now = time.time()
        until = min(now + PERMIT_SECONDS, request["deadline"], execution.expires_at, execution.deadline,
                    session.get(Job, request["job_id"]).lease_until)
        if self.platform.settings.require_qualification:
            from .qualification import current_qualification
            bundle, generation = self.platform.active(session)
            qualification = current_qualification(session, self.platform.settings, generation, bundle.get("default_profile", "local"))
            if qualification is None:
                fail("qualification_required")
            until = min(until, qualification.expires_at)
        if int((until - now) * 1000) < 1:
            fail("permit_expired")
        return now, until

    def authorize_request(self, gateway_id, request_id, request, capability, workload):
        from pydantic import ValidationError
        from .gateway.requests import Authorize
        try:
            Authorize(gateway_id=gateway_id, request_id=request_id, request=request, capability=capability, workload=workload)
        except ValidationError:
            fail("inference_request_denied")
        with self._transaction() as session:
            if session.get(RequestLedger, request_id):
                fail("request_replayed", 409)
            execution, helper, *_ = self._checks(session, gateway_id, request, capability, workload)
            now, until = self._permit(session, execution, request)
            permit = secrets.token_urlsafe(32)
            session.add(RequestLedger(id=request_id, job_id=request["job_id"], fence=execution.fence, binding_id=helper.binding_id,
                        session_id=helper.id, gateway_id=gateway_id, workload=workload, request=request,
                        permit_digest=checksum(permit), permit_until=until, created_at=now, updated_at=now, evidence={"connection": helper.identity}))
            return {"permit": permit, "permit_ttl_ms": max(1, int((until - now) * 1000))}

    def renew_permit(self, gateway_id, request_id, capability, workload, permit, preparation=None):
        with self._transaction() as session:
            row = session.get(RequestLedger, request_id)
            now = time.time()
            if (row is None or row.gateway_id != gateway_id or row.workload != workload or row.status != "active"
                    or row.permit_until <= now or not secrets.compare_digest(row.permit_digest, checksum(permit))):
                fail("permit_expired")
            execution, helper, bundle, binding, role, expected, key = self._checks(session, gateway_id, row.request, capability, workload, existing=row)
            if preparation is not None:
                try:
                    verified = verify_preparation(preparation, key, expected, Verification(helper.identity, "connection-only"), binding, role,
                        request_id=request_id, body_digest=row.request["body_digest"], now=int(now), mode=bundle["mode"])
                except ProtocolError:
                    fail("model_identity_mismatch")
                row.evidence = {**row.evidence, "preparation": verified.assertion, "pinning": verified.pinning}
            now, row.permit_until = self._permit(session, execution, row.request)
            row.updated_at = now
            return {"permit": permit, "permit_ttl_ms": max(1, int((row.permit_until - now) * 1000))}

    def finish(self, gateway_id, request_id, capability, workload, permit, outcome, stopped):
        with self._transaction() as session:
            self._gateway(session, gateway_id)
            row = session.get(RequestLedger, request_id)
            if row is None or row.gateway_id != gateway_id or row.workload != workload:
                fail("permit_expired")
            if permit is None:
                # Lost authorize response: trusted gateway certifies no prompt was sent.
                execution = session.get(ExecutionAuthority, row.job_id)
                if (outcome != "authority_unavailable" or stopped is not True or execution is None
                        or execution.fence != row.fence or execution.context["workload"] != workload
                        or not execution.capability_digest
                        or not secrets.compare_digest(execution.capability_digest, checksum(capability))):
                    fail("permit_expired")
            elif not secrets.compare_digest(row.permit_digest, checksum(permit)):
                fail("permit_expired")
            if row.status != "active":
                fail("request_replayed")
            if outcome == "completed":
                if row.permit_until <= time.time() or "preparation" not in row.evidence:
                    fail("permit_expired")
                self._checks(session, gateway_id, row.request, capability, workload, existing=row)
            row.status, row.outcome, row.updated_at = "finished", outcome, time.time()
            if not stopped:
                row.outcome = "model_cancel_unconfirmed"
                self._inhibit(session, row.binding_id)
            row.evidence = {**row.evidence, "stop_confirmed": stopped}
        return {}
