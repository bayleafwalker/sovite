"""Explicit, auditable declassification decisions over exact output bytes.

An installed output contract selects authorized approvers and destinations.
Approvers must perform its procedure and supply evidence before calling this
boundary. This module records that decision; it never treats an LLM assertion
or a declared target class as proof that a transformation is safe.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import math
import re
import time

from .db import MetadataRow, canonical, digest
from .flow import class_ranks, contract_revision
from .service import PlatformError
from .workflows import validate_revision_ref


_RECORD_FIELDS = {
    "release_id", "generation", "contract_id", "contract_revision", "source_job_id",
    "source_digest", "output_class", "destinations", "artifact_key", "artifact_sha256",
    "actor", "actor_role", "evidence_refs", "approved_at",
}


@dataclass
class _Verification:
    platform: object
    session: object
    # Both metadata bytes and content strings are immutable snapshots. A final
    # authority pass may reuse only proofs obtained by this call and session.
    verified: dict[str, tuple[bytes, str]] = field(default_factory=dict)
    rechecking: bool = False


_VERIFICATION: ContextVar[_Verification | None] = ContextVar("output_verification", default=None)


@contextmanager
def _verification_scope(platform, session):
    current = _VERIFICATION.get()
    if current is not None and current.platform is platform and current.session is session:
        yield current, False
        return
    current = _Verification(platform, session)
    token = _VERIFICATION.set(current)
    try:
        yield current, True
    finally:
        _VERIFICATION.reset(token)


@contextmanager
def _rechecking(verification):
    previous = verification.rechecking
    verification.rechecking = True
    try:
        yield
    finally:
        verification.rechecking = previous


def _integrity_error() -> PlatformError:
    return PlatformError("output_release_integrity_failed", "The approved output record failed integrity verification", 503)


def _request(record: dict, content: str) -> dict:
    """The exact approved input is also the durable release's content identity."""
    return {field: record[field] for field in (
        "source_job_id", "source_digest", "contract_id", "contract_revision",
        "generation", "evidence_refs", "actor", "actor_role",
    )} | {"content": content}


def _require_approver(platform, contract: dict, actor: str, actor_role: str) -> None:
    enabled = (actor_role == "operator" and actor == "operator" and bool(platform.settings.operator_token)) or (
        actor_role == "caller" and bool(platform.settings.caller_tokens.get(actor)))
    if not enabled or actor not in contract["approvers"]:
        raise PlatformError("declassification_denied", "This identity cannot approve or access the output contract", 403)


