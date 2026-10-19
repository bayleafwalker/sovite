#!/usr/bin/env python3
"""Render the complete Helm release and check concrete deployment invariants."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "deploy" / "helm" / "sovite"


def render(helm, values, namespace="sovite", release="sovite"):
    options = [argument for value in values for argument in ("-f", str(value))]
    subprocess.run([helm, "lint", str(CHART), *options, "--strict"], check=True)
    result = subprocess.run([helm, "template", release, str(CHART), "--namespace", namespace, *options],
                            check=True, text=True, capture_output=True)
    resources = [row for row in yaml.safe_load_all(result.stdout) if row]
    if not resources:
        raise ValueError("Helm produced no resources")
    names = [(row["kind"], row["metadata"]["name"]) for row in resources]
    if len(set(names)) != len(names):
        raise ValueError("Duplicate kind/name in the release")
    kinds = {row["kind"] for row in resources}
    for kind in ("Deployment", "Service", "ServiceAccount", "NetworkPolicy"):
        if kind not in kinds:
            raise ValueError("Missing full-platform resource: " + kind)
    for row in resources:
        if row["kind"] not in ("Deployment", "StatefulSet"):
            continue
        spec = row["spec"]["template"]["spec"]
        role = row["spec"]["template"]["metadata"]["labels"]["sovite.component"]
        for container in spec["containers"]:
            if not container.get("readinessProbe") or not container.get("livenessProbe"):
                raise ValueError("Missing lifecycle probes: " + role)
            if not container.get("resources"):
                raise ValueError("Missing resource budgets: " + role)
            if container.get("securityContext", {}).get("allowPrivilegeEscalation") is not False:
                raise ValueError("Privilege escalation is enabled: " + role)
            if role == "native-harness":
                env = {item["name"] for item in container.get("env", [])}
                forbidden = {"SOVITE_DATABASE_URL", "SOVITE_OPERATOR_TOKEN", "SOVITE_CALLER_TOKENS", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"}
                if env & forbidden or any(m["name"] == "trust" for m in container.get("volumeMounts", [])):
                    raise ValueError("Native harness has platform authority or private state credentials")
                if spec.get("automountServiceAccountToken") is not False:
                    raise ValueError("Native harness inherits a Kubernetes API credential")
    # This is an exact check on generated documents, not a claim of cluster enforcement.
    default_denies = [row for row in resources if row["kind"] == "NetworkPolicy" and row["metadata"]["name"].endswith("-default-deny")]
    if not default_denies or default_denies[0]["spec"].get("egress") != [] or default_denies[0]["spec"].get("ingress") != []:
        raise ValueError("Missing default-deny ingress/egress policy")
    for row in resources:
        if row["kind"] == "NetworkPolicy":
            for rule in row["spec"].get("egress", []):
                for target in rule.get("to", []):
                    if target.get("ipBlock", {}).get("cidr") in ("0.0.0.0/0", "::/0"):
                        raise ValueError("Unrestricted egress in a generated policy")
    return result.stdout, {"resources": len(resources), "workloads": [name for kind, name in names if kind in ("Deployment", "StatefulSet")],
                           "scope": "Helm rendering and structural invariants only; no cluster or model execution"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helm", default=shutil.which("helm") or "helm")
    parser.add_argument("--values", action="append", default=[], type=Path)
    parser.add_argument("--namespace", default="sovite")
    parser.add_argument("--release", default="sovite")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--bundle", type=Path, help="Also verify package, service, compute and network bindings against this bundle")
    args = parser.parse_args()
    try:
        binding_report = {}
        if args.bundle:
            from deployment_contract import load_values, validate_bindings
            from sovite.policy import load_bundle
            bundle, _ = load_bundle(args.bundle)
            binding_report = {"bindings": validate_bindings(bundle, load_values(CHART, args.values), args.namespace, release=args.release)}
        text, report = render(args.helm, args.values, args.namespace, args.release)
        report.update(binding_report)
        if args.output:
            args.output.write_text(text)
        print(json.dumps(report, indent=2))
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Deployment validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
