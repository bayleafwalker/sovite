"""Owner-admitted repair lineage over existing jobs, releases and retained bytes.

Disabled unless explicitly composed with a current Workspace reader, an exact
feedback output contract, and a qualified executor accepting repair_context.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import or_, select

from .db import Job
from .repository_runs import _timestamp, expected_preview_id, validate_run_packet
from .service import PlatformError
from . import output_contracts as outputs
from .flow import class_ranks


def _need(condition, code="repository_repair_unavailable"):
    if not condition:
        raise PlatformError(code, "The repository repair does not match current owner authority", 409)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, allow_nan=False,
                                     sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            _need(key not in value)
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: _need(False))


@dataclass(frozen=True)
class RepairPolicy:
    max_repairs: int
    feedback_contract_ids: tuple[str, ...]

    def __post_init__(self):
        _need(type(self.max_repairs) is int and 1 <= self.max_repairs <= 3
              and bool(self.feedback_contract_ids)
              and all(isinstance(value, str) and value for value in self.feedback_contract_ids))


def preview_id(principal_id, parent_run_id, candidate_sha256, feedback_release_id, max_cost_usd):
    # All mutable request selectors participate in the principal-bound intent.
    return _digest({"principal":principal_id,
                    "repair":{"parent_run_id":parent_run_id, "candidate_sha256":candidate_sha256,
                              "feedback_release_id":feedback_release_id, "max_cost_usd":max_cost_usd}})


def feedback(platform, session, parent, repair, recipe):
    record, _, content = outputs._read(platform, session, repair["feedback_release_id"])
    _need(record["source_job_id"] == parent.id
          and record["contract_id"] in repair["feedback_contract_ids"])
    ranks = class_ranks(platform.active(session)[0])
    input_class = recipe.get("input_class", recipe["grant"]["input_class"])
    _need(recipe["model"]["destination"] in record["destinations"]
          and ranks[record["output_class"]] <= ranks[input_class])
    _need(len(content.encode("utf-8")) <= 65536)
    value = _json(content)
    from .repository_repair_contract.runtime import load_bundle
    load_bundle().validate_schema("RepairFeedback.json", value)
    _need(value["parent_run_id"] == parent.id
        and value["candidate_export_sha256"] == repair["candidate_sha256"]
        and value["candidate_tip_sha"] == repair["candidate_tip_sha"]
        and bool(value["feedback"].strip()))
    _need(_digest(value) == repair["feedback_sha256"])
    return value


def candidate_release(platform, session, parent, repair, recipe):
    """Workspace read authority never substitutes for model disclosure."""
    retained = [entry for entry in parent.resolved["repository_run"]["evidence"]
                if entry.get("label") == "retained-candidate"]
    _need(len(retained) == 1 and retained[0]["sha256"] == repair["candidate_sha256"]
          and retained[0]["receipt"]["sha256"] == repair["candidate_sha256"]
          and retained[0]["receipt"]["release_id"] == repair["candidate_release_id"])
    record, _, content = outputs._read(platform, session, repair["candidate_release_id"])
    ranks = class_ranks(platform.active(session)[0])
    input_class = recipe.get("input_class", recipe["grant"]["input_class"])
    _need(record["source_job_id"] == parent.id
          and record["output_class"] == retained[0]["receipt"]["output_class"]
          and ranks[record["output_class"]] <= ranks[input_class]
          and recipe["model"]["destination"] in record["destinations"]
          and hashlib.sha256(content.encode("utf-8")).hexdigest() == repair["candidate_sha256"])
    return content


def admit(platform, session, run, caller, recipe, task):
    """Called inside Platform.submit's existing authority transaction."""
    repair = copy.deepcopy(run["repair"])
    _need(set(repair) == {"parent_run_id", "root_run_id", "repair_number", "max_repairs",
        "candidate_sha256", "candidate_tip_sha", "candidate_release_id", "feedback_release_id", "feedback_sha256",
        "feedback_contract_ids", "input_key", "input_sha256"})
    parent = session.get(Job, repair["parent_run_id"], with_for_update=True)
    _need(parent is not None and parent.caller == caller and parent.status == "completed")
    prior = parent.resolved.get("repository_run")
    _need(not prior.get('recoveries') if isinstance(prior,dict) else False)
    _need(isinstance(prior, dict) and parent.task == task
          and all(prior.get(key) == run.get(key) for key in (
              "workspace_id", "repository_id", "executor_id", "base_sha", "allowed_areas", "binding_id",
              "principal_id", "grant_id", "input_class", "generation", "publication_revision",
              "qualification_ref", "qualification_sha256", "qualification_expires_at")))
    root_id = prior.get("repair", {}).get("root_run_id", parent.id)
    number = prior.get("repair", {}).get("repair_number", 0) + 1
    _need(root_id == repair["root_run_id"] and number == repair["repair_number"]
          and type(repair["max_repairs"]) is int and number <= repair["max_repairs"] <= 3)
    root = session.get(Job, root_id, with_for_update=True)
    _need(root is not None and root.caller == caller and root.resolved.get("repository_run") is not None)
    root_run = root.resolved["repository_run"]
    _need(_timestamp(run["deadline"], "deadline") <= _timestamp(root_run["deadline"], "deadline")
          and run["concurrency"] <= root_run["concurrency"]
          and run["max_cost_usd"] <= prior["max_cost_usd"])
    # The authority lock serializes all child admission. JSON lineage remains
    # execution-owner state in the existing Job records, not a second ledger.
    chain = list(session.scalars(select(Job).where(Job.caller == caller, or_(Job.id == root_id,
        Job.resolved["repository_run"]["repair"]["root_run_id"].as_string() == root_id)).limit(5)))
    _need(all(row.resolved.get("repository_run", {}).get("repair", {}).get("parent_run_id") != parent.id
              for row in chain), "repository_repair_successor_exists")
    _need(len(chain) == number and all(row.status == "completed" for row in chain))
    usage = Decimal(0)
    for row in chain:
        current = row.resolved["repository_run"]
        _need(not current.get("active_slots") and all(r.get("state") == "settled"
                                                     for r in current.get("budget_reservations", [])),
              "repository_repair_usage_unknown")
        cost = current.get("usage_usd")
        _need(type(cost) in {int, float} and math.isfinite(cost) and cost >= 0)
        usage += Decimal(str(cost))
    _need(usage + Decimal(str(run["max_cost_usd"])) <= Decimal(str(root_run["max_cost_usd"])),
          "repository_repair_budget_exceeded")
    platform.authorize_existing_job(session, parent, purpose="observe")
    feedback(platform, session, parent, repair, recipe)
    candidate_release(platform, session, parent, repair, recipe)
    # Exact input bytes were prepared before this transaction. Recheck their
    # content identity after current authority, just as output release does.
    value = _json(platform.store.get(repair["input_key"], repair["input_sha256"]))
    _need(set(value) == {"candidate_export", "feedback"}
          and hashlib.sha256(value["candidate_export"].encode()).hexdigest() == repair["candidate_sha256"]
          and _digest(value["feedback"]) == repair["feedback_sha256"])
    return repair


