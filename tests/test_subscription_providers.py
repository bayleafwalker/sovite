from __future__ import annotations

import asyncio
import copy
import fcntl
import json
import os
import signal
import sys
import time
from pathlib import Path

import httpx
import pytest

from sovite import policy
from sovite.adapters import AdapterRunner, RuntimeFailure, codex_subscription_auth

ROOT = Path(__file__).parents[1]


def provider_bundle(adapter="codex-subscription", *, enforced=False, external=False):
    bundle = policy.load_bundle(ROOT / "config/platform.local.yaml")[0]
    bundle.update(apiVersion="sovite.platform/v1alpha2", restricted_class="C3",
                  connector_roles=[], mode="enforced" if enforced else "development")
    retention = {"prompt_logging": False, "response_logging": False, "persistent_context": False,
                 "in_memory_cache": {"scope": "job", "max_seconds": 300,
                                     "clear_on_cancel": True, "clear_on_session_close": True}}
    for destination in bundle["destinations"]:
        destination.update(trust_tier="operator-owned", retention=copy.deepcopy(retention))
        if enforced:
            destination["allowed_hosts"] = ["native.internal"]
    hosts = {"codex-subscription": ["chatgpt.com", "auth.openai.com"],
             "claude-subscription": ["api.anthropic.com"], "opencode-go": ["opencode.ai"]}[adapter]
    bundle["destinations"].append({"id": "public-provider", "kind": "cloud", "boundary": "public",
        "clearance": "C0", "allowed_hosts": hosts, "trust_tier": "provider", "retention": copy.deepcopy(retention)})
    model = {"id": bundle["models"][0]["id"], "adapter": adapter, "enabled": True,
             "destination": "public-provider", "model": {"codex-subscription": "gpt-6.1-sol",
             "claude-subscription": "claude-sonnet-5", "opencode-go": "deepseek-v4.1-flash"}[adapter],
             "identity_protocol": "provider-service" if adapter == "opencode-go" else "native-subscription"}
    native = {"codex-subscription": "native-codex", "claude-subscription": "native-claude", "opencode-go": "native-opencode"}[adapter]
    harness = {"id": bundle["harnesses"][0]["id"], "kind": "agent-http" if external else native,
               "digest": "sha256:" + "a" * 64}
    if external:
        harness.update(endpoint="http://native.internal/run", capabilities=[native], idempotent=True)
    if adapter == "opencode-go":
        model.update(endpoint=policy.GO_ENDPOINT, wire_api="chat", api_key_env="TEST_GO_KEY")
    else:
        harness["authentication"] = {"mode": "subscription",
            ("credential_file_env" if adapter == "codex-subscription" else "credential_env"):
            ("TEST_CODEX_AUTH_FILE" if adapter == "codex-subscription" else "TEST_CLAUDE_TOKEN")}
    bundle["models"] = [model]
    bundle["harnesses"] = [harness]
    for grant in bundle["grants"]:
        grant.update(input_class="C0", output_class="C0")
    return bundle


def recipe(adapter="codex-subscription", **kwargs):
    bundle = provider_bundle(adapter, **kwargs)
    return policy.resolve_recipe(policy.validate_bundle(bundle), bundle["grants"][0]["id"])


def auth_bytes(access="access", refresh="refresh"):
    return json.dumps({"auth_mode": "chatgpt", "OPENAI_API_KEY": None,
                       "tokens": {"access_token": access, "refresh_token": refresh,
                                  "id_token": "id", "account_id": "account"}}).encode()


def provision_auth(monkeypatch, tmp_path):
    source = tmp_path / "subscription-auth.json"
    source.write_bytes(auth_bytes())
    source.chmod(0o600)
    monkeypatch.setenv("TEST_CODEX_AUTH_FILE", str(source))
    return source


@pytest.mark.parametrize("adapter", ["codex-subscription", "claude-subscription", "opencode-go"])
@pytest.mark.parametrize("external", [False, True])
def test_provider_bindings_validate_without_fake_weights_digest(adapter, external):
    bundle = provider_bundle(adapter, enforced=True, external=external)
    policy.validate_bundle(bundle)
    identity = asyncio.run(AdapterRunner().verify_model_identity(bundle["models"][0], True))
    assert identity["immutable_weights_verified"] is False
    assert identity["model"] == bundle["models"][0]["model"]


