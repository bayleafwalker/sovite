"""Bounded final-row-lock clock checks against actual frozen owner fixtures.

Clock advance follows the real Session.get(..., with_for_update=True) call.
SQLite does not establish PostgreSQL blocking: this isolates the decision-time
predicate at that actual row-read/locking boundary; root owns real PG waits.
"""
import pytest

from test_execution_leases import owner, database, gateway_bundle  # noqa: F401
from test_execution_leases import submit, pull, start, snapshot
from sovite.db import WorkerExecutionLease
from sovite.service import PlatformError


def advance_after_lease_row_lock(owner, monkeypatch, *, target):
    session_class = owner.platform.db.sessions.class_
    original = session_class.get
    entered = []

    def get_then_advance(session, entity, *args, **kwargs):
        result = original(session, entity, *args, **kwargs)
        if entity is WorkerExecutionLease and kwargs.get("with_for_update"):
            assert result is not None, "final-lock fixture needs an existing lease"
            entered.append(result.job_id)
            owner.clock[0] = target
        return result

    monkeypatch.setattr(session_class, "get", get_then_advance)
    return entered


def test_reserved_replay_rejects_expiry_after_final_lease_lock(owner, monkeypatch):
    submit(owner)
    reserved = pull(owner)
    # Valid same-key live replay and no effects before the isolated fault.
    before = snapshot(owner.platform)
    assert pull(owner) == reserved
    assert snapshot(owner.platform) == before
    entered = advance_after_lease_row_lock(
        owner, monkeypatch, target=reserved["job"]["lease_until"] + 1
    )
    before = snapshot(owner.platform)
    with pytest.raises(PlatformError) as caught:
        pull(owner)
    assert entered
    assert caught.value.code == "execution_fenced" and caught.value.status == 409
    assert snapshot(owner.platform) == before


def test_reclaimed_reserve_timestamps_follow_final_existing_lease_lock(owner, monkeypatch):
    submitted = submit(owner)
    first = pull(owner)
    assert first["job"]["lease_until"] == owner.clock[0] + owner.platform.settings.lease_seconds
    assert start(owner, first)["state"] == "started"
    owner.clock[0] = first["job"]["lease_until"] + 1
    owner.service.recover_expired()  # Actual explicit recovery, before baseline.
    assert owner.platform.get_job(submitted.id).status == "retry_wait"
    entered = advance_after_lease_row_lock(
        owner, monkeypatch, target=owner.clock[0] + 100
    )
    replacement = pull(owner, operation="after-final-lock")
    assert entered and replacement["state"] == "reserved"
    assert replacement["job"]["fence"] != first["job"]["fence"]
    assert replacement["job"]["lease_until"] == owner.clock[0] + owner.platform.settings.lease_seconds
    assert replacement["job"]["lease_until"] > owner.clock[0]


def test_first_start_final_lease_lock_expiry_refusal_control(owner, monkeypatch):
    submit(owner)
    reserved = pull(owner)
    entered = advance_after_lease_row_lock(
        owner, monkeypatch, target=reserved["job"]["lease_until"] + 1
    )
    before = snapshot(owner.platform)
    with pytest.raises(PlatformError) as caught:
        start(owner, reserved)
    assert entered
    assert caught.value.code == "execution_fenced" and caught.value.status == 409
    assert snapshot(owner.platform) == before
