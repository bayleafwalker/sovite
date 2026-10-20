from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from sqlalchemy import Boolean, Float, Index, Integer, JSON, String, Text, UniqueConstraint, create_engine, event, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .settings import validate_database_schema


class Base(DeclarativeBase):
    pass


class MetadataRow(Base):
    __tablename__ = "platform_metadata"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)


class BundleRevision(Base):
    __tablename__ = "bundle_revisions"
    generation: Mapped[str] = mapped_column(String(80), primary_key=True)
    bundle: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class GrantState(Base):
    __tablename__ = "grant_runtime_state"
    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    inhibited: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(String(500), default="")
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


class BindingState(Base):
    __tablename__ = "binding_runtime_state"
    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    inhibited: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(String(500), default="")
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    receipt: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    request_digest: Mapped[str] = mapped_column(String(80))
    caller: Mapped[str] = mapped_column(String(160), index=True)
    grant_id: Mapped[str] = mapped_column(String(160), index=True)
    generation: Mapped[str] = mapped_column(String(80))
    resolved: Mapped[dict] = mapped_column(JSON)
    task: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)
    started_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[float] = mapped_column(Float, default=time.time)
    lease_owner: Mapped[str | None] = mapped_column(String(96), nullable=True)
    lease_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    artifact_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    artifact_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    public_projection: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    released_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    created_at: Mapped[float] = mapped_column(Float)
    actor: Mapped[str] = mapped_column(String(160))
    action: Mapped[str] = mapped_column(String(100))
    target: Mapped[str] = mapped_column(String(160))
    detail: Mapped[dict] = mapped_column(JSON)
    previous_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    identity: Mapped[str] = mapped_column(String(160))
    role: Mapped[str] = mapped_column(String(32))
    expires_at: Mapped[float] = mapped_column(Float, index=True)


class Qualification(Base):
    __tablename__ = "qualifications"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    generation: Mapped[str] = mapped_column(String(80), index=True)
    profile: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[float] = mapped_column(Float)
    passed: Mapped[bool] = mapped_column(Boolean)
    issuer: Mapped[str] = mapped_column(String(160))
    payload: Mapped[dict] = mapped_column(JSON)
    signature: Mapped[str] = mapped_column(Text)
    public_key: Mapped[str] = mapped_column(Text)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    updated_at: Mapped[float] = mapped_column(Float)
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ExecutionAuthority(Base):
    __tablename__ = "execution_authority"
    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    fence: Mapped[str] = mapped_column(String(64))
    capability_digest: Mapped[str] = mapped_column(String(64))
    context: Mapped[dict] = mapped_column(JSON)
    expires_at: Mapped[float] = mapped_column(Float)
    deadline: Mapped[float] = mapped_column(Float)


class HelperEnrollment(Base):
    __tablename__ = "helper_enrollments"
    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    binding_id: Mapped[str] = mapped_column(String(160))
    installation_id: Mapped[str] = mapped_column(String(160))
    role: Mapped[str] = mapped_column(String(160))
    expires_at: Mapped[float] = mapped_column(Float)
    certificate_digest: Mapped[str] = mapped_column(String(64), unique=True)
    public_key: Mapped[str] = mapped_column(Text)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class HelperSession(Base):
    __tablename__ = "helper_sessions"
    __table_args__ = (Index("ix_helper_sessions_retention", "active", "last_seen", "id"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    binding_id: Mapped[str] = mapped_column(String(160), index=True)
    enrollment_id: Mapped[str] = mapped_column(String(160))
    gateway_id: Mapped[str] = mapped_column(String(64))
    generation: Mapped[str] = mapped_column(String(80))
    challenge: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    identity: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_seen: Mapped[float] = mapped_column(Float)


class RequestLedger(Base):
    __tablename__ = "inference_requests"
    __table_args__ = (Index("ix_inference_requests_retention", "status", "updated_at", "id"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), index=True)
    fence: Mapped[str] = mapped_column(String(64))
    binding_id: Mapped[str] = mapped_column(String(160), index=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    gateway_id: Mapped[str] = mapped_column(String(64))
    workload: Mapped[str] = mapped_column(String(160))
    request: Mapped[dict] = mapped_column(JSON)
    permit_digest: Mapped[str] = mapped_column(String(64))
    permit_until: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32), default="active")
    outcome: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)


