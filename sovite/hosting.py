"""Host-owned limits for the service-only hosted profile."""
from __future__ import annotations

import copy
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

from .policy import DEFINITIONS, DIGEST, ID, V2_DESTINATION, PolicyError, bundle_generation, obj, validate_bundle


NAMESPACE = {"type": "string", "pattern": r"^[a-z][a-z0-9-]{0,62}$(?!\n)"}
DESTINATION = copy.deepcopy(DEFINITIONS["destinations"])
DESTINATION["required"].append("retention")
DESTINATION["properties"]["retention"] = copy.deepcopy(V2_DESTINATION["properties"]["retention"])
SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "urn:sovite:hosting:v1", "title": "HostingEnvelope",
    **obj(["apiVersion", "workspace", "installation_id", "revision", "placement", "allowed_data_classes", "destinations", "connector_kinds", "connector_image_digests", "harnesses"], {
        "apiVersion": {"const": "sovite.hosting/v1"}, "workspace": ID, "installation_id": ID,
        "revision": {"type": "integer", "minimum": 1, "maximum": 2147483647},
        "placement": obj(["namespace", "resources"], {
            "namespace": NAMESPACE,
            "resources": obj(["max_replicas", "max_cpu_millis", "max_memory_bytes"], {
                "max_replicas": {"type": "integer", "minimum": 1, "maximum": 1000},
                "max_cpu_millis": {"type": "integer", "minimum": 1, "maximum": 1000000},
                "max_memory_bytes": {"type": "integer", "minimum": 1, "maximum": 1099511627776},
            }),
        }),
        "allowed_data_classes": {"type": "array", "items": {"enum": ["C0", "C1"]}, "minItems": 1, "maxItems": 2, "uniqueItems": True, "contains": {"const": "C0"}},
        "destinations": {"type": "array", "items": DESTINATION, "minItems": 1, "maxItems": 100},
        "connector_kinds": {"type": "array", "items": {"const": "model-gateway"}, "minItems": 1, "maxItems": 1, "uniqueItems": True},
        "connector_image_digests": {"type": "array", "items": DIGEST, "minItems": 1, "maxItems": 100, "uniqueItems": True},
        "harnesses": {"type": "array", "items": DEFINITIONS["harnesses"], "minItems": 1, "maxItems": 100},
    }),
}


def bounded_json(raw: bytes, limit: int = 262144):
    if len(raw) > limit:
        raise ValueError("Document exceeds size limit")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite JSON number")))
    except (RecursionError, UnicodeError) as exc:
        raise ValueError("Invalid JSON document") from exc


def load_json(path: str | Path, limit: int = 262144):
    with Path(path).open("rb") as stream:
        return bounded_json(stream.read(limit + 1), limit)


def _service(host: str, namespace: str) -> bool:
    return bool(re.fullmatch(r"[a-z][a-z0-9-]{0,62}\." + re.escape(namespace) + r"\.svc(?:\.cluster\.local)?", host))


def validate_envelope(value: dict) -> dict:
    try:
        if len(json.dumps(value, allow_nan=False).encode()) > 262144:
            raise ValueError("Hosting envelope exceeds size limit")
    except (RecursionError, TypeError) as exc:
        raise ValueError("Invalid hosting envelope") from exc
    errors = list(Draft202012Validator(SCHEMA).iter_errors(value))
    if errors:
        raise ValueError("Invalid hosting envelope: " + errors[0].message)
    namespace = value["placement"]["namespace"]
    for section in ("destinations", "harnesses"):
        ids = [item["id"] for item in value[section]]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate hosting " + section)
    for destination in value["destinations"]:
        if destination["kind"] != "private" or destination["clearance"] not in value["allowed_data_classes"] or not destination.get("allowed_hosts"):
            raise ValueError("Hosted destinations require approved classes and exact private service hosts")
        if any(not _service(host, namespace) for host in destination["allowed_hosts"]):
            raise ValueError("Hosted destinations must be services in the approved namespace")
    hosts = {host for destination in value["destinations"] for host in destination["allowed_hosts"]}
    for harness in value["harnesses"]:
        if harness["kind"] == "demo" or not harness.get("digest") or any(field in harness for field in ("image", "executable", "api_key_env")):
            raise ValueError("Hosted harnesses require approved packages without images, executables or secrets")
        for field in ("endpoint", "cancel_endpoint"):
            if field in harness:
                endpoint = urlsplit(harness[field])
                if endpoint.scheme not in {"http", "https"} or endpoint.hostname not in hosts or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
                    raise ValueError("Hosted harness endpoints must be approved internal services")
    return copy.deepcopy(value)


def load_envelope(path: str | Path | None) -> dict | None:
    if path is None:
        return None
    return validate_envelope(load_json(path))


