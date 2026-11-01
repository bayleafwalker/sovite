"""Bearer-only, private collection owner surface; no effect endpoints."""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from . import collection_owner as owner
from .collection_identity import (CollectionCredential, InvalidCollectionInput, MAX_REQUEST_BYTES,
                                  authenticate_collection, parse_closed_json)


def install_routes(app, platform, settings):
    credentials = tuple(CollectionCredential(value["principal_id"], capability, value["token"])
                        for capability, value in settings.collection_credentials.items())
    prefix = "/internal/collection-owner/v1"

    def reject(status, code):
        return JSONResponse({"detail": {"code": code, "message": "Collection owner request refused"}}, status_code=status)

    def auth(request):
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            raise owner.CollectionOwnerError("collection_unauthorized", 401)
        credential = authenticate_collection(header[7:], credentials)
        if credential is None:
            raise owner.CollectionOwnerError("collection_unauthorized", 401)
        return credential

    async def body(request, fields):
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_REQUEST_BYTES:
                raise owner.CollectionOwnerError("collection_invalid_request", 422)
            chunks.append(chunk)
        raw = b"".join(chunks)
        try:
            value = parse_closed_json(raw)
        except InvalidCollectionInput:
            raise owner.CollectionOwnerError("collection_invalid_request", 422) from None
        if set(value) != fields:
            raise owner.CollectionOwnerError("collection_invalid_request", 422)
        return raw, value

    async def invoke(function):
        try:
            return await function()
        except owner.CollectionOwnerError as error:
            return reject(error.status, error.code)
        except Exception:
            return reject(503, "collection_unavailable")

    @app.post(prefix + "/sessions")
    async def create(request: Request):
        async def operation():
            credential = auth(request)
            raw, _ = await body(request, {"schema_version", "workspace_id", "repository_id", "generation",
                  "suite_version", "suite_digest", "deployment_digest", "permit_digest", "deadline_ms", "retain_until_ms"})
            result = owner.create(platform.db, raw=raw, credential=credential,
                                  idempotency_key=request.headers.get("idempotency-key", ""))
            return JSONResponse(result, status_code=201)
        return await invoke(operation)

    @app.get(prefix + "/sessions/{session_id}")
    async def read(session_id: str, request: Request):
        async def operation():
            return owner.read(platform.db, session_id=session_id, credential=auth(request))
        return await invoke(operation)

    @app.get(prefix + "/sessions/{session_id}/currentness")
    async def currentness(session_id: str, request: Request):
        async def operation():
            return owner.currentness(platform.db, session_id=session_id, credential=auth(request))
        return await invoke(operation)

    async def mutate(request, session_id, action, fields):
        async def operation():
            credential = auth(request)
            _, data = await body(request, fields)
            return owner.transition(platform.db, session_id=session_id, credential=credential,
                  expected_revision=data["expected_revision"], immutable_digest=data["immutable_digest"], action=action,
                  **({"epoch_deadline_ms": data["deadline_ms"], "retain_until_ms": data["retain_until_ms"],
                      "authorization_digest": data["authorization_digest"]} if action == "start" else {}))
        return await invoke(operation)

    @app.post(prefix + "/sessions/{session_id}/admit")
    async def admit(session_id: str, request: Request):
        return await mutate(request, session_id, "admit", {"expected_revision", "immutable_digest"})

    @app.post(prefix + "/sessions/{session_id}/epochs")
    async def start(session_id: str, request: Request):
        async def operation():
            credential = auth(request)
            _, data = await body(request, {"expected_revision", "immutable_digest", "purpose", "authorization_digest",
                                           "deadline_ms", "retain_until_ms"})
            if data["purpose"] != "execution":
                raise owner.CollectionOwnerError("collection_conflict", 409)
            return owner.transition(platform.db, session_id=session_id, credential=credential,
                 expected_revision=data["expected_revision"], immutable_digest=data["immutable_digest"], action="start",
                 epoch_deadline_ms=data["deadline_ms"], retain_until_ms=data["retain_until_ms"],
                 authorization_digest=data["authorization_digest"])
        return await invoke(operation)

    @app.post(prefix + "/sessions/{session_id}/epochs/{epoch}/close")
    async def close(session_id: str, epoch: str, request: Request):
        async def operation():
            credential = auth(request)
            if epoch != "1":
                raise owner.CollectionOwnerError("collection_conflict", 409)
            _, data = await body(request, {"expected_revision", "immutable_digest"})
            return owner.transition(platform.db, session_id=session_id, credential=credential,
                  expected_revision=data["expected_revision"], immutable_digest=data["immutable_digest"], action="close")
        return await invoke(operation)

    @app.post(prefix + "/sessions/{session_id}/cancel")
    async def cancel(session_id: str, request: Request):
        async def operation():
            credential = auth(request)
            _, data = await body(request, {"expected_revision", "immutable_digest", "reason_code"})
            return owner.cancel(platform.db, session_id=session_id, credential=credential, **data)
        return await invoke(operation)
