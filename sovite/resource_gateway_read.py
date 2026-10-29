"""Uninstalled resource-owner read seam for one named Kubernetes pod observation.

The caller packet is a selector, not authentication. All target configuration,
current attempt authority and exact-byte disclosure decisions come from trusted
owner composition. No API route, operation journal or live gateway is installed.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Mapping

from .adapters import AdapterRunner

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_KUBE_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_NAMESPACE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_REVISION = re.compile(r"^sha256:[0-9a-f]{64}$")
_REQUEST = {"workspace_id", "resource_domain_id", "observation_id"}
_CONTEXT = {"tenant_id", "principal_id", "attempt_id", "fence"}
_BINDING = _REQUEST | {"tenant_id", "namespace", "pod_name", "tool", "source_class",
                       "output_class", "destination", "output_contract_id", "output_contract_revision"}
_PHASES = {"Pending", "Running", "Succeeded", "Failed", "Unknown"}


class ResourceReadError(ValueError):
    """A fixed public reason, never upstream or credential content."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _ids(value: Any, fields: set[str]) -> bool:
    return (isinstance(value, Mapping) and set(value) == fields
            and all(isinstance(value[k], str) and _ID.fullmatch(value[k]) for k in fields))


def _binding(value: Any, request: Mapping[str, str], context: Mapping[str, str]) -> dict:
    if not isinstance(value, Mapping) or set(value) != _BINDING:
        raise ResourceReadError("binding_unavailable")
    if any(not isinstance(value[k], str) or not _ID.fullmatch(value[k]) for k in
           _REQUEST | {"tenant_id", "source_class", "output_class", "destination", "output_contract_id"}):
        raise ResourceReadError("binding_unavailable")
    if any(value[k] != request[k] for k in _REQUEST) or value["tenant_id"] != context["tenant_id"]:
        raise ResourceReadError("binding_denied")
    if (not isinstance(value["namespace"], str) or not _NAMESPACE.fullmatch(value["namespace"])
            or not isinstance(value["pod_name"], str) or not _KUBE_NAME.fullmatch(value["pod_name"])
            or not isinstance(value["output_contract_revision"], str)
            or not _REVISION.fullmatch(value["output_contract_revision"])):
        raise ResourceReadError("binding_unavailable")
    tool = value["tool"]
    if (not isinstance(tool, dict) or set(tool) - {"id", "kind", "namespace", "allowed_operations", "endpoint", "api_key_env", "token_file", "ca_file", "timeout_seconds"}
            or tool.get("kind") != "kubernetes-read"
            or tool.get("namespace") != value["namespace"]
            or tool.get("allowed_operations") != ["list-pods"]):
        raise ResourceReadError("binding_unavailable")
    return copy.deepcopy(dict(value))


class ResourceGatewayRead:
    """Compose a trusted catalog, attempt checker, release authority and adapter.

    ``authorize(context, binding, stage)`` checks the current exact attempt and
    local resource authority. ``release(context, binding, content, digest)``
    checks the current output contract and destination for those exact bytes.
    Only literal True authorizes. These are owner callbacks, not model decisions.
    """

    def __init__(self, catalog: Any, authorize: Any, release: Any, runner: AdapterRunner):
        self.catalog, self.authorize, self.release, self.runner = catalog, authorize, release, runner

    def _resolve(self, request, context):
        try:
            value = self.catalog.resolve(dict(request))
        except Exception:
            raise ResourceReadError("binding_unavailable") from None
        return _binding(value, request, context)

    def _authorize(self, context, binding, stage):
        try:
            allowed = self.authorize(copy.deepcopy(context), copy.deepcopy(binding), stage)
        except Exception:
            allowed = False
        if allowed is not True:
            raise ResourceReadError("authorization_denied")

    async def read(self, request: Any, *, context: Any) -> dict:
        if not _ids(request, _REQUEST) or not _ids(context, _CONTEXT):
            raise ResourceReadError("invalid_request")
        request, context = dict(request), dict(context)
        binding = self._resolve(request, context)
        self._authorize(context, binding, "before_read")
        try:
            raw = await self.runner.execute_tool(copy.deepcopy(binding["tool"]),
                                                 {"operation": "list-pods", "arguments": {}},
                                                 "resource-owner-read")
        except Exception:
            raise ResourceReadError("target_unavailable") from None
        try:
            if not isinstance(raw, dict) or raw.get("kind") != "PodList" or not isinstance(raw.get("items"), list):
                raise ValueError
            matches = [item for item in raw["items"] if isinstance(item, dict)
                       and isinstance(item.get("metadata"), dict)
                       and item["metadata"].get("name") == binding["pod_name"]
                       and item["metadata"].get("namespace") == binding["namespace"]]
            if len(matches) != 1 or not isinstance(matches[0].get("status"), dict):
                raise ValueError
            phase = matches[0]["status"].get("phase")
            if phase not in _PHASES:
                raise ValueError
        except (ValueError, TypeError):
            raise ResourceReadError("observation_unavailable") from None
        if self._resolve(request, context) != binding:
            raise ResourceReadError("binding_changed")
        self._authorize(context, binding, "before_disclosure")
        projection = {"schema_version": "sovite-resource-pod-observation/v1",
                      "workspace_id": request["workspace_id"], "resource_domain_id": request["resource_domain_id"],
                      "observation_id": request["observation_id"], "namespace": binding["namespace"],
                      "pod_name": binding["pod_name"], "phase": phase}
        content = json.dumps(projection, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        try:
            allowed = self.release(copy.deepcopy(context),
                                   {k: copy.deepcopy(v) for k, v in binding.items() if k != "tool"},
                                   content, digest)
        except Exception:
            allowed = False
        if allowed is not True:
            raise ResourceReadError("release_denied")
        if self._resolve(request, context) != binding:
            raise ResourceReadError("binding_changed")
        self._authorize(context, binding, "before_disclosure")
        return {"observation": projection, "sha256": digest,
                "output_contract_id": binding["output_contract_id"],
                "output_contract_revision": binding["output_contract_revision"]}
