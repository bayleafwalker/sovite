"""Protected-side retained transport; Workspace owns the artifact and membership.

These private files are publication intent and recovery locators, not grants or
work/execution state. They must live outside disposable candidate workers and
be backed up by the host owner. flock coordinates one host only. No installed
public route, automatic executor activation or acceptance authority is added.
"""
from __future__ import annotations

import base64
import copy
import fcntl
import hashlib
import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .repository_sessions import _atomic_write, _canonical_json, _canonical_root, _unique_pairs

MAX_BYTES = 8 * 1024 * 1024
_SHA = re.compile(r"^[0-9a-f]{64}$")
_ULID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


class CandidateRetentionError(ValueError):
    pass


def _need(value):
    if not value:
        raise CandidateRetentionError("candidate-retention-unavailable")


@dataclass(frozen=True)
class WorkspaceTarget:
    """Trusted host configuration; never taken from the candidate payload."""
    endpoint: str
    workspace_id: str
    destination: str
    allowed_classes: frozenset[str]

    def __post_init__(self):
        url = urlsplit(self.endpoint)
        _need(url.scheme == "https" and url.netloc and not url.username and not url.password
              and url.path in ("", "/") and not url.query and not url.fragment)
        _need(_ULID.fullmatch(self.workspace_id) and self.destination == "workspace-" + self.workspace_id)
        _need(isinstance(self.allowed_classes, frozenset) and bool(self.allowed_classes)
              and self.allowed_classes <= {"C0", "C1", "C2", "C3"})

    def identity(self):
        return {"endpoint": self.endpoint.rstrip("/"), "workspace_id": self.workspace_id,
                "destination": self.destination}


