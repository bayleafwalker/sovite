"""Owned local C0 evaluation fixture; never a hosted installation or qualification."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit

import httpx
import yaml

from .policy import validate_bundle
from .qualification import (
    initialize_keys,
    issue_qualification,
    runtime_settings_fingerprint,
)
from .service import Platform
from .settings import Settings

HANDOVER_INSTRUCTIONS = (
    "Return only one JSON object, no markdown, analysis or planning. "
    'Use exactly this structure: {"observations":[{"source_id":"M1","fact":"exact source text",'
    '"unknown":"what remains unknown","next_check":"a cautious proposed check","owner":"unassigned"},'
    '{"source_id":"M2","fact":"exact source text","unknown":"what remains unknown",'
    '"next_check":"a cautious proposed check","owner":"unassigned"},'
    '{"source_id":"M3","fact":"exact source text","unknown":"what remains unknown",'
    '"next_check":"a cautious proposed check","owner":"unassigned"}],"outcomes_verified":false}. '
    "Copy each source text exactly into fact. Do not invent causes, measurements, people, prices, "
    "deadlines or completed repairs. Unknowns are uncertainty, proposed checks are future actions. "
    "Keep unknown and next_check concise. No repairs or outcomes have been verified."
)


def local_model(endpoint: str, name: str, expected_digest: str) -> dict:
    url = urlsplit(endpoint)
    if (
        url.scheme != "http"
        or url.hostname != "127.0.0.1"
        or not url.port
        or url.path
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError("Evaluation requires an explicit loopback model origin")
    with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as client:
        rows = client.get(endpoint + "/api/tags").raise_for_status().json()["models"]
        observed = next(
            (m for m in rows if m["name"] == name and m["digest"] == expected_digest),
            None,
        )
        if observed is None:
            raise ValueError("The explicitly selected model/digest is not installed")
        info = (
            client.post(endpoint + "/api/show", json={"model": name})
            .raise_for_status()
            .json()
        )
        # The failed qwen3:4b alias is Thinking-2507; think:false cannot turn it
        # into Instruct. Refuse that known-incompatible tuple before any source.
        if info.get("model_info", {}).get("general.finetune") == "Thinking":
            raise ValueError(
                "This final-answer evaluation requires an explicitly selected non-thinking model"
            )
    return {
        "id": "handover-model",
        "name": "Maintenance handover model",
        "adapter": "ollama",
        "endpoint": endpoint,
        "model": name,
        "digest": "sha256:" + expected_digest,
        "destination": "private-runtime",
        "timeout_seconds": 600,
        "parameters": {
            "temperature": 0,
            "think": False,
            "num_predict": 1024,
            "num_ctx": 4096,
        },
    }


def evaluation_bundle(model: dict) -> dict:
    return validate_bundle(
        {
            "apiVersion": "sovite.platform/v1alpha1",
            "kind": "PlatformBundle",
            "metadata": {
                "name": "bayleaf-private-workflow",
                "display_name": "bayleaf workspace",
                "version": "1",
            },
            "mode": "development",
            "default_profile": "private-evaluation",
            "data_classes": [
                {"id": "C0", "name": "Public fixture", "rank": 0},
                {"id": "C1", "name": "Operational metadata", "rank": 1},
            ],
            "destinations": [
                {
                    "id": "private-runtime",
                    "name": "Local runtime",
                    "kind": "private",
                    "clearance": "C0",
                    "allowed_hosts": ["127.0.0.1"],
                },
                {
                    "id": "private-state",
                    "name": "Private artifacts",
                    "kind": "private",
                    "clearance": "C0",
                },
                {
                    "id": "events",
                    "name": "Bounded events",
                    "kind": "private",
                    "clearance": "C1",
                },
            ],
            "models": [{**model, "id": "bootstrap-model"}],
            "harnesses": [
                {
                    "id": "analysis",
                    "name": "Bounded local analysis",
                    "kind": "tool-agent",
                }
            ],
            "tools": [],
            "agents": [
                {
                    "id": "bootstrap-agent",
                    "name": "Bootstrap",
                    "harness": "analysis",
                    "model": "bootstrap-model",
                    "tools": [],
                }
            ],
            "compute": [
                {
                    "id": "local",
                    "name": "Local worker",
                    "kind": "local",
                    "destination": "private-runtime",
                }
            ],
            "grants": [
                {
                    "id": "maintenance",
                    "name": "Maintenance handover",
                    "agent": "bootstrap-agent",
                    "callers": ["workspace-agent"],
                    "input_class": "C0",
                    "output_class": "C0",
                    "compute": "local",
                    "state_destination": "private-state",
                    "telemetry_destination": "events",
                    "telemetry_class": "C1",
                    "receipt_class": "C1",
                    "caller_clearance": "C1",
                    "return_contract": "reference",
                    "timeout_seconds": 600,
                    "max_attempts": 1,
                }
            ],
            "profiles": [
                {
                    "id": "private-evaluation",
                    "name": "Local C0 evaluation",
                    "components": ["control", "worker", "private-artifacts", "model"],
                }
            ],
        }
    )


def record_children(root: Path, children: list):
    """Advisory resource ledger for owned process cleanup checks, no authority."""
    fd = os.open(
        root / "owned-processes.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
    )
    with os.fdopen(fd, "w") as stream:
        json.dump({"pids": [child.pid for child in children]}, stream)


@dataclass
class Evaluation:
    root: Path
    settings: Settings
    platform: Platform
    model: dict
    origin: str
    caller_token: str
    children: list
    environment: dict
    log: object

    def client(self):
        return httpx.Client(
            base_url=self.origin,
            headers={"Authorization": "Bearer " + self.settings.operator_token},
            trust_env=False,
            follow_redirects=False,
            timeout=20,
        )

    def setup(self, *, instructions: str = HANDOVER_INSTRUCTIONS) -> dict:
        with self.client() as owner:
            principal = (
                owner.post(
                    "/api/setup/principals",
                    json={
                        "name": "bayleaf setup agent",
                        "ttl_seconds": 3600,
                        "envelope": {
                            "models": [self.model],
                            "harnesses": ["analysis"],
                            "grants": ["maintenance"],
                            "max_steps": 2,
                        },
                    },
                )
                .raise_for_status()
                .json()
            )
            token = principal.pop("token")
            with httpx.Client(
                base_url=self.origin,
                headers={"Authorization": "Bearer " + token},
                trust_env=False,
                timeout=20,
            ) as agent:
                context = agent.get("/api/setup/context").raise_for_status().json()
                draft = (
                    agent.post(
                        "/api/setup/drafts",
                        json={
                            "idempotency_key": "wf01-setup",
                            "base_generation": context["generation"],
                            "model": self.model,
                            "agent_id": "maintenance-helper",
                            "agent_name": "Maintenance handover helper",
                            "harness": "analysis",
                            "instructions": instructions,
                            "max_steps": 2,
                            "grant": "maintenance",
                        },
                    )
                    .raise_for_status()
                    .json()
                )
                path = "/api/setup/drafts/" + draft["id"] + "/activate"
                if (
                    agent.post(
                        path, json={"generation": draft["generation"]}
                    ).status_code
                    != 401
                ):
                    raise RuntimeError(
                        "Setup agent unexpectedly activated configuration"
                    )
            owner.get("/api/setup/drafts/" + draft["id"]).raise_for_status()
            owner.post(
                path, json={"generation": draft["generation"]}
            ).raise_for_status()
            owner.post(
                "/api/setup/principals/" + principal["id"] + "/revoke"
            ).raise_for_status()
            with httpx.Client(
                base_url=self.origin,
                headers={"Authorization": "Bearer " + token},
                trust_env=False,
            ) as revoked:
                if revoked.get("/api/setup/context").status_code != 403:
                    raise RuntimeError("Setup revocation did not take effect")
            return {
                "principal_id": principal["id"],
                "draft_id": draft["id"],
                "generation": draft["generation"],
                "agent_activation_refused": True,
                "setup_principal_revoked": True,
            }

    def authorize_development(self, generation: str):
        evidence = {
            "generation": generation,
            "runtime_binding": runtime_settings_fingerprint(self.settings),
            "profile": "private-evaluation",
            "evidence_kind": "development-integration-authorization",
            "scope": "Owned local C0 fixture assessment only; not production isolation or hosted qualification.",
            "checks": [
                {"name": "explicit-model-identity", "status": "passed"},
                {"name": "unqualified-dispatch-refused", "status": "passed"},
            ],
        }
        return issue_qualification(
            self.platform.db,
            self.settings,
            generation,
            "private-evaluation",
            evidence,
            valid_days=1,
            issuer="local-development-operator",
        ).id

    def start_worker(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "from sovite.cli import main; main()", "worker"],
            env=self.environment,
            cwd=self.root,
            stdout=self.log,
            stderr=self.log,
        )
        self.children.append(process)
        record_children(self.root, self.children)

    def private_artifact(self, receipt: str) -> tuple[dict, bytes]:
        with self.client() as client:
            jobs = client.get("/api/jobs").raise_for_status().json()["items"]
            job = next(j for j in jobs if j["receipt"] == receipt)
            body = (
                client.get("/api/jobs/" + job["id"] + "/artifact")
                .raise_for_status()
                .content
            )
            return job, body


@contextmanager
def local_evaluation(root: Path, model: dict):
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    (root / "bundle.yaml").write_text(yaml.safe_dump(evaluation_bundle(model)))
    caller, operator = secrets.token_urlsafe(40), secrets.token_urlsafe(40)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        origin = "http://127.0.0.1:" + str(listener.getsockname()[1])
        settings = Settings(
            database_url="sqlite:///" + str(root / "control.db"),
            bundle_path=str(root / "bundle.yaml"),
            artifact_path=str(root / "artifacts"),
            operator_token=operator,
            caller_tokens={"workspace-agent": caller},
            public_origin=origin,
            secure_cookies=False,
            setup_api_enabled=True,
            setup_installation_id="bayleaf-local-wf01",
            qualification_private_key=str(root / "signer.pem"),
            qualification_public_keys=[str(root / "public.pem")],
        )
        platform = Platform(settings)
        try:
            platform.db.migrate()
            platform.initialize()
            initialize_keys(settings)
            env = {
                key: os.environ[key]
                for key in ("PATH", "LANG", "LC_ALL")
                if key in os.environ
            }
            env.update(
                {
                    "SOVITE_DATABASE_URL": settings.database_url,
                    "SOVITE_DATABASE_MIGRATE": "never",
                    "SOVITE_DATABASE_SCHEMA": "",
                    "SOVITE_BUNDLE_PATH": settings.bundle_path,
                    "SOVITE_ARTIFACT_PATH": settings.artifact_path,
                    "SOVITE_ARTIFACT_BACKEND": "filesystem",
                    "SOVITE_OPERATOR_TOKEN": operator,
                    "SOVITE_CALLER_TOKENS": json.dumps(settings.caller_tokens),
                    "SOVITE_PUBLIC_ORIGIN": origin,
                    "SOVITE_SECURE_COOKIES": "false",
                    "SOVITE_REQUIRE_QUALIFICATION": "true",
                    "SOVITE_QUALIFICATION_PUBLIC_KEYS": json.dumps(
                        settings.qualification_public_keys
                    ),
                    "SOVITE_SETUP_API_ENABLED": "true",
                    "SOVITE_SETUP_INSTALLATION_ID": settings.setup_installation_id,
                    "SOVITE_EMBEDDED_WORKER": "false",
                    "SOVITE_POLL_SECONDS": ".2",
                }
            )
            children = []
            with (root / "processes.log").open("wb") as log:
                try:
                    children.append(
                        subprocess.Popen(
                            [
                                sys.executable,
                                "-m",
                                "uvicorn",
                                "sovite.api:create_app",
                                "--factory",
                                "--fd",
                                str(listener.fileno()),
                                "--log-level",
                                "error",
                                "--no-access-log",
                            ],
                            env=env,
                            cwd=root,
                            pass_fds=(listener.fileno(),),
                            stdout=log,
                            stderr=log,
                        )
                    )
                    with httpx.Client(timeout=2, trust_env=False) as health:
                        for _ in range(150):
                            try:
                                if (
                                    health.get(origin + "/health/ready").status_code
                                    == 200
                                ):
                                    break
                            except httpx.TransportError:
                                pass
                            if children[0].poll() is not None:
                                raise RuntimeError(
                                    "Owned Sovite API stopped before readiness"
                                )
                            time.sleep(0.1)
                        else:
                            raise RuntimeError("Owned Sovite API readiness timeout")
                    record_children(root, children)
                    yield Evaluation(
                        root,
                        settings,
                        platform,
                        model,
                        origin,
                        caller,
                        children,
                        env,
                        log,
                    )
                finally:
                    for child in children:
                        child.terminate()
                    for child in children:
                        try:
                            child.wait(timeout=20)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait()

        finally:
            listener.close()
            platform.db.engine.dispose()
