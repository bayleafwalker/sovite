from types import SimpleNamespace
import asyncio

import httpx

import pytest
from fastapi import FastAPI

from sovite import collection_owner as owner
from sovite.collection_owner_api import install_routes
from sovite.db import Database
from sovite.settings import Settings
from test_collection_owner_cancel import fixture
from test_collection_owner_lifecycle import snapshot


ROLES = ("collection-operator", "collection-agent", "collection-reconciler", "collection-recovery")


def credentials():
    return {role: {"principal_id": role, "token": role * 3} for role in ROLES}


def test_disabled_by_default_and_token_collision():
    assert not Settings().collection_owner_enabled
    data = credentials()
    with pytest.raises(ValueError):
        Settings(collection_owner_enabled=True, collection_credentials=data, operator_token=data[ROLES[0]]["token"])
    data[ROLES[1]]["principal_id"] = data[ROLES[0]]["principal_id"]
    with pytest.raises(ValueError):
        Settings(collection_owner_enabled=True, collection_credentials=data)


def test_auth_scope_invalid_json_and_unavailable_default(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/owner.db")
    db.migrate()
    try:
        digest = fixture(db)
        settings = Settings(collection_owner_enabled=True, collection_credentials=credentials(), operator_token="x" * 24)
        app = FastAPI()
        install_routes(app, SimpleNamespace(db=db), settings)
        async def exercise():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
                prefix = "/internal/collection-owner/v1/sessions/session"
                assert (await client.get(prefix)).status_code == 401
                assert (await client.get(prefix, headers={"Authorization": "Bearer " + "x" * 24})).status_code == 401
                agent = {"Authorization": "Bearer " + credentials()[ROLES[1]]["token"]}
                operator = {"Authorization": "Bearer " + credentials()[ROLES[0]]["token"]}
                assert (await client.get(prefix, headers=agent)).status_code == 200
                assert (await client.get(prefix + "/currentness", headers=agent)).status_code == 503
                assert (await client.post(prefix + "/admit", headers=agent,
                    json={"expected_revision": 0, "immutable_digest": digest})).status_code == 403
                assert (await client.post(prefix + "/admit", headers=operator,
                    content='{"expected_revision":0,"expected_revision":0,"immutable_digest":"x"}')).status_code == 422
                assert (await client.post(prefix + "/admit", headers=operator,
                    json={"expected_revision": 0, "immutable_digest": digest})).status_code == 503
                assert (await client.post(prefix + "/epochs/2/close")).status_code == 401
                assert (await client.post(prefix + "/operations", headers=agent, json={})).status_code == 404
        asyncio.run(exercise())
    finally:
        db.engine.dispose()


def test_api_cancel_stays_available_when_currentness_unavailable(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/owner.db")
    db.migrate()
    try:
        digest = fixture(db)
        settings = Settings(collection_owner_enabled=True, collection_credentials=credentials())
        app = FastAPI()
        install_routes(app, SimpleNamespace(db=db), settings)
        headers = {"Authorization": "Bearer " + credentials()[ROLES[0]]["token"]}
        async def exercise():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
                path = "/internal/collection-owner/v1/sessions/session/cancel"
                payload = {"expected_revision": 0, "immutable_digest": digest, "reason_code": "stop"}
                assert (await client.post(path, headers=headers, json=payload)).status_code == 200
                assert (await client.post(path, headers=headers, json=payload)).status_code == 409
        asyncio.run(exercise())
    finally:
        db.engine.dispose()


def test_main_app_does_not_register_collection_routes_by_default(tmp_path):
    from sovite.api import create_app
    settings = Settings(database_url=f"sqlite:///{tmp_path}/unused.db", artifact_path=str(tmp_path / "artifacts"))
    app = create_app(settings, repository_run_owner=object())
    try:
        assert not any(route.path.startswith("/internal/collection-owner/v1") for route in app.routes)
    finally:
        app.state.platform.db.engine.dispose()
