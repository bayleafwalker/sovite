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


ID = {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_.-]{0,95}$"}
TEXT = {"type": "string", "maxLength": 16000}
DIGEST = {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}
REFS = {"type": "array", "items": ID, "uniqueItems": True, "maxItems": 100}


def obj(required: list[str], properties: dict) -> dict:
    return {"type": "object", "required": required, "properties": properties,
            "additionalProperties": False}


def named(required: list[str], properties: dict) -> dict:
    return obj(["id", *required], {"id": ID, "name": TEXT, "description": TEXT, **properties})


DEFINITIONS = {
    "data_classes": named(["rank"], {"rank": {"type": "integer", "minimum": 0, "maximum": 100}}),
    "destinations": named(["clearance", "kind"], {
        "clearance": ID, "kind": {"enum": ["local", "private", "cloud"]},
        "allowed_hosts": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 253}, "uniqueItems": True},
    }),
    "models": named(["adapter", "model", "destination"], {
        "adapter": {"enum": ["demo", "ollama", "openai-compatible"]},
        "endpoint": {"type": "string", "minLength": 1, "maxLength": 2048},
        "model": {"type": "string", "minLength": 1, "maxLength": 512},
        "digest": DIGEST, "destination": ID,
        "wire_api": {"enum": ["chat", "responses"]},
        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600},
        "identity_protocol": {"enum": ["ollama"]},
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
        "kind": {"enum": ["demo", "tool-agent", "native-codex", "agent-http"]},
        "image": TEXT, "digest": DIGEST, "version": TEXT,
        "capabilities": REFS, "endpoint": TEXT,
        "cancel_endpoint": TEXT,
        "api_key_env": {"type": "string", "pattern": "^[A-Z][A-Z0-9_]{1,95}$"},
        "executable": {"type": "string", "minLength": 1, "maxLength": 512},
        "idempotent": {"type": "boolean"},
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
        "return_contract": {"const": "reference"}, "enabled": {"type": "boolean"},
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
            "default_profile": ID,
            **{key: {"type": "array", "items": definition, "maxItems": 1000}
               for key, definition in DEFINITIONS.items()},
        }),
}


def bundle_generation(bundle: dict) -> str:
    encoded = json.dumps(bundle, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _indexes(bundle: dict) -> dict[str, dict[str, dict]]:
    return {key: {item["id"]: item for item in bundle[key]} for key in DEFINITIONS}


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
    errors = sorted(Draft202012Validator(SCHEMA).iter_errors(bundle), key=lambda e: str(list(e.path)))
    if errors:
        err = errors[0]
        path = ".".join(map(str, err.absolute_path)) or "bundle"
        _fail(f"{path}: {err.message}")
    for key in DEFINITIONS:
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

    for dst in bundle["destinations"]:
        ref("data_classes", dst["clearance"], dst["id"])
        for host in dst.get("allowed_hosts", []):
            if host in {"*", "0.0.0.0/0", "::/0"} or "/" in host or "*" in host:
                _fail(f"{dst['id']}: destination hosts must be exact hosts; no wildcard egress")
    for model in bundle["models"]:
        dest = ref("destinations", model["destination"], model["id"])
        if model["adapter"] != "demo":
            if not model.get("endpoint"):
                _fail(f"{model['id']}: a serving endpoint is required")
            _endpoint(model["endpoint"], model["id"], local=dest["kind"] != "cloud")
            hosts = dest.get("allowed_hosts", [])
            if hosts and urlsplit(model["endpoint"]).hostname not in hosts:
                _fail(f"{model['id']}: endpoint is outside its declared destination hosts")
            if model.get("tls_trust") and any(urlsplit(url).scheme != "https" for url in (model["endpoint"], model.get("identity_endpoint", model["endpoint"]))):
                _fail(f"{model['id']}: a pinned TLS trust anchor requires HTTPS endpoints")
        if bundle["mode"] == "enforced" and (model["adapter"] == "demo" or not model.get("digest")):
            _fail(f"{model['id']}: enforced installations require a real model with a content digest")
        if model.get("identity_endpoint"):
            _endpoint(model["identity_endpoint"], model["id"], local=dest["kind"] != "cloud")
            hosts = dest.get("allowed_hosts", [])
            if hosts and urlsplit(model["identity_endpoint"]).hostname not in hosts:
                _fail(f"{model['id']}: identity endpoint is outside its declared destination hosts")
        if bundle["mode"] == "enforced" and model["adapter"] != "ollama" and model.get("identity_protocol") != "ollama":
            _fail(f"{model['id']}: this release cannot verify a content digest through a generic provider API")
    for harness in bundle["harnesses"]:
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
        if harness["kind"] == "native-codex" and model["adapter"] not in {"ollama", "openai-compatible"}:
            _fail(f"{agent['id']}: native Codex requires a supported model endpoint")
    for grant in bundle["grants"]:
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
        for tool in tools:
            if ranks[tool["input_class"]] < context_rank:
                _fail(f"{grant['id']}: {tool['id']} cannot receive this agent's arguments")
        telemetry = ref("destinations", grant["telemetry_destination"], grant["id"])
        telemetry_class = grant.get("telemetry_class", min(ranks, key=ranks.get))
        if ranks[telemetry["clearance"]] < ranks[telemetry_class]:
            _fail(f"{grant['id']}: telemetry destination clearance is too low")
        receipt_class = grant.get("receipt_class", min(ranks, key=ranks.get))
        caller_class = grant.get("caller_clearance", receipt_class)
        if ranks[receipt_class] > ranks[caller_class]:
            _fail(f"{grant['id']}: caller cannot receive even the bounded receipt")
    if bundle.get("default_profile"):
        ref("profiles", bundle["default_profile"], "default_profile")
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
    return copy.deepcopy(recipe)


def authorize_job(bundle: dict, grant_id: str, caller_id: str, input_class: str,
                  *, is_operator: bool = False) -> dict:
    recipe = resolve_recipe(bundle, grant_id)
    grant = recipe["grant"]
    if not grant.get("enabled", True):
        raise PolicyError("grant_disabled")
    if not is_operator and caller_id not in grant["callers"]:
        raise PolicyError("caller_denied")
    ranks = {item["id"]: item["rank"] for item in bundle["data_classes"]}
    if input_class not in ranks or ranks[input_class] > ranks[grant["input_class"]]:
        raise PolicyError("class_denied")
    if grant["return_contract"] != "reference":
        raise PolicyError("unsupported_contract")
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
    return {"generation": bundle_generation(bundle), "edges": edges,
            "readiness": "Declared policy only; verify runtime placement and network enforcement separately."}
