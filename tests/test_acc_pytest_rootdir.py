"""Acceptance: the plain pytest entry point collects from the repo root (planner-owned)."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTEST = Path(sys.executable).parent / "pytest"


def _collect(*targets):
    return subprocess.run([str(PYTEST), "--collect-only", "-q", "-p", "no:cacheprovider", *targets],
                          cwd=ROOT, capture_output=True, text=True)


def test_plain_pytest_collects_root_relative_imports():
    p = _collect('tests/test_pg_test_diagnostics.py')
    assert p.returncode == 0, p.stdout[-1500:] + p.stderr[-500:]
    assert "error" not in p.stdout.splitlines()[-1].lower()