@pytest.mark.parametrize("adapter", ["codex-subscription", "claude-subscription", "opencode-go"])
@pytest.mark.parametrize("change", ["private", "digest"])
def test_public_provider_cannot_be_declared_private_or_immutable(adapter, change):
    bundle = provider_bundle(adapter)
    if change == "private":
        bundle["destinations"][-1]["boundary"] = "private"
    else:
        bundle["models"][0]["digest"] = "sha256:" + "b" * 64
    with pytest.raises(policy.PolicyError):
        policy.validate_bundle(bundle)


@pytest.mark.parametrize("field,value", [("endpoint", "https://api.openai.com/v1"),
                                         ("api_key_env", "OPENAI_API_KEY"),
                                         ("wire_api", "responses"),
                                         ("identity_endpoint", "https://api.openai.com")])
def test_subscription_binding_rejects_api_route_substitution(field, value):
    bundle = provider_bundle()
    bundle["models"][0][field] = value
    with pytest.raises(policy.PolicyError):
        policy.validate_bundle(bundle)


def test_external_native_capabilities_and_authentication_are_unambiguous():
    bundle = provider_bundle(external=True)
    bundle["harnesses"][0]["capabilities"].append("native-claude")
    with pytest.raises(policy.PolicyError):
        policy.validate_bundle(bundle)
    bundle = provider_bundle()
    bundle["harnesses"][0]["authentication"]["credential_env"] = "WRONG_TOKEN"
    with pytest.raises(policy.PolicyError):
        policy.validate_bundle(bundle)


@pytest.mark.parametrize("adapter", ["codex-subscription", "claude-subscription", "opencode-go"])
def test_enforced_native_execution_requires_separate_isolation(adapter):
    with pytest.raises(RuntimeFailure, match="native_process_requires_external_isolation"):
        asyncio.run(AdapterRunner().run_native(recipe(adapter, enforced=True), "task", "job"))


@pytest.mark.parametrize("failure", [None, "failure", "cancel"])
def test_codex_uses_explicit_subscription_and_persists_refresh(monkeypatch, tmp_path, failure):
    source = provision_auth(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "AMBIENT-MUST-NOT-BE-FORWARDED")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "AMBIENT-MUST-NOT-BE-FORWARDED")
    homes = []
    async def fake(self, command, *, cwd, env, prompt, capture=True):
        assert command[command.index("--model") + 1] == "gpt-6.1-sol"
        assert command[command.index("--sandbox") + 1] == "read-only"
        assert "OPENAI_API_KEY" not in env and "ANTHROPIC_API_KEY" not in env
        assert "model_provider=\"openai\"" in command and "forced_login_method=\"chatgpt\"" in command
        assert "features.shell_tool=false" in command and "features.apps=false" in command
        assert not any("model_providers.sovite" in item for item in command)
        home = Path(env["CODEX_HOME"])
        homes.append(home)
        assert (home / "auth.json").read_bytes() == auth_bytes()
        (home / "auth.json").write_bytes(auth_bytes("rotated-access", "rotated-refresh"))
        Path(command[command.index("--output-last-message") + 1]).write_text("answer")
        if failure == "failure":
            raise RuntimeFailure("native_runtime_failed")
        if failure == "cancel":
            raise asyncio.CancelledError
        return b""
    monkeypatch.setattr(AdapterRunner, "native_process", fake)
    if failure:
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else RuntimeFailure):
            asyncio.run(AdapterRunner().run_native(recipe(), "task", "job"))
    else:
        result = asyncio.run(AdapterRunner().run_native(recipe(), "task", "job"))
        assert result["answer"] == "answer"
    assert source.read_bytes() == auth_bytes("rotated-access", "rotated-refresh")
    assert source.stat().st_mode & 0o077 == 0
    assert all(not home.exists() for home in homes)
    with codex_subscription_auth(recipe()["harness"], tmp_path / "retry"):
        pass


@pytest.mark.parametrize("defect", ["missing", "api", "symlink", "permissions", "oversize", "relative"])
def test_codex_rejects_unusable_or_ambient_credential_profiles(monkeypatch, tmp_path, defect):
    source = provision_auth(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "MUST-NOT-BE-USED")
    if defect == "missing":
        monkeypatch.delenv("TEST_CODEX_AUTH_FILE")
    elif defect == "api":
        source.write_text('{"auth_mode":"apikey","OPENAI_API_KEY":"not-a-subscription"}')
    elif defect == "symlink":
        link = tmp_path / "link.json"
        link.symlink_to(source)
        monkeypatch.setenv("TEST_CODEX_AUTH_FILE", str(link))
    elif defect == "permissions":
        source.chmod(0o644)
    elif defect == "oversize":
        source.write_bytes(b" " * 65537)
    elif defect == "relative":
        monkeypatch.setenv("TEST_CODEX_AUTH_FILE", "auth.json")
    with pytest.raises(RuntimeFailure, match="subscription_credential"):
        with codex_subscription_auth(recipe()["harness"], tmp_path / "native"):
            pytest.fail("bad credentials reached native execution")