def recheck(platform, session, row, recipe):
    repair = row.resolved.get("repository_run", {}).get("repair")
    if repair is None:
        return
    parent = session.get(Job, repair["parent_run_id"])
    _need(parent is not None and parent.caller == row.caller and parent.status == "completed")
    feedback(platform, session, parent, repair, recipe)
    candidate_release(platform, session, parent, repair, recipe)


def _parent_packet(parent, maximum_usd):
    prior = parent.resolved["repository_run"]
    _need(maximum_usd <= prior["max_cost_usd"])
    return validate_run_packet({"schema_version":"portable-work/v1",
        **{key:prior[key] for key in ("workspace_id", "repository_id", "executor_id", "base_sha", "deadline", "concurrency")},
        "goal":parent.task, "max_cost_usd":maximum_usd})


class RepositoryRepairService:
    def __init__(self, owner, retention, policy):
        _need(isinstance(policy, RepairPolicy))
        self.owner, self.retention, self.policy = owner, retention, policy

    def feedback_context(self, body, *, principal_id):
        """Read exact approval inputs; this operation makes no approval."""
        from .repository_repair_contract.runtime import load_bundle, FEEDBACK_CONTEXT_OPERATION
        from .flow import contract_revision
        contract_bundle = load_bundle()
        contract_bundle.validate_request(FEEDBACK_CONTEXT_OPERATION, body)
        _need(body["feedback_contract_id"] in self.policy.feedback_contract_ids)
        owner, platform = self.owner, self.owner.platform
        _need(getattr(getattr(owner.executor, "executor", None), "supports_repository_repair_context", False) is True)
        projection = owner.get(body["parent_run_id"], principal_id=principal_id)
        _need(projection["state"] == "completed")
        with platform.db.sessions.begin() as session:
            platform.db.authority_lock(session)
            bundle, generation = platform.active(session)
            contract = next((c for c in bundle.get("output_contracts", [])
                             if c["id"] == body["feedback_contract_id"]), None)
            _need(contract is not None)
            outputs._require_approver(platform, contract, principal_id, "caller")
            parent, recipe = outputs._source(platform, session, body["parent_run_id"], generation)
            _need(parent.caller == principal_id)
            owner._binding(_parent_packet(parent, parent.resolved["repository_run"]["max_cost_usd"]), principal_id)
            ranks = class_ranks(bundle)
            input_class = recipe.get("input_class", recipe["grant"]["input_class"])
            _need(ranks[recipe["grant"]["output_class"]] <= ranks[contract["source_class"]]
                  and ranks[contract["output_class"]] <= ranks[input_class]
                  and recipe["model"]["destination"] in contract["destinations"])
            retained = [entry for entry in parent.resolved["repository_run"]["evidence"]
                        if entry.get("label") == "retained-candidate"]
            _need(len(retained) == 1)
            source = {"candidate_sha256":retained[0]["sha256"],
                      "candidate_release_id":retained[0]["receipt"]["release_id"]}
            data = _json(candidate_release(platform, session, parent, source, recipe))
            _need(data["run_id"] == parent.id)
            platform.store.get(parent.artifact_key, parent.artifact_sha256)
            result = {"parent_run_id":parent.id, "source_digest":"sha256:"+parent.artifact_sha256,
                "contract_id":contract["id"], "contract_revision":contract_revision(contract),
                "expected_generation":generation, "candidate_sha256":retained[0]["sha256"],
                "candidate_tip_sha":data["tip_sha"]}
            # Mutable settings can revoke an approving identity during I/O.
            outputs._require_approver(platform, contract, principal_id, "caller")
            platform.authorize_existing_job(session, parent, purpose="observe")
            candidate_release(platform, session, parent, source, recipe)
            owner._binding(_parent_packet(parent, parent.resolved["repository_run"]["max_cost_usd"]), principal_id)
            contract_bundle.validate_response(FEEDBACK_CONTEXT_OPERATION, 200, result)
            return result

    async def submit(self, body, *, principal_id, idempotency_key):
        owner, platform = self.owner, self.owner.platform
        from .repository_repair_contract.runtime import load_bundle, OPERATION
        load_bundle().validate_request(OPERATION, body)
        _need(getattr(getattr(owner.executor, "executor", None), "supports_repository_repair_context", False) is True)
        _need(body["preview_id"] == preview_id(principal_id, body["parent_run_id"],
              body["candidate_sha256"], body["feedback_release_id"], body["max_cost_usd"]))
        projection = owner.get(body["parent_run_id"], principal_id=principal_id)
        with platform.db.sessions() as session:
            parent = session.get(Job, body["parent_run_id"])
            _need(parent is not None and parent.caller == principal_id)
            # Original owner-authenticated intent supplies every field except
            # a lowerable child spend cap. No second packet wire model.
            packet = _parent_packet(parent, body["max_cost_usd"])
        owner._binding(packet, principal_id)
        _need(projection["state"] == "completed" and projection["repository_id"] == packet["repository_id"])
        retained = [entry for entry in projection["evidence"] if entry.get("label") == "retained-candidate"]
        _need(len(retained) == 1 and retained[0]["sha256"] == body["candidate_sha256"])
        # Current class/destination and actual Workspace membership are checked
        # by CandidateRetention.recover before bytes enter this owner process.
        raw = await self.retention.recover(retained[0]["receipt"])
        export = _json(raw)
        _need(export["schema_version"] == "sovite-repository-candidate-export/v1"
              and export["run_id"] == body["parent_run_id"] and export["base_sha"] == packet["base_sha"]
              and export["repository_id"] == packet["repository_id"] and export["workspace_id"] == packet["workspace_id"])
        with platform.db.sessions.begin() as session:
            platform.db.authority_lock(session)
            parent = session.get(Job, body["parent_run_id"])
            _need(parent is not None and parent.caller == principal_id)
            prior = parent.resolved["repository_run"]
            repair = {"parent_run_id":parent.id, "root_run_id":prior.get("repair", {}).get("root_run_id", parent.id),
                      "repair_number":prior.get("repair", {}).get("repair_number", 0)+1,
                      "max_repairs":self.policy.max_repairs, "candidate_sha256":body["candidate_sha256"],
                      "candidate_tip_sha":export["tip_sha"], "candidate_release_id":retained[0]["receipt"]["release_id"],
                      "feedback_release_id":body["feedback_release_id"],
                      "feedback_contract_ids":list(self.policy.feedback_contract_ids)}
            # Read the exact approved feedback under its existing source chain.
            record, _, content = outputs._read(platform, session, body["feedback_release_id"])
            repair["feedback_sha256"] = _digest(_json(content))
            recipe = platform.authorize_existing_job(session, parent, purpose="observe")
            approved = feedback(platform, session, parent, repair, recipe)
            _need(candidate_release(platform, session, parent, repair, recipe).encode("utf-8") == raw)
        key = "repairinput_" + _digest({"candidate":body["candidate_sha256"], "feedback":repair["feedback_sha256"]})
        input_key, input_sha = platform.store.put(key, {"candidate_export":raw.decode("utf-8"), "feedback":approved})
        repair.update(input_key=input_key, input_sha256=input_sha)
        run = owner.submit(packet, preview_id=expected_preview_id(packet, principal_id),
                           principal_id=principal_id, idempotency_key=idempotency_key, repair=repair)
        return {"run_id":run["id"], "parent_run_id":repair["parent_run_id"],
                "root_run_id":repair["root_run_id"], "repair_number":repair["repair_number"]}


