"""Class-aware result release for authenticated callers and scoped readers.

The HTTP/MCP layer authenticates the identity passed here. A configured caller
can observe only its own receipt; a separate reader needs an explicit grant
allowlist entry. Neither path grants submission or operator authority. Artifact
storage is read outside the authority transaction and every release repeats the
current policy checks after digest verification.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import TYPE_CHECKING

from sqlalchemy import select

from .db import Job
from .service import PUBLIC_STATUS, PlatformError

if TYPE_CHECKING:
    from .service import Platform


@dataclass(frozen=True)
class _Artifact:
    job_id: str
    generation: str
    key: str
    sha256: str


def _row(platform: Platform, session, receipt: str, identity: str, *, reader: bool) -> Job:
    try:
        valid_receipt = (isinstance(receipt, str) and 1 <= len(receipt) <= 256
                         and bool(receipt.encode("utf-8")))
    except UnicodeError:
        valid_receipt = False
    if not valid_receipt:
        raise PlatformError("not_found", "Receipt not found", 404)
    # Cookie identities can outlive a removed caller credential. Recheck the
    # currently configured principal as well as the grant below.
    if not platform.settings.caller_tokens.get(identity):
        raise PlatformError("not_found", "Receipt not found", 404)
    query = select(Job).where(Job.receipt == receipt)
    if not reader:
        query = query.where(Job.caller == identity)
    row = session.scalar(query)
    if row is None:
        raise PlatformError("not_found", "Receipt not found", 404)
    if reader and identity not in row.resolved["grant"].get("result_readers", []):
        # Do not reveal receipt existence to an unrelated principal.
        raise PlatformError("not_found", "Receipt not found", 404)
    return row


def _authorize(platform: Platform, session, row: Job, identity: str, *, reader: bool):
    # Recheck the producer and any exact released input it consumed. Revoking a
    # source after downstream execution must also prevent downstream disclosure.
    recipe = platform.authorize_existing_job(session, row)
    grant = recipe["grant"]
    if reader and identity not in grant.get("result_readers", []):
        raise PlatformError("not_found", "Receipt not found", 404)
    bundle, _ = platform.active(session)
    ranks = {item["id"]: item["rank"] for item in bundle["data_classes"]}
    receipt_class = grant.get("receipt_class", min(ranks, key=ranks.get))
    clearance = grant.get("caller_clearance", receipt_class)
    caller_cleared = ranks[clearance] >= ranks[grant["output_class"]]
    return grant, caller_cleared


def _projection(row: Job) -> dict:
    result = {"receipt": row.receipt, "status": PUBLIC_STATUS.get(row.status, "unavailable")}
    if row.status == "completed" and (
        row.public_projection != {"receipt": row.receipt, "status": "available"}
        or row.released_at is None
        or not row.artifact_key
        or not row.artifact_sha256
    ):
        result["status"] = "unavailable"
    return result


def _reference(projection: dict, grant: dict, *, cleared: bool) -> dict:
    if grant["return_contract"] == "class-aware" and not cleared:
        return {**projection, "reader": {"tool": "read_result", "receipt": projection["receipt"]}}
    return projection


def _artifact(row: Job) -> _Artifact:
    return _Artifact(row.id, row.generation, row.artifact_key, row.artifact_sha256)


def _read(platform: Platform, artifact: _Artifact) -> dict:
    def invalid_constant(_value):
        raise ValueError("Non-finite result")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate result field")
            result[key] = value
        return result

    try:
        if artifact.key != f"results/{artifact.job_id}/{artifact.sha256}.json":
            raise ValueError("Result storage identity does not match its job")
        body = platform.store.get(artifact.key, artifact.sha256)
        result = json.loads(body.decode("utf-8"), parse_constant=invalid_constant,
                            object_pairs_hook=unique_object)
        if not isinstance(result, dict):
            raise ValueError("Result must be a JSON object")
        return result
    except Exception as exc:
        # Storage/provider errors can contain locations or result fragments.
        raise PlatformError("artifact_integrity_failed", "The result artifact is unavailable or failed integrity verification", 503) from exc


def _observe(platform: Platform, session, identity: str, row: Job, projection: dict):
    platform.db.audit(session, identity, "receipt.observed", row.id,
                      {"status": projection["status"]})


def result_status(platform: Platform, receipt: str, caller: str) -> dict:
    """Return bounded status, or full verified content under a class-aware grant.

    Caller clearance is installed policy; request parameters cannot raise it.
    Legacy reference grants retain their exact two-field response.
    """
    with platform.db.sessions.begin() as session:
        platform.db.authority_lock(session)
        row = _row(platform, session, receipt, caller, reader=False)
        try:
            grant, cleared = _authorize(platform, session, row, caller, reader=False)
        except PlatformError:
            projection = {"receipt": row.receipt, "status": "unavailable"}
            _observe(platform, session, caller, row, projection)
            return projection
        projection = _projection(row)
        if grant["return_contract"] != "class-aware" or not cleared or projection["status"] != "available":
            _observe(platform, session, caller, row, projection)
            return _reference(projection, grant, cleared=cleared)
        artifact = _artifact(row)

    result = _read(platform, artifact)

    with platform.db.sessions.begin() as session:
        platform.db.authority_lock(session)
        row = _row(platform, session, receipt, caller, reader=False)
        try:
            grant, cleared = _authorize(platform, session, row, caller, reader=False)
        except PlatformError:
            projection = {"receipt": row.receipt, "status": "unavailable"}
            _observe(platform, session, caller, row, projection)
            return projection
        projection = _projection(row)
        if (_artifact(row) != artifact or projection["status"] != "available"
                or grant["return_contract"] != "class-aware" or not cleared):
            projection["status"] = "unavailable"
            _observe(platform, session, caller, row, projection)
            return _reference(projection, grant, cleared=cleared)
        platform.db.audit(session, caller, "result.content_read", row.id, {"access": "caller"})
        return {**projection, "result": result}


def scoped_read(platform: Platform, receipt: str, reader: str) -> dict:
    """Read one released artifact using explicit grant-scoped read authority.

    Membership in result_readers grants clearance only for that grant's output,
    not submission, other grants or operator endpoints.
    """
    with platform.db.sessions.begin() as session:
        platform.db.authority_lock(session)
        row = _row(platform, session, receipt, reader, reader=True)
        _authorize(platform, session, row, reader, reader=True)
        projection = _projection(row)
        if projection["status"] != "available":
            _observe(platform, session, reader, row, projection)
            return projection
        artifact = _artifact(row)

    result = _read(platform, artifact)

    with platform.db.sessions.begin() as session:
        platform.db.authority_lock(session)
        row = _row(platform, session, receipt, reader, reader=True)
        _authorize(platform, session, row, reader, reader=True)
        projection = _projection(row)
        if _artifact(row) != artifact or projection["status"] != "available":
            raise PlatformError("result_unavailable", "The released result is no longer available", 409)
        platform.db.audit(session, reader, "result.content_read", row.id, {"access": "reader"})
        return {**projection, "result": result}
