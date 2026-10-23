"""Independent owner counterexamples; copied only by root after scope admission.

Use -k 'not postgresql' for the permitted SQLite review. The imported fixtures
are the actual frozen Platform/Database owner fixtures, not replacement state.
Synthetic artifact tuples exercise the existing trusted-local finish boundary;
they claim no artifact readback, remote completion or real storage evidence.
"""
import copy

import pytest

from test_execution_leases import owner, database, gateway_bundle  # noqa: F401
from test_execution_leases import submit, pull, start, snapshot
from sovite.db import ExecutionAuthority, WorkerExecutionLease
from sovite.service import PlatformError
from sovite.worker import Worker


def test_repeated_new_start_replay_key_remains_already_started(owner):
    submit(owner)
    reserved = pull(owner)
    assert start(owner, reserved)["state"] == "started"
    assert start(owner, reserved, operation="new-start-key")["state"] == "already_started"
    before = snapshot(owner.platform)
    assert start(owner, reserved, operation="new-start-key")["state"] == "already_started"
    assert snapshot(owner.platform) == before


def test_original_start_key_remains_current_after_coherent_renewal(owner):
    submit(owner)
    reserved = pull(owner)
    first = start(owner, reserved)
    owner.clock[0] += 5
    assert owner.service.renew(first["job_id"], fence=first["fence"]) is True
    before = snapshot(owner.platform)
    assert start(owner, reserved)["state"] == "already_started"
    assert snapshot(owner.platform) == before


def test_fixed_execution_deadline_prevents_late_artifact_release(owner):
    owner.platform.settings.job_timeout_seconds = 1
    submit(owner)
    worker = Worker(owner.platform)
    job = worker.claim()
    owner.clock[0] = job.execution_deadline + 1
    assert owner.clock[0] < job.lease_until
    worker.finish(job.id, artifact=("synthetic-no-read-artifact", "a" * 64), fence=job.execution_fence)
    current = owner.platform.get_job(job.id)
    assert current.public_projection is None, "elapsed fixed deadline published an artifact"
    assert current.status != "completed"


def test_current_trusted_finish_before_fixed_deadline_positive_control(owner):
    owner.platform.settings.job_timeout_seconds = 1
    submit(owner)
    worker = Worker(owner.platform)
    job = worker.claim()
    assert owner.clock[0] < job.execution_deadline < job.lease_until
    owner.clock[0] += 0.5
    assert owner.clock[0] < job.execution_deadline
    worker.finish(job.id, artifact=("synthetic-no-read-artifact", "a" * 64), fence=job.execution_fence)
    current = owner.platform.get_job(job.id)
    assert current.status == "completed"
    assert current.public_projection == {"receipt": current.receipt, "status": "available"}
    with owner.platform.db.sessions() as session:
        assert session.get(WorkerExecutionLease, job.id).phase == "finished"
        if owner.model == "gateway":
            assert session.get(ExecutionAuthority, job.id).capability_digest == ""
    assert owner.platform.db.verify_audit()["valid"]


@pytest.mark.parametrize("owner", ["gateway"], indirect=True)
def test_actual_binding_inhibit_prevents_artifact_release(owner):
    submit(owner)
    worker = Worker(owner.platform)
    job = worker.claim()
    owner.platform.inhibit_binding(job.resolved["model"]["id"], "independent fixture stop")
    assert owner.platform.get_job(job.id).cancel_requested is False
    worker.finish(job.id, artifact=("synthetic-no-read-artifact", "a" * 64), fence=job.execution_fence)
    assert owner.platform.get_job(job.id).public_projection is None, "inhibited gateway binding published an artifact"


def test_first_start_uses_owner_time_after_actual_lock_acquisition(owner, monkeypatch):
    submit(owner)
    reserved = pull(owner)
    original = owner.platform.db.authority_lock

    def lock_then_advance_clock(session):
        original(session)
        # Controlled owner-clock advance at the real lock boundary models a
        # queued transaction reaching the lock after its reservation expired.
        owner.clock[0] = reserved["job"]["lease_until"] + 1

    monkeypatch.setattr(owner.platform.db, "authority_lock", lock_then_advance_clock)
    before = snapshot(owner.platform)
    with pytest.raises(PlatformError) as caught:
        start(owner, reserved)
    assert caught.value.code == "execution_fenced"
    assert snapshot(owner.platform) == before


def test_current_start_under_real_authority_lock_positive_control(owner, monkeypatch):
    submit(owner)
    reserved = pull(owner)
    assert reserved["job"]["lease_until"] == owner.clock[0] + owner.platform.settings.lease_seconds
    original = owner.platform.db.authority_lock
    acquired = []

    def witnessed_lock(session):
        original(session)
        acquired.append(True)

    monkeypatch.setattr(owner.platform.db, "authority_lock", witnessed_lock)
    first = start(owner, reserved)
    assert acquired and first["state"] == "started"
    assert first["deadline"] == owner.clock[0] + 120
    assert owner.platform.db.verify_audit()["valid"]


def test_reserve_samples_lease_epoch_after_actual_lock_acquisition(owner, monkeypatch):
    submit(owner)
    original = owner.platform.db.authority_lock

    def lock_then_advance_clock(session):
        original(session)
        owner.clock[0] += 100

    monkeypatch.setattr(owner.platform.db, "authority_lock", lock_then_advance_clock)
    reserved = pull(owner)
    assert reserved["state"] == "reserved"
    assert reserved["job"]["lease_until"] == owner.clock[0] + owner.platform.settings.lease_seconds
    assert reserved["job"]["lease_until"] > owner.clock[0]


