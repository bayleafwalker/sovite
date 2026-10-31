"""Compile and authorize declarative platform bundles.

The compiler reasons over declared information classes. Runtime identity,
network isolation and qualification evidence are separate deployment duties;
accepting a configuration is deliberately not an assertion that they exist.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from jsonschema import Draft202012Validator

from .flow import (FlowError, check_destination, check_role_input, resolve_input_class,
                   validate_output_contracts)


class PolicyError(ValueError):
    """A bounded, caller-safe denial. Details belong in operator validation."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        self.message = message or {
            "invalid_bundle": "The platform configuration is invalid.",
            "unknown_grant": "The requested grant is unavailable.",
            "grant_disabled": "The requested grant is unavailable.",
            "caller_denied": "The requested grant is unavailable.",
            "class_denied": "The request exceeds the permitted data class.",
            "unsupported_contract": "The return contract is unsupported.",
        }.get(code, "The request is not authorized.")
        super().__init__(self.message)


ID = {"type": "string", "pattern": r"^[A-Za-z][A-Za-z0-9_.-]{0,95}$(?!\n)"}
TEXT = {"type": "string", "maxLength": 16000}
REF_TEXT = {"type": "string", "minLength": 1, "maxLength": 2048,
            "pattern": r"^[^\s\x00-\x1f\x7f](?:[^\x00-\x1f\x7f]*[^\s\x00-\x1f\x7f])?$(?!\n)"}
