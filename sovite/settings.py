from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path


def validate_database_schema(schema: str | None):
    if schema is not None and (not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema) or schema == "public" or schema == "information_schema" or schema.startswith("pg_")):
        raise ValueError("SOVITE_DATABASE_SCHEMA must be a non-public, non-system lower-case PostgreSQL identifier (1-63 characters)")


@dataclass
class Settings:
    database_url: str = "sqlite:///.state/control.db"
    database_schema: str | None = None
    database_migrate: str = "startup"
    bundle_path: str = "config/platform.local.yaml"
    artifact_path: str = ".state/artifacts"
    artifact_backend: str = "filesystem"
    s3_endpoint: str | None = None
    s3_bucket: str = "sovite-private"
    s3_region: str = "us-east-1"
    operator_token: str = ""
    caller_tokens: dict[str, str] = field(default_factory=dict)
    public_origin: str = "http://localhost:8080"
    secure_cookies: bool = True
    session_ttl_seconds: int = 3600
    lease_seconds: int = 90
    poll_seconds: float = 2.0
    job_timeout_seconds: int = 300
    max_attempts: int = 3
    require_qualification: bool = True
    qualification_private_key: str = ".state/qualification-private.pem"
    qualification_public_keys: list[str] = field(default_factory=lambda: [".state/qualification-public.pem"])
    display_name: str = "Sovite"
    embedded_worker: bool = False
    max_request_bytes: int = 65536
    compute_id: str = "local"
    deployment_binding: str = ""

    def __post_init__(self):
        validate_database_schema(self.database_schema)
        if self.database_migrate not in {"startup", "never"}:
            raise ValueError("SOVITE_DATABASE_MIGRATE must be startup or never")

    @classmethod
    def from_database_env(cls) -> "Settings":
        return cls(database_url=os.environ.get("SOVITE_DATABASE_URL", cls.database_url),
                   database_schema=os.environ.get("SOVITE_DATABASE_SCHEMA") or None,
                   database_migrate=os.environ.get("SOVITE_DATABASE_MIGRATE", "startup"))

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        def truth(key: str, default: bool) -> bool:
            return env.get("SOVITE_" + key, str(default)).lower() in {"1", "true", "yes"}
        caller_tokens = json.loads(env.get("SOVITE_CALLER_TOKENS", "{}"))
        if not isinstance(caller_tokens, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in caller_tokens.items()):
            raise ValueError("SOVITE_CALLER_TOKENS must be a JSON object mapping caller identities to tokens")
        defaults = cls.from_database_env()
        result = cls(
            database_url=defaults.database_url,
            database_schema=defaults.database_schema,
            database_migrate=defaults.database_migrate,
            bundle_path=env.get("SOVITE_BUNDLE_PATH", defaults.bundle_path),
            artifact_path=env.get("SOVITE_ARTIFACT_PATH", defaults.artifact_path),
            artifact_backend=env.get("SOVITE_ARTIFACT_BACKEND", defaults.artifact_backend),
            s3_endpoint=env.get("SOVITE_S3_ENDPOINT"),
            s3_bucket=env.get("SOVITE_S3_BUCKET", defaults.s3_bucket),
            s3_region=env.get("SOVITE_S3_REGION", defaults.s3_region),
            operator_token=env.get("SOVITE_OPERATOR_TOKEN", ""),
            caller_tokens=caller_tokens,
            public_origin=env.get("SOVITE_PUBLIC_ORIGIN", defaults.public_origin).rstrip("/"),
            secure_cookies=truth("SECURE_COOKIES", True),
            session_ttl_seconds=int(env.get("SOVITE_SESSION_TTL_SECONDS", "3600")),
            lease_seconds=int(env.get("SOVITE_LEASE_SECONDS", "90")),
            poll_seconds=float(env.get("SOVITE_POLL_SECONDS", "2")),
            job_timeout_seconds=int(env.get("SOVITE_JOB_TIMEOUT_SECONDS", "300")),
            max_attempts=int(env.get("SOVITE_MAX_ATTEMPTS", "3")),
            require_qualification=truth("REQUIRE_QUALIFICATION", True),
            qualification_private_key=env.get("SOVITE_QUALIFICATION_PRIVATE_KEY", defaults.qualification_private_key),
            qualification_public_keys=json.loads(env.get("SOVITE_QUALIFICATION_PUBLIC_KEYS", json.dumps(defaults.qualification_public_keys))),
            display_name=env.get("SOVITE_DISPLAY_NAME", defaults.display_name),
            embedded_worker=truth("EMBEDDED_WORKER", False),
            max_request_bytes=int(env.get("SOVITE_MAX_REQUEST_BYTES", "65536")),
            compute_id=env.get("SOVITE_COMPUTE_ID", "local"),
            deployment_binding=env.get("SOVITE_DEPLOYMENT_BINDING", ""),
        )
        if result.artifact_backend not in {"filesystem", "s3"}:
            raise ValueError("artifact backend must be filesystem or s3")
        if len(result.operator_token) < 24:
            raise ValueError("SOVITE_OPERATOR_TOKEN must contain at least 24 characters")
        if any(len(v) < 24 for v in result.caller_tokens.values()):
            raise ValueError("caller tokens must contain at least 24 characters")
        if len(set([result.operator_token, *result.caller_tokens.values()])) != len(result.caller_tokens) + 1:
            raise ValueError("operator and caller tokens must be distinct")
        return result