def _source(platform, session, source_job_id: str, expected_generation: str):
    source = platform.get_job(source_job_id, session)
    try:
        recipe = platform.authorize_existing_job(session, source)
    except (AttributeError, KeyError, TypeError, ValueError):
        # Persisted source resolution is an integrity boundary too. Malformed
        # metadata must not turn a release read into an unbounded exception.
        raise PlatformError("artifact_integrity_failed", "The completed source record failed integrity verification", 503) from None
    if (source.generation != expected_generation or source.status != "completed"
            or source.public_projection != {"receipt": source.receipt, "status": "available"}
            or source.released_at is None or not source.artifact_key
            or not isinstance(source.artifact_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", source.artifact_sha256)):
        raise PlatformError("source_unavailable", "The exact completed source is unavailable", 409)
    if source.artifact_key != f"results/{source.id}/{source.artifact_sha256}.json":
        raise PlatformError("artifact_integrity_failed", "The source artifact identity failed integrity verification", 503)
    return source, recipe


def _contract(bundle: dict, contract_id: str, revision: str) -> dict:
    found = next((c for c in bundle.get("output_contracts", []) if c["id"] == contract_id), None)
    if found is None or contract_revision(found) != revision:
        raise PlatformError("output_contract_changed", "The exact installed output contract is unavailable", 409)
    return found


def _release_authority(platform, session, record):
    """Recheck the exact current contract, source chain and approving identity."""
    bundle, generation = platform.active(session)
    if generation != record["generation"]:
        raise PlatformError("revision_superseded", "The output release belongs to a superseded installation", 409)
    contract = _contract(bundle, record["contract_id"], record["contract_revision"])
    _require_approver(platform, contract, record["actor"], record["actor_role"])
    source, recipe = _source(platform, session, record["source_job_id"], generation)
    if record["source_digest"] != "sha256:" + source.artifact_sha256:
        raise PlatformError("source_digest_mismatch", "The reviewed source artifact differs from the completed job", 409)
    ranks = class_ranks(bundle)
    if ranks[recipe["grant"]["output_class"]] > ranks[contract["source_class"]]:
        raise PlatformError("declassification_denied", "The source exceeds the contract's class ceiling", 403)
    _require_approver(platform, contract, record["actor"], record["actor_role"])
    return source, contract


def approve_output(platform, *, source_job_id: str, source_digest: str, contract_id: str,
                   expected_contract_revision: str, expected_generation: str, content: str,
                   evidence_refs: list[dict], actor: str, is_operator: bool = False) -> dict:
    try:
        valid_content = isinstance(content, str) and bool(content.strip()) and len(content.encode("utf-8")) <= platform.settings.max_request_bytes
    except UnicodeError:
        valid_content = False
    if not valid_content:
        raise PlatformError("invalid_output", "Output must be nonempty and fit the configured task limit", 422)
    if not evidence_refs or len(evidence_refs) > 100:
        raise PlatformError("evidence_required", "Contract approval requires bounded verification evidence", 422)
    evidence = [validate_revision_ref(ref, "evidence_ref") for ref in evidence_refs]
    actor_role = "operator" if is_operator else "caller"
    approved = {"source_job_id": source_job_id, "source_digest": source_digest,
        "contract_id": contract_id, "contract_revision": expected_contract_revision,
        "generation": expected_generation, "evidence_refs": evidence,
        "actor": actor, "actor_role": actor_role}
    try:
        request_digest = digest(_request(approved, content))
    except (TypeError, ValueError):
        raise PlatformError("invalid_output", "The approval must contain valid UTF-8 identities and content", 422) from None
    release_id = "release_" + request_digest
    # Keep the authority lock through integrity checks and the durable decision.
    # Time and live settings still change under this lock, so the last pass must
    # recheck authority after I/O. Unreferenced artifacts grant no release.
    with platform.db.sessions.begin() as session:
        platform.db.authority_lock(session)
        with _verification_scope(platform, session) as (verification, _):
            source, contract = _release_authority(platform, session, approved)
            try:
                platform.store.get(source.artifact_key, source.artifact_sha256)
            except Exception:
                raise PlatformError("artifact_integrity_failed", "The source artifact failed integrity verification", 503) from None
            existing = session.get(MetadataRow, release_id)
            if existing is not None:
                _read(platform, session, release_id)
                with _rechecking(verification):
                    record, _, _ = _read(platform, session, release_id)
                return {"release_id": release_id, "output_class": record["output_class"],
                        "output_digest": "sha256:" + record["artifact_sha256"],
                        "contract_revision": expected_contract_revision, "destinations": record["destinations"]}
            key, sha = platform.store.put(release_id, {"content": content})
            with _rechecking(verification):
                source, contract = _release_authority(platform, session, approved)
            record = {"release_id": release_id, "generation": expected_generation,
                      "contract_id": contract_id, "contract_revision": expected_contract_revision,
                      "source_job_id": source.id, "source_digest": source_digest,
                      "output_class": contract["output_class"], "destinations": contract["destinations"],
                      "artifact_key": key, "artifact_sha256": sha, "actor": actor, "actor_role": actor_role,
                      "evidence_refs": evidence, "approved_at": time.time()}
            session.add(MetadataRow(key=release_id, value=record))
            platform.db.audit(session, actor, "output.declassified", release_id,
                {"contract_revision": expected_contract_revision, "source_digest": source_digest,
                 "output_digest": "sha256:" + sha, "output_class": contract["output_class"]})
    return {"release_id": release_id, "output_class": record["output_class"],
            "output_digest": "sha256:" + sha, "contract_revision": expected_contract_revision,
            "destinations": record["destinations"]}


def _record(platform, session, release_id: str) -> tuple[dict, dict]:
    if not isinstance(release_id, str) or not re.fullmatch(r"release_[0-9a-f]{64}", release_id):
        raise PlatformError("output_release_unavailable", "Output release is unavailable", 404)
    row = session.get(MetadataRow, release_id)
    if row is None:
        raise PlatformError("output_release_unavailable", "Output release is unavailable", 404)
    record = row.value
    if not isinstance(record, dict) or set(record) != _RECORD_FIELDS or record["release_id"] != release_id:
        raise _integrity_error()
    if any(not isinstance(record[field], str) or not record[field] for field in (
        "generation", "contract_id", "contract_revision", "source_job_id", "source_digest",
        "output_class", "artifact_key", "artifact_sha256", "actor", "actor_role",
    )):
        raise _integrity_error()
    try:
        valid_timestamp = type(record["approved_at"]) in {int, float} and math.isfinite(record["approved_at"])
        for field in ("generation", "contract_id", "contract_revision", "source_job_id", "source_digest",
                      "output_class", "artifact_key", "artifact_sha256", "actor", "actor_role"):
            record[field].encode("utf-8")
    except (OverflowError, UnicodeError):
        raise _integrity_error() from None
    if (not isinstance(record["artifact_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", record["artifact_sha256"])
            or record["artifact_key"] != f"results/{release_id}/{record['artifact_sha256']}.json"
            or record["actor_role"] not in {"caller", "operator"}
            or not isinstance(record["actor"], str)
            or not valid_timestamp):
        raise _integrity_error()
    try:
        if not isinstance(record["evidence_refs"], list) or not 1 <= len(record["evidence_refs"]) <= 100:
            raise ValueError("Invalid evidence")
        for ref in record["evidence_refs"]:
            validate_revision_ref(ref, "evidence_ref")
    except (ValueError, TypeError):
        raise _integrity_error() from None
    bundle, generation = platform.active(session)
    if record["generation"] != generation:
        raise PlatformError("revision_superseded", "The output release belongs to a superseded installation", 409)
    contract = _contract(bundle, record["contract_id"], record["contract_revision"])
    if record["output_class"] != contract["output_class"] or record["destinations"] != contract["destinations"]:
        raise _integrity_error()
    return record, contract


def _read(platform, session, release_id: str) -> tuple[dict, dict, str]:
    # Only the outer read performs the final pass. Nested source reads collect
    # proofs once; final traversal reauthorizes every ancestor without reading
    # artifacts again. This keeps long review/rework chains linear.
    with _verification_scope(platform, session) as (verification, outermost):
        result = _read_once(platform, session, release_id, verification)
        if outermost:
            with _rechecking(verification):
                result = _read_once(platform, session, release_id, verification)
        return result


def _read_once(platform, session, release_id: str, verification: _Verification) -> tuple[dict, dict, str]:
    record, _ = _record(platform, session, release_id)
    # Bind proofs to metadata observed before any recursive source/storage I/O.
    # A mutable ORM value must not change which bytes an earlier read proved.
    metadata = canonical(record)
    if verification.rechecking:
        proof = verification.verified.get(release_id)
        if proof is None or metadata != proof[0]:
            raise _integrity_error()
        # Fresh current-policy checks are never cached, including qualification
        # expiry, live settings, all source bindings and every approving actor.
        _, contract = _release_authority(platform, session, record)
        return record, contract, proof[1]
    source, contract = _release_authority(platform, session, record)

    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate released content field")
            value[key] = item
        return value

    try:
        platform.store.get(source.artifact_key, source.artifact_sha256)
        value = json.loads(platform.store.get(record["artifact_key"], record["artifact_sha256"]).decode("utf-8"),
                           object_pairs_hook=unique_object)
        if (not isinstance(value, dict) or set(value) != {"content"}
                or not isinstance(value["content"], str) or not value["content"].strip()):
            raise ValueError("Invalid released content")
        content = value["content"]
        content.encode("utf-8")
    except Exception:
        raise PlatformError("artifact_integrity_failed", "The approved output failed integrity verification", 503) from None
    if release_id != "release_" + digest(_request(record, content)):
        raise _integrity_error()
    verification.verified[release_id] = (metadata, content)
    return record, contract, content


def read_output(platform, release_id: str, actor: str, *, is_operator: bool = False) -> dict:
    with platform.db.sessions.begin() as session:
        platform.db.authority_lock(session)
        _, contract = _record(platform, session, release_id)
        role = "operator" if is_operator else "caller"
        _require_approver(platform, contract, actor, role)
        record, contract, content = _read(platform, session, release_id)
        _require_approver(platform, contract, actor, role)
        platform.db.audit(session, actor, "output.release_read", release_id)
        return {"release_id": release_id, "output_class": record["output_class"],
                "destinations": record["destinations"], "content": content}


def transfer_content(platform, session, binding: dict, role: str,
                     transfer_id: str | None, release_id: str) -> str:
    transfer = binding["transfers"].get(transfer_id)
    if not transfer or transfer["target"] != {"kind": "role", "role": role} or not transfer["contract_id"]:
        raise PlatformError("transfer_mismatch", "The output release does not name a bound role transfer", 409)
    record, contract, content = _read(platform, session, release_id)
    source = platform.get_job(record["source_job_id"], session)
    source_binding = source.resolved.get("workflow_binding", {})
    if (source_binding.get("binding_digest") != binding["binding_digest"]
            or source.resolved.get("workflow_role") != transfer["from_role"]
            or record["contract_id"] != transfer["contract_id"]
            or record["contract_revision"] != transfer["contract_revision"]):
        raise PlatformError("transfer_mismatch", "Approved output belongs to a different workflow source or contract", 409)
    target = binding["roles"][role]
    bundle, _ = platform.active(session)
    if (target["model_destination"] not in contract["destinations"]
            or class_ranks(bundle)[record["output_class"]] > class_ranks(bundle)[target["input_class"]]):
        raise PlatformError("transfer_mismatch", "The target role cannot receive the approved output", 403)
    return content