DIGEST = {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$(?!\n)"}
REFS = {"type": "array", "items": ID, "uniqueItems": True, "maxItems": 100}
ENV_NAME = {"type": "string", "pattern": "^[A-Z][A-Z0-9_]{1,95}$"}
NATIVE_HARNESSES = {"native-codex", "native-claude", "native-opencode"}
SUBSCRIPTION_HARNESSES = {"codex-subscription": "native-codex", "claude-subscription": "native-claude"}
SUBSCRIPTION_HOSTS = {"codex-subscription": {"chatgpt.com", "auth.openai.com"},
                      "claude-subscription": {"api.anthropic.com"}}
GO_ENDPOINT = "https://opencode.ai/inference/go/openai/v1"
PUBLIC_RETENTION_SECONDS = 30 * 24 * 3600


def go_endpoint_matches(model):
    wire=model.get('wire_api','chat')
    suffix={'chat':'/chat/completions','responses':'/responses'}.get(wire)
    return suffix is not None and model.get('endpoint','').rstrip('/') in {GO_ENDPOINT,GO_ENDPOINT+suffix}


def native_harness_kind(harness: dict) -> str | None:
    """Resolve a fixed native runtime without accepting caller-selected executables."""
    if harness["kind"] in NATIVE_HARNESSES:
        return harness["kind"]
    matches = NATIVE_HARNESSES.intersection(harness.get("capabilities", [])) if harness["kind"] == "agent-http" else set()
    if len(matches) > 1:
        raise PolicyError("invalid_bundle", "An external harness must name exactly one native runtime capability.")
    return next(iter(matches), None)


def obj(required: list[str], properties: dict) -> dict:
    return {"type": "object", "required": required, "properties": properties,
            "additionalProperties": False}


def named(required: list[str], properties: dict) -> dict:
    return obj(["id", *required], {"id": ID, "name": TEXT, "description": TEXT, **properties})


REVISION_REF_SCHEMA = obj(
    ["owner", "resource_id", "revision", "content_digest"],
    {"owner": REF_TEXT, "resource_id": REF_TEXT, "revision": REF_TEXT,
     "content_digest": DIGEST},
)


DEFINITIONS = {
    "data_classes": named(["rank"], {"rank": {"type": "integer", "minimum": 0, "maximum": 100}}),
    "destinations": named(["clearance", "kind"], {
        "clearance": ID, "kind": {"enum": ["local", "private", "cloud"]},
        "boundary": {"enum": ["private", "public"]},
        "allowed_hosts": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 253}, "uniqueItems": True},
    }),
    "models": named(["adapter", "model", "destination"], {
        "adapter": {"enum": ["demo", "ollama", "openai-compatible", "codex-subscription", "claude-subscription", "opencode-go"]},
        "endpoint": {"type": "string", "minLength": 1, "maxLength": 2048},
        "model": {"type": "string", "minLength": 1, "maxLength": 512},
        "digest": DIGEST, "destination": ID,
        "wire_api": {"enum": ["chat", "responses"]},
        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600},
        "identity_protocol": {"enum": ["ollama", "native-subscription", "provider-service"]},
        "identity_endpoint": {"type": "string", "maxLength": 2048},
        "api_key_env": {"type": "string", "pattern": "^[A-Z][A-Z0-9_]{1,95}$"},
        "tls_trust": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,62}$"},
        "max_concurrency": {"type": "integer", "minimum": 1, "maximum": 64},
        "parameters": obj([], {
            "temperature": {"type": "number", "minimum": 0, "maximum": 2},
            "think": {"type": "boolean"},
            "reasoning_effort": {"enum": ["none", "minimal", "low", "medium", "high", "xhigh"]},
            "num_predict": {"type": "integer", "minimum": 1, "maximum": 32768},
            "num_ctx": {"type": "integer", "minimum": 128, "maximum": 1048576},
            "max_tokens": {"type": "integer", "minimum": 1, "maximum": 32768},
            "seed": {"type": "integer"},
        }),
    }),
    "harnesses": named(["kind"], {
        "kind": {"enum": ["demo", "tool-agent", "native-codex", "native-claude", "native-opencode", "agent-http"]},
        "image": TEXT, "digest": DIGEST, "version": TEXT,
        "capabilities": REFS, "endpoint": TEXT,
        "cancel_endpoint": TEXT,
        "api_key_env": {"type": "string", "pattern": "^[A-Z][A-Z0-9_]{1,95}$"},
        "executable": {"type": "string", "minLength": 1, "maxLength": 512},
        "idempotent": {"type": "boolean"},
        "authentication": obj(["mode"], {
            "mode": {"const": "subscription"},
            "credential_file_env": ENV_NAME, "credential_env": ENV_NAME,
        }),
    }),
    "tools": named(["kind", "input_class", "output_class", "destination", "allowed_operations"], {
        "kind": {"enum": ["http-json", "kubernetes-read"]},
        "endpoint": TEXT, "input_class": ID, "output_class": ID, "destination": ID,
        "allowed_operations": REFS,
        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120},
        "api_key_env": {"type": "string", "pattern": "^[A-Z][A-Z0-9_]{1,95}$"},
        "namespace": ID,
        "read_only": {"type": "boolean"},
    }),
    "agents": named(["harness", "model", "tools"], {
        "harness": ID, "model": ID, "tools": REFS, "instructions": TEXT,
        "max_steps": {"type": "integer", "minimum": 1, "maximum": 32},
        "version": TEXT,
    }),
    "compute": named(["kind", "destination"], {
        "kind": {"enum": ["local", "kubernetes"]}, "destination": ID,
        "namespace": ID,
        "profile": obj([], {
            "accelerator": TEXT, "replicas": {"type": "integer", "minimum": 0, "maximum": 1000},
            "cpu": {"type": ["string", "number"]}, "memory": TEXT,
            "worker_image": TEXT, "node_selector": {"type": "object", "additionalProperties": {"type": "string"}},
        }),
    }),
    "grants": named(["agent", "callers", "input_class", "output_class", "compute",
                     "state_destination", "telemetry_destination", "return_contract"], {
        "agent": ID, "callers": REFS, "input_class": ID, "output_class": ID,
        "compute": ID, "state_destination": ID, "telemetry_destination": ID,
        "telemetry_class": ID, "receipt_class": ID, "caller_clearance": ID,
        "return_contract": {"enum": ["reference", "class-aware"]}, "enabled": {"type": "boolean"},
        "result_readers": REFS,
        "workflow_only": {"type": "boolean"},
        "repository_currentness_required": {"type": "boolean"},
        "on_outage": {"const": "queue"},
        "max_attempts": {"type": "integer", "minimum": 1, "maximum": 10},
        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 3600},
    }),
    "profiles": named(["components"], {"components": REFS}),
}

SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "urn:sovite:platform-bundle:v1alpha1",
    "title": "PlatformBundle", **obj(
        ["apiVersion", "kind", "metadata", "mode", "data_classes", "destinations",
         "models", "harnesses", "tools", "agents", "compute", "grants", "profiles"],
        {
            "apiVersion": {"const": "sovite.platform/v1alpha1"},
            "kind": {"const": "PlatformBundle"},
            "metadata": obj(["name", "display_name"], {"name": ID, "display_name": TEXT,
                              "description": TEXT, "version": TEXT}),
            "mode": {"enum": ["development", "enforced"]},
            "restricted_class": ID,
            "discovery_class": ID,
            "output_contracts": {"type": "array", "maxItems": 100, "items": named(
                ["source_class", "output_class", "destinations", "approvers", "procedure_ref"], {
                    "source_class": ID, "output_class": ID,
                    "destinations": {**REFS, "minItems": 1},
                    "approvers": {**REFS, "minItems": 1},
                    "procedure_ref": REVISION_REF_SCHEMA,
                })},
            "default_profile": ID,
            **{key: {"type": "array", "items": definition, "maxItems": 1000}
               for key, definition in DEFINITIONS.items()},
        }),
}


