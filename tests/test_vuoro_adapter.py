import asyncio
from dataclasses import replace
from datetime import datetime, timezone, timedelta
import json
import pytest
import httpx
from sovite.vuoro_adapter import (
    LocalBinding,
    LocalSoviteApplication,
    SoviteInvocationRefused,
    register_sovite_catalog,
)
from vuoro_service.identity import Identity, InvocationContext
from vuoro_service.catalog import CatalogRegistry

RECEIPT = "rcpt_" + "x" * 32


def binding(**changes):
    values = dict(
        workspace_id="bayleaf",
        repo_id="maintenance",
        principal_id="local:worker:0",
        endpoint="http://127.0.0.1:8080",
        token="caller-test-token-long-enough-0000",
        grants=frozenset({"handover"}),
        reference_key=b"test-integrity-key-not-a-credential-000",
    )
    values.update(changes)
    return LocalBinding(**values)


def context(**changes):
    identity = Identity(
        actor="worker",
        environment="test",
        workspace_id="bayleaf",
        principal_id="local:worker:0",
        authorities=frozenset({"sovite:request", "sovite:read"}),
        repo_ids=frozenset({"maintenance"}),
    )
    values = dict(
        identity=identity,
        repo_id="maintenance",
        request_id="r",
        basis_revision=None,
        catalog_revision="test",
        idempotency_requirement="required",
        idempotency_key="logical-task",
    )
    values.update(changes)
    return InvocationContext(**values)


def arguments(**changes):
    value = dict(
        work_id=1, run_id="run-worker", grant_id="handover", task="PRIVATE-SOURCE"
    )
    value.update(changes)
    return value


def app(handler=None, guard=lambda *_: None, bindings=None):
    handler = handler or (
        lambda _: httpx.Response(
            200,
            json={
                "result": {
                    "isError": False,
                    "structuredContent": {"receipt": RECEIPT, "status": "pending"},
                }
            },
        )
    )
    return LocalSoviteApplication(
        bindings or (binding(),), guard, httpx.MockTransport(handler)
    )


def invoke(application, args=None, ctx=None, op="sovite.task.request-v1"):
    return asyncio.run(application.invoke(op, args or arguments(), ctx or context()))


def test_request_and_status_survive_restart_without_leaking_private_output():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "result": {
                    "isError": False,
                    "structuredContent": {"receipt": RECEIPT, "status": "available"},
                }
            },
        )

    result = invoke(app(handler))
    assert set(result) == {
        "receipt",
        "status",
        "task_binding",
    } and "PRIVATE" not in json.dumps(result)
    args = arguments(receipt=RECEIPT, task_binding=result["task_binding"])
    args.pop("task")
    assert invoke(app(handler), args, op="sovite.task.status-v1") == result
    assert seen[-1]["params"]["arguments"] == {"receipt": RECEIPT}


def test_request_idempotency_is_stable_and_bound_to_work_run_and_identity():
    keys = []

    def handler(request):
        keys.append(
            json.loads(request.content)["params"]["arguments"]["idempotency_key"]
        )
        return httpx.Response(
            200,
            json={
                "result": {
                    "isError": False,
                    "structuredContent": {"receipt": RECEIPT, "status": "pending"},
                }
            },
        )

    a = app(handler)
    invoke(a)
    invoke(a)
    invoke(a, arguments(work_id=2))
    assert keys[0] == keys[1] and keys[0] != keys[2]


@pytest.mark.parametrize(
    "change",
    [
        {"work_id": 2},
        {"run_id": "other-run"},
        {"receipt": "rcpt_" + "y" * 32},
        {"grant_id": "other"},
    ],
)
def test_receipt_cannot_be_rebound_to_different_work(change):
    result = invoke(app())
    args = arguments(receipt=RECEIPT, task_binding=result["task_binding"])
    args.pop("task")
    args.update(change)
    with pytest.raises(SoviteInvocationRefused):
        invoke(
            app(lambda _: pytest.fail("reached upstream")),
            args,
            op="sovite.task.status-v1",
        )


@pytest.mark.parametrize(
    "dimension", ["workspace", "repository", "principal", "expiry", "authority"]
)
def test_wrong_identity_binding_is_refused_before_upstream(dimension):
    ctx = context()
    identity = ctx.identity
    if dimension == "workspace":
        identity = replace(identity, workspace_id="other")
    if dimension == "repository":
        ctx = replace(ctx, repo_id="other")
    if dimension == "principal":
        identity = replace(identity, principal_id="local:other:0")
    if dimension == "expiry":
        identity = replace(
            identity, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
        )
    if dimension == "authority":
        identity = replace(identity, authorities=frozenset())
    with pytest.raises(SoviteInvocationRefused):
        invoke(
            app(lambda _: pytest.fail("reached upstream")),
            ctx=replace(ctx, identity=identity),
        )


def test_stale_work_guard_blocks_request_and_status():
    def guard(*_):
        raise SoviteInvocationRefused("stale run")

    with pytest.raises(SoviteInvocationRefused):
        invoke(app(lambda _: pytest.fail("reached upstream"), guard))


@pytest.mark.parametrize(
    "view",
    [
        {"receipt": RECEIPT, "status": "available", "answer": "PRIVATE"},
        {"receipt": RECEIPT, "status": "mystery"},
        {"receipt": "bad", "status": "pending"},
    ],
)
def test_upstream_extra_fields_and_unknown_status_never_escape(view):
    a = app(
        lambda _: httpx.Response(
            200, json={"result": {"isError": False, "structuredContent": view}}
        )
    )
    with pytest.raises(SoviteInvocationRefused) as error:
        invoke(a)
    assert "PRIVATE" not in str(error.value)


def test_registration_and_configuration_do_not_expose_artifact_or_setup_routes():
    registry = CatalogRegistry()
    register_sovite_catalog(registry, app())
    assert {op.name for op in registry.catalog().operations} == {
        "sovite.task.request-v1",
        "sovite.task.status-v1",
    }
    assert binding().token not in repr(binding())
    with pytest.raises(ValueError):
        binding(endpoint="https://arbitrary.example")
    with pytest.raises(ValueError):
        LocalSoviteApplication(
            (binding(), binding(principal_id="local:other:0")), lambda *_: None
        )
