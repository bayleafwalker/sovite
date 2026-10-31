"""Resource-owner local operation history for the pod-observation gateway.

This store is deliberately independent of execution and publication state.  A
single local database serializes admission; it is not a distributed lease or a
claim of exactly-once execution across gateway replicas.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from pathlib import Path


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()


class OperationConflict(ValueError):
    pass


class ResourceOperationStore:
    """Explicit installation, durable admission, and immutable observation."""

    def __init__(self, path: str | Path):
        self.path = str(path)

    def install(self) -> None:
        self._check_directory()
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            self._check_file()
        else:
            os.close(fd)
        for suffix in ('-wal', '-shm', '-journal'):
            self._check_sidecar(suffix)
        with sqlite3.connect(self.path) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError("resource_store_version_unsupported")
            db.execute("CREATE TABLE IF NOT EXISTS resource_operations (operation_id TEXT PRIMARY KEY, request_digest TEXT NOT NULL, request BLOB NOT NULL, state TEXT NOT NULL, observation BLOB, observation_digest TEXT, release TEXT, release_metadata BLOB)")
            db.execute("CREATE TABLE IF NOT EXISTS resource_policy (id INTEGER PRIMARY KEY CHECK (id=1), policy BLOB NOT NULL)")
            db.execute("PRAGMA user_version = 1")

    def _connect(self):
        self._check_directory()
        self._check_file()
        for suffix in ('-wal', '-shm', '-journal'):
            self._check_sidecar(suffix)
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        if db.execute("PRAGMA user_version").fetchone()[0] != 1:
            db.close()
            raise RuntimeError("resource_store_uninstalled")
        return db

    def _check_directory(self):
        info = Path(self.path).parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise RuntimeError("resource_store_directory_not_private")

    def _check_file(self):
        try:
            info = Path(self.path).lstat()
        except FileNotFoundError:
            raise RuntimeError("resource_store_uninstalled") from None
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise RuntimeError("resource_store_file_not_private")

    def _check_sidecar(self, suffix):
        try:
            info = Path(self.path + suffix).lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise RuntimeError("resource_store_sidecar_not_private")

    def admit(self, operation_id: str, request: dict) -> dict:
        if not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", operation_id):
            raise ValueError("invalid_operation_id")
        encoded = canonical(request)
        digest = hashlib.sha256(encoded).hexdigest()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT request_digest,state,observation,observation_digest,release,release_metadata FROM resource_operations WHERE operation_id=?", (operation_id,)).fetchone()
            if row:
                if row[0] != digest:
                    raise OperationConflict("operation_conflict")
                return {"state": "unknown" if row[1] == "admitted" else row[1], "observation": row[2], "sha256": row[3], "release": row[4], "release_metadata": json.loads(row[5]) if row[5] else None}
            db.execute("INSERT INTO resource_operations (operation_id, request_digest, request, state) VALUES (?, ?, ?, 'admitted')", (operation_id, digest, encoded))
            db.commit()
            return {"state": "admitted", "observation": None, "sha256": None, "release": None, "release_metadata": None}

    def lookup(self, operation_id: str) -> dict | None:
        """Read the exact existing local intent for a typed follow-up; no authority."""
        if not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", operation_id):
            raise ValueError("invalid_operation_id")
        with self._connect() as db:
            row = db.execute("SELECT request,state FROM resource_operations WHERE operation_id=?", (operation_id,)).fetchone()
            return {"request": json.loads(row[0]), "state": row[1]} if row else None

    def observe(self, operation_id: str, observation: bytes, release: str, metadata: dict | None = None) -> None:
        if not isinstance(observation, bytes) or len(observation) > 4096 or release not in {"allowed", "denied"}:
            raise ValueError("invalid_observation")
        digest = "sha256:" + hashlib.sha256(observation).hexdigest()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE resource_operations SET state='observed', observation=?, observation_digest=?, release=?, release_metadata=? WHERE operation_id=? AND state='admitted'", (observation, digest, release, canonical(metadata or {}), operation_id)).rowcount
            if changed != 1:
                raise OperationConflict("operation_not_admitted")
            db.commit()

    def set_policy(self, policy: dict) -> None:
        """Replace resource-owner policy; called only by the separate admin app."""
        encoded = canonical(policy)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO resource_policy (id, policy) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET policy=excluded.policy", (encoded,))
            db.commit()

    def policy(self) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT policy FROM resource_policy WHERE id=1").fetchone()
            return json.loads(row[0]) if row else None
