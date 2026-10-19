#!/usr/bin/env python3
"""Render, bind and optionally apply a complete private Kubernetes installation.

The review contains public configuration and secret references. Credential values
are read only for apply and are sent to kubectl on stdin, never to Helm values.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import yaml

from deployment_contract import load_values, validate_bindings
from validate_deployment import CHART, render


def run(args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, text=True, capture_output=True, **kwargs)


def digest(value):
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def chart_identity():
    return {str(path.relative_to(CHART)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(CHART.rglob("*")) if path.is_file()}


def read_trust(values, directory):
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    trust = {}
    for filename in values["qualification"]["publicKeyFiles"]:
        if Path(filename).name != filename:
            raise ValueError("Qualification key names must be plain filenames")
        content = (directory / filename).read_text()
        if "PRIVATE KEY" in content or not isinstance(load_pem_public_key(content.encode()), Ed25519PublicKey):
            raise ValueError("Trust configuration requires Ed25519 public keys")
        trust[filename] = content
    return trust


def public_prerequisites(bundle_text, values, namespace, trust):
    return [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}},
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": values["bundle"]["configMap"], "namespace": namespace},
         "data": {values["bundle"]["key"]: bundle_text}},
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": values["qualification"]["configMap"], "namespace": namespace}, "data": trust},
    ]


def normalized_resources(resources):
    """Remove only the self-reference from the fingerprint's input."""
    result = copy.deepcopy(resources)
    for resource in result:
        pod = resource.get("spec", {}).get("template", {}).get("spec", {})
        for container in pod.get("containers", []) + pod.get("initContainers", []):
            for entry in container.get("env", []):
                if entry.get("name") == "SOVITE_DEPLOYMENT_BINDING":
                    entry["value"] = "<deployment-binding>"
    return sorted(result, key=lambda row: (row["kind"], row["metadata"].get("namespace", ""), row["metadata"]["name"]))


def fingerprint(resources, chart, values, generation, context, namespace, release):
    public_values = copy.deepcopy(values)
    public_values["deploymentBinding"] = "<deployment-binding>"
    return digest({"schema": "sovite.deployment-binding/v1", "resources": normalized_resources(resources),
                   "chart_files": chart, "values": public_values, "bundle_generation": generation,
                   "context": context, "namespace": namespace, "release": release})