class SetupPrincipal(Base):
    __tablename__ = "setup_principals"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    installation_id: Mapped[str] = mapped_column(String(160))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    envelope: Mapped[dict] = mapped_column(JSON)
    expires_at: Mapped[float] = mapped_column(Float)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class SetupDraft(Base):
    __tablename__ = "setup_drafts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    author: Mapped[str] = mapped_column(String(64), index=True)
    installation_id: Mapped[str] = mapped_column(String(160))
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True)
    request_digest: Mapped[str] = mapped_column(String(80))
    base_generation: Mapped[str] = mapped_column(String(80))
    generation: Mapped[str] = mapped_column(String(80))
    bundle: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(32), default="draft")
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


MIGRATIONS = (
    (1, tuple(table for table in Base.metadata.sorted_tables if table.name not in {"binding_runtime_state", "execution_authority", "helper_enrollments", "helper_sessions", "inference_requests", "setup_principals", "setup_drafts"})),
    (2, (BindingState.__table__,)),
    (3, (ExecutionAuthority.__table__, HelperEnrollment.__table__, HelperSession.__table__, RequestLedger.__table__)),
    (4, (SetupPrincipal.__table__, SetupDraft.__table__)),
)


class DatabaseSchemaError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


class Database:
    schema_version = MIGRATIONS[-1][0]

    def __init__(self, url: str, schema: str | None = None):
        validate_database_schema(schema)
        parsed = make_url(url)
        if schema is not None and parsed.get_backend_name() != "postgresql":
            raise ValueError("SOVITE_DATABASE_SCHEMA is supported only on PostgreSQL")
        self.schema = schema
        self.path = Path(parsed.database) if parsed.get_backend_name() == "sqlite" and parsed.database not in {None, "", ":memory:"} else None
        kwargs: dict[str, Any] = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if ":memory:" in url:
                from sqlalchemy.pool import StaticPool
                kwargs["poolclass"] = StaticPool
        self.engine = create_engine(url, **kwargs)
        if schema is not None:
            self.engine = self.engine.execution_options(schema_translate_map={None: schema})
        if self.engine.dialect.name == "sqlite":
            @event.listens_for(self.engine, "connect")
            def setup_sqlite(dbapi_connection, connection_record):
                dbapi_connection.execute("PRAGMA foreign_keys=ON")
                dbapi_connection.execute("PRAGMA busy_timeout=30000")
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self, migrate: str = "startup"):
        if migrate == "startup":
            self.migrate()
        elif migrate == "never":
            self.check()
        else:
            raise ValueError("SOVITE_DATABASE_MIGRATE must be startup or never")

    def _structure(self, connection, tables):
        inspector = inspect(connection)
        names = set(inspector.get_table_names(schema=self.schema))
        for table in tables:
            if table.name not in names:
                raise DatabaseSchemaError("database_schema_incomplete", f"Missing table {table.name}; restore or migrate a supported schema")
            columns = {column["name"]: column for column in inspector.get_columns(table.name, schema=self.schema)}
            for column in table.columns:
                actual = columns.get(column.name)
                if actual is None or actual["type"]._type_affinity is not column.type._type_affinity or actual["nullable"] != column.nullable or getattr(actual["type"], "length", None) != getattr(column.type, "length", None):
                    raise DatabaseSchemaError("database_schema_incomplete", f"Incompatible column {table.name}.{column.name}")
            primary = inspector.get_pk_constraint(table.name, schema=self.schema)["constrained_columns"]
            if primary != [column.name for column in table.primary_key]:
                raise DatabaseSchemaError("database_schema_incomplete", f"Incompatible primary key on {table.name}")
            indexes = inspector.get_indexes(table.name, schema=self.schema)
            for index in table.indexes:
                if not any(row["column_names"] == [column.name for column in index.columns] and bool(row["unique"]) == bool(index.unique) for row in indexes):
                    raise DatabaseSchemaError("database_schema_incomplete", f"Missing index on {table.name}")
            unique = inspector.get_unique_constraints(table.name, schema=self.schema)
            for constraint in table.constraints:
                if isinstance(constraint, UniqueConstraint) and not any(row["column_names"] == [column.name for column in constraint.columns] for row in unique):
                    raise DatabaseSchemaError("database_schema_incomplete", f"Missing unique constraint on {table.name}")

    def _version(self, connection) -> int:
        names = set(inspect(connection).get_table_names(schema=self.schema))
        if MetadataRow.__tablename__ not in names:
            if names.intersection(Base.metadata.tables):
                raise DatabaseSchemaError("database_schema_incomplete", "Sovite tables exist without platform_metadata")
            return 0
        self._structure(connection, [MetadataRow.__table__])
        value = connection.execute(select(MetadataRow.value).where(MetadataRow.key == "schema_version")).scalar_one_or_none()
        if value is None:
            raise DatabaseSchemaError("database_schema_incomplete", "Missing schema_version metadata")
        if not isinstance(value, dict) or set(value) != {"version"} or type(value["version"]) is not int or value["version"] < 1:
            raise DatabaseSchemaError("database_schema_unknown", "Unsupported database schema_version")
        if value["version"] > self.schema_version:
            raise DatabaseSchemaError("database_schema_newer", "Unsupported database schema_version; use a compatible release")
        return value["version"]

    def _audit_head(self, connection):
        value = connection.execute(select(MetadataRow.value).where(MetadataRow.key == "audit_head")).scalar_one_or_none()
        if not isinstance(value, dict) or type(value.get("sequence")) is not int or value["sequence"] < 0 or not isinstance(value.get("hash"), str) or len(value["hash"]) != 64 or any(char not in "0123456789abcdef" for char in value["hash"]):
            raise DatabaseSchemaError("database_schema_incomplete", "Missing or invalid audit_head metadata")

    def check(self):
        if self.path is not None and not self.path.is_file():
            raise DatabaseSchemaError("database_schema_missing", "Run sovite db migrate before startup")
        with self.engine.connect() as connection:
            version = self._version(connection)
            if version == 0:
                raise DatabaseSchemaError("database_schema_missing", "Run sovite db migrate before startup")
            if version < self.schema_version:
                raise DatabaseSchemaError("database_schema_older", "Run sovite db migrate with the migration role before startup")
            self._structure(connection, Base.metadata.sorted_tables)
            self._audit_head(connection)

    def migrate(self):
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.engine.begin() as connection:
            if self.schema is not None and not inspect(connection).has_schema(self.schema):
                raise DatabaseSchemaError("database_schema_missing", "Provision the configured PostgreSQL schema before migration")
            version = self._version(connection)
            if version:
                self._structure(connection, [table for number, tables in MIGRATIONS if number <= version for table in tables])
                self._audit_head(connection)
            if connection.dialect.name == "sqlite" and version < self.schema_version:
                connection.exec_driver_sql("PRAGMA journal_mode=WAL")
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            for number, tables in MIGRATIONS:
                if number <= version:
                    continue
                for table in tables:
                    table.create(connection, checkfirst=True)
                if number == 1:
                    connection.execute(MetadataRow.__table__.insert(), [
                        {"key": "schema_version", "value": {"version": number}},
                        {"key": "audit_head", "value": {"sequence": 0, "hash": "0" * 64}},
                    ])
                else:
                    connection.execute(MetadataRow.__table__.update().where(MetadataRow.key == "schema_version").values(value={"version": number}))
            self._structure(connection, Base.metadata.sorted_tables)
            self._audit_head(connection)

    def authority_lock(self, session: Session):
        from sqlalchemy import update
        session.execute(update(MetadataRow).where(MetadataRow.key == "audit_head").values(value=MetadataRow.value))

    def audit(self, session: Session, actor: str, action: str, target: str, detail: dict | None = None) -> AuditEvent:
        # Lock acquisition also serializes audit appenders using SQLite's write lock.
        from sqlalchemy import update
        session.execute(update(MetadataRow).where(MetadataRow.key == "audit_head").values(value=MetadataRow.value))
        head = session.execute(select(MetadataRow).where(MetadataRow.key == "audit_head").with_for_update()).scalar_one()
        session.refresh(head)
        sequence = head.value["sequence"] + 1
        created = time.time()
        body = {"sequence": sequence, "created_at": created, "actor": actor, "action": action, "target": target, "detail": detail or {}, "previous_hash": head.value["hash"]}
        event_row = AuditEvent(**body, hash=digest(body))
        session.add(event_row)
        head.value = {"sequence": sequence, "hash": event_row.hash}
        return event_row

    def verify_audit(self) -> dict:
        expected_hash = "0" * 64
        expected_sequence = 1
        with self.sessions() as session:
            rows = session.scalars(select(AuditEvent).order_by(AuditEvent.sequence)).all()
            for row in rows:
                body = {name: getattr(row, name) for name in ("sequence", "created_at", "actor", "action", "target", "detail", "previous_hash")}
                if row.sequence != expected_sequence or row.previous_hash != expected_hash or row.hash != digest(body):
                    return {"valid": False, "events": len(rows), "failed_sequence": row.sequence}
                expected_hash, expected_sequence = row.hash, expected_sequence + 1
            head = session.get(MetadataRow, "audit_head")
            valid = head is not None and head.value == {"sequence": len(rows), "hash": expected_hash}
            return {"valid": valid, "events": len(rows), "head": expected_hash, "limitation": "Local chain integrity; external checkpoints are required to detect full-history replacement."}