def prepare_execution(platform, job, workspace, protected_root):
    """Seed exact retained Git bytes before the native mutable process starts."""
    import base64
    import os
    import re
    import tempfile
    from .repository_sessions import _git
    repair = job.resolved["repository_run"]["repair"]
    # Current exact candidate and feedback disclosure precedes seeding as well
    # as every subsequent owner lease/model-use authorization check.
    with platform.db.sessions.begin() as session:
        platform.db.authority_lock(session)
        current_job = session.get(Job, job.id)
        _need(current_job is not None)
        platform.authorize_existing_job(session, current_job)
    value = _json(platform.store.get(repair["input_key"], repair["input_sha256"]))
    raw = value["candidate_export"].encode("utf-8")
    _need(hashlib.sha256(raw).hexdigest() == repair["candidate_sha256"])
    data = _json(raw)
    base, tip = job.resolved["repository_run"]["base_sha"], repair["candidate_tip_sha"]
    _need(re.fullmatch(r"[0-9a-f]{40}", tip) and data["base_sha"] == base
          and data["tip_sha"] == tip and data["run_id"] == repair["parent_run_id"])
    _need(_digest(data["receipt_projection"]) == data["receipt_projection_sha256"]
          and data["receipt_projection"]["base_sha"] == base
          and data["receipt_projection"]["tip_sha"] == tip
          and data["receipt_projection"]["patch_sha256"] == data["patch_sha256"])
    bundle = base64.b64decode(data["bundle_base64"], validate=True)
    _need(len(bundle) <= 8*1024*1024 and hashlib.sha256(bundle).hexdigest() == data["bundle_sha256"])
    checkout = workspace.worktree
    current = _git("rev-parse", "HEAD", cwd=checkout).decode().strip()
    _need(current in {base, tip} and not _git("status", "--porcelain", "--untracked-files=all", cwd=checkout))
    fd, path = tempfile.mkstemp(prefix="repair-seed-", dir=protected_root)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(bundle); stream.flush(); os.fsync(stream.fileno())
        _git("bundle", "verify", path, cwd=checkout)
        _need(_git("bundle", "list-heads", path, cwd=checkout).decode().splitlines()
              == [tip+" refs/heads/candidate"])
        _git("fetch", "--no-tags", "--no-recurse-submodules", path, "refs/heads/candidate", cwd=checkout)
    finally:
        os.unlink(path)
    previous = base
    commits = _git("rev-list", "--reverse", "--parents", base+".."+tip, cwd=checkout).decode().splitlines()
    _need(0 < len(commits) <= 1000)
    for line in commits:
        fields = line.split(); _need(len(fields) == 2 and fields[1] == previous)
        previous = fields[0]
    _need(previous == tip)
    patch = _git("diff", "--binary", "--no-ext-diff", "--no-textconv", "--no-renames", base, tip, "--", cwd=checkout)
    _need(hashlib.sha256(patch).hexdigest() == data["patch_sha256"])
    areas = job.resolved["repository_run"]["allowed_areas"]
    for name in filter(None, _git("diff", "--name-only", "-z", "--no-renames", base, tip, "--", cwd=checkout).split(b"\0")):
        name = name.decode("utf-8")
        _need(any(area == "." or name == area or name.startswith(area+"/") for area in areas))
    for entry in filter(None, _git("ls-tree", "-rz", tip, cwd=checkout).split(b"\0")):
        _need(entry.split(b" ", 1)[0] in {b"100644", b"100755"})
    if current == base:
        _git("reset", "--hard", tip, cwd=checkout)
    with platform.db.sessions.begin() as session:
        platform.db.authority_lock(session)
        current_job = session.get(Job, job.id)
        _need(current_job is not None)
        recipe = platform.authorize_existing_job(session, current_job)
        parent = session.get(Job, repair["parent_run_id"])
        approved = feedback(platform, session, parent, repair, recipe)
    return {key:repair[key] for key in ("root_run_id", "parent_run_id", "repair_number", "candidate_tip_sha")} | {
        "feedback":approved["feedback"], "feedback_sha256":repair["feedback_sha256"]}