SCHEMA_V1ALPHA2 = copy.deepcopy(SCHEMA)
SCHEMA_V1ALPHA2["$id"] = "urn:sovite:platform-bundle:v1alpha2"
SCHEMA_V1ALPHA2["required"] += ["restricted_class", "connector_roles"]
V2_PROPERTIES = SCHEMA_V1ALPHA2["properties"]
V2_PROPERTIES["apiVersion"] = {"const": "sovite.platform/v1alpha2"}
V2_PROPERTIES["restricted_class"] = ID
V2_PROPERTIES["connector_roles"] = {"type": "array", "maxItems": 1000, "items": named(
    ["destination", "helper_image_digest", "engine_image_digest"], {
        "destination": ID, "helper_image_digest": DIGEST, "engine_image_digest": DIGEST,
    })}
V2_DESTINATION = V2_PROPERTIES["destinations"]["items"]
V2_DESTINATION["required"] += ["trust_tier", "retention"]
V2_DESTINATION["properties"].update({
    "trust_tier": {"enum": ["operator-owned", "rented", "provider"]},
    "retention": obj(["prompt_logging", "response_logging", "persistent_context", "in_memory_cache"], {
        "prompt_logging": {"type": "boolean"}, "response_logging": {"type": "boolean"},
        "persistent_context": {"type": "boolean"},
        "in_memory_cache": obj(["scope", "max_seconds", "clear_on_cancel", "clear_on_session_close"], {
            "scope": {"enum": ["job", "provider"]},
            "max_seconds": {"type": "integer", "minimum": 1},
            "clear_on_cancel": {"type": "boolean"}, "clear_on_session_close": {"type": "boolean"},
        }),
        "max_seconds": {"type":"integer","minimum":1,"maximum":PUBLIC_RETENTION_SECONDS},
        "authorization_ref": {**TEXT,"minLength":1,"pattern":"\\S"},
    }),
    "restricted_clearance": obj(["classes", "rationale", "authorization_ref"], {
        "classes": {**REFS, "minItems": 1},
        "rationale": {**TEXT, "minLength": 1, "pattern": "\\S"},
        "authorization_ref": {**TEXT, "minLength": 1, "pattern": "\\S"},
    }),
})
V2_MODEL = V2_PROPERTIES["models"]["items"]
V2_MODEL["required"] += ["enabled"]
V2_MODEL["properties"].update({
    "enabled": {"type": "boolean"},
    "connection": obj(["kind", "enrollment_role"], {"kind": {"const": "model-gateway"}, "enrollment_role": ID}),
    "identity": obj(["protocol", "pinning"], {
        "protocol": {"const": "helper-signed-v1"},
        "pinning": {"enum": ["required", "unpinned-development"]},
        "engine_version": {"type": "string", "minLength": 1, "maxLength": 512, "pattern": "\\S"},
        "weights_manifest_digest": DIGEST,
    }),
    "first_token_timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600},
    "cancel_bound_seconds": {"type": "integer", "minimum": 1, "maximum": 600},
})
V2_MODEL["allOf"] = [{"if": {"required": ["connection"]}, "then": {
    "required": ["identity", "wire_api", "max_concurrency", "timeout_seconds",
                 "first_token_timeout_seconds", "cancel_bound_seconds"],
}}]
V2_PROPERTIES["grants"]["items"]["properties"]["availability_wait_seconds"] = {"type": "integer", "minimum": 1, "maximum": 86400}


