"""Static role-separation checks for the offline Compose composition."""
from pathlib import Path
import json
import os
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_compose_worker_does_not_inherit_control_auth_or_harness_tokens():
    document = yaml.safe_load((ROOT / "compose.yaml").read_text())
    control = document["services"]["control"]["environment"]
    worker = document["services"]["worker"]["environment"]

    assert {"SOVITE_OPERATOR_TOKEN", "SOVITE_CALLER_TOKENS"} <= set(control)
    assert not ({"SOVITE_OPERATOR_TOKEN", "SOVITE_CALLER_TOKENS", "SOVITE_HARNESS_TOKEN"} & set(worker))


def test_opt_in_remote_worker_has_owner_token_but_no_platform_or_storage_credentials():
    document = yaml.safe_load((ROOT / "compose.yaml").read_text())
    remote = document["services"]["remote-worker"]
    env = remote["environment"]
    assert remote["profiles"] == ["remote-worker"]
    assert remote["entrypoint"] == ["sovite-remote-worker"]
    assert env["SOVITE_CONTROL_URL"] == "${SOVITE_REMOTE_CONTROL_URL:-}"
    assert env["SOVITE_EXECUTION_TOKEN"] == "${SOVITE_EXECUTION_TOKEN:-}"
    assert remote["volumes"] == ["remote-execution-state:/state"]
    forbidden = {
        "SOVITE_DATABASE_URL", "SOVITE_DATABASE_SCHEMA", "SOVITE_DATABASE_MIGRATE",
        "SOVITE_ARTIFACT_PATH", "SOVITE_S3_ENDPOINT", "SOVITE_S3_BUCKET",
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "SOVITE_OPERATOR_TOKEN",
        "SOVITE_AUTHORITY_TOKEN", "SOVITE_CALLER_TOKENS", "SOVITE_EXECUTION_TOKENS",
    }
    assert forbidden.isdisjoint(env)


def test_docker_compose_render_keeps_control_auth_out_of_worker():
    docker = shutil.which("docker")
    if not docker:
        pytest.skip("Docker Compose is required for the offline composition render")
    version = subprocess.run([docker, "compose", "version"], capture_output=True, text=True)
    if version.returncode:
        pytest.skip("Docker Compose plugin is unavailable")

    env = os.environ.copy()
    env.update({
        "POSTGRES_PASSWORD": "synthetic-db-password",
        "AWS_ACCESS_KEY_ID": "synthetic-storage-key",
        "AWS_SECRET_ACCESS_KEY": "synthetic-storage-secret",
        "SOVITE_OPERATOR_TOKEN": "synthetic-operator-token",
        "SOVITE_CALLER_TOKENS": '{"synthetic-caller":"synthetic-caller-token"}',
        "SOVITE_EXECUTION_TOKENS": '{"synthetic-workload":"synthetic-execution-token"}',
        "GARAGE_RPC_SECRET": "synthetic-garage-rpc-secret",
        "GARAGE_ADMIN_TOKEN": "synthetic-garage-admin-token",
    })
    result = subprocess.run([docker, "compose", "--profile", "remote-worker", "-f", str(ROOT / "compose.yaml"),
                             "config", "--format", "json"], cwd=ROOT, env=env,
                            capture_output=True, text=True, check=True, timeout=30)
    document = json.loads(result.stdout)
    control = document["services"]["control"]["environment"]
    worker = document["services"]["worker"]["environment"]
    assert {"SOVITE_OPERATOR_TOKEN", "SOVITE_CALLER_TOKENS"} <= set(control)
    assert control["SOVITE_EXECUTION_TOKENS"] == '{"synthetic-workload":"synthetic-execution-token"}'
    assert not ({"SOVITE_OPERATOR_TOKEN", "SOVITE_CALLER_TOKENS", "SOVITE_HARNESS_TOKEN"} & set(worker))
    remote = document["services"]["remote-worker"]
    remote_env = remote["environment"]
    assert remote_env["SOVITE_CONTROL_URL"] == ""
    assert remote_env["SOVITE_EXECUTION_TOKEN"] == ""
    assert remote["volumes"] == [{"type": "volume", "source": "remote-execution-state", "target": "/state", "volume": {}}]
    assert not ({"SOVITE_DATABASE_URL", "SOVITE_S3_ENDPOINT", "AWS_ACCESS_KEY_ID",
                 "AWS_SECRET_ACCESS_KEY", "SOVITE_OPERATOR_TOKEN", "SOVITE_CALLER_TOKENS"}
                & set(remote_env))
