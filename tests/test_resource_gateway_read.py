import asyncio
import copy
import hashlib
import json

import pytest

from sovite.resource_gateway_read import ResourceGatewayRead, ResourceReadError

REQUEST = {"workspace_id": "workspace-a", "resource_domain_id": "domain-a", "observation_id": "pod-health"}
CONTEXT = {"tenant_id": "tenant-a", "principal_id": "alice", "attempt_id": "attempt-1", "fence": "fence-1"}
BINDING = REQUEST | {"tenant_id": "tenant-a", "namespace": "application", "pod_name": "web-0",
    "tool": {"kind": "kubernetes-read", "namespace": "application", "allowed_operations": ["list-pods"],
             "endpoint": "https://cluster.invalid", "token_file": "/synthetic/no-token",
             "api_key_env": "SOVITE_TEST_READ_TOKEN"},
    "source_class": "C2", "output_class": "C1", "destination": "workbench",
    "output_contract_id": "pod-projection", "output_contract_revision": "sha256:" + "a" * 64}
RAW = {"kind": "PodList", "items": [{"metadata": {"name": "web-0", "namespace": "application",
        "labels": {"sensitive": "never-return"}}, "status": {"phase": "Running", "message": "private-message"},
        "spec": {"token": "never-return"}}]}


class Catalog:
    def __init__(self): self.binding = copy.deepcopy(BINDING)
    def resolve(self, request): return copy.deepcopy(self.binding)


class Runner:
    def __init__(self): self.calls = []; self.raw = copy.deepcopy(RAW); self.after = lambda: None
    async def execute_tool(self, tool, call, invocation_id):
        self.calls.append((tool, call, invocation_id)); self.after(); return copy.deepcopy(self.raw)


@pytest.fixture
def setup():
    catalog, runner, decisions = Catalog(), Runner(), []
    def authorize(context, binding, stage):
        decisions.append(("auth", context, binding, stage)); return True
    def release(context, binding, content, digest):
        decisions.append(("release", context, binding, content, digest)); return True
    return catalog, runner, decisions, ResourceGatewayRead(catalog, authorize, release, runner)


def read(gateway, request=REQUEST, context=CONTEXT):
    return asyncio.run(gateway.read(request, context=context))


def test_projection_is_exact_and_release_binds_bytes_and_current_context(setup):
    _, runner, decisions, gateway = setup
    result = read(gateway)
    assert result["observation"] == {"schema_version": "sovite-resource-pod-observation/v1", **REQUEST,
                                     "namespace": "application", "pod_name": "web-0", "phase": "Running"}
    release = next(row for row in decisions if row[0] == "release")
    assert release[1] == CONTEXT and release[2]["output_contract_revision"] == BINDING["output_contract_revision"]
    assert release[2]["destination"] == "workbench" and "tool" not in release[2]
    assert json.loads(release[3]) == result["observation"]
    assert release[4] == result["sha256"] == "sha256:" + hashlib.sha256(release[3]).hexdigest()
    assert "never-return" not in json.dumps(result) and "private-message" not in json.dumps(result)
    assert runner.calls[0][1] == {"operation": "list-pods", "arguments": {}}


@pytest.mark.parametrize("change", ["extra", "tenant", "workspace", "domain", "observation", "stale", "policy"])
def test_denials_happen_before_target_call(setup, change):
    catalog, runner, _, gateway = setup
    request, context = dict(REQUEST), dict(CONTEXT)
    if change == "extra": request["url"] = "https://bad.invalid"
    elif change == "tenant": context["tenant_id"] = "other"
    elif change in {"workspace", "domain", "observation"}:
        request[{"workspace": "workspace_id", "domain": "resource_domain_id", "observation": "observation_id"}[change]] = "other"
    elif change == "stale":
        context["fence"] = "old-fence"
        gateway.authorize = lambda supplied, _binding, _stage: supplied == CONTEXT
    else:
        gateway.authorize = lambda *_: False
    with pytest.raises(ResourceReadError): read(gateway, request, context)
    assert runner.calls == []


@pytest.mark.parametrize("field", ["output_contract_id", "output_contract_revision", "source_class", "output_class", "destination"])
def test_changed_binding_blocks_disclosure(setup, field):
    catalog, runner, _, gateway = setup
    replacement = "sha256:" + "b" * 64 if field == "output_contract_revision" else "changed"
    runner.after = lambda: catalog.binding.__setitem__(field, replacement)
    with pytest.raises(ResourceReadError, match="binding_changed"): read(gateway)
    assert len(runner.calls) == 1


@pytest.mark.parametrize("field", ["name", "namespace"])
def test_wrong_target_never_releases(setup, field):
    _, runner, decisions, gateway = setup
    runner.raw["items"][0]["metadata"][field] = "wrong"
    with pytest.raises(ResourceReadError, match="observation_unavailable"): read(gateway)
    assert not any(row[0] == "release" for row in decisions)


def test_release_denial_and_late_revocation_hide_upstream(setup):
    _, _, _, gateway = setup
    gateway.release = lambda *_: False
    with pytest.raises(ResourceReadError, match="release_denied") as error: read(gateway)
    assert "private-message" not in str(error.value)
    gateway.release = lambda *_: True
    gateway.authorize = lambda _c, _b, stage: stage == "before_read"
    with pytest.raises(ResourceReadError, match="authorization_denied"): read(gateway)


def test_malformed_target_error_is_generic(setup):
    _, runner, _, gateway = setup
    runner.raw = {"kind": "PodList", "items": [{"private": "never-return"}]}
    with pytest.raises(ResourceReadError, match="observation_unavailable") as error: read(gateway)
    assert "never-return" not in str(error.value)


