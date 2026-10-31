"""Build and inspect the ordinary wheel without package indexes or dependencies."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ("index.html", "app.js", "styles.css", "mark.svg")


def test_installed_wheel_contains_and_mounts_console_assets(tmp_path):
    wheelhouse = tmp_path / "wheelhouse"
    target = tmp_path / "installed"
    wheelhouse.mkdir()
    target.mkdir()
    build_python = sys.executable
    pip_available = subprocess.run(
        [build_python, "-m", "pip", "--version"], capture_output=True, text=True,
    ).returncode == 0
    if pip_available:
        subprocess.run([
            build_python, "-m", "pip", "wheel", "--no-index", "--no-deps",
            "--no-build-isolation", "--wheel-dir", str(wheelhouse), str(ROOT),
        ], check=True, cwd=tmp_path, capture_output=True, text=True, timeout=120)
    else:
        uv = shutil.which("uv")
        assert uv is not None, "pip or uv is required to build the installed-wheel check"
        env = os.environ.copy()
        env.setdefault("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
        subprocess.run([
            uv, "build", "--no-build-isolation", "--python", build_python,
            "--wheel", "--out-dir", str(wheelhouse), str(ROOT),
        ], check=True, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    wheels = list(wheelhouse.glob("sovite_platform-*.whl"))
    assert len(wheels) == 1
    if pip_available:
        subprocess.run([
            build_python, "-m", "pip", "install", "--no-index", "--no-deps",
            "--target", str(target), str(wheels[0]),
        ], check=True, cwd=tmp_path, capture_output=True, text=True, timeout=60)
    else:
        env = os.environ.copy()
        env.setdefault("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
        subprocess.run([
            shutil.which("uv"), "pip", "install", "--no-deps", "--target", str(target), str(wheels[0]),
        ], check=True, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)

    script = """
from importlib.resources import files
from pathlib import Path
from hashlib import sha256
import sovite
from sovite.api import create_app
from sovite.settings import Settings

package = Path(sovite.__file__).resolve().parent
assert package.is_relative_to(Path(__import__('os').environ['EXPECTED_PACKAGE_ROOT']).resolve())
for name in __ASSET_NAMES__:
    resource = files('sovite').joinpath('static', name)
    assert resource.is_file() and resource.read_bytes(), name
app = create_app(Settings(operator_token='synthetic-operator-token-000000'))
assert not any(getattr(route, 'path', '').startswith('/internal/execution/v1/') for route in app.routes)
from sovite.execution_contract.runtime import load_bundle
bundle = load_bundle()
assert bundle.manifest['contractId'] == 'sovite.execution.v1'
assert len(bundle.manifest['operations']) == 7
console = next(route for route in app.routes if getattr(route, 'name', None) == 'console')
assert Path(console.app.directory).resolve() == (package / 'static').resolve()
assert all((Path(console.app.directory) / name).is_file() for name in __ASSET_NAMES__)
contract = files('sovite.execution_contract.vuoro_contract_c3c73ae52d117dd8407c15d4b4b549461e753e30de4783134a2cad02f5dd1ab4_client')
data = contract.joinpath('data')
integrity = data.joinpath('integrity.json').read_bytes()
assert sha256(integrity).hexdigest() == __DATA_INTEGRITY_SHA256__
assert data.joinpath('operation-manifest.json').is_file()
assert data.joinpath('openapi', 'contract.openapi.json').is_file()
assert data.joinpath('schema', 'contract.schema.json').is_file()
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(target)
    env["EXPECTED_PACKAGE_ROOT"] = str(target)
    script = script.replace("__ASSET_NAMES__", repr(ASSETS))
    script = script.replace("__DATA_INTEGRITY_SHA256__", repr("9cc4d082e2243729ab36f1f88b55722435128e770f5af19e4c007bdb080c5679"))
    subprocess.run([sys.executable, "-c", script], check=True, cwd=tmp_path, env=env,
                   capture_output=True, text=True, timeout=30)


def test_installed_wheel_refuses_invalid_remote_worker_heartbeat_before_journal_io(tmp_path):
    wheelhouse = tmp_path / "wheelhouse"
    target = tmp_path / "installed"
    wheelhouse.mkdir()
    target.mkdir()
    uv = shutil.which("uv")
    assert uv is not None, "uv is required to build the cold installed-wheel check"
    base_env = os.environ.copy()
    base_env.setdefault("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    subprocess.run([
        uv, "build", "--no-build-isolation", "--python", sys.executable,
        "--wheel", "--out-dir", str(wheelhouse), str(ROOT),
    ], check=True, cwd=tmp_path, env=base_env, capture_output=True, text=True, timeout=120)
    wheels = list(wheelhouse.glob("sovite_platform-*.whl"))
    assert len(wheels) == 1
    subprocess.run([
        uv, "pip", "install", "--no-deps", "--target", str(target), str(wheels[0]),
    ], check=True, cwd=tmp_path, env=base_env, capture_output=True, text=True, timeout=60)

    for heartbeat in (None, "relative-heartbeat"):
        journal = tmp_path / ("execution-missing.json" if heartbeat is None else "execution-relative.json")
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("SOVITE_", "AWS_"))}
        env.update(
            PYTHONPATH=str(target),
            SOVITE_EXECUTION_INSTANCE_ID="cold-wheel-startup-test",
            SOVITE_EXECUTION_JOURNAL_PATH=str(journal),
            SOVITE_CONTROL_URL="https://127.0.0.1:9",
            SOVITE_EXECUTION_TOKEN="synthetic-cold-wheel-execution-token",
        )
        if heartbeat is not None:
            env["SOVITE_REMOTE_WORKER_HEALTH_PATH"] = heartbeat
        result = subprocess.run(
            [sys.executable, "-m", "sovite.remote_worker_cli"],
            env=env, cwd=tmp_path, capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 2
        assert result.stderr.strip() == "SOVITE_REMOTE_WORKER_HEALTH_PATH must be an absolute path"
        assert not result.stdout
        assert not journal.exists(), "cold wheel configuration refusal must precede journal I/O"
