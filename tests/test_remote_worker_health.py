from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from sovite.remote_worker_health import (
    HEALTH_PATH_ENV,
    heartbeat_is_fresh,
    maintain_heartbeat,
    write_heartbeat,
)


ROOT = Path(__file__).resolve().parents[1]


def test_remote_worker_health_is_a_bounded_local_heartbeat(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat"
    assert not heartbeat_is_fresh(path, now=100)

    write_heartbeat(path, now=90)
    assert heartbeat_is_fresh(path, now=100, max_age=10)
    assert not heartbeat_is_fresh(path, now=101, max_age=10)

    path.write_text("not-a-timestamp", encoding="ascii")
    assert not heartbeat_is_fresh(path, now=100)
    path.write_text("101", encoding="ascii")
    assert not heartbeat_is_fresh(path, now=100)


def test_remote_worker_health_command_reports_missing_and_current_heartbeat(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat"
    env = {**os.environ, HEALTH_PATH_ENV: str(path)}

    # The installed entry point calls main; exercise its exact probe behavior.
    no_entrypoint = subprocess.run(
        [sys.executable, "-c", "from sovite.remote_worker_health import main; raise SystemExit(main())"],
        env=env, text=True, capture_output=True,
    )
    assert no_entrypoint.returncode == 1
    write_heartbeat(path)
    healthy = subprocess.run(
        [sys.executable, "-c", "from sovite.remote_worker_health import main; raise SystemExit(main())"],
        env=env, text=True, capture_output=True,
    )
    assert healthy.returncode == 0, healthy.stderr


def test_remote_worker_event_loop_heartbeat_stops_with_owner_event(tmp_path: Path) -> None:
    async def exercise() -> None:
        path = tmp_path / "heartbeat"
        stop = asyncio.Event()
        task = asyncio.create_task(maintain_heartbeat(path, stop))
        try:
            await asyncio.sleep(0)
            assert heartbeat_is_fresh(path)
        finally:
            stop.set()
            await task

    asyncio.run(exercise())


def test_enforced_installer_render_has_remote_worker_lifecycle_probes(tmp_path: Path) -> None:
    # Exercise the release installer (including chart lint, the two-pass review
    # binding, enforced mode and structural probe validation) with remote-worker
    # mode enabled. Synthetic values and trust are created by this test only.
    import json
    import yaml
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    bundle = yaml.safe_load((ROOT / "config/platform.kubernetes.yaml").read_text())
    bundle["mode"] = "enforced"
    # This fixture is a render-only contract probe; keeping it free of active
    # grants avoids introducing fake runtime credentials or network authority.
    bundle["grants"] = []
    for model in bundle["models"]:
        model["digest"] = "sha256:" + "a" * 64
    for harness in bundle["harnesses"]:
        harness["digest"] = "sha256:" + "b" * 64
    bundle_path = tmp_path / "bundle.yaml"
    bundle_path.write_text(yaml.safe_dump(bundle, sort_keys=False))

    trust_dir = tmp_path / "trust"
    trust_dir.mkdir()
    key = Ed25519PrivateKey.generate().public_key()
    (trust_dir / "qualification-public.pem").write_bytes(
        key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )

    values_path = tmp_path / "values.yaml"
    values_path.write_text(yaml.safe_dump({
        "mode": "enforced",
        "image": {"repository": "registry.example/sovite-platform", "tag": "0.1.19", "digest": "sha256:" + "1" * 64},
        "nativeHarness": {"image": {"repository": "registry.example/sovite-platform-codex", "tag": "0.1.19", "digest": "sha256:" + "2" * 64}},
        "postgres": {"image": "postgres:17.11-alpine@sha256:" + "3" * 64},
        "garage": {"image": "dxflrs/garage:v2.4.1@sha256:" + "4" * 64},
        "ollama": {"image": "ollama/ollama:0.40.6@sha256:" + "5" * 64},
        "remoteWorker": {
            "enabled": True,
            "controlUrl": "https://sovite.example.internal",
            "instanceId": "remote-worker-test",
        },
        "networkPolicy": {
            "enabled": True,
            "ciliumDns": True,
            "extraDnsNames": ["sovite.example.internal"],
            "remoteWorkerPeers": [{
                "hosts": ["192.0.2.10"], "ports": [443],
                "cidrs": ["192.0.2.10/32"],
            }],
        },
    }, sort_keys=False))

    output = tmp_path / "review.yaml"
    result = subprocess.run([
        sys.executable, str(ROOT / "scripts/install_kubernetes.py"),
        "--context", "synthetic-test", "--bundle", str(bundle_path),
        "--values", str(values_path), "--trust-dir", str(trust_dir),
        "--render-only", "--render-output", str(output),
    ], cwd=ROOT, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(output.with_suffix(".review.json").read_text())["reviewed_resources"] > 0
    rows = [row for row in yaml.safe_load_all(output.read_text()) if row]
    deployment = next(
        row for row in rows
        if row["kind"] == "Deployment" and row["metadata"]["name"] == "sovite-remote-worker"
    )
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["readinessProbe"]["exec"]["command"] == ["sovite-remote-worker-health"]
    assert container["livenessProbe"]["exec"]["command"] == ["sovite-remote-worker-health"]
    assert {entry["name"] for entry in container["env"]} >= {HEALTH_PATH_ENV}
    assert not {"SOVITE_DATABASE_URL", "SOVITE_S3_ENDPOINT", "AWS_ACCESS_KEY_ID"} & {
        entry["name"] for entry in container["env"]
    }