def test_codex_profile_lock_and_concurrent_rotation_are_preserved(monkeypatch, tmp_path):
    source = provision_auth(monkeypatch, tmp_path)
    harness = recipe()["harness"]
    with codex_subscription_auth(harness, tmp_path / "first"):
        with pytest.raises(RuntimeFailure, match="subscription_profile_busy"):
            with codex_subscription_auth(harness, tmp_path / "second"):
                pass
    with pytest.raises(RuntimeFailure, match="subscription_credential_changed"):
        with codex_subscription_auth(harness, tmp_path / "third"):
            (tmp_path / "third/auth.json").write_bytes(auth_bytes("new", "new"))
            source.write_bytes(auth_bytes("external", "external"))
    assert source.read_bytes() == auth_bytes("external", "external")


def test_claude_uses_only_explicit_subscription_token_and_no_tools(monkeypatch):
    monkeypatch.setenv("TEST_CLAUDE_TOKEN", "EXPLICIT-OAUTH")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "AMBIENT-API")
    seen = []
    async def fake(self, command, *, cwd, env, prompt, capture=True):
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "EXPLICIT-OAUTH"
        assert "ANTHROPIC_API_KEY" not in env
        assert command[command.index("--model") + 1] == "claude-sonnet-5"
        assert command[command.index("--tools") + 1] == ""
        assert "--bare" in command and "--strict-mcp-config" in command
        assert command[command.index("--mcp-config") + 1] == '{"mcpServers":{}}'
        seen.append(Path(env["HOME"]))
        return b'{"type":"result","subtype":"success","is_error":false,"result":"answer"}'
    monkeypatch.setattr(AdapterRunner, "native_process", fake)
    result = asyncio.run(AdapterRunner().run_native(recipe("claude-subscription"), "task", "job"))
    assert result["answer"] == "answer"
    assert all(not path.exists() for path in seen)