def test_renew_refuses_lease_that_expires_before_lock_is_acquired(owner, monkeypatch):
    submit(owner)
    worker = Worker(owner.platform)
    job = worker.claim()
    assert worker.renew(job.id) is True
    original = owner.platform.db.authority_lock

    def lock_then_advance_clock(session):
        original(session)
        owner.clock[0] = job.lease_until + 1

    monkeypatch.setattr(owner.platform.db, "authority_lock", lock_then_advance_clock)
    before = snapshot(owner.platform)
    assert worker.renew(job.id, fence=job.execution_fence) is False
    assert snapshot(owner.platform) == before


@pytest.mark.parametrize("expired_boundary", ["lease", "fixed-deadline"])
def test_finish_refuses_expiry_observed_after_actual_lock_acquisition(owner, monkeypatch, expired_boundary):
    owner.platform.settings.job_timeout_seconds = 1
    submit(owner)
    worker = Worker(owner.platform)
    job = worker.claim()
    assert owner.clock[0] < job.execution_deadline < job.lease_until
    original = owner.platform.db.authority_lock

    def lock_then_advance_clock(session):
        original(session)
        owner.clock[0] = (job.lease_until if expired_boundary == "lease" else job.execution_deadline) + 1

    monkeypatch.setattr(owner.platform.db, "authority_lock", lock_then_advance_clock)
    before = snapshot(owner.platform)
    worker.finish(job.id, artifact=("synthetic-no-read-artifact", "a" * 64), fence=job.execution_fence)
    assert owner.platform.get_job(job.id).public_projection is None
    assert snapshot(owner.platform) == before


def test_current_trusted_handback_positive_control(owner):
    submit(owner)
    worker = Worker(owner.platform)
    job = worker.claim()
    assert owner.clock[0] < job.execution_deadline and owner.clock[0] < job.lease_until
    worker.hand_back(job.id, fence=job.execution_fence)
    current = owner.platform.get_job(job.id)
    assert current.status == "retry_wait" and current.attempts == 0
    with owner.platform.db.sessions() as session:
        assert session.get(WorkerExecutionLease, job.id).phase == "finished"
        if owner.model == "gateway":
            assert session.get(ExecutionAuthority, job.id).capability_digest == ""
    assert owner.platform.db.verify_audit()["valid"]


@pytest.mark.parametrize("expired_boundary", ["lease", "fixed-deadline"])
def test_handback_refuses_expiry_observed_after_actual_lock_acquisition(owner, monkeypatch, expired_boundary):
    owner.platform.settings.job_timeout_seconds = 1
    submit(owner)
    worker = Worker(owner.platform)
    job = worker.claim()
    assert owner.clock[0] < job.execution_deadline < job.lease_until
    original = owner.platform.db.authority_lock

    def lock_then_advance_clock(session):
        original(session)
        owner.clock[0] = (job.lease_until if expired_boundary == "lease" else job.execution_deadline) + 1

    monkeypatch.setattr(owner.platform.db, "authority_lock", lock_then_advance_clock)
    before = snapshot(owner.platform)
    worker.hand_back(job.id, fence=job.execution_fence)
    assert snapshot(owner.platform) == before, "expired task uncharged an attempt through handback"


@pytest.mark.parametrize("field", ["instance", "operation"])
def test_dotted_opaque_ids_are_rejected_by_frozen_ascii_id_contract(owner, field):
    # R4a frozen requirements, strict common wire types: Id is1..64 ASCII
    # [A-Za-z0-9_-]+. Extraction explicitly retains that instance grammar.
    assert pull(owner, **{field: "contains_dot"}) == {"state": "idle"}
    before = snapshot(owner.platform)
    with pytest.raises(PlatformError) as caught:
        pull(owner, **{field: "contains.dot"})
    assert caught.value.code == "invalid_request" and caught.value.status == 422
    assert snapshot(owner.platform) == before


@pytest.mark.parametrize("owner", ["gateway"], indirect=True)
@pytest.mark.parametrize("corruption", ["workload", "deadline"])
def test_renew_refuses_incoherent_gateway_authority(owner, corruption):
    submit(owner)
    worker = Worker(owner.platform)
    job = worker.claim()
    assert worker.renew(job.id) is True
    with owner.platform.db.sessions.begin() as session:
        row = session.get(ExecutionAuthority, job.id)
        if corruption == "workload":
            changed = copy.deepcopy(row.context)
            changed["workload"] = "foreign-workload"
            row.context = changed
        else:
            row.deadline += 100
    before = snapshot(owner.platform)
    owner.clock[0] += 1
    assert worker.renew(job.id) is False, "incoherent gateway authority renewed"
    assert snapshot(owner.platform) == before


@pytest.mark.parametrize("owner", ["gateway"], indirect=True)
def test_revoked_gateway_started_context_cannot_replay_as_current(owner):
    submit(owner)
    reserved = pull(owner)
    first = start(owner, reserved)
    assert first["state"] == "started" and first["execution"]["capability"]
    assert start(owner, reserved)["state"] == "already_started"
    with owner.platform.db.sessions.begin() as session:
        row = session.get(ExecutionAuthority, first["job_id"])
        assert row.capability_digest
        # Controlled digest-only revocation isolates inference authority from
        # otherwise unchanged Job/lease/current grant and binding state.
        row.capability_digest = ""
    before = snapshot(owner.platform)
    with pytest.raises(PlatformError) as caught:
        start(owner, reserved)
    assert caught.value.code == "execution_fenced" and caught.value.status == 409
    assert snapshot(owner.platform) == before