def envelope_binding(envelope: dict) -> dict:
    return {"digest": bundle_generation(envelope), "revision": envelope["revision"], "workspace": envelope["workspace"], "installation_id": envelope["installation_id"]}


def hosted_generation(bundle: dict, envelope: dict | None) -> str:
    if envelope is None:
        return bundle_generation(bundle)
    return bundle_generation({"bundle": bundle, "hosting": envelope_binding(envelope)})


def _quantity(value, *, memory: bool) -> Decimal:
    pattern = r"([0-9]+(?:\.[0-9]+)?)(Ki|Mi|Gi|Ti)?" if memory else r"([0-9]+(?:\.[0-9]+)?)(m)?"
    match = re.fullmatch(pattern, str(value))
    if not match:
        raise ValueError("Unsupported hosted resource quantity")
    unit = match[2]
    try:
        return Decimal(match[1]) * ({None: 1, "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4}[unit] if memory else (1 if unit == "m" else 1000))
    except InvalidOperation as exc:
        raise ValueError("Invalid hosted resource quantity") from exc


def compile_bundle(bundle: dict, envelope: dict | None = None) -> tuple[dict, str]:
    bundle = validate_bundle(bundle)
    if envelope is None:
        return bundle, bundle_generation(bundle)
    envelope = validate_envelope(envelope)
    try:
        if len(json.dumps(bundle, allow_nan=False).encode()) > 2_000_000:
            raise ValueError("Bundle exceeds size limit")
        if bundle["apiVersion"] != "sovite.platform/v1alpha2" or bundle["mode"] != "enforced":
            raise ValueError("Hosted bundles require enforced v1alpha2")
        classes = envelope["allowed_data_classes"]
        for item in bundle["data_classes"]:
            if item["id"] not in {"C0", "C1", "C2", "C3"} or item["rank"] != int(item["id"][1]):
                raise ValueError("Hosted data classes require canonical C0..C3 ranks")
        approved = {item["id"]: item for item in envelope["destinations"]}
        for destination in bundle["destinations"]:
            declaration = {key: value for key, value in destination.items() if key in DESTINATION["properties"]}
            if declaration != approved.get(destination["id"]) or destination["clearance"] not in classes or destination["trust_tier"] != "operator-owned" or "restricted_clearance" in destination:
                raise ValueError("Destination exceeds hosting envelope")
        if bundle["tools"] or any(agent["tools"] for agent in bundle["agents"]):
            raise ValueError("Hosted tools are not supported")
        for model in bundle["models"]:
            if model.get("connection", {}).get("kind") not in envelope["connector_kinds"] or any(field in model for field in ("endpoint", "identity_endpoint", "api_key_env", "tls_trust")):
                raise ValueError("Hosted models require internal model-gateway connections only")
        for role in bundle["connector_roles"]:
            if any(role[field] not in envelope["connector_image_digests"] for field in ("helper_image_digest", "engine_image_digest")):
                raise ValueError("Connector package is outside hosting envelope")
        for harness in bundle["harnesses"]:
            if harness not in envelope["harnesses"]:
                raise ValueError("Harness package is outside hosting envelope")
        limits = envelope["placement"]["resources"]
        replicas, cpu, memory = 0, Decimal(0), Decimal(0)
        for compute in bundle["compute"]:
            if compute["kind"] != "kubernetes" or compute.get("namespace") != envelope["placement"]["namespace"]:
                raise ValueError("Compute must use the host-selected namespace")
            profile = compute.get("profile", {})
            if set(profile) != {"replicas", "cpu", "memory"} or profile["replicas"] < 1 or profile["replicas"] > limits["max_replicas"]:
                raise ValueError("Compute profile must declare bounded replicas, cpu and memory only")
            if not 0 < _quantity(profile["cpu"], memory=False) <= limits["max_cpu_millis"] or not 0 < _quantity(profile["memory"], memory=True) <= limits["max_memory_bytes"]:
                raise ValueError("Compute resources exceed hosting envelope")
            replicas += profile["replicas"]
            cpu += _quantity(profile["cpu"], memory=False) * profile["replicas"]
            memory += _quantity(profile["memory"], memory=True) * profile["replicas"]
        if replicas > limits["max_replicas"] or cpu > limits["max_cpu_millis"] or memory > limits["max_memory_bytes"]:
            raise ValueError("Aggregate compute resources exceed hosting envelope")
        for grant in bundle["grants"]:
            if any(grant[field] not in classes for field in ("input_class", "output_class", "telemetry_class", "receipt_class", "caller_clearance") if field in grant):
                raise ValueError("Grant class exceeds hosting envelope")
    except (ValueError, RecursionError) as exc:
        raise PolicyError("invalid_bundle", str(exc)) from exc
    return bundle, hosted_generation(bundle, envelope)