def test_existing_kubernetes_adapter_uses_fixed_target_without_returning_credential(setup, monkeypatch):
    import httpx
    from sovite.adapters import AdapterRunner
    catalog, _, decisions, gateway = setup
    monkeypatch.setenv("SOVITE_TEST_READ_TOKEN", "synthetic-token")
    catalog.binding["tool"].update(endpoint="https://cluster.invalid", api_key_env="SOVITE_TEST_READ_TOKEN")
    calls = []
    def target(request):
        calls.append(request)
        return httpx.Response(200, json=RAW)
    gateway.runner = AdapterRunner(transport=httpx.MockTransport(target))
    result = read(gateway)
    assert len(calls) == 1
    assert calls[0].method == "GET" and str(calls[0].url) == "https://cluster.invalid/api/v1/namespaces/application/pods"
    assert calls[0].headers["authorization"] == "Bearer synthetic-token"
    assert "synthetic-token" not in json.dumps(result)
    assert "synthetic-token" not in repr(next(row for row in decisions if row[0] == "release"))


def test_revocation_during_release_denies_disclosure(setup):
    _, _, _, gateway = setup
    authorized = {"allowed": True}
    gateway.authorize = lambda *_: authorized["allowed"]
    def revoke(*_):
        authorized["allowed"] = False
        return True
    gateway.release = revoke
    with pytest.raises(ResourceReadError, match="authorization_denied"): read(gateway)


def test_release_requires_literal_true_and_hides_error(setup):
    _, _, _, gateway = setup
    gateway.release = lambda *_: 1
    with pytest.raises(ResourceReadError, match="release_denied"): read(gateway)
    def broken(*_): raise ValueError("secret-upstream-data")
    gateway.release = broken
    with pytest.raises(ResourceReadError) as error: read(gateway)
    assert str(error.value) == "release_denied"


def test_literal_credential_in_catalog_tool_is_rejected_before_target(setup):
    catalog, runner, _, gateway = setup
    catalog.binding["tool"]["credential"] = "secret-value"
    with pytest.raises(ResourceReadError, match="binding_unavailable") as error: read(gateway)
    assert runner.calls == [] and "secret-value" not in str(error.value)


def test_contract_revocation_during_release_blocks_return(setup):
    catalog, _, _, gateway = setup
    def change_contract(*_):
        catalog.binding["output_contract_revision"] = "sha256:" + "b" * 64
        return True
    gateway.release = change_contract
    with pytest.raises(ResourceReadError, match="binding_changed"): read(gateway)


@pytest.mark.parametrize("endpoint", [None, "http://cluster.invalid", "http://bob:literal-secret@cluster.invalid",
    "https://bob:literal-secret@cluster.invalid", "https://cluster.invalid/path", "https://cluster.invalid?x=1",
    "https://cluster.invalid#fragment", "https://cluster.invalid:", "https://cluster.invalid:0",
    "https://cluster.invalid:65536", "https://", "https://cluster.invalid\n", "https://cluster.invalid ",
    "https://cluster.invalid\\evil", "https://cluster.invalid?", "https://cluster.invalid#"])
def test_invalid_endpoint_is_refused_before_any_http_or_credential_read(setup, monkeypatch, endpoint):
    import httpx
    from sovite.adapters import AdapterRunner
    catalog, _, _, gateway = setup
    if endpoint is None: del catalog.binding["tool"]["endpoint"]
    else: catalog.binding["tool"]["endpoint"] = endpoint
    catalog.binding["tool"]["api_key_env"] = "SOVITE_TEST_READ_TOKEN"
    monkeypatch.setenv("SOVITE_TEST_READ_TOKEN", "synthetic-token")
    calls = []
    gateway.runner = AdapterRunner(transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200, json=RAW)))
    with pytest.raises(ResourceReadError, match="binding_unavailable") as error: read(gateway)
    assert calls == []
    assert "literal-secret" not in str(error.value) and "synthetic-token" not in str(error.value)


@pytest.mark.parametrize("field,value", [
    ("id", 4), ("api_key_env", []), ("api_key_env", "BAD=VALUE"),
    ("token_file", None), ("token_file", "relative"), ("token_file", "/tmp/contains\nnewline"),
    ("ca_file", False), ("ca_file", "relative"), ("timeout_seconds", True),
    ("timeout_seconds", 0), ("timeout_seconds", -1), ("timeout_seconds", 301),
    ("timeout_seconds", 1.5), ("timeout_seconds", float("inf")),
])
def test_malformed_tool_config_refuses_before_target(setup, field, value):
    catalog, runner, _, gateway = setup
    catalog.binding["tool"][field] = value
    with pytest.raises(ResourceReadError, match="binding_unavailable"): read(gateway)
    assert runner.calls == []


def test_missing_explicit_credential_reference_refuses_before_target(setup):
    catalog, runner, _, gateway = setup
    del catalog.binding["tool"]["token_file"]
    with pytest.raises(ResourceReadError, match="binding_unavailable"): read(gateway)
    assert runner.calls == []


def test_missing_explicit_environment_reference_refuses_before_target(setup):
    catalog, runner, _, gateway = setup
    del catalog.binding["tool"]["api_key_env"]
    with pytest.raises(ResourceReadError, match="binding_unavailable"): read(gateway)
    assert runner.calls == []


@pytest.mark.parametrize("endpoint", ["https://cluster.invalid/", "https://cluster.invalid:6443", "https://[::1]:6443/"])
def test_valid_explicit_https_origin_is_accepted(setup, endpoint):
    catalog, runner, _, gateway = setup
    catalog.binding["tool"]["endpoint"] = endpoint
    assert read(gateway)["observation"]["phase"] == "Running"
    assert len(runner.calls) == 1
