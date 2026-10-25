import hashlib
import importlib.util
import json
from pathlib import Path
from zipfile import ZipFile


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
