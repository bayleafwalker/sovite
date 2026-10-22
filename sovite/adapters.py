"""Explicit runtime adapters. A failed model is never replaced by another model."""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import signal
import stat
import ssl
import tempfile
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

from .policy import GO_ENDPOINT, native_harness_kind


class RuntimeFailure(Exception):
    def __init__(self, code: str, retryable: bool = False):
        super().__init__(code)
        self.code, self.retryable = code, retryable


def endpoint_url(endpoint: str, suffix: str) -> str:
    return endpoint.rstrip("/") if endpoint.rstrip("/").endswith(suffix) else endpoint.rstrip("/") + suffix


def require_model_adapter(model: dict) -> None:
    # EXACT_BINDING, PIN_BEFORE_PROMPT: no direct fallback while the gateway is unavailable.
    if model.get("connection", {}).get("kind") == "model-gateway":
        raise RuntimeFailure("model_gateway_adapter_unavailable")


def model_trust_path(model: dict) -> Path | None:
    name = model.get("tls_trust")
    if not name:
        return None
    path = Path(os.environ.get("SOVITE_MODEL_TRUST_DIR", "/model-trust")) / f"{name}.pem"
    if not path.is_file():
        raise RuntimeFailure("model_trust_missing")
    return path


def model_verify(model: dict) -> ssl.SSLContext | bool:
    # A pinned anchor replaces, not extends, the system roots for this model.
    path = model_trust_path(model)
    if path is None:
        return True
    try:
        return ssl.create_default_context(cafile=str(path))
    except (OSError, ssl.SSLError):
        raise RuntimeFailure("model_trust_missing") from None


def token_headers(config: dict) -> dict[str, str]:
    env_name = config.get("api_key_env")
    if not env_name:
        return {}
    value = os.environ.get(env_name)
    if not value:
        raise RuntimeFailure("adapter_credential_missing")
    return {"Authorization": f"Bearer {value}"}


def subscription_token(harness: dict) -> str:
    auth = harness.get("authentication", {})
    name = auth.get("credential_env")
    value = os.environ.get(name, "") if isinstance(name, str) else ""
    if auth.get("mode") != "subscription" or not value:
        raise RuntimeFailure("subscription_credential_missing")
    return value


def _chatgpt_auth(raw: bytes) -> dict:
    try:
        auth = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise RuntimeFailure("subscription_credential_invalid") from None
    tokens = auth.get("tokens") if isinstance(auth, dict) else None
    if not isinstance(auth, dict) or auth.get("auth_mode") != "chatgpt" or auth.get("OPENAI_API_KEY") or not isinstance(tokens, dict):
        raise RuntimeFailure("subscription_credential_invalid")
    if any(not isinstance(tokens.get(key), str) or not tokens[key].strip() for key in ("access_token", "refresh_token")):
        raise RuntimeFailure("subscription_credential_invalid")
    return auth


