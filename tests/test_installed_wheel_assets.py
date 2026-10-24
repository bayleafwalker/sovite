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
    build_python = shutil.which("python") or sys.executable
    subprocess.run([
        build_python, "-m", "pip", "wheel", "--no-index", "--no-deps",
        "--no-build-isolation", "--wheel-dir", str(wheelhouse), str(ROOT),
    ], check=True, cwd=tmp_path, capture_output=True, text=True, timeout=120)
    wheels = list(wheelhouse.glob("sovite_platform-*.whl"))
    assert len(wheels) == 1
    subprocess.run([
        build_python, "-m", "pip", "install", "--no-index", "--no-deps",
        "--target", str(target), str(wheels[0]),
    ], check=True, cwd=tmp_path, capture_output=True, text=True, timeout=60)

    script = """
from importlib.resources import files
from pathlib import Path
import sovite
from sovite.api import create_app
from sovite.settings import Settings

package = Path(sovite.__file__).resolve().parent
assert package.is_relative_to(Path(__import__('os').environ['EXPECTED_PACKAGE_ROOT']).resolve())
for name in __ASSET_NAMES__:
    resource = files('sovite').joinpath('static', name)
    assert resource.is_file() and resource.read_bytes(), name
app = create_app(Settings(operator_token='synthetic-operator-token-000000'))
console = next(route for route in app.routes if getattr(route, 'name', None) == 'console')
assert Path(console.app.directory).resolve() == (package / 'static').resolve()
assert all((Path(console.app.directory) / name).is_file() for name in __ASSET_NAMES__)
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(target)
    env["EXPECTED_PACKAGE_ROOT"] = str(target)
    script = script.replace("__ASSET_NAMES__", repr(ASSETS))
    subprocess.run([sys.executable, "-c", script], check=True, cwd=tmp_path, env=env,
                   capture_output=True, text=True, timeout=30)
