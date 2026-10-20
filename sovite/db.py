from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from sqlalchemy import Boolean, Float, Integer, JSON, String, Text, create_engine, event, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


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


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


class Database:
    def __init__(self, url: str):
        if url.startswith("sqlite:///") and ":memory:" not in url:
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if ":memory:" in url:
                from sqlalchemy.pool import StaticPool
                kwargs["poolclass"] = StaticPool
        self.engine = create_engine(url, **kwargs)
        if self.engine.dialect.name == "sqlite":
            @event.listens_for(self.engine, "connect")
            def setup_sqlite(dbapi_connection, connection_record):
                dbapi_connection.execute("PRAGMA foreign_keys=ON")
                dbapi_connection.execute("PRAGMA busy_timeout=30000")
                dbapi_connection.execute("PRAGMA journal_mode=WAL")
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self):
        with self.sessions.begin() as session:
            MetadataRow.__table__.create(session.connection(), checkfirst=True)
            version = session.get(MetadataRow, "schema_version")
            if version is not None and version.value not in ({"version": 1}, {"version": 2}):
                raise ValueError("Unsupported database schema_version; expected version 1 or 2")
            # Version 2 adds binding_runtime_state: binding inhibits are operational state,
            # independent of the bundle's enabled flag, and survive bundle revisions.
            Base.metadata.create_all(session.connection())
            if version is None:
                session.add(MetadataRow(key="schema_version", value={"version": 2}))
            else:
                version.value = {"version": 2}
            if session.get(MetadataRow, "audit_head") is None:
                session.add(MetadataRow(key="audit_head", value={"sequence": 0, "hash": "0" * 64}))

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