def bundle_generation(bundle: dict) -> str:
    encoded = json.dumps(bundle, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _indexes(bundle: dict) -> dict[str, dict[str, dict]]:
    sections = [*DEFINITIONS, *(["connector_roles"] if "connector_roles" in bundle else [])]
    return {key: {item["id"]: item for item in bundle[key]} for key in sections}


def _fail(message: str) -> None:
    raise PolicyError("invalid_bundle", message)


def _endpoint(value: str, label: str, *, local: bool) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        _fail(f"{label}: endpoint must be HTTP(S) without embedded credentials, query or fragment")
    if parsed.scheme == "http" and not local:
        _fail(f"{label}: cloud endpoints require HTTPS")


def validate_bundle(bundle: dict) -> dict:
    """Return an independent validated bundle; reject unresolved/unsafe bindings."""
    v2 = isinstance(bundle, dict) and bundle.get("apiVersion") == "sovite.platform/v1alpha2"
    schema = SCHEMA_V1ALPHA2 if v2 else SCHEMA
    errors = sorted(Draft202012Validator(schema).iter_errors(bundle), key=lambda e: str(list(e.path)))
    if errors:
        err = errors[0]
        path = ".".join(map(str, err.absolute_path)) or "bundle"
        _fail(f"{path}: {err.message}")
    for key in [*DEFINITIONS, *(["connector_roles"] if v2 else [])]:
        ids = [item["id"] for item in bundle[key]]
        if len(ids) != len(set(ids)):
            _fail(f"{key}: duplicate identifiers")
    idx = _indexes(bundle)
    ranks = {key: value["rank"] for key, value in idx["data_classes"].items()}
    if not ranks or len(set(ranks.values())) != len(ranks):
        _fail("data_classes: declare a nonempty total order with distinct ranks")

    def ref(section: str, key: str, source: str) -> dict:
        if key not in idx[section]:
            _fail(f"{source}: unresolved {section} reference {key}")
        return idx[section][key]

    if "restricted_class" in bundle:
        ref("data_classes", bundle["restricted_class"], "restricted_class")
    if "discovery_class" in bundle:
        ref("data_classes", bundle["discovery_class"], "discovery_class")
    if v2:
        for role in bundle["connector_roles"]:
            ref("destinations", role["destination"], role["id"])

    def receiving(dest: dict, context_rank: int, source: str) -> None:
        if v2 and dest["trust_tier"] == "rented" and context_rank >= ranks[bundle["restricted_class"]]:
            context_class = next(key for key, rank in ranks.items() if rank == context_rank)
            if context_class not in dest.get("restricted_clearance", {}).get("classes", []):
                _fail(f"{source}: rented destination {dest['id']} requires explicit restricted_clearance for {context_class}")

    for dst in bundle["destinations"]:
        ref("data_classes", dst["clearance"], dst["id"])
        if v2 and "restricted_clearance" in dst:
            for cls in dst["restricted_clearance"]["classes"]:
                ref("data_classes", cls, dst["id"])
                if not ranks[bundle["restricted_class"]] <= ranks[cls] <= ranks[dst["clearance"]]:
                    _fail(f"{dst['id']}: restricted_clearance classes must be restricted-or-higher and within destination clearance")
        for host in dst.get("allowed_hosts", []):
            if host in {"*", "0.0.0.0/0", "::/0"} or "/" in host or "*" in host:
                _fail(f"{dst['id']}: destination hosts must be exact hosts; no wildcard egress")
    for model in bundle["models"]:
        dest = ref("destinations", model["destination"], model["id"])
        subscription = model["adapter"] in SUBSCRIPTION_HARNESSES
        go = model["adapter"] == "opencode-go"
        if subscription:
            if model.get("identity_protocol") != "native-subscription":
                _fail(f"{model['id']}: subscription models require explicit native-subscription identity semantics")
            if any(field in model for field in ("endpoint", "api_key_env", "digest", "identity_endpoint", "tls_trust", "wire_api", "connection", "identity")):
                _fail(f"{model['id']}: subscription model selection cannot carry API routing, API credentials or a weights digest")
            if dest["kind"] != "cloud" or dest.get("boundary", "public") != "public":
                _fail(f"{model['id']}: native subscription inference requires a declared cloud destination")
        elif model.get("identity_protocol") == "native-subscription":
            _fail(f"{model['id']}: native-subscription identity requires a subscription adapter")
        if go:
            if model.get("identity_protocol") != "provider-service" or model.get("digest") or model.get("identity_endpoint"):
                _fail(f"{model['id']}: OpenCode Go requires provider-service identity, not an immutable weights digest")
            if not go_endpoint_matches(model):
                _fail(f"{model['id']}: OpenCode Go requires its declared Go service endpoint")
            if not model.get("api_key_env") or dest["kind"] != "cloud" or dest.get("boundary", "public") != "public":
                _fail(f"{model['id']}: OpenCode Go requires an explicit Go key, supported API and cloud destination")
            if model.get('wire_api','chat')=='responses' and (not v2 or bundle['mode']!='enforced'):
                _fail(f"{model['id']}: Go Responses requires enforced v1alpha2 public-data policy")
        elif model.get("identity_protocol") == "provider-service":
            _fail(f"{model['id']}: provider-service identity is supported only for the OpenCode Go adapter")
        if v2:
            retention=dest['retention']
            declared='max_seconds' in retention or 'authorization_ref' in retention
            public=(declared and go and model.get('wire_api')=='responses'
                and bundle['mode']=='enforced' and dest['trust_tier']=='provider'
                and dest['clearance']=='C0' and ranks.get('C0')==0
                and {'max_seconds','authorization_ref'}<=retention.keys())
            if declared and not public:
                _fail(f"{model['id']}: declared upstream retention is limited to public C0 Go Responses")
            if go and model.get('wire_api')=='responses' and not public:
                _fail(f"{model['id']}: Go Responses requires explicit truthful public-data retention")
            cache=retention['in_memory_cache']
            if public:
                if (not retention['prompt_logging'] or not retention['response_logging']
                    or retention['persistent_context'] or retention['max_seconds']!=PUBLIC_RETENTION_SECONDS
                    or cache['scope']!='provider' or cache['max_seconds']>retention['max_seconds']
                    or cache['clear_on_cancel'] or cache['clear_on_session_close']):
                    _fail(f"{model['id']}: public provider retention must declare its 30-day bound without local-clear claims")
            elif cache['scope']!='job':
                _fail(f"{model['id']}: provider-managed caching requires explicit public-data retention")
            if not public and any(retention[field] for field in ("prompt_logging", "response_logging", "persistent_context")):
                _fail(f"{model['id']}: inference retention requires prompt_logging, response_logging and persistent_context false (including upstream providers)")
            if "connection" in model:
                if model["adapter"] not in {"ollama", "openai-compatible"}:
                    _fail(f"{model['id']}: this adapter does not support a model-gateway connection")
                role = ref("connector_roles", model["connection"]["enrollment_role"], model["id"])
                # EXACT_BINDING: the helper role cannot redirect the declared placement.
                if role["destination"] != model["destination"]:
                    _fail(f"{model['id']}: binding and connector role must name the same destination")
                if any(field in model for field in ("endpoint", "api_key_env", "tls_trust")):
                    _fail(f"{model['id']}: gateway bindings cannot carry upstream endpoints, credentials or worker TLS trust")
                if any(field in model for field in ("digest", "identity_protocol", "identity_endpoint")):
                    _fail(f"{model['id']}: ambiguous legacy/new identity fields")
                identity = model["identity"]
                # PIN_BEFORE_PROMPT: these declarations are prerequisites, not runtime evidence.
                if bundle["mode"] == "enforced" and identity["pinning"] != "required":
                    _fail(f"{model['id']}: enforced gateway identity requires pinning required")
                if identity["pinning"] == "required" and not all(identity.get(field) for field in ("engine_version", "weights_manifest_digest")):
                    _fail(f"{model['id']}: required identity pins include engine_version and weights_manifest_digest")
                if model["adapter"] == "demo" or (model["adapter"] == "ollama" and model["wire_api"] != "chat"):
                    _fail(f"{model['id']}: gateway adapter and wire_api mismatch")
                # BOUNDED_STOP: declared deadlines must be positive and internally consistent.
                if any(model[field] > model["timeout_seconds"] for field in ("first_token_timeout_seconds", "cancel_bound_seconds")):
                    _fail(f"{model['id']}: first-token timeout and cancel bound cannot exceed timeout_seconds")
                continue
            if bundle["mode"] == "enforced" and not (subscription or go):
                _fail(f"{model['id']}: enforced v1alpha2 requires a model-gateway connection; direct endpoints are development-only")
            if any(field in model for field in ("identity", "first_token_timeout_seconds", "cancel_bound_seconds")):
                _fail(f"{model['id']}: gateway identity and deadlines require a model-gateway connection")
        if subscription:
            if bundle["mode"] == "enforced" and not SUBSCRIPTION_HOSTS[model["adapter"]].issubset(dest.get("allowed_hosts", [])):
                _fail(f"{model['id']}: enforced subscription inference requires its fixed service hosts in the destination allowlist")
            continue
        if model["adapter"] != "demo":
            if not model.get("endpoint"):
                _fail(f"{model['id']}: a serving endpoint is required")
            _endpoint(model["endpoint"], model["id"], local=dest["kind"] != "cloud")
            hosts = dest.get("allowed_hosts", [])
            if hosts and urlsplit(model["endpoint"]).hostname not in hosts:
                _fail(f"{model['id']}: endpoint is outside its declared destination hosts")
            if model.get("tls_trust") and any(urlsplit(url).scheme != "https" for url in (model["endpoint"], model.get("identity_endpoint", model["endpoint"]))):
                _fail(f"{model['id']}: a pinned TLS trust anchor requires HTTPS endpoints")
        if go and bundle["mode"] == "enforced" and not dest.get("allowed_hosts"):
            _fail(f"{model['id']}: enforced OpenCode Go requires exact destination hosts")
        if bundle["mode"] == "enforced" and not go and (model["adapter"] == "demo" or not model.get("digest")):
            _fail(f"{model['id']}: enforced installations require a real model with a content digest")
        if model.get("identity_endpoint"):
            _endpoint(model["identity_endpoint"], model["id"], local=dest["kind"] != "cloud")
            hosts = dest.get("allowed_hosts", [])
            if hosts and urlsplit(model["identity_endpoint"]).hostname not in hosts:
                _fail(f"{model['id']}: identity endpoint is outside its declared destination hosts")
        if bundle["mode"] == "enforced" and not go and model["adapter"] != "ollama" and model.get("identity_protocol") != "ollama":
            _fail(f"{model['id']}: this release cannot verify a content digest through a generic provider API")
    for harness in bundle["harnesses"]:
        native_harness_kind(harness)
        if harness["kind"] == "agent-http":
            if not harness.get("endpoint"):
                _fail(f"{harness['id']}: agent-http requires an endpoint")
            _endpoint(harness["endpoint"], harness["id"], local=True)
            if harness.get("cancel_endpoint"):
                _endpoint(harness["cancel_endpoint"], harness["id"], local=True)
        if bundle["mode"] == "enforced" and (harness["kind"] == "demo" or not harness.get("digest")):
            _fail(f"{harness['id']}: enforced installations require a non-demo, digest-pinned harness")
        if harness.get("image") and "@sha256:" in harness["image"] and harness.get("digest"):
            if harness["image"].rsplit("@", 1)[-1] != harness["digest"]:
                _fail(f"{harness['id']}: image and declared harness digest disagree")
    for tool in bundle["tools"]:
        dest = ref("destinations", tool["destination"], tool["id"])
        for field in ("input_class", "output_class"):
            ref("data_classes", tool[field], tool["id"])
        if ranks[tool["input_class"]] > ranks[dest["clearance"]] or ranks[tool["output_class"]] > ranks[dest["clearance"]]:
            _fail(f"{tool['id']}: tool destination clearance is too low for arguments or results")
        receiving(dest, max(ranks[tool["input_class"]], ranks[tool["output_class"]]), tool["id"])
        if tool["kind"] == "http-json" and not tool.get("endpoint"):
            _fail(f"{tool['id']}: HTTP tool endpoint is required")
        if tool.get("endpoint"):
            _endpoint(tool["endpoint"], tool["id"], local=dest["kind"] != "cloud")
            hosts = dest.get("allowed_hosts", [])
            if hosts and urlsplit(tool["endpoint"]).hostname not in hosts:
                _fail(f"{tool['id']}: endpoint is outside its declared destination hosts")
        if not tool["allowed_operations"]:
            _fail(f"{tool['id']}: an explicit nonempty operation allowlist is required")
        if tool["kind"] == "kubernetes-read" and any(op not in {"list-pods", "list-events", "list-deployments", "list-services"} for op in tool["allowed_operations"]):
            _fail(f"{tool['id']}: unsupported Kubernetes read operation")
    for compute in bundle["compute"]:
        ref("destinations", compute["destination"], compute["id"])
        if compute["kind"] == "kubernetes" and not compute.get("namespace"):
            _fail(f"{compute['id']}: Kubernetes compute requires a namespace")
    for agent in bundle["agents"]:
        harness = ref("harnesses", agent["harness"], agent["id"])
        model = ref("models", agent["model"], agent["id"])
        for tool_id in agent["tools"]:
            ref("tools", tool_id, agent["id"])
        if (harness["kind"] == "demo") != (model["adapter"] == "demo"):
            _fail(f"{agent['id']}: synthetic harness and model must be paired explicitly")
        if harness["kind"] == "demo" and agent["tools"]:
            _fail(f"{agent['id']}: synthetic evaluation cannot access real tools")
        native = native_harness_kind(harness)
        if model["adapter"] in SUBSCRIPTION_HARNESSES:
            if native != SUBSCRIPTION_HARNESSES[model["adapter"]]:
                _fail(f"{agent['id']}: subscription models require their matching native harness")
            authentication = harness.get("authentication", {})
            required = "credential_file_env" if native == "native-codex" else "credential_env"
            if set(authentication) != {"mode", required} or authentication.get("mode") != "subscription":
                _fail(f"{agent['id']}: subscription authentication requires exactly mode and {required}")
        elif "authentication" in harness:
            _fail(f"{agent['id']}: subscription credentials cannot be used with API model routing")
        if native and agent["tools"]:
            _fail(f"{agent['id']}: native harnesses do not translate declared HTTP tools into native tool authority")
        if native == "native-codex" and model["adapter"] not in {"ollama", "openai-compatible", "codex-subscription",'opencode-go'}:
            _fail(f"{agent['id']}: native Codex requires a supported model endpoint")
        if native == "native-claude" and model["adapter"] != "claude-subscription":
            _fail(f"{agent['id']}: native Claude requires explicit Claude subscription model routing")
        if native == "native-opencode" and model["adapter"] != "opencode-go":
            _fail(f"{agent['id']}: native OpenCode requires explicit OpenCode Go model routing")
        if v2 and harness["kind"] == "tool-agent" and model.get("wire_api", "chat") != "chat":
            _fail(f"{agent['id']}: tool-agent requires wire_api chat")
        if native=='native-codex' and model['adapter']=='opencode-go' and model.get('wire_api')!='responses':
            _fail(f"{agent['id']}: native Codex Go requires Responses through the repository runtime")
        if v2 and native == "native-codex" and model["adapter"] != "codex-subscription" and (model["adapter"] not in {"openai-compatible",'opencode-go'} or model.get("wire_api") != "responses"):
            _fail(f"{agent['id']}: native Codex requires a supported wire_api responses service")
    for grant in bundle["grants"]:
        if v2 and any("connection" in model for model in bundle["models"]):
            if grant.get("on_outage", "queue") == "queue" and "availability_wait_seconds" not in grant:
                _fail(f"{grant['id']}: gateway bundles require finite availability_wait_seconds for every queue grant")
        agent = ref("agents", grant["agent"], grant["id"])
        compute = ref("compute", grant["compute"], grant["id"])
        model = idx["models"][agent["model"]]
        harness = idx["harnesses"][agent["harness"]]
        if harness["kind"] == "agent-http":
            site = idx["destinations"][compute["destination"]]
            for field in ("endpoint", "cancel_endpoint"):
                if field in harness:
                    _endpoint(harness[field], harness["id"], local=site["kind"] != "cloud")
                    hosts = site.get("allowed_hosts", [])
                    if hosts and urlsplit(harness[field]).hostname not in hosts:
                        _fail(f"{harness['id']}: agent endpoint is outside its execution destination hosts")
            if bundle["mode"] == "enforced" and not site.get("allowed_hosts"):
                _fail(f"{harness['id']}: enforced external harness requires exact execution destination hosts")
        for field in ("input_class", "output_class", "telemetry_class", "receipt_class", "caller_clearance"):
            if field in grant:
                ref("data_classes", grant[field], grant["id"])
        tools = [idx["tools"][t] for t in agent["tools"]]
        context_rank = max([ranks[grant["input_class"]], *[ranks[t["output_class"]] for t in tools]])
        if ranks[grant["output_class"]] < context_rank:
            _fail(f"{grant['id']}: an agent cannot lower the class of its input or tool results")
        context_rank = max(context_rank, ranks[grant["output_class"]])
        for destination in [model["destination"], compute["destination"], grant["state_destination"]]:
            dest = ref("destinations", destination, grant["id"])
            if ranks[dest["clearance"]] < context_rank:
                _fail(f"{grant['id']}: {destination} cannot receive the resolved context class")
            receiving(dest, context_rank, grant["id"])
            try:
                check_destination(bundle, dest, next(c for c, rank in ranks.items() if rank == context_rank))
            except FlowError as exc:
                _fail(f"{grant['id']}: {exc}")
        for tool in tools:
            if ranks[tool["input_class"]] < context_rank:
                _fail(f"{grant['id']}: {tool['id']} cannot receive this agent's arguments")
            receiving(idx["destinations"][tool["destination"]], context_rank, grant["id"])
            try:
                check_destination(bundle, idx["destinations"][tool["destination"]],
                                  next(c for c, rank in ranks.items() if rank == context_rank))
            except FlowError as exc:
                _fail(f"{grant['id']}: {exc}")
        telemetry = ref("destinations", grant["telemetry_destination"], grant["id"])
        telemetry_class = grant.get("telemetry_class", min(ranks, key=ranks.get))
        if ranks[telemetry["clearance"]] < ranks[telemetry_class]:
            _fail(f"{grant['id']}: telemetry destination clearance is too low")
        receiving(telemetry, ranks[telemetry_class], grant["id"])
        receipt_class = grant.get("receipt_class", min(ranks, key=ranks.get))
        caller_class = grant.get("caller_clearance", receipt_class)
        if ranks[receipt_class] > ranks[caller_class]:
            _fail(f"{grant['id']}: caller cannot receive even the bounded receipt")
    if bundle.get("default_profile"):
        ref("profiles", bundle["default_profile"], "default_profile")
    try:
        validate_output_contracts(bundle)
    except FlowError as exc:
        _fail(str(exc))
    return copy.deepcopy(bundle)


class UniqueLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader: UniqueLoader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, (str, int, bool)) or key in mapping:
            _fail("YAML contains an invalid or duplicate mapping key")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def load_bundle(path: str | Path) -> tuple[dict, str]:
    raw = Path(path).read_bytes()
    if len(raw) > 2_000_000:
        _fail("Bundle exceeds the 2 MB configuration limit")
    try:
        bundle = yaml.load(raw, Loader=UniqueLoader)
    except yaml.YAMLError as exc:
        raise PolicyError("invalid_bundle", "Bundle is not valid YAML") from exc
    bundle = validate_bundle(bundle)
    return bundle, bundle_generation(bundle)