class CandidateRetention:
    def __init__(self, root: Path, target: WorkspaceTarget, client):
        # client is the actual released AsyncVuoroClient, not a delivery callback.
        self.root = _canonical_root(Path(root), create=True)
        self.target, self.client = target, client
        _need(client.profile.endpoint.rstrip("/") == target.endpoint.rstrip("/"))

    @contextmanager
    def _locked(self, key):
        _need(isinstance(key, str) and _SHA.fullmatch(key))
        fd = os.open(self.root / (key + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            import stat
            info = os.fstat(fd)
            _need(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                  and info.st_nlink == 1 and not stat.S_IMODE(info.st_mode) & 0o077)
            # Nonblocking: never block an asyncio event loop behind another
            # publisher which needs that loop to release its lock.
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield self.root / (key + ".json")
        finally:
            os.close(fd)

    def _read(self, path):
        import stat
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            _need(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                  and info.st_nlink == 1 and not stat.S_IMODE(info.st_mode) & 0o077
                  and info.st_size <= 2 * MAX_BYTES)
            with os.fdopen(fd, "rb", closefd=False) as stream:
                record = json.loads(stream.read(2 * MAX_BYTES + 1), object_pairs_hook=_unique_pairs)
        finally:
            os.close(fd)
        _need(record["schema_version"] == "sovite-candidate-publication/v1"
              and record["target"] == self.target.identity())
        raw = record["release"]["content"].encode("utf-8")
        _need(len(raw) <= MAX_BYTES and hashlib.sha256(raw).hexdigest() == record["release"]["sha256"])
        self._disclosure(record["release"])
        return record

    def _disclosure(self, release):
        _need(release["destination"] == self.target.destination
              and release["output_class"] in self.target.allowed_classes
              and ("destination_hosts" not in release or urlsplit(self.target.endpoint).hostname in release["destination_hosts"]))

    async def _identity(self):
        who = await self.client.invoke("knowledge.workspace.identity-v1", {"workspace_id": self.target.workspace_id})
        _need(who["workspace_id"] == self.target.workspace_id and isinstance(who["principal_id"], str)
              and {"knowledge:read", "knowledge:contribute"} <= set(who["effective_authorities"]))
        return who["principal_id"]

    async def prepare(self, exporter, run_id, attempt_id, *, release_id, principal_id, workspace_id):
        """Persist exact current output release before any artifact write."""
        who = await self._identity()
        release = exporter.release_snapshot(run_id, attempt_id, release_id=release_id,
            destination=self.target.destination, principal_id=principal_id, workspace_id=workspace_id)
        self._disclosure(release)
        raw = release["content"].encode("utf-8")
        _need(len(raw) <= MAX_BYTES)
        data = json.loads(raw)
        _need(data["run_id"] == run_id and data["attempt_id"] == attempt_id and data["workspace_id"] == workspace_id)
        identity = {"target": self.target.identity(), "run_id": run_id, "attempt_id": attempt_id,
                    "release_id": release_id}
        key = hashlib.sha256(_canonical_json(identity)).hexdigest()
        begin = {"workspace_id": self.target.workspace_id, "expected_principal_id": who,
                 "expected_sha256": release["sha256"], "expected_size": len(raw),
                 "media_type": "application/json", "name": "repository-candidate.json"}
        record = {"schema_version": "sovite-candidate-publication/v1", "target": self.target.identity(),
                  "release": release, "begin": begin, "key": key, "phase": "prepared",
                  "producer": {"principal_id": principal_id, "workspace_id": workspace_id},
                  "artifact_id": None, "version_id": None}
        with self._locked(key) as path:
            if path.exists() or path.is_symlink():
                old = self._read(path)
                _need(old["release"] == release and old["begin"] == begin and old["key"] == key)
            else:
                _atomic_write(path, _canonical_json(record), exclusive=True)
        return key

    async def publish(self, key, exporter=None):
        """Resume ambiguous writes by exact version/key; never allocate a replacement."""
        try:
            with self._locked(key) as path:
                record = self._read(path)
                _need(record["key"] == key and record["phase"] in {"prepared", "begin-unknown", "reserved", "upload-unknown", "finalize-unknown", "ready"})
                who = await self._identity()
                begin = record["begin"]
                _need(begin == {"workspace_id": self.target.workspace_id,
                    "expected_principal_id": begin["expected_principal_id"],
                    "expected_sha256": record["release"]["sha256"],
                    "expected_size": len(record["release"]["content"].encode("utf-8")),
                    "media_type": "application/json", "name": "repository-candidate.json"})
                def recheck():
                    _need(exporter is not None)
                    exporter.recheck_snapshot(record["release"], **record["producer"])
                if record["version_id"] is None:
                    recheck()
                    _need(who == begin["expected_principal_id"])
                    record["phase"] = "begin-unknown"
                    _atomic_write(path, _canonical_json(record))
                    result = await self.client.invoke("knowledge.artifact.begin-v1", begin, idempotency_key="candidate-begin:" + key)
                    _need(_ULID.fullmatch(result["artifact_id"]) and _ULID.fullmatch(result["version_id"]))
                    record.update(artifact_id=result["artifact_id"], version_id=result["version_id"], phase="reserved")
                    _atomic_write(path, _canonical_json(record))
                args = {"workspace_id": self.target.workspace_id, "version_id": record["version_id"]}
                # A lost upload/finalize may already have committed. Finalize
                # first; only the owner's exact object-missing refusal permits
                # upload. Generic conflict/timeouts never trigger reupload.
                if record["phase"] in {"upload-unknown", "finalize-unknown", "ready"}:
                    try:
                        result = await self._finalize(args, key, path, record)
                        return self._receipt(record, result)
                    except Exception as exc:
                        if getattr(exc, "code", None) != "knowledge-object-missing":
                            raise
                _need(who == begin["expected_principal_id"])
                recheck()
                record["phase"] = "upload-unknown"
                _atomic_write(path, _canonical_json(record))
                await self.client.invoke("knowledge.artifact.upload-v1", {**args,
                    "content_base64": base64.b64encode(record["release"]["content"].encode("utf-8")).decode("ascii")})
                result = await self._finalize(args, key, path, record)
                return self._receipt(record, result)
        except Exception:
            raise CandidateRetentionError("candidate-retention-unavailable") from None

    async def _finalize(self, args, key, path, record):
        record["phase"] = "finalize-unknown"
        _atomic_write(path, _canonical_json(record))
        result = await self.client.invoke("knowledge.artifact.finalize-v1", args, idempotency_key="candidate-finalize:" + key)
        _need(result["processing_state"] == "ready" and result["artifact_id"] == record["artifact_id"]
              and result["version_id"] == record["version_id"] and result["sha256"] == record["release"]["sha256"]
              and result["size"] == record["begin"]["expected_size"])
        record["phase"] = "ready"
        _atomic_write(path, _canonical_json(record))
        return result

    def _receipt(self, record, result):
        return {**self.target.identity(), "artifact_id": record["artifact_id"], "version_id": record["version_id"],
                "sha256": result["sha256"], "size": result["size"],
                "release_id": record["release"]["release_id"], "output_class": record["release"]["output_class"]}

    async def retain(self, exporter, key):
        """Publish then append a locator to the real execution owner's history.

        Repeating after a lost acknowledgement converges. Recording history
        does not restart the attempt or revive its expired execution grant.
        """
        receipt = await self.publish(key, exporter)
        with self._locked(key) as path:
            record = self._read(path)
            _need(record["phase"] == "ready")
            data = json.loads(record["release"]["content"])
            from .db import Job
            with exporter.platform.db.sessions.begin() as session:
                exporter.platform.db.authority_lock(session)
                job = session.get(Job, data["run_id"], with_for_update=True)
                _need(job is not None)
                recovery=data.get('kind')=='repository-recovery-artifact'
                if recovery:
                    from .repository_recovery import record_for
                    recovered=record_for(job,attempt_id=data['attempt_id'])
                    _need(recovered['artifact_sha256']==record['release']['sha256'])
                else:
                    _need(job.status=='completed')
                resolved = copy.deepcopy(job.resolved)
                run = resolved["repository_run"]
                _need(run["workspace_id"] == data["workspace_id"] and run["repository_id"] == data["repository_id"]
                      and any(a["id"] == data["attempt_id"] and (recovery or a["state"] == "completed") for a in run["attempts"]))
                label='retained-recovery-candidate' if recovery else 'retained-candidate'
                entry = {"label": label, "uri": "workspace://" + self.target.workspace_id
                         + "/artifacts/" + receipt["artifact_id"] + "/versions/" + receipt["version_id"],
                         "sha256": receipt["sha256"], "receipt": receipt}
                if recovery:entry['attempt_id']=data['attempt_id']
                current = [e for e in run["evidence"] if e['label']==label
                           and (not recovery or e.get('attempt_id')==data['attempt_id'])]
                _need(not current or current == [entry])
                if not current:
                    run["evidence"].append(entry)
                    job.resolved = resolved
                    exporter.platform.db.audit(session, "candidate-retention", "repository_candidate.retained", job.id,
                                               {"attempt_id": data["attempt_id"], "version_id": receipt["version_id"],
                                                "sha256": receipt["sha256"]})
        return receipt

    async def recover(self, receipt):
        """Read exact ready bytes with this reader's current membership and policy.

        The host supplies an owner-anchored receipt. Its release fields are
        provenance only. The current target policy must independently permit
        this class/destination before the network can return candidate bytes.
        """
        try:
            _need(all(receipt[k] == v for k, v in self.target.identity().items()))
            self._disclosure({"destination": receipt["destination"], "output_class": receipt["output_class"]})
            _need(_ULID.fullmatch(receipt["artifact_id"]) and _ULID.fullmatch(receipt["version_id"])
                  and _SHA.fullmatch(receipt["sha256"]) and type(receipt["size"]) is int
                  and 0 <= receipt["size"] <= MAX_BYTES)
            result = await self.client.invoke("knowledge.artifact.download-v1", {"workspace_id": self.target.workspace_id,
                "artifact_id": receipt["artifact_id"], "version": receipt["version_id"]})
            _need(result["processing_state"] == "ready" and result["artifact_id"] == receipt["artifact_id"]
                  and result["version_id"] == receipt["version_id"] and result["sha256"] == receipt["sha256"]
                  and result["size"] == receipt["size"])
            _need(len(result["content_base64"]) <= 4 * ((MAX_BYTES + 2) // 3))
            raw = base64.b64decode(result["content_base64"], validate=True)
            _need(len(raw) == receipt["size"] and hashlib.sha256(raw).hexdigest() == receipt["sha256"])
            return raw
        except Exception:
            raise CandidateRetentionError("candidate-retention-unavailable") from None


class CandidatePublicationService:
    """Opt-in composition of the real export owner and released Vuoro client.

    Runtime composition supplies a fixed profile/target; request bodies cannot
    select credentials, endpoints, clearance or an approval procedure.
    """
    def __init__(self, exporter, root, target, profile, credential_resolver, *, transport=None):
        self.exporter, self.root, self.target = exporter, Path(root), target
        self.profile, self.credential_resolver, self.transport = profile, credential_resolver, transport

    async def retain(self, run_id, attempt_id, release_id, *, principal_id, workspace_id):
        from vuoro_client import AsyncVuoroClient
        identity = {"target": self.target.identity(), "run_id": run_id,
                    "attempt_id": attempt_id, "release_id": release_id}
        key = hashlib.sha256(_canonical_json(identity)).hexdigest()
        async with AsyncVuoroClient(self.profile, self.credential_resolver, transport=self.transport) as client:
            retention = CandidateRetention(self.root, self.target, client)
            # The API caller has already passed owner.get. Check the original
            # authenticated caller too before replaying saved transport intent.
            with retention._locked(key) as path:
                exists = path.exists() or path.is_symlink()
                if exists:
                    record = retention._read(path)
                    _need(record["producer"] == {"principal_id": principal_id, "workspace_id": workspace_id})
            if not exists:
                await retention.prepare(self.exporter, run_id, attempt_id, release_id=release_id,
                                        principal_id=principal_id, workspace_id=workspace_id)
            return await retention.retain(self.exporter, key)

    async def recover(self, receipt):
        """Use the configured current reader, never release provenance as access."""
        from vuoro_client import AsyncVuoroClient
        async with AsyncVuoroClient(self.profile, self.credential_resolver, transport=self.transport) as client:
            return await CandidateRetention(self.root, self.target, client).recover(receipt)
