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
    authority_token: str = ""
    execution_workload_identity: str = "worker"
    execution_tokens: dict[str, str] = field(default_factory=dict)
    authority_retention_days: int = 7
    author_token: str = ""
    activator_token: str = ""
    hosting_envelope_path: str | None = None
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
    qualification_issuers: list[dict] = field(default_factory=list)
    qualification_revoked_keys: list[str] = field(default_factory=list)
    display_name: str = "Sovite"
    embedded_worker: bool = False
    max_request_bytes: int = 65536
    compute_id: str = "local"
    deployment_binding: str = ""
    setup_api_enabled: bool = False
    setup_installation_id: str = ""
    installation_id: str = ""
    repository_runtime_config: str | None = None

    def __post_init__(self):
        if set(self.caller_tokens) & {"operator", "author", "activator"}:
            raise ValueError("Caller identities must not reuse administrative role names")
        if not isinstance(self.execution_tokens, dict) or not all(
            isinstance(identity, str)
            and re.fullmatch(r"[A-Za-z0-9._:-]{1,160}", identity)
            and isinstance(token, str)
            and len(token) >= 24
            for identity, token in self.execution_tokens.items()
        ):
            raise ValueError("execution tokens must map workload identities to 24+ character credentials")
        if self.installation_id and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,159}", self.installation_id):
            raise ValueError("SOVITE_INSTALLATION_ID must be an installation identifier of 1..160 characters")
        if type(self.authority_retention_days) is not int or self.authority_retention_days < 1:
            raise ValueError("authority retention days must be a positive integer")
        if self.authority_token and (len(self.authority_token) < 24 or self.authority_token in [self.operator_token, self.author_token, self.activator_token, *self.caller_tokens.values()]):
            raise ValueError("authority credential must be dedicated and contain at least 24 characters")
        credentials = [*self.execution_tokens.values()]
        if len(set(credentials)) != len(credentials) or any(
            token in [self.operator_token, self.author_token, self.activator_token,
                      self.authority_token, *self.caller_tokens.values()]
            for token in credentials
        ):
            raise ValueError("execution credentials must be unique and separate from other roles")
        if self.setup_api_enabled and self.hosting_envelope_path:
            raise ValueError("The bounded setup API is local-only; hosted delegation is not qualified")
        if self.setup_api_enabled and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,159}", self.setup_installation_id):
            raise ValueError("An explicit installation identity is required for the setup API")
        validate_database_schema(self.database_schema)
        if self.database_migrate not in {"startup", "never"}:
            raise ValueError("SOVITE_DATABASE_MIGRATE must be startup or never")

    @classmethod
    def from_database_env(cls) -> "Settings":
        return cls(database_url=os.environ.get("SOVITE_DATABASE_URL", cls.database_url),
                   database_schema=os.environ.get("SOVITE_DATABASE_SCHEMA") or None,
                   database_migrate=os.environ.get("SOVITE_DATABASE_MIGRATE", "startup"))

    @classmethod
    def from_env(cls, *, require_auth: bool = True, auth_role: str | None = None) -> "Settings":
        # Servers load all roles. Trusted local CLI processes load only their role.
        # The database-backed worker is not an operator and must not need an
        # operator credential merely to initialize its bounded execution loop.
        if auth_role not in {None, "operator", "author", "activator", "worker"}:
            raise ValueError("Unknown authentication role")
        env = os.environ
        def truth(key: str, default: bool) -> bool:
            return env.get("SOVITE_" + key, str(default)).lower() in {"1", "true", "yes"}
        caller_tokens = json.loads(env.get("SOVITE_CALLER_TOKENS", "{}")) if auth_role is None else {}
        if not isinstance(caller_tokens, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in caller_tokens.items()):
            raise ValueError("SOVITE_CALLER_TOKENS must be a JSON object mapping caller identities to tokens")
        execution_tokens = json.loads(env.get("SOVITE_EXECUTION_TOKENS", "{}")) if auth_role is None else {}
        if not isinstance(execution_tokens, dict) or not all(
            isinstance(k, str) and isinstance(v, str)
            for k, v in execution_tokens.items()
        ):
            raise ValueError("SOVITE_EXECUTION_TOKENS must map workload identities to credentials")
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
            operator_token=env.get("SOVITE_OPERATOR_TOKEN", "") if auth_role in {None, "operator"} else "",
            author_token=env.get("SOVITE_AUTHOR_TOKEN", "") if auth_role in {None, "author"} else "",
            activator_token=env.get("SOVITE_ACTIVATOR_TOKEN", "") if auth_role in {None, "activator"} else "",
            hosting_envelope_path=env.get("SOVITE_HOSTING_ENVELOPE") or None,
            caller_tokens=caller_tokens if auth_role is None else {},
            authority_token=env.get("SOVITE_AUTHORITY_TOKEN", "") if auth_role is None else "",
            execution_workload_identity=env.get("SOVITE_EXECUTION_WORKLOAD_IDENTITY", "worker"),
            execution_tokens=execution_tokens if auth_role is None else {},
            authority_retention_days=int(env.get("SOVITE_AUTHORITY_RETENTION_DAYS", "7")),
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
            qualification_issuers=json.loads(env.get("SOVITE_QUALIFICATION_ISSUERS", "[]")),
            qualification_revoked_keys=json.loads(env.get("SOVITE_QUALIFICATION_REVOKED_KEYS", "[]")),
            display_name=env.get("SOVITE_DISPLAY_NAME", defaults.display_name),
            embedded_worker=truth("EMBEDDED_WORKER", False),
            max_request_bytes=int(env.get("SOVITE_MAX_REQUEST_BYTES", "65536")),
            compute_id=env.get("SOVITE_COMPUTE_ID", "local"),
            deployment_binding=env.get("SOVITE_DEPLOYMENT_BINDING", ""),
            setup_api_enabled=truth("SETUP_API_ENABLED", False),
            setup_installation_id=env.get("SOVITE_SETUP_INSTALLATION_ID", ""),
            installation_id=env.get("SOVITE_INSTALLATION_ID", ""),
            repository_runtime_config=env.get("SOVITE_REPOSITORY_RUNTIME_CONFIG") or None,
        )
        if result.artifact_backend not in {"filesystem", "s3"}:
            raise ValueError("artifact backend must be filesystem or s3")
        if require_auth and auth_role in {None, "operator"} and len(result.operator_token) < 24:
            raise ValueError("SOVITE_OPERATOR_TOKEN must contain at least 24 characters")
        if require_auth and auth_role in {"author", "activator"} and len(getattr(result, auth_role + "_token")) < 24:
            raise ValueError(f"SOVITE_{auth_role.upper()}_TOKEN must contain at least 24 characters")
        if require_auth and any(len(v) < 24 for v in result.caller_tokens.values()):
            raise ValueError("caller tokens must contain at least 24 characters")
        roles = [token for token in (result.operator_token, *result.caller_tokens.values(), result.author_token, result.activator_token) if token]
        if require_auth and (any(len(token) < 24 for token in roles) or len(set(roles)) != len(roles)):
            raise ValueError("role tokens must be distinct and contain at least 24 characters")
        if result.hosting_envelope_path and (not result.require_qualification or (require_auth and auth_role is None and (not result.author_token or not result.activator_token))):
            raise ValueError("Hosted mode requires qualification and separate author and activator tokens")
        from .qualification import validate_issuers
        validate_issuers(result)
        return result