def resolve_recipe(bundle: dict, grant_id: str) -> dict:
    idx = _indexes(bundle)
    if grant_id not in idx["grants"]:
        raise PolicyError("unknown_grant")
    grant = idx["grants"][grant_id]
    agent = idx["agents"][grant["agent"]]
    model = idx["models"][agent["model"]]
    compute = idx["compute"][grant["compute"]]
    tools = [idx["tools"][item] for item in agent["tools"]]
    destination_ids = {model["destination"], compute["destination"], grant["state_destination"],
                       grant["telemetry_destination"], *[tool["destination"] for tool in tools]}
    recipe = {
        "generation": bundle_generation(bundle), "grant": grant, "agent": agent,
        "model": model, "harness": idx["harnesses"][agent["harness"]],
        "tools": tools, "compute": compute,
        "destinations": [idx["destinations"][key] for key in sorted(destination_ids)],
        "mode": bundle["mode"],
    }
    if bundle["apiVersion"] == "sovite.platform/v1alpha2":
        recipe["restricted_class"] = bundle["restricted_class"]
        role = model.get("connection", {}).get("enrollment_role")
        recipe["connector_roles"] = [idx["connector_roles"][role]] if role else []
    return copy.deepcopy(recipe)


def authorize_job(bundle: dict, grant_id: str, caller_id: str, input_class: str | None,
                  *, is_operator: bool = False, workflow_bound: bool = False) -> dict:
    recipe = resolve_recipe(bundle, grant_id)
    grant = recipe["grant"]
    if not grant.get("enabled", True):
        raise PolicyError("grant_disabled")
    if not is_operator and caller_id not in grant["callers"]:
        raise PolicyError("caller_denied")
    if grant.get("workflow_only", False) and not workflow_bound:
        raise PolicyError("workflow_binding_required")
    try:
        selected = resolve_input_class(bundle, input_class)
        check_role_input(bundle, grant_id, selected)
    except FlowError as exc:
        raise PolicyError(exc.code) from exc
    if grant["return_contract"] not in {"reference", "class-aware"}:
        raise PolicyError("unsupported_contract")
    recipe["input_class"] = selected
    return recipe


def flow_projection(bundle: dict) -> dict:
    """Machine-readable declared graph for inspection; no readiness claims."""
    edges = []
    for grant in bundle["grants"]:
        recipe = resolve_recipe(bundle, grant["id"])
        label = grant["output_class"]
        for purpose, destination in (
            ("inference", recipe["model"]["destination"]),
            ("execution", recipe["compute"]["destination"]),
            ("private-state", grant["state_destination"]),
            ("bounded-telemetry", grant["telemetry_destination"]),
        ):
            edges.append({"grant": grant["id"], "purpose": purpose, "destination": destination,
                          "class": grant.get("telemetry_class", "C0") if purpose == "bounded-telemetry" else label})
        for tool in recipe["tools"]:
            edges.append({"grant": grant["id"], "purpose": "tool-arguments-and-results", "destination": tool["destination"],
                          "tool": tool["id"], "class": label})
    projection = {"generation": bundle_generation(bundle), "edges": edges,
                  "readiness": "Declared policy only; verify runtime placement and network enforcement separately."}
    if bundle["apiVersion"] == "sovite.platform/v1alpha2":
        projection.update(copy.deepcopy({key: bundle[key] for key in (
            "restricted_class", "destinations", "models", "connector_roles", "grants")}))
    return projection