def _credential_bytes(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise RuntimeFailure("subscription_credential_invalid")
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise RuntimeFailure("subscription_credential_invalid")
    return raw


@contextlib.contextmanager
def codex_subscription_auth(harness: dict, native_home: Path):
    """Use only an explicit file profile and retain refreshes across all outcomes."""
    auth = harness.get("authentication", {})
    name = auth.get("credential_file_env")
    value = os.environ.get(name, "") if isinstance(name, str) else ""
    if auth.get("mode") != "subscription" or not value or not Path(value).is_absolute():
        raise RuntimeFailure("subscription_credential_missing")
    source = Path(value)
    lock_fd = None
    temporary = None
    try:
        lock_fd = os.open(source.with_name(source.name + ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            raise RuntimeFailure("subscription_credential_invalid")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeFailure("subscription_profile_busy", True) from None
        original = _credential_bytes(source)
        _chatgpt_auth(original)
        native_home.mkdir(parents=True, exist_ok=True)
        target = native_home / "auth.json"
        target.write_bytes(original)
        target.chmod(0o600)
        try:
            yield
        finally:
            refreshed = _credential_bytes(target)
            _chatgpt_auth(refreshed)
            if refreshed != original:
                if _credential_bytes(source) != original:
                    raise RuntimeFailure("subscription_credential_changed")
                fd, temporary = tempfile.mkstemp(prefix="." + source.name + ".", dir=source.parent)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(refreshed)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, source)
                temporary = None
    except OSError:
        raise RuntimeFailure("subscription_credential_unavailable") from None
    finally:
        if temporary is not None:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
        if lock_fd is not None:
            os.close(lock_fd)


class AdapterRunner:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self.transport = transport
        self.authorization_check = None

    def client(self, *, timeout=60, verify=True) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=httpx.Timeout(timeout), follow_redirects=False, trust_env=False, transport=self.transport, verify=verify)

    async def request_json(self, method: str, url: str, *, payload=None, headers=None, timeout=60, verify=True) -> dict:
        if self.authorization_check is not None:
            self.authorization_check()
        try:
            async with self.client(timeout=timeout, verify=verify) as client:
                async with client.stream(method, url, json=payload, headers=headers or {}) as response:
                    if response.status_code in {408, 425, 429, 502, 503, 504}:
                        raise RuntimeFailure("upstream_unavailable", True)
                    if response.status_code >= 300:
                        raise RuntimeFailure("upstream_rejected")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 4 * 1024 * 1024:
                            raise RuntimeFailure("upstream_response_too_large")
                    result = json.loads(data)
                    if not isinstance(result, dict):
                        raise RuntimeFailure("upstream_protocol_error")
                    return result
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
            # A peer that disconnects mid-response (a crashed or killed service) is an outage too;
            # callers with external effects turn it into execution_uncertain.
            raise RuntimeFailure("upstream_unavailable", True) from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise RuntimeFailure("upstream_protocol_error") from None

    async def run(self, recipe: dict, task: str, job_id: str) -> dict:
        harness, model = recipe["harness"], recipe["model"]
        require_model_adapter(model)
        kind = harness["kind"]
        if kind == "demo":
            if model["adapter"] != "demo":
                raise RuntimeFailure("incompatible_demo_configuration")
            # Deliberately synthetic; useful to assess the console and release mechanism.
            # This is neither a production diagnostic nor an implicit fallback.
            await asyncio.sleep(float(model.get("parameters", {}).get("delay_seconds", 0.1)))
            return {"kind": "synthetic-assessment", "synthetic": True, "task": task, "answer": "Synthetic assessment. No production system or model was contacted.", "agent_id": recipe["agent"]["id"], "model_id": model["id"], "generation": recipe["generation"], "observations": ["Result access is governed by the installed " + recipe["grant"].get("return_contract", "reference") + " contract, output class and current reader authority."]}
        if kind in {"native-codex", "native-claude", "native-opencode"}:
            return await self.run_native(recipe, task, job_id)
        if kind == "agent-http":
            return await self.agent_http(recipe, task, job_id)
        if kind != "tool-agent":
            raise RuntimeFailure("unsupported_harness")
        await self.verify_model_identity(model, recipe.get("mode") == "enforced")
        return await self.tool_agent(recipe, task, job_id)

    async def verify_model_identity(self, model: dict, enforced: bool = False) -> dict:
        require_model_adapter(model)
        if model["adapter"] in {"codex-subscription", "claude-subscription"}:
            if model.get("identity_protocol") != "native-subscription" or model.get("digest"):
                raise RuntimeFailure("model_identity_protocol_unsupported")
            return {"status": "native-provider-selection", "provider": model["adapter"],
                    "model": model["model"], "immutable_weights_verified": False}
        if model["adapter"] == "opencode-go":
            if model.get("identity_protocol") != "provider-service" or model.get("digest") or model.get("endpoint", "").rstrip("/") not in {GO_ENDPOINT, GO_ENDPOINT + "/chat/completions"}:
                raise RuntimeFailure("model_identity_protocol_unsupported")
            return {"status": "declared-provider-service", "provider": "opencode-go",
                    "model": model["model"], "endpoint": GO_ENDPOINT, "immutable_weights_verified": False}
        expected = model.get("digest")
        if not expected:
            if enforced:
                raise RuntimeFailure("model_identity_unpinned")
            return {"status": "unpinned-development"}
        protocol = model.get("identity_protocol", "ollama" if model["adapter"] == "ollama" else None)
        if protocol != "ollama":
            raise RuntimeFailure("model_identity_protocol_unsupported")
        base = model.get("identity_endpoint", model.get("endpoint", "").removesuffix("/v1"))
        result = await self.request_json("GET", endpoint_url(base, "/api/tags"), headers=token_headers(model), timeout=15, verify=model_verify(model))
        models = result.get("models", [])
        for candidate in models if isinstance(models, list) else []:
            if candidate.get("name") in {model["model"], model["model"] + ":latest"}:
                actual = candidate.get("digest", "")
                actual = actual if actual.startswith("sha256:") else "sha256:" + actual
                if actual != expected:
                    raise RuntimeFailure("model_identity_mismatch")
                return {"status": "verified", "digest": actual}
        raise RuntimeFailure("model_identity_missing")

    async def agent_http(self, recipe: dict, task: str, job_id: str) -> dict:
        require_model_adapter(recipe.get("model", {}))
        harness = recipe["harness"]
        endpoint = harness.get("endpoint")
        if not endpoint:
            raise RuntimeFailure("agent_endpoint_missing")
        try:
            result = await self.request_json("POST", endpoint, payload={"task_id": job_id, "agent_id": recipe["agent"]["id"], "configuration_revision": recipe["generation"], "input": {"task": task}}, headers={**token_headers(harness), "Idempotency-Key": job_id}, timeout=int(recipe["grant"].get("timeout_seconds", 300)))
        except RuntimeFailure as exc:
            if exc.retryable and not harness.get("idempotent", False):
                raise RuntimeFailure("external_execution_uncertain", False) from None
            raise
        except asyncio.CancelledError:
            cancel_endpoint = harness.get("cancel_endpoint")
            if cancel_endpoint:
                # This is best-effort remote cancellation. Release authorization is
                # checked locally even when a disconnected runtime cannot be stopped.
                try:
                    async with self.client(timeout=5) as client:
                        await client.post(cancel_endpoint, json={"task_id": job_id, "agent_id": recipe["agent"]["id"], "configuration_revision": recipe["generation"]}, headers=token_headers(harness))
                except Exception:
                    pass
            raise
        return {"kind": "external-agent-result", "agent_id": recipe["agent"]["id"], "generation": recipe["generation"], "result": result}

    def tool_schema(self, tool: dict) -> dict:
        operations = tool.get("allowed_operations", [])
        # Advertise exactly what execution accepts: Kubernetes reads take no arguments
        # because configuration fixes the namespace and resource paths.
        arguments = {"type": "object", "maxProperties": 0, "description": "Must be empty; the namespace is fixed by configuration."} if tool["kind"] == "kubernetes-read" else {"type": "object", "additionalProperties": True}
        return {"type": "function", "function": {"name": tool["id"], "description": tool.get("description", "Read scoped operational information using an approved operation."), "parameters": {"type": "object", "properties": {"operation": {"type": "string", "enum": operations}, "arguments": arguments}, "required": ["operation"], "additionalProperties": False}}}

    async def tool_agent(self, recipe: dict, task: str, job_id: str) -> dict:
        agent, model = recipe["agent"], recipe["model"]
        require_model_adapter(model)
        tools = {tool["id"]: tool for tool in recipe.get("tools", [])}
        messages: list[dict] = [{"role": "system", "content": agent.get("instructions", "Complete the task using only approved tools. Report uncertainty and cite observed evidence.")}, {"role": "user", "content": task}]
        trace: list[dict] = []
        for step in range(min(int(agent.get("max_steps", 8)), 20)):
            payload: dict = {"model": model["model"], "messages": messages, "stream": False}
            if tools:
                payload["tools"] = [self.tool_schema(tool) for tool in tools.values()]
            parameters = model.get("parameters", {})
            finish_reason = None
            if model["adapter"] == "ollama":
                payload["options"] = {k: v for k, v in parameters.items() if k in {"temperature", "num_ctx", "num_predict", "seed", "top_p", "top_k"}}
                if "think" in parameters:
                    payload["think"] = parameters["think"]
                result = await self.request_json("POST", endpoint_url(model["endpoint"], "/api/chat"), payload=payload, headers=token_headers(model), timeout=int(model.get("timeout_seconds", 120)), verify=model_verify(model))
                message = result.get("message")
                finish_reason = result.get("done_reason")
            elif model["adapter"] in {"openai-compatible", "opencode-go"}:
                if model.get("wire_api", "chat") != "chat":
                    raise RuntimeFailure("tool_agent_requires_chat_api")
                payload.update({k: v for k, v in parameters.items() if k in {"temperature", "max_tokens", "top_p", "seed", "reasoning_effort"}})
                headers = token_headers(model)
                if model["adapter"] == "opencode-go":
                    await self.verify_model_identity(model, recipe.get("mode") == "enforced")
                    headers.update({"x-opencode-session": job_id, "User-Agent": "sovite-platform/opencode-go"})
                    payload["store"] = False
                result = await self.request_json("POST", endpoint_url(model["endpoint"], "/chat/completions"), payload=payload, headers=headers, timeout=int(model.get("timeout_seconds", 120)), verify=model_verify(model))
                if model["adapter"] == "opencode-go" and result.get("model") != model["model"]:
                    raise RuntimeFailure("model_identity_mismatch")
                choices = result.get("choices", [])
                message = choices[0].get("message") if choices and isinstance(choices[0], dict) else None
                finish_reason = choices[0].get("finish_reason") if choices and isinstance(choices[0], dict) else None
            else:
                raise RuntimeFailure("unsupported_model_adapter")
            if not isinstance(message, dict):
                raise RuntimeFailure("model_protocol_error")
            if finish_reason == "length":
                raise RuntimeFailure("model_output_truncated")
            if finish_reason == "content_filter":
                raise RuntimeFailure("model_output_filtered")
            calls = message.get("tool_calls", [])
            if not isinstance(calls, list) or len(calls) > 8:
                raise RuntimeFailure("invalid_tool_calls")
            if not calls:
                content = message.get("content")
                if not isinstance(content, str):
                    raise RuntimeFailure("model_protocol_error")
                if not content.strip():
                    raise RuntimeFailure("model_empty_result")
                return {"kind": "agent-result", "finish_reason": finish_reason, "answer": content, "trace": trace, "model_id": model["id"], "agent_id": agent["id"], "generation": recipe["generation"], "steps": step + 1}
            messages.append({"role": "assistant", "content": message.get("content", ""), "tool_calls": calls})
            for index, call in enumerate(calls):
                function = call.get("function", {}) if isinstance(call, dict) else {}
                name = function.get("name")
                if name not in tools:
                    raise RuntimeFailure("undeclared_tool_requested")
                arguments = function.get("arguments", {})
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = None
                if not isinstance(arguments, dict) or set(arguments) - {"operation", "arguments"} or (tools[name]["kind"] == "kubernetes-read" and arguments.get("arguments")):
                    # Malformed calls are never executed; the model sees the expected shape and may
                    # correct itself within the step limit. Undeclared tools and denied operations stay fatal.
                    output = {"error": "invalid_tool_arguments", "expected": {"operation": tools[name].get("allowed_operations", []), "arguments": {}}}
                    if tools[name]["kind"] == "kubernetes-read":
                        output["hint"] = "Pass no arguments; the namespace is fixed by configuration."
                    trace.append({"tool": name, "operation": None, "result": output})
                else:
                    output = await self.execute_tool(tools[name], arguments, f"{job_id}:{step}:{index}")
                    trace.append({"tool": name, "operation": arguments.get("operation"), "result": output})
                tool_message: dict = {"role": "tool", "content": json.dumps(output, ensure_ascii=False), "name": name}
                if call.get("id"):
                    tool_message["tool_call_id"] = call["id"]
                messages.append(tool_message)
        raise RuntimeFailure("step_limit_reached")

    async def execute_tool(self, tool: dict, call: dict, invocation_id: str) -> dict:
        operation, arguments = call.get("operation"), call.get("arguments", {})
        if operation not in tool.get("allowed_operations", []) or not isinstance(arguments, dict):
            raise RuntimeFailure("tool_operation_denied")
        if tool["kind"] == "http-json":
            return await self.request_json("POST", tool["endpoint"], payload={"operation": operation, "arguments": arguments}, headers={**token_headers(tool), "Idempotency-Key": invocation_id}, timeout=int(tool.get("timeout_seconds", 30)))
        if tool["kind"] != "kubernetes-read":
            raise RuntimeFailure("unsupported_tool")
        # These paths are selected by trusted configuration. The model cannot choose
        # an arbitrary URL, API resource, namespace, HTTP verb, or service-account token.
        namespace = tool.get("namespace", "default")
        ns = quote(namespace, safe="")
        paths = {"list-pods": f"/api/v1/namespaces/{ns}/pods", "list-events": f"/api/v1/namespaces/{ns}/events", "list-services": f"/api/v1/namespaces/{ns}/services", "list-deployments": f"/apis/apps/v1/namespaces/{ns}/deployments"}
        if operation not in paths or arguments:
            raise RuntimeFailure("tool_operation_denied")
        token = os.environ.get(tool.get("api_key_env", "SOVITE_KUBERNETES_TOKEN"))
        if not token:
            try:
                token = Path(tool.get("token_file", "/var/run/secrets/kubernetes.io/serviceaccount/token")).read_text().strip()
            except OSError:
                raise RuntimeFailure("tool_credential_missing") from None
        ca = tool.get("ca_file", "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
        verify = ssl.create_default_context(cafile=ca) if Path(ca).exists() else True
        return await self.request_json("GET", tool.get("endpoint", "https://kubernetes.default.svc").rstrip("/") + paths[operation], headers={"Authorization": "Bearer " + token}, timeout=int(tool.get("timeout_seconds", 30)), verify=verify)


    @staticmethod
    def native_environment(work: Path, native_home: Path) -> dict[str, str]:
        native_home.mkdir(parents=True, exist_ok=True)
        env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
               "HOME": str(native_home), "LANG": "C.UTF-8", "TMPDIR": str(work)}
        for name, leaf in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                           ("XDG_STATE_HOME", "state"), ("XDG_CACHE_HOME", "cache")):
            path = native_home / leaf
            path.mkdir(parents=True, exist_ok=True)
            env[name] = str(path)
        return env

    async def native_process(self, command: list[str], *, cwd: str | Path,
                             env: dict[str, str], prompt: str, capture: bool = True) -> bytes:
        if self.authorization_check is not None:
            self.authorization_check()
        try:
            process = await asyncio.create_subprocess_exec(
                *command, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        except (OSError, ValueError):
            raise RuntimeFailure("native_runtime_unavailable") from None

        async def terminate():
            # A leader exit is not evidence that its owned process group is empty.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                pass
            finally:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            await process.wait()

        async def send():
            try:
                process.stdin.write(prompt.encode())
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()

        async def receive():
            if not capture:
                return b""
            output = bytearray()
            while chunk := await process.stdout.read(65536):
                output.extend(chunk)
                if len(output) > 4 * 1024 * 1024:
                    raise RuntimeFailure("native_result_too_large")
            return bytes(output)

        streams = [asyncio.create_task(send()), asyncio.create_task(receive())]
        try:
            _, output = await asyncio.gather(*streams)
            await process.wait()
            if process.returncode:
                raise RuntimeFailure("native_runtime_failed")
            return output
        except BaseException:
            for stream in streams:
                stream.cancel()
            await asyncio.gather(*streams, return_exceptions=True)
            await terminate()
            raise

    @staticmethod
    def native_result(recipe: dict, answer: str, kind: str, identity: dict, isolated: bool) -> dict:
        if not isinstance(answer, str) or not answer.strip():
            raise RuntimeFailure("native_empty_result")
        return {"kind": kind + "-result", "answer": answer, "agent_id": recipe["agent"]["id"],
                "model_id": recipe["model"]["id"], "generation": recipe["generation"],
                "model_identity": identity,
                "isolation": "separate harness service" if isolated else "development process boundary; not a production security isolation claim"}

    async def run_native(self, recipe: dict, task: str, job_id: str, *, isolated: bool = False) -> dict:
        kind = native_harness_kind(recipe["harness"])
        if kind == "native-codex":
            if recipe["model"]["adapter"] == "codex-subscription":
                return await self.native_codex_subscription(recipe, task, job_id, isolated=isolated)
            return await self.native_codex(recipe, task, job_id, isolated=isolated)
        if kind == "native-claude":
            return await self.native_claude(recipe, task, job_id, isolated=isolated)
        if kind == "native-opencode":
            return await self.native_opencode(recipe, task, job_id, isolated=isolated)
        raise RuntimeFailure("unsupported_harness")

    @staticmethod
    def native_subscription_guard(recipe: dict, adapter: str, isolated: bool):
        if recipe["model"]["adapter"] != adapter:
            raise RuntimeFailure("native_provider_mismatch")
        require_model_adapter(recipe["model"])
        if recipe.get("tools"):
            raise RuntimeFailure("native_tools_unsupported")
        if recipe.get("mode", "development") == "enforced" and not isolated:
            raise RuntimeFailure("native_process_requires_external_isolation")

    async def native_codex_subscription(self, recipe: dict, task: str, job_id: str, *, isolated: bool = False) -> dict:
        self.native_subscription_guard(recipe, "codex-subscription", isolated)
        harness, model = recipe["harness"], recipe["model"]
        identity = await self.verify_model_identity(model, recipe.get("mode") == "enforced")
        home_root = os.environ.get("SOVITE_NATIVE_HOME_ROOT")
        with tempfile.TemporaryDirectory(prefix="sovite-native-") as directory, \
                tempfile.TemporaryDirectory(prefix="sovite-native-home-", dir=home_root) if home_root else contextlib.nullcontext(None) as separate_home:
            work = Path(directory)
            home = Path(separate_home) if separate_home else work / "home"
            env = self.native_environment(work, home)
            codex_home = home / ".codex"
            codex_home.mkdir()
            env["CODEX_HOME"] = str(codex_home)
            result_path = work / "result.txt"
            command = [harness.get("executable", "codex"), "exec", "--json", "--skip-git-repo-check",
                       "--sandbox", "read-only", "--output-last-message", str(result_path), "--model", model["model"]]
            settings = {
                "model_provider": "openai", "forced_login_method": "chatgpt", "cli_auth_credentials_store": "file",
                "history.persistence": "none", "web_search": "disabled", "features.shell_tool": False,
                "tools.view_image": False, "features.apps": False, "apps._default.enabled": False,
                "agents.enabled": False, "features.multi_agent": False, "features.hooks": False,
                "features.remote_plugin": False, "features.skill_mcp_dependency_install": False,
                "features.memories": False, "project_doc_max_bytes": 0, "analytics.enabled": False,
                "check_for_update_on_startup": False, "feedback.enabled": False}
            effort = model.get("parameters", {}).get("reasoning_effort")
            if effort is not None:
                settings["model_reasoning_effort"] = effort
            for key, value in settings.items():
                command += ["-c", key + "=" + json.dumps(value)]
            command.append("-")
            with codex_subscription_auth(harness, codex_home):
                await self.native_process(command, cwd=work, env=env,
                    prompt=recipe["agent"].get("instructions", "") + "\n\n" + task, capture=False)
            try:
                if result_path.stat().st_size > 4 * 1024 * 1024:
                    raise RuntimeFailure("native_result_too_large")
                answer = result_path.read_text()
            except (OSError, UnicodeDecodeError):
                raise RuntimeFailure("native_result_missing") from None
            return self.native_result(recipe, answer, "native-codex", identity, isolated)

    async def native_claude(self, recipe: dict, task: str, job_id: str, *, isolated: bool = False) -> dict:
        self.native_subscription_guard(recipe, "claude-subscription", isolated)
        harness, model = recipe["harness"], recipe["model"]
        identity = await self.verify_model_identity(model, recipe.get("mode") == "enforced")
        token = subscription_token(harness)
        with tempfile.TemporaryDirectory(prefix="sovite-native-") as directory:
            work = Path(directory)
            env = self.native_environment(work, work / "home")
            env.update(CLAUDE_CODE_OAUTH_TOKEN=token, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                       CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST="1")
            command = [harness.get("executable", "claude"), "--print", "--bare", "--output-format", "json",
                       "--model", model["model"], "--tools", "", "--disallowedTools", "mcp__*",
                       "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                       "--setting-sources", "", "--no-session-persistence",
                       "--system-prompt", recipe["agent"].get("instructions", "")]
            raw = await self.native_process(command, cwd=work, env=env, prompt=task)
            try:
                result = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                raise RuntimeFailure("native_protocol_error") from None
            if not isinstance(result, dict) or result.get("is_error") or result.get("subtype") != "success":
                raise RuntimeFailure("native_runtime_failed")
            return self.native_result(recipe, result.get("result"), "native-claude", identity, isolated)

    async def native_opencode(self, recipe: dict, task: str, job_id: str, *, isolated: bool = False) -> dict:
        self.native_subscription_guard(recipe, "opencode-go", isolated)
        harness, model = recipe["harness"], recipe["model"]
        identity = await self.verify_model_identity(model, recipe.get("mode") == "enforced")
        credential = token_headers(model)["Authorization"].removeprefix("Bearer ")
        selection = "opencode-go/" + model["model"]
        deny = [{"action": "*", "resource": "*", "effect": "deny"}]
        config = {
            "model": selection,
            "providers": {"opencode-go": {
                "settings": {"baseURL": GO_ENDPOINT, "apiKey": "{env:SOVITE_GO_CREDENTIAL}"},
                "headers": {"x-opencode-session": job_id, "User-Agent": "sovite-platform/opencode-go"},
                "models": {model["model"]: {"modelID": model["model"], "name": model["model"]}}}},
            "agents": {"sovite": {"mode": "primary", "model": selection,
                                    "system": recipe["agent"].get("instructions", ""), "permissions": deny},
                       **{name: {"model": selection, "permissions": deny}
                          for name in ("title", "summary", "compaction")}},
            "policies": [{"action": "provider.use", "resource": "*", "effect": "deny"},
                         {"action": "provider.use", "resource": "opencode-go", "effect": "allow"},
                         {"action": "tool.use", "resource": "*", "effect": "deny"},
                         {"action": "integration.use", "resource": "*", "effect": "deny"}],
            "default_agent": "sovite", "permissions": deny, "mcp": {"servers": {}},
            "plugins": [], "instructions": [], "skills": [], "update": "disable",
            "snapshots": False, "share": "disabled", "warming": False, "compaction": {"auto": False}}
        with tempfile.TemporaryDirectory(prefix="sovite-native-") as directory:
            work = Path(directory)
            env = self.native_environment(work, work / "home")
            env.update(SOVITE_GO_CREDENTIAL=credential, OPENCODE_CONFIG_CONTENT=json.dumps(config),
                       OPENCODE_DISABLE_DEFAULT_PLUGINS="1", OPENCODE_DISABLE_LSP_DOWNLOAD="1",
                       OPENCODE_DISABLE_CLAUDE_CODE="1", OPENCODE_DISABLE_PROJECT_CONFIG="1",
                       OPENCODE_DISABLE_MODELS_FETCH="1", OPENCODE_DISABLE_AUTOCOMPACT="1")
            command = [harness.get("executable", "opencode"), "run", "--standalone", "--format", "json",
                       "--model", selection, "--agent", "sovite"]
            raw = await self.native_process(command, cwd=work, env=env, prompt=task)
            pieces, finished = [], False
            try:
                for line in raw.splitlines():
                    event = json.loads(line)
                    if not isinstance(event, dict) or event.get("type") == "error":
                        raise RuntimeFailure("native_runtime_failed")
                    if event.get("type") == "text":
                        part = event.get("part", {})
                        if not isinstance(part, dict) or not isinstance(part.get("text"), str):
                            raise RuntimeFailure("native_protocol_error")
                        pieces.append(part["text"])
                    elif event.get("type") == "step_finish":
                        part = event.get("part", {})
                        if not isinstance(part, dict) or part.get("reason") != "stop":
                            raise RuntimeFailure("native_incomplete_result")
                        finished = True
            except (ValueError, UnicodeDecodeError):
                raise RuntimeFailure("native_protocol_error") from None
            if not finished:
                raise RuntimeFailure("native_incomplete_result")
            return self.native_result(recipe, "".join(pieces), "native-opencode", identity, isolated)


    async def native_codex(self, recipe: dict, task: str, job_id: str, *, isolated: bool = False) -> dict:
        harness, model = recipe["harness"], recipe["model"]
        require_model_adapter(model)
        if model["adapter"] != "openai-compatible" or model.get("wire_api") != "responses":
            raise RuntimeFailure("codex_requires_responses_provider")
        # Inline native execution is supported only in the explicit development
        # profile. Production native runtimes belong in separately provisioned pods.
        if recipe.get("mode", "development") == "enforced" and not isolated:
            raise RuntimeFailure("native_process_requires_external_isolation")
        await self.verify_model_identity(model, recipe.get("mode") == "enforced")
        source_workspace = os.environ.get("SOVITE_NATIVE_WORKSPACE") if isolated else None
        if source_workspace and not Path(source_workspace).is_dir():
            raise RuntimeFailure("native_workspace_missing")
        # Codex refuses to install its sandbox helpers under the temporary directory, so
        # its home can live under a separate root; without one, its sandbox cannot start.
        home_root = os.environ.get("SOVITE_NATIVE_HOME_ROOT")
        with tempfile.TemporaryDirectory(prefix="sovite-native-") as work, \
                tempfile.TemporaryDirectory(prefix="sovite-native-home-", dir=home_root) if home_root else contextlib.nullcontext(None) as separate_home:
            result_path = str(Path(work) / "result.txt")
            native_home = Path(separate_home) if separate_home else Path(work) / "home"
            native_home.mkdir(exist_ok=True)
            env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(native_home), "LANG": "C.UTF-8", "TMPDIR": work}
            auth_env = model.get("api_key_env")
            if auth_env:
                token_headers(model)  # Validate presence before launching.
                env[auth_env] = os.environ[auth_env]
            trust = model_trust_path(model)
            if trust:
                env["CODEX_CA_CERTIFICATE"] = str(trust)
            command = [harness.get("executable", "codex"), "exec", "--json", "--skip-git-repo-check", "--sandbox", "read-only", "--output-last-message", result_path,
                "-c", 'model_provider="sovite"', "-c", "model=" + json.dumps(model["model"]),
                "-c", 'model_providers.sovite.name="Sovite configured provider"',
                "-c", "model_providers.sovite.base_url=" + json.dumps(model["endpoint"]),
                "-c", 'model_providers.sovite.wire_api="responses"',
                # The native service has no Internet path; an offered search tool only costs model turns.
                "-c", 'web_search="disabled"']
            if auth_env:
                command += ["-c", "model_providers.sovite.env_key=" + json.dumps(auth_env)]
            reasoning_effort = model.get("parameters", {}).get("reasoning_effort")
            if reasoning_effort is not None:
                command += ["-c", "model_reasoning_effort=" + json.dumps(reasoning_effort), "-c", "model_supports_reasoning_summaries=true"]
            command.append("-")
            try:
                process = await asyncio.create_subprocess_exec(*command, cwd=source_workspace or work, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            except FileNotFoundError:
                raise RuntimeFailure("native_runtime_unavailable") from None
            try:
                await process.communicate((recipe["agent"].get("instructions", "") + "\n\n" + task).encode())
            except asyncio.CancelledError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                raise
            if process.returncode != 0:
                raise RuntimeFailure("native_runtime_failed")
            try:
                path = Path(result_path)
                if path.stat().st_size > 4 * 1024 * 1024:
                    raise RuntimeFailure("native_result_too_large")
                answer = path.read_text()
            except OSError:
                raise RuntimeFailure("native_result_missing") from None
            if not answer.strip():
                raise RuntimeFailure("native_empty_result")
            return {"kind": "native-codex-result", "answer": answer, "agent_id": recipe["agent"]["id"], "model_id": model["id"], "generation": recipe["generation"], "isolation": "separate harness service" if isolated else "development process boundary; not a production security isolation claim"}
