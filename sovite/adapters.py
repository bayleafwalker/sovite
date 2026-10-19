"""Explicit runtime adapters. A failed model is never replaced by another model."""
from __future__ import annotations

import asyncio
import json
import os
import ssl
import tempfile
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx


class RuntimeFailure(Exception):
    def __init__(self, code: str, retryable: bool = False):
        super().__init__(code)
        self.code, self.retryable = code, retryable


def endpoint_url(endpoint: str, suffix: str) -> str:
    return endpoint.rstrip("/") if endpoint.rstrip("/").endswith(suffix) else endpoint.rstrip("/") + suffix


def token_headers(config: dict) -> dict[str, str]:
    env_name = config.get("api_key_env")
    if not env_name:
        return {}
    value = os.environ.get(env_name)
    if not value:
        raise RuntimeFailure("adapter_credential_missing")
    return {"Authorization": f"Bearer {value}"}


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
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError):
            raise RuntimeFailure("upstream_unavailable", True) from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise RuntimeFailure("upstream_protocol_error") from None

    async def run(self, recipe: dict, task: str, job_id: str) -> dict:
        harness, model = recipe["harness"], recipe["model"]
        kind = harness["kind"]
        if kind == "demo":
            if model["adapter"] != "demo":
                raise RuntimeFailure("incompatible_demo_configuration")
            # Deliberately synthetic; useful to assess the console and release mechanism.
            # This is neither a production diagnostic nor an implicit fallback.
            await asyncio.sleep(float(model.get("parameters", {}).get("delay_seconds", 0.1)))
            return {"kind": "synthetic-assessment", "synthetic": True, "task": task, "answer": "Synthetic private result. No production system or model was contacted.", "agent_id": recipe["agent"]["id"], "model_id": model["id"], "generation": recipe["generation"], "observations": ["The result is private; the caller receives a server-created receipt only."]}
        if kind == "native-codex":
            return await self.native_codex(recipe, task, job_id)
        if kind == "agent-http":
            return await self.agent_http(recipe, task, job_id)
        if kind != "tool-agent":
            raise RuntimeFailure("unsupported_harness")
        await self.verify_model_identity(model, recipe.get("mode") == "enforced")
        return await self.tool_agent(recipe, task, job_id)

    async def verify_model_identity(self, model: dict, enforced: bool = False) -> dict:
        expected = model.get("digest")
        if not expected:
            if enforced:
                raise RuntimeFailure("model_identity_unpinned")
            return {"status": "unpinned-development"}
        protocol = model.get("identity_protocol", "ollama" if model["adapter"] == "ollama" else None)
        if protocol != "ollama":
            raise RuntimeFailure("model_identity_protocol_unsupported")
        base = model.get("identity_endpoint", model.get("endpoint", "").removesuffix("/v1"))
        result = await self.request_json("GET", endpoint_url(base, "/api/tags"), headers=token_headers(model), timeout=15)
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
        return {"type": "function", "function": {"name": tool["id"], "description": tool.get("description", "Read scoped operational information using an approved operation."), "parameters": {"type": "object", "properties": {"operation": {"type": "string", "enum": operations}, "arguments": {"type": "object", "additionalProperties": True}}, "required": ["operation"], "additionalProperties": False}}}

    async def tool_agent(self, recipe: dict, task: str, job_id: str) -> dict:
        agent, model = recipe["agent"], recipe["model"]
        tools = {tool["id"]: tool for tool in recipe.get("tools", [])}
        messages: list[dict] = [{"role": "system", "content": agent.get("instructions", "Complete the task using only approved tools. Report uncertainty and cite observed evidence.")}, {"role": "user", "content": task}]
        trace: list[dict] = []
        for step in range(min(int(agent.get("max_steps", 8)), 20)):
            payload: dict = {"model": model["model"], "messages": messages, "stream": False}
            if tools:
                payload["tools"] = [self.tool_schema(tool) for tool in tools.values()]
            parameters = model.get("parameters", {})
            if model["adapter"] == "ollama":
                payload["options"] = {k: v for k, v in parameters.items() if k in {"temperature", "num_ctx", "num_predict", "seed", "top_p", "top_k"}}
                if "think" in parameters:
                    payload["think"] = parameters["think"]
                result = await self.request_json("POST", endpoint_url(model["endpoint"], "/api/chat"), payload=payload, headers=token_headers(model), timeout=int(model.get("timeout_seconds", 120)))
                message = result.get("message")
            elif model["adapter"] == "openai-compatible":
                if model.get("wire_api", "chat") != "chat":
                    raise RuntimeFailure("tool_agent_requires_chat_api")
                payload.update({k: v for k, v in parameters.items() if k in {"temperature", "max_tokens", "top_p", "seed", "reasoning_effort"}})
                result = await self.request_json("POST", endpoint_url(model["endpoint"], "/chat/completions"), payload=payload, headers=token_headers(model), timeout=int(model.get("timeout_seconds", 120)))
                choices = result.get("choices", [])
                message = choices[0].get("message") if choices and isinstance(choices[0], dict) else None
            else:
                raise RuntimeFailure("unsupported_model_adapter")
            if not isinstance(message, dict):
                raise RuntimeFailure("model_protocol_error")
            calls = message.get("tool_calls", [])
            if not isinstance(calls, list) or len(calls) > 8:
                raise RuntimeFailure("invalid_tool_calls")
            if not calls:
                content = message.get("content")
                if not isinstance(content, str):
                    raise RuntimeFailure("model_protocol_error")
                if not content.strip():
                    raise RuntimeFailure("model_empty_result")
                return {"kind": "agent-result", "answer": content, "trace": trace, "model_id": model["id"], "agent_id": agent["id"], "generation": recipe["generation"], "steps": step + 1}
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
                if not isinstance(arguments, dict) or set(arguments) - {"operation", "arguments"}:
                    # Malformed calls are never executed; the model sees the expected shape and may
                    # correct itself within the step limit. Undeclared tools and denied operations stay fatal.
                    output = {"error": "invalid_tool_arguments", "expected": {"operation": tools[name].get("allowed_operations", []), "arguments": {}}}
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

    async def native_codex(self, recipe: dict, task: str, job_id: str, *, isolated: bool = False) -> dict:
        harness, model = recipe["harness"], recipe["model"]
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
        with tempfile.TemporaryDirectory(prefix="sovite-native-") as work:
            result_path = str(Path(work) / "result.txt")
            native_home = Path(work) / "home"
            native_home.mkdir()
            env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(native_home), "LANG": "C.UTF-8", "TMPDIR": work}
            auth_env = model.get("api_key_env")
            if auth_env:
                token_headers(model)  # Validate presence before launching.
                env[auth_env] = os.environ[auth_env]
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
