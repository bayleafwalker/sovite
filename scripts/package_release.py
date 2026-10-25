#!/usr/bin/env python3
"""Package the source release and per-file hashes without runtime state or secrets."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import stat
import tomllib
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {"README.md", "pyproject.toml", "requirements.lock", "uv.lock", "Dockerfile", "compose.yaml", ".gitignore", ".dockerignore"}
SOURCE_DIRS = {"sovite", "config", "contracts", "deploy", "scripts", "tests", "docs"}
SOURCE_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".json", ".toml", ".sh", ".html", ".js", ".css", ".svg", ".txt", ".tpl", ".tsp"}
EXCLUDED_PARTS = {"__pycache__", ".pytest_cache", ".state", ".venv", "node_modules", "build", "dist", "backups"}


def source_files():
    paths = [ROOT / name for name in ROOT_FILES]
    for directory in SOURCE_DIRS:
        paths.extend(path for path in (ROOT / directory).rglob("*")
                     if path.is_file() and path.suffix in SOURCE_SUFFIXES
                     and not set(path.relative_to(ROOT).parts) & EXCLUDED_PARTS)
    for path in sorted(set(paths)):
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            raise ValueError(f"Release source must be a regular file inside the repository: {path.relative_to(ROOT)}")
        yield path


def validate_contract_provenance(files):
    by_name = {path.relative_to(ROOT).as_posix(): path for path in files}
    provenance_path = "contracts/execution-v1/provenance.json"
    provenance = json.loads(by_name[provenance_path].read_text())
    sdk_root = ROOT / "sovite/execution_contract" / provenance["generatedSdk"]["package"].rsplit(".", 1)[-1]

    for relative, expected in provenance["sourceSha256"].items():
        path = f"contracts/execution-v1/{relative}"
        if path not in by_name or hashlib.sha256(by_name[path].read_bytes()).hexdigest() != expected:
            raise ValueError(f"Execution contract source provenance does not match {path}")
    if provenance["generator"]["runtimePackage"] != "vuoro-contracts==0.2.0":
        raise ValueError("Execution contract runtime package is not the qualified release")
    if provenance["generator"]["runtimeWheelSha256"] != "7bc6558076fff11591b6382641608a95afebb846c254309673351a29854e12c9":
        raise ValueError("Execution contract runtime wheel hash is not the qualified release")

    # Hash exactly the files the source release can contain. Runtime imports
    # create interpreter-specific __pycache__/*.pyc files inside this package;
    # those files are excluded from the release and must not affect provenance.
    package_files = sorted(
        path for path in files
        if path.is_relative_to(sdk_root)
        and path.suffix in SOURCE_SUFFIXES
        and not (set(path.relative_to(sdk_root).parts) & EXCLUDED_PARTS)
    )
    tree_hash = hashlib.sha256()
    for path in package_files:
        relative = path.relative_to(sdk_root).as_posix()
        body = path.read_bytes()
        tree_hash.update(relative.encode("utf-8"))
        tree_hash.update(b"\0")
        tree_hash.update(body)
        tree_hash.update(b"\0")
    if tree_hash.hexdigest() != provenance["generatedSdk"]["treeSha256"]:
        raise ValueError("Generated execution SDK tree differs from its provenance hash")

    integrity_file = sdk_root / "data/integrity.json"
    if hashlib.sha256(integrity_file.read_bytes()).hexdigest() != provenance["generatedSdk"]["dataIntegritySha256"]:
        raise ValueError("Generated execution SDK data inventory hash differs from provenance")
    integrity = json.loads(integrity_file.read_text())
    for relative, expected in integrity["files"].items():
        asset = sdk_root / "data" / relative
        if not asset.is_file() or hashlib.sha256(asset.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Generated execution SDK data hash differs for {relative}")
    runtime = (ROOT / "sovite/execution_contract/runtime.py").read_text()
    if provenance["generatedSdk"]["dataIntegritySha256"] not in runtime:
        raise ValueError("Execution runtime loader is not pinned to the generated SDK inventory")

    required = {
        "uv.lock",
        "contracts/execution-v1/main.tsp",
        "contracts/execution-v1/tspconfig.yaml",
        "contracts/execution-v1/contract.json",
        provenance_path,
        "sovite/execution_contract/runtime.py",
        "sovite/execution_contract/" + provenance["generatedSdk"]["package"].rsplit(".", 1)[-1] + "/data/integrity.json",
    }
    if not required.issubset(by_name):
        raise ValueError("Source release archive is missing locked execution contract inputs or SDK integrity data")


def package(output: Path):
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    files = list(source_files())
    created = datetime.now(timezone.utc)
    validate_contract_provenance(files)
    manifest = {
        "format": "sovite-source-release/v1",
        "version": version,
        "created_at": created.isoformat(),
        "contents": "Application, configuration, deployment, tests and operating evidence",
        "excluded": ["runtime state", "credentials and signing keys", "model weights", "third-party binaries", "internal browser screenshots"],
        "files": {},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            relative = path.relative_to(ROOT).as_posix()
            body = path.read_bytes()
            # Source can refer to secrets, but must never contain a real private key.
            if re.search(rb"(?m)^-----BEGIN (?:RSA |OPENSSH )?PRIVATE KEY-----\r?$", body):
                raise ValueError(f"Private key material found in release source: {relative}")
            manifest["files"][relative] = {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}
            entry = ZipInfo("sovite/" + relative, date_time=created.timetuple()[:6])
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | (0o755 if path.suffix == ".sh" else 0o644)) << 16
            entry.compress_type = ZIP_DEFLATED
            archive.writestr(entry, body)
        archive.writestr("sovite/RELEASE-MANIFEST.json", json.dumps(manifest, indent=2) + "\n")
    with ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise ValueError("Release archive verification failed")
        for relative, expected in manifest["files"].items():
            body = archive.read("sovite/" + relative)
            if hashlib.sha256(body).hexdigest() != expected["sha256"]:
                raise ValueError("Release member hash differs from manifest")
    return {"file": str(output.resolve()), "version": version, "source_files": len(files),
            "bytes": output.stat().st_size, "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "sovite-platform-0.1.19.zip")
    args = parser.parse_args()
    print(json.dumps(package(args.output), indent=2))


if __name__ == "__main__":
    main()
