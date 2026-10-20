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
ROOT_FILES = {"README.md", "pyproject.toml", "requirements.lock", "Dockerfile", "compose.yaml", ".gitignore", ".dockerignore"}
SOURCE_DIRS = {"sovite", "config", "deploy", "scripts", "tests", "docs"}
SOURCE_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".json", ".toml", ".sh", ".html", ".js", ".css", ".svg", ".txt", ".tpl"}
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


def package(output: Path):
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    files = list(source_files())
    created = datetime.now(timezone.utc)
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
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "sovite-platform-0.1.9.zip")
    args = parser.parse_args()
    print(json.dumps(package(args.output), indent=2))


if __name__ == "__main__":
    main()