def test_claude_does_not_use_ambient_oauth_or_api_auth(monkeypatch):
    monkeypatch.delenv("TEST_CLAUDE_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "AMBIENT-OAUTH")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "AMBIENT-API")
    with pytest.raises(RuntimeFailure, match="subscription_credential_missing"):
        asyncio.run(AdapterRunner().run_native(recipe("claude-subscription"), "task", "job"))


@pytest.mark.parametrize("body", [b"not json", b'{"subtype":"error","result":"private"}',
                                   b'{"subtype":"success","is_error":true,"result":"private"}',
                                   b'{"subtype":"success","result":""}'])
def test_claude_rejects_error_or_empty_results(monkeypatch, body):
    monkeypatch.setenv("TEST_CLAUDE_TOKEN", "explicit")
    async def fake(self, *args, **kwargs):
        return body
    monkeypatch.setattr(AdapterRunner, "native_process", fake)
    with pytest.raises(RuntimeFailure):
        asyncio.run(AdapterRunner().run_native(recipe("claude-subscription"), "task", "job"))


def test_native_opencode_uses_go_provider_and_explicit_tool_denial(monkeypatch):
    monkeypatch.setenv("TEST_GO_KEY", "EXPLICIT-GO")
    monkeypatch.setenv("OPENAI_API_KEY", "AMBIENT-API")
    seen = []
    async def fake(self, command, *, cwd, env, prompt, capture=True):
        assert env["SOVITE_GO_CREDENTIAL"] == "EXPLICIT-GO"
        assert "OPENAI_API_KEY" not in env
        assert "--standalone" in command
        assert command[command.index("--model") + 1] == "opencode-go/deepseek-v4.1-flash"
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        provider = config["providers"]["opencode-go"]
        assert provider["settings"]["baseURL"] == policy.GO_ENDPOINT
        assert provider["headers"]["x-opencode-session"] == "job"
        assert set(config["providers"]) == {"opencode-go"}
        assert config["policies"] == [
            {"action":"provider.use","resource":"*","effect":"deny"},
            {"action":"provider.use","resource":"opencode-go","effect":"allow"},
            {"action":"tool.use","resource":"*","effect":"deny"},
            {"action":"integration.use","resource":"*","effect":"deny"}]
        assert all(agent["model"] == config["model"] for agent in config["agents"].values())
        assert all(agent["permissions"] == [{"action":"*","resource":"*","effect":"deny"}] for agent in config["agents"].values())
        assert config["mcp"] == {"servers": {}} and config["plugins"] == []
        assert config["warming"] is False and config["update"] == "disable"
        seen.append(Path(env["HOME"]))
        return b'{"type":"text","part":{"text":"answer"}}\n{"type":"step_finish","part":{"reason":"stop"}}\n'
    monkeypatch.setattr(AdapterRunner, "native_process", fake)
    assert asyncio.run(AdapterRunner().run_native(recipe("opencode-go"), "task", "job"))["answer"] == "answer"
    assert all(not path.exists() for path in seen)


@pytest.mark.parametrize("output", [b"not json", b'{"type":"error","message":"private"}',
    b'{"type":"text","part":{"text":"partial"}}',
    b'{"type":"text","part":{"text":"partial"}}\n{"type":"step_finish","part":{"reason":"length"}}'])
def test_native_opencode_never_calls_partial_or_error_output_complete(monkeypatch, output):
    monkeypatch.setenv("TEST_GO_KEY", "explicit")
    async def fake(self, *args, **kwargs):
        return output
    monkeypatch.setattr(AdapterRunner, "native_process", fake)
    with pytest.raises(RuntimeFailure):
        asyncio.run(AdapterRunner().run_native(recipe("opencode-go"), "task", "job"))


@pytest.mark.parametrize("wrong_model", [False, True])
def test_go_chat_pins_route_model_and_per_job_headers(monkeypatch, wrong_model):
    monkeypatch.setenv("TEST_GO_KEY", "explicit")
    resolved = recipe("opencode-go")
    resolved["harness"]["kind"] = "tool-agent"
    requests = []
    async def respond(request):
        requests.append(request)
        payload = json.loads(request.content)
        assert payload["model"] == "deepseek-v4.1-flash" and payload["store"] is False
        assert str(request.url) == policy.GO_ENDPOINT + "/chat/completions"
        assert request.headers["x-opencode-session"] == "job"
        assert request.headers["user-agent"] == "sovite-platform/opencode-go"
        assert request.headers["authorization"] == "Bearer explicit"
        return httpx.Response(200, json={"model": "other" if wrong_model else payload["model"],
            "choices":[{"finish_reason":"stop","message":{"content":"answer"}}]})
    operation = AdapterRunner(httpx.MockTransport(respond)).tool_agent(resolved, "task", "job")
    if wrong_model:
        with pytest.raises(RuntimeFailure, match="model_identity_mismatch"):
            asyncio.run(operation)
    else:
        assert asyncio.run(operation)["answer"] == "answer"
    assert len(requests) == 1


@pytest.mark.parametrize("leader_exits", [False, True])
def test_native_cancellation_terminates_children_even_after_leader_exit(tmp_path, leader_exits):
    heartbeat = tmp_path / "heartbeat"
    child = "import signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); p=pathlib.Path(" + repr(str(heartbeat)) + ")\nwhile True:\n p.write_text(str(time.time_ns())); time.sleep(0.02)\n"
    leader = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); " + ("sys.exit(0)" if leader_exits else "time.sleep(60)")
    async def scenario():
        runner = AdapterRunner()
        env = runner.native_environment(tmp_path, tmp_path / "home")
        operation = asyncio.create_task(runner.native_process([sys.executable, "-c", leader], cwd=tmp_path, env=env, prompt=""))
        for _ in range(150):
            if heartbeat.exists():
                break
            await asyncio.sleep(0.02)
        assert heartbeat.exists()
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(operation, 5)
        await asyncio.sleep(0.1)
        last = heartbeat.read_text()
        await asyncio.sleep(0.15)
        assert heartbeat.read_text() == last
    asyncio.run(scenario())


def test_native_stdout_limit_is_enforced_on_real_subprocess(tmp_path):
    runner = AdapterRunner()
    env = runner.native_environment(tmp_path, tmp_path / "home")
    with pytest.raises(RuntimeFailure, match="native_result_too_large"):
        asyncio.run(runner.native_process([sys.executable, "-c", "import sys; sys.stdout.write('x'*(4*1024*1024+1))"],
            cwd=tmp_path, env=env, prompt=""))
