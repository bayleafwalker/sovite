import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
from zipfile import ZipFile

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sovite_package_release", ROOT / "scripts/package_release.py")
package_release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package_release)


def test_release_archive_contains_locked_tsp_sources_and_verified_sdk_inventory(tmp_path):
    output = tmp_path / "sovite-source.zip"
    package_release.package(output)
    with ZipFile(output) as archive:
        names = set(archive.namelist())
        required = {
            "sovite/uv.lock",
            "sovite/contracts/execution-v1/main.tsp",
            "sovite/contracts/execution-v1/tspconfig.yaml",
            "sovite/contracts/execution-v1/contract.json",
            "sovite/contracts/execution-v1/provenance.json",
            "sovite/sovite/execution_contract/runtime.py",
            "sovite/contracts/execution-v2/source/main.tsp",
            "sovite/contracts/execution-v2/source/tspconfig.yaml",
            "sovite/contracts/execution-v2/source/contract.json",
            "sovite/contracts/execution-v2/provenance.json",
            "sovite/sovite/execution_contract/runtime_v2.py",
            "sovite/sovite/execution_contract/vuoro_contract_61440b32de59136facdd786826aa33c916da1dbb981dc6f3fc2115ce2f952d02_client/clients/python/vuoro_contract_61440b32de59136facdd786826aa33c916da1dbb981dc6f3fc2115ce2f952d02_client/data/integrity.json",
            "sovite/sovite/execution_contract/vuoro_contract_c3c73ae52d117dd8407c15d4b4b549461e753e30de4783134a2cad02f5dd1ab4_client/data/integrity.json",
        }
        assert required <= names
        manifest = json.loads(archive.read("sovite/RELEASE-MANIFEST.json"))
        for name in required - {"sovite/RELEASE-MANIFEST.json"}:
            relative = name.removeprefix("sovite/")
            body = archive.read(name)
            assert hashlib.sha256(body).hexdigest() == manifest["files"][relative]["sha256"]
        provenance = json.loads(archive.read("sovite/contracts/execution-v1/provenance.json"))
        sdk_data = "sovite/sovite/execution_contract/vuoro_contract_c3c73ae52d117dd8407c15d4b4b549461e753e30de4783134a2cad02f5dd1ab4_client/data/"
        inventory = json.loads(archive.read(sdk_data + "integrity.json"))
        assert hashlib.sha256(archive.read(sdk_data + "integrity.json")).hexdigest() == provenance["generatedSdk"]["dataIntegritySha256"]
        for path, expected in inventory["files"].items():
            assert hashlib.sha256(archive.read(sdk_data + path)).hexdigest() == expected
        v2 = json.loads(archive.read("sovite/contracts/execution-v2/provenance.json"))
        v2_data = "sovite/sovite/execution_contract/vuoro_contract_61440b32de59136facdd786826aa33c916da1dbb981dc6f3fc2115ce2f952d02_client/clients/python/vuoro_contract_61440b32de59136facdd786826aa33c916da1dbb981dc6f3fc2115ce2f952d02_client/data/"
        v2_inventory = json.loads(archive.read(v2_data + "integrity.json"))
        assert hashlib.sha256(archive.read(v2_data + "integrity.json")).hexdigest() == v2["generatedSdk"]["dataIntegritySha256"]
        for path, expected in v2_inventory["files"].items():
            assert hashlib.sha256(archive.read(v2_data + path)).hexdigest() == expected


def test_source_release_includes_native_luna_catalog_license_with_exact_manifest(tmp_path):
    relative = "sovite/repository_native_luna_catalog.LICENSE"
    expected = (ROOT / relative).read_bytes()
    output = tmp_path / "sovite-source.zip"
    package_release.package(output)

    with ZipFile(output) as archive:
        assert archive.read("sovite/" + relative) == expected
        manifest = json.loads(archive.read("sovite/RELEASE-MANIFEST.json"))
        assert manifest["files"][relative] == {
            "sha256": hashlib.sha256(expected).hexdigest(),
            "bytes": len(expected),
        }


def test_contract_provenance_ignores_runtime_bytecode_but_rejects_sdk_source_edits(tmp_path, monkeypatch):
    sandbox_root = tmp_path / "source"
    source_paths = [ROOT / "uv.lock"]
    for version in ("v1", "v2"):
        provenance = json.loads((ROOT / f"contracts/execution-{version}/provenance.json").read_text())
        sdk_name = provenance["generatedSdk"]["package"].rsplit(".", 1)[-1]
        sdk_source = ROOT / "sovite/execution_contract" / sdk_name
        runtime = ROOT / "sovite/execution_contract" / ("runtime.py" if version == "v1" else "runtime_v2.py")
        contract_root = ROOT / f"contracts/execution-{version}"
        source_paths.extend([runtime, *(path for path in contract_root.rglob("*") if path.is_file())])
        source_paths.extend(path for path in sdk_source.rglob("*")
                            if path.is_file() and path.suffix in package_release.SOURCE_SUFFIXES
                            and "__pycache__" not in path.parts)
    copied = []
    for source in source_paths:
        relative = source.relative_to(ROOT)
        destination = sandbox_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied.append(destination)
    provenance = json.loads((sandbox_root / "contracts/execution-v1/provenance.json").read_text())
    sdk_name = provenance["generatedSdk"]["package"].rsplit(".", 1)[-1]
    sandbox_sdk = sandbox_root / "sovite/execution_contract" / sdk_name
    bytecode = sandbox_sdk / "__pycache__/untracked.cpython-999.pyc"
    bytecode.parent.mkdir(parents=True, exist_ok=True)
    bytecode.write_bytes(b"interpreter-specific cache")

    monkeypatch.setattr(package_release, "ROOT", sandbox_root)
    package_release.validate_contract_provenance(copied)

    source_file = sandbox_sdk / "models/__init__.py"
    source_file.write_bytes(source_file.read_bytes() + b"\n# provenance mutation\n")
    with pytest.raises(ValueError, match="Generated execution SDK tree differs"):
        package_release.validate_contract_provenance(copied)