def required_secret_keys(values):
    keys = {"SOVITE_DATABASE_URL", "SOVITE_OPERATOR_TOKEN", "SOVITE_CALLER_TOKENS", "SOVITE_HARNESS_TOKEN", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"}
    if values["postgres"]["enabled"]:
        keys.add("POSTGRES_PASSWORD")
    if values["garage"]["enabled"]:
        keys |= {"GARAGE_RPC_SECRET", "GARAGE_ADMIN_TOKEN"}
    return keys


def validate_credentials(credentials, values, bundle):
    if not isinstance(credentials, dict) or required_secret_keys(values) - credentials.keys():
        raise ValueError("Runtime secret JSON is missing required keys")
    if not all(isinstance(value, str) and value for value in credentials.values()):
        raise ValueError("Runtime credentials must be nonempty strings")
    if any(len(credentials[key]) < 24 for key in ("SOVITE_OPERATOR_TOKEN", "SOVITE_HARNESS_TOKEN")):
        raise ValueError("Runtime authority tokens require at least 24 characters")
    callers = json.loads(credentials["SOVITE_CALLER_TOKENS"])
    if not isinstance(callers, dict) or any(not isinstance(token, str) or len(token) < 24 for token in callers.values()):
        raise ValueError("Caller credentials must be an object of tokens with at least 24 characters")
    declared_callers = {caller for grant in bundle["grants"] if grant.get("enabled", True) for caller in grant["callers"]}
    if declared_callers - callers.keys():
        raise ValueError("An enabled grant's caller has no provisioned credential")
    authorities = [credentials["SOVITE_OPERATOR_TOKEN"], credentials["SOVITE_HARNESS_TOKEN"], *callers.values()]
    if len(set(authorities)) != len(authorities):
        raise ValueError("Operator, harness and caller credentials must be distinct")


def observed_api_endpoints(kubectl, values):
    if not values["kubernetesTools"]["enabled"]:
        return None
    result = run([*kubectl, "get", "endpointslices.discovery.k8s.io", "-n", "default", "-l", "kubernetes.io/service-name=kubernetes", "-o", "json"])
    slices = json.loads(result.stdout)["items"]
    addresses = {address for row in slices for point in row.get("endpoints", [])
                 if point.get("conditions", {}).get("ready") is not False for address in point.get("addresses", [])}
    ports = {port["port"] for row in slices for port in row.get("ports", []) if port.get("protocol", "TCP") == "TCP"}
    if not addresses or ports != {values["kubernetesTools"]["apiServerPort"]}:
        raise ValueError("Configured API port does not match the selected cluster's observed Kubernetes service endpoints")
    return addresses


def build_review(args):
    from sovite.policy import load_bundle

    bundle, generation = load_bundle(args.bundle)
    bundle_text = args.bundle.read_text()
    values = load_values(CHART, args.values)
    bindings = validate_bindings(bundle, values, args.namespace, release=args.release)
    trust = read_trust(values, args.trust_dir)
    prerequisites = public_prerequisites(bundle_text, values, args.namespace, trust)
    values["bundle"]["revision"] = hashlib.sha256(bundle_text.encode()).hexdigest()
    chart = chart_identity()
    # A real hash of inputs lets enforced templates render before the final
    # manifest hash is known; it is replaced before any review is published.
    values["deploymentBinding"] = digest({"chart": chart, "bundle": generation, "trust": trust})
    args.render_output.parent.mkdir(parents=True, exist_ok=True)
    effective = args.render_output.with_suffix(".effective-values.yaml")
    effective.write_text(yaml.safe_dump(values, sort_keys=True))
    output, _ = render(args.helm, [effective], args.namespace, args.release)
    resources = prerequisites + [row for row in yaml.safe_load_all(output) if row]
    binding = fingerprint(resources, chart, values, generation, args.context, args.namespace, args.release)
    values["deploymentBinding"] = binding
    effective.write_text(yaml.safe_dump(values, sort_keys=True))
    output, report = render(args.helm, [effective], args.namespace, args.release)
    resources = prerequisites + [row for row in yaml.safe_load_all(output) if row]
    if fingerprint(resources, chart, values, generation, args.context, args.namespace, args.release) != binding:
        raise ValueError("Deployment fingerprint is not stable across rendering")
    if chart_identity() != chart:
        raise ValueError("Chart changed during rendering; repeat review")
    args.render_output.write_text(yaml.safe_dump_all(resources, sort_keys=False))
    prerequisites_path = args.render_output.with_suffix(".prerequisites.yaml")
    prerequisites_path.write_text(yaml.safe_dump_all(prerequisites, sort_keys=False))
    summary = {"schema": "sovite.deployment-review/v1", "context": args.context, "namespace": args.namespace, "release": args.release,
               "deployment_binding": binding, "bundle_generation": generation, "bundle_file_sha256": values["bundle"]["revision"],
               "chart_file_sha256": chart, "effective_values": str(effective),
               "effective_values_sha256": hashlib.sha256(effective.read_bytes()).hexdigest(),
               "public_prerequisites": str(prerequisites_path), "reviewed_resources": len(resources),
               "secret_reference": {"name": values["runtimeSecret"], "namespace": args.namespace, "required_keys": sorted(required_secret_keys(values))},
               "bindings": bindings, **report}
    args.render_output.with_suffix(".review.json").write_text(json.dumps(summary, indent=2) + "\n")
    return bundle, values, prerequisites, effective, summary, chart


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", default="sovite")
    parser.add_argument("--release", default="sovite")
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--values", required=True, action="append", type=Path)
    parser.add_argument("--runtime-env", type=Path, help="JSON runtime credentials; read only when applying")
    parser.add_argument("--trust-dir", required=True, type=Path, help="Declared Ed25519 public keys, included in the review")
    parser.add_argument("--helm", default="helm")
    parser.add_argument("--render-only", action="store_true")
    parser.add_argument("--render-output", type=Path, default=Path("release.rendered.yaml"))
    args = parser.parse_args()
    try:
        bundle, values, prerequisites, effective, summary, chart = build_review(args)
        print(json.dumps(summary, indent=2))
        if args.render_only:
            return 0
        if not args.runtime_env:
            raise ValueError("Apply requires --runtime-env")
        credentials = json.loads(args.runtime_env.read_text())
        validate_credentials(credentials, values, bundle)
        kubectl = ["kubectl", "--context", args.context]
        if values["networkPolicy"]["ciliumDns"]:
            run([*kubectl, "get", "crd", "ciliumnetworkpolicies.cilium.io"])
        addresses = observed_api_endpoints(kubectl, values)
        validate_bindings(bundle, values, args.namespace, release=args.release, live_api_addresses=addresses, credentials=credentials)
        if chart_identity() != chart:
            raise ValueError("Chart changed after review; repeat the installation")
        if hashlib.sha256(effective.read_bytes()).hexdigest() != summary["effective_values_sha256"]:
            raise ValueError("Effective values changed after review; repeat the installation")
        for resource in prerequisites:
            run([*kubectl, "apply", "-f", "-"], input=json.dumps(resource))
        secret = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": values["runtimeSecret"], "namespace": args.namespace},
                  "type": "Opaque", "stringData": credentials}
        run([*kubectl, "apply", "-f", "-"], input=json.dumps(secret))
        run([args.helm, "upgrade", "--install", args.release, str(CHART), "--kube-context", args.context,
             "--namespace", args.namespace, "-f", effective, "--wait", "--timeout", "10m"])
        print("Release reconciled with deployment binding " + summary["deployment_binding"] + ". Collect deployment evidence before grant activation.")
    except (ValueError, OSError, subprocess.CalledProcessError, KeyError, TypeError) as error:
        # Subprocess output and malformed credential exceptions can contain
        # secrets. A configuration-only render error can safely explain itself.
        detail = f": {error}" if args.render_only and not isinstance(error, subprocess.CalledProcessError) else ""
        print(f"Kubernetes installation failed: {type(error).__name__}{detail}. No credential values are logged.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
