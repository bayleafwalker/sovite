#!/usr/bin/env python3
"""Resolve selected OCI images through Docker, producing actual deployment pins."""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess


def repository(reference):
    value = reference.split("@", 1)[0]
    if value.rfind(":") > value.rfind("/"):
        value = value.rsplit(":", 1)[0]
    first = value.split("/", 1)[0]
    if "/" not in value:
        return "docker.io/library/" + value
    if not ("." in first or ":" in first or first == "localhost"):
        return "docker.io/" + value
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", default="linux/amd64")
    parser.add_argument("--control", required=True, help="Published control image reference; this script never pushes images")
    parser.add_argument("--native-harness")
    parser.add_argument("--postgres", default="postgres:17.11-alpine")
    parser.add_argument("--garage", default="dxflrs/garage:v2.4.1")
    parser.add_argument("--ollama", default="ollama/ollama:0.40.6")
    parser.add_argument("--output", type=Path, default=Path("images.lock.json"))
    args = parser.parse_args()
    if not shutil.which("docker"):
        parser.error("Docker is required to resolve and verify registry images")
    if args.output.exists():
        parser.error("Output exists; use a new filename so the prior pin set remains reviewable")
    result = {"platform": args.platform, "images": {}}
    for key in ("control", "native_harness", "postgres", "garage", "ollama"):
        requested = getattr(args, key)
        if not requested:
            continue
        subprocess.run(["docker", "pull", "--platform", args.platform, requested], check=True)
        output = subprocess.run(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", requested],
                                check=True, text=True, capture_output=True)
        references = [value for value in json.loads(output.stdout) if repository(value) == repository(requested)]
        if not references or not re.search(r"@sha256:[0-9a-f]{64}$", references[0]):
            raise RuntimeError(f"No registry digest returned for {key}")
        result["images"][key] = {"requested": requested, "resolved": references[0]}
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(f"Wrote verified registry references to {args.output}. Review and bind them into the bundle and Helm values before qualification.")


if __name__ == "__main__":
    main()
