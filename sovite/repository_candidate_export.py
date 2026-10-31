"""Uninstalled, release-gated export of an owner-retained repository candidate.

No public route is installed. The caller must already be an output-contract
approver of an exact UTF-8 release whose source is the completed repository job.
The protected owner-review preparation function is not an external read API.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

from . import output_contracts as outputs
from .db import Job
from .repository_sessions import (RepositorySessionManager, _canonical_json, _run_git,
                                  validate_packet)

_MAX_EXPORT_BYTES = 8 * 1024 * 1024
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class CandidateExportError(ValueError):
    """Fixed error without paths, fences, candidate bytes or upstream details."""


def _need(condition):
    if not condition:
        raise CandidateExportError("candidate-export-unavailable")


def _receipt_bytes(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        _need(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
              and not stat.S_IMODE(info.st_mode) & 0o077 and info.st_size <= 256 * 1024)
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return stream.read(256 * 1024 + 1)
    finally:
        os.close(fd)


class CandidateExportOwner:
    def __init__(self, platform: Any, resolver: Any, execution: Any):
        if execution.platform is not platform or execution.resolver is not resolver:
            raise CandidateExportError("candidate-export-unavailable")
        self.platform, self.resolver, self.execution = platform, resolver, execution

    def _job(self, session, run_id, attempt_id, principal_id, workspace_id):
        _need(all(isinstance(v, str) and _ID.fullmatch(v) for v in (run_id, attempt_id, principal_id, workspace_id)))
        job = session.get(Job, run_id, with_for_update=True)
        _need(job is not None and job.caller == principal_id and job.status == "completed")
        record = job.resolved.get("repository_run")
        _need(isinstance(record, dict) and record.get("workspace_id") == workspace_id
              and record.get("principal_id") == principal_id)
        attempts = record.get("attempts")
        _need(isinstance(attempts, list) and len(attempts) > 0 and attempts[-1].get("id") == attempt_id
              and attempts[-1].get("state") == "completed")
        self.platform.authorize_existing_job(session, job)
        self.execution.check_authorization(job)
        return job

    def _compose(self, job, attempt_id):
        run = job.resolved["repository_run"]
        attempt = run["attempts"][-1]
        identity = {"schema_version":"sovite-repository-session/v1", "binding_id":run["binding_id"],
                    "workspace_id":run["workspace_id"], "repository_id":run["repository_id"],
                    "job_id":job.id, "attempt_id":attempt_id}
        key = hashlib.sha256(_canonical_json(identity)).hexdigest()
        manager = RepositorySessionManager(self.execution.state_root, self.resolver)
        lock = manager._lock(key)
        try:
            record = manager._read_record(manager._record_path(key))
            _need(isinstance(record, dict) and record.get("state") == "candidate" and record.get("attempt_key") == key)
            packet = validate_packet(record["packet"])
            _need(manager._attempt_key(packet) == key and manager._packet_key(packet) == attempt["packet_sha256"]
                  and record.get("packet_sha256") == attempt["packet_sha256"]
                  and hashlib.sha256(packet.fence.encode()).hexdigest() == attempt["fence_sha256"]
                  and packet.generation == job.generation and packet.base_sha == run["base_sha"]
                  and packet.binding_id == run["binding_id"] and packet.deadline == run["deadline"]
                  and list(packet.allowed_areas) == run["allowed_areas"])
            mirror = run["mirror_root"]
            worktree = manager._worktree_path(key)
            _need(record.get("mirror_root") == mirror and record.get("worktree") == str(worktree))
            evidence = run.get("evidence")
            if isinstance(evidence, list):
                evidence = [entry for entry in evidence if entry.get("label") == "candidate"]
            expected_uri = f"sovite://repository-runs/{job.id}/{attempt_id}/candidate"
            _need(isinstance(evidence, list) and len(evidence) == 1 and evidence[0].get("label") == "candidate"
                  and evidence[0].get("uri") == expected_uri)
            raw = _receipt_bytes(manager.receipts_root / f"{key}.json")
            _need(hashlib.sha256(raw).hexdigest() == evidence[0].get("sha256"))
            receipt = manager._read_record(manager.receipts_root / f"{key}.json")
            _need(isinstance(receipt, dict) and _canonical_json(receipt) == raw
                  and receipt == record.get("candidate") and receipt.get("publication_eligible") is True
                  and receipt.get("tip_sha") == attempt.get("checkpoint_sha")
                  and receipt.get("packet_sha256") == attempt["packet_sha256"])
            snapshot = manager._candidate_snapshot(packet, worktree, Path(mirror))
            _need(manager._candidate_core(snapshot) == manager._candidate_core(receipt))
            # A linear sequence prevents an unrelated merge parent bringing its history into the bundle.
            status, parents = _run_git("-C", str(worktree), "rev-list", "--parents", f"{packet.base_sha}..HEAD", max_bytes=256 * 1024)
            _need(status == 0 and isinstance(parents, bytes))
            lines = parents.decode("ascii").splitlines()
            _need(lines and all(len(line.split()) == 2 for line in lines)
                  and lines[0].split()[0] == receipt["tip_sha"] and lines[-1].split()[1] == packet.base_sha)
            # A private temporary bare repo names the approved commit explicitly;
            # never let a candidate-controlled worktree HEAD select exported bytes.
            objects = str(Path(mirror) / "objects")
            _need("\n" not in objects and "\r" not in objects)
            export_limit = min(self.platform.settings.max_request_bytes, _MAX_EXPORT_BYTES)
            _need(type(export_limit) is int and export_limit > 0)
            with tempfile.TemporaryDirectory(prefix="sovite-candidate-export-") as tmp:
                status, _ = _run_git("init", "--bare", "--object-format=sha1", tmp, max_bytes=8192)
                _need(status == 0)
                (Path(tmp) / "objects" / "info" / "alternates").write_text(objects + "\n")
                status, _ = _run_git(f"--git-dir={tmp}", "update-ref", "refs/heads/candidate", receipt["tip_sha"], max_bytes=8192)
                _need(status == 0)
                status, bundle = _run_git(f"--git-dir={tmp}", "bundle", "create", "-", "refs/heads/candidate", f"^{packet.base_sha}",
                                          max_bytes=export_limit)
                _need(status == 0 and isinstance(bundle, bytes) and bundle)
            after = manager._candidate_snapshot(packet, worktree, Path(mirror))
            _need(manager._candidate_core(after) == manager._candidate_core(receipt))
            # The stored owner receipt contains a live fence. Export every other
            # receipt field as a separately hashed projection, never that token.
            receipt_projection = manager._candidate_core(receipt)
            del receipt_projection["fence"]
            projection_sha256 = hashlib.sha256(_canonical_json(receipt_projection)).hexdigest()
            payload = {"schema_version":"sovite-repository-candidate-export/v1", "run_id":job.id,
                       "attempt_id":attempt_id, "workspace_id":run["workspace_id"], "repository_id":run["repository_id"],
                       "base_sha":packet.base_sha, "tip_sha":receipt["tip_sha"],
                       "patch_sha256":receipt["patch_sha256"], "receipt_sha256":evidence[0]["sha256"],
                       "receipt_projection":receipt_projection, "receipt_projection_sha256":projection_sha256,
                       "bundle_sha256":hashlib.sha256(bundle).hexdigest(),
                       "bundle_base64":base64.b64encode(bundle).decode("ascii")}
            content = _canonical_json(payload).decode("utf-8")
            _need(len(content.encode("utf-8")) <= export_limit)
            return content
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            os.close(lock)

    def _prepare_for_owner_review(self, run_id, attempt_id, *, principal_id, workspace_id):
        """Protected-side only; never install this method as a caller read route."""
        try:
            with self.platform.db.sessions.begin() as session:
                self.platform.db.authority_lock(session)
                job = self._job(session, run_id, attempt_id, principal_id, workspace_id)
                content = self._compose(job, attempt_id)
                self._job(session, run_id, attempt_id, principal_id, workspace_id)
                return content
        except Exception:
            raise CandidateExportError("candidate-export-unavailable") from None

    def export(self, run_id, attempt_id, *, release_id, destination, principal_id, workspace_id):
        return self.release_snapshot(run_id, attempt_id, release_id=release_id, destination=destination,
                                     principal_id=principal_id, workspace_id=workspace_id)["content"]

    def release_snapshot(self, run_id, attempt_id, *, release_id, destination, principal_id, workspace_id):
        """Exact released bytes and disclosure provenance, not a future read grant."""
        try:
            _need(isinstance(destination, str) and _ID.fullmatch(destination))
            with self.platform.db.sessions.begin() as session:
                self.platform.db.authority_lock(session)
                job = self._job(session, run_id, attempt_id, principal_id, workspace_id)
                record, contract = outputs._record(self.platform, session, release_id)
                outputs._require_approver(self.platform, contract, principal_id, "caller")
                _need(record["source_job_id"] == run_id and destination in record["destinations"])
                content = self._compose(job, attempt_id)
                record, contract, released = outputs._read(self.platform, session, release_id)
                _need(record["source_job_id"] == run_id and destination in record["destinations"] and released == content)
                self._job(session, run_id, attempt_id, principal_id, workspace_id)
                outputs._require_approver(self.platform, contract, principal_id, "caller")
                self.platform.db.audit(session, principal_id, "repository_candidate.exported", run_id,
                                       {"attempt_id": attempt_id, "release_id": release_id,
                                        "destination": destination, "bundle_sha256": json.loads(content)["bundle_sha256"]})
                return {"content": released, "release_id": release_id,
                        "output_class": record["output_class"], "destination": destination,
                        "contract_id": record["contract_id"], "contract_revision": record["contract_revision"],
                        "sha256": hashlib.sha256(released.encode("utf-8")).hexdigest(),
                        "destination_hosts": next(d for d in self.platform.active(session)[0]["destinations"] if d["id"] == destination).get("allowed_hosts", [])}
        except Exception:
            raise CandidateExportError("candidate-export-unavailable") from None

    def recheck_snapshot(self, snapshot, *, principal_id, workspace_id):
        """Reauthorize saved exact bytes before a new external upload."""
        try:
            data = json.loads(snapshot["content"])
            with self.platform.db.sessions.begin() as session:
                self.platform.db.authority_lock(session)
                self._job(session, data["run_id"], data["attempt_id"], principal_id, workspace_id)
                record, contract, content = outputs._read(self.platform, session, snapshot["release_id"])
                outputs._require_approver(self.platform, contract, principal_id, "caller")
                destination = next(d for d in self.platform.active(session)[0]["destinations"] if d["id"] == snapshot["destination"])
                _need(content == snapshot["content"] and record["output_class"] == snapshot["output_class"]
                      and snapshot["destination"] in record["destinations"]
                      and destination.get("allowed_hosts", []) == snapshot["destination_hosts"])
        except Exception:
            raise CandidateExportError("candidate-export-unavailable") from None
