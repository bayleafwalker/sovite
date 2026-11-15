"""Synthetic commissioning-contract tests; no deployment evidence is collected."""
from __future__ import annotations

import asyncio
import base64
import copy
import json
import time
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient

from sovite import cli
from sovite.api import create_app
from sovite.db import Qualification, canonical
from sovite.qualification import (
    COMMISSIONING_SCHEMA, OPERATOR_CHECKS, ROLE_CHECKS, current_qualification,
    import_qualification, initialize_keys, issue_qualification, key_digest,
    qualification_deadline, runtime_settings_fingerprint, verify_qualification,
    write_qualification,
)
from sovite.service import PlatformError
from sovite.worker import Worker
from test_backend import OPERATOR, setup  # noqa: F401 (pytest fixture)
from test_hosting import hosted, signing  # noqa: F401 (pytest fixture)


def observations(settings, generation, role="operator", deadline=None):
    return {
        "schema": "sovite.commissioning/v1", "generation": generation,
        "runtime_binding": runtime_settings_fingerprint(settings), "profile": "local",
        "evidence_kind": "deployment", "scope": "Synthetic policy fixture; no deployed isolation or model competence established",
        "checks": [{"name": name, "status": "passed", "observed_at": time.time() - 60,
                    "valid_until": deadline, "evidence_ref": "fixture://commissioning/" + name,
                    "evidence_sha256": "sha256:" + "a" * 64}
                   for name in sorted(ROLE_CHECKS.get(role, OPERATOR_CHECKS))],
    }


def standalone(setup):
    settings, platform, _ = setup
    settings.require_qualification = True
    initialize_keys(settings)
    return settings, platform, platform.active()[1]


def resign(row, settings):
    key = serialization.load_pem_private_key(Path(settings.qualification_private_key).read_bytes(), password=None)
    row.signature = base64.b64encode(key.sign(canonical(row.payload))).decode()


def test_legacy_signing_moves_expiry_without_observing_anything(setup, monkeypatch):
    """Reproduce the old contract; legacy records keep that bounded compatibility."""
    settings, platform, generation = standalone(setup)
    evidence = {"generation": generation, "profile": "local", "scope": "Old fixture, no new observations", "checks": [{"name": "receipt-boundary", "status": "passed"}]}
    first = issue_qualification(platform.db, settings, generation, "local", evidence, valid_days=7)
    monkeypatch.setattr(time, "time", lambda: first.created_at + 6 * 86400)
    second = issue_qualification(platform.db, settings, generation, "local", evidence, valid_days=7)
    assert second.expires_at == first.expires_at + 6 * 86400
    assert second.payload["evidence"] == first.payload["evidence"]
    assert not verify_qualification(first, settings, now=first.expires_at)


def test_until_change_roundtrip_persists_without_calendar_renewal(setup, tmp_path, monkeypatch):
    settings, platform, generation = standalone(setup)
    row = issue_qualification(None, settings, generation, "local", observations(settings, generation), until_change=True)
    assert row.expires_at == 0 and row.payload["schema"] == COMMISSIONING_SCHEMA
    assert qualification_deadline(row, settings) is None
    path = tmp_path / "signed.json"
    write_qualification(row, str(path))
    imported = import_qualification(platform.db, settings, generation, str(path))
    assert imported.signature == row.signature
    monkeypatch.setattr(time, "time", lambda: row.created_at + 365 * 86400)
    assert verify_qualification(imported, settings)
    with platform.db.sessions() as session:
        assert current_qualification(session, settings, generation).id == row.id
    assert platform.submit("incident-forensics", "fixture", "cloud-codex", "after-one-year")


def test_resigning_does_not_extend_real_evidence_deadline(setup, monkeypatch):
    settings, platform, generation = standalone(setup)
    deadline = time.time() + 7 * 86400
    evidence = observations(settings, generation, deadline=deadline)
    first = issue_qualification(None, settings, generation, "local", evidence, until_change=True)
    monkeypatch.setattr(time, "time", lambda: deadline - 1)
    second = issue_qualification(None, settings, generation, "local", evidence, until_change=True)
    assert second.created_at > first.created_at
    assert qualification_deadline(first, settings) == qualification_deadline(second, settings) == deadline
    assert verify_qualification(second, settings)
    assert not verify_qualification(second, settings, now=deadline)
    monkeypatch.setattr(time, "time", lambda: deadline)
    with pytest.raises(ValueError, match="dated deployment evidence"):
        issue_qualification(None, replace(settings, qualification_private_key="/absent"), generation, "local", evidence, until_change=True)
    with pytest.raises(ValueError, match="must not reset"):
        issue_qualification(None, settings, generation, "local", evidence)


@pytest.mark.parametrize("failure", ["missing_observation", "future", "nan", "bool_observation", "huge_observation", "huge_deadline", "missing_deadline", "expired", "before_observation", "bool_deadline", "missing_ref", "bad_digest", "duplicate", "missing_required", "not_run", "synthetic", "binding", "schema"])
def test_incomplete_or_stale_commissioning_fails_before_key_access(setup, failure):
    settings, platform, generation = standalone(setup)
    evidence = observations(settings, generation)
    check = evidence["checks"][0]
    if failure == "missing_observation": check.pop("observed_at")
    elif failure == "future": check["observed_at"] = time.time() + 3600
    elif failure == "nan": check["observed_at"] = float("nan")
    elif failure == "bool_observation": check["observed_at"] = True
    elif failure == "huge_observation": check["observed_at"] = 10**400
    elif failure == "huge_deadline": check["valid_until"] = 10**400
    elif failure == "missing_deadline": check.pop("valid_until")
    elif failure == "expired": check["valid_until"] = time.time() - 1
    elif failure == "before_observation": check["valid_until"] = check["observed_at"] - 1
    elif failure == "bool_deadline": check["valid_until"] = True
    elif failure == "missing_ref": check.pop("evidence_ref")
    elif failure == "bad_digest": check["evidence_sha256"] = "a" * 64
    elif failure == "duplicate": evidence["checks"].append(copy.deepcopy(check))
    elif failure == "missing_required": evidence["checks"].pop()
    elif failure == "not_run": check["status"] = "not_run"
    elif failure == "synthetic": evidence["evidence_kind"] = "synthetic-integration"
    elif failure == "binding": evidence.pop("runtime_binding")
    elif failure == "schema": evidence.pop("schema")
    with pytest.raises(ValueError, match="Commissioning requires"):
        issue_qualification(None, replace(settings, qualification_private_key="/absent"), generation, "local", evidence, until_change=True)


@pytest.mark.parametrize("failure", ["revoked", "removed_key", "runtime", "generation", "profile", "signature", "legacy_zero", "legacy_validity", "deadline", "future", "binding", "huge_observation", "huge_deadline", "huge_created_at", "bool_expiry"])
def test_commissioning_rechecks_negative_authority_inputs(setup, failure):
    settings, platform, generation = standalone(setup)
    row = issue_qualification(None, settings, generation, "local", observations(settings, generation), until_change=True)
    if failure == "revoked": settings.qualification_revoked_keys = [key_digest(row.public_key)]
    elif failure == "removed_key": settings.qualification_public_keys = []
    elif failure == "runtime": settings.artifact_path += "/different"
    elif failure == "generation": row.payload["generation"] = "other-generation"
    elif failure == "profile": row.payload["profile"] = "other-profile"
    elif failure == "signature": row.signature = "wrong"
    elif failure == "legacy_zero":
        row.payload["schema"] = "sovite.qualification/v1"
        row.payload.pop("validity")
    elif failure == "legacy_validity": row.payload["schema"] = "sovite.qualification/v1"
    elif failure == "deadline": row.payload["evidence"]["checks"][0]["valid_until"] = time.time() - 1
    elif failure == "future": row.payload["evidence"]["checks"][0]["observed_at"] = time.time() + 3600
    elif failure == "binding": row.payload["evidence"]["runtime_binding"] = "other-binding"
    elif failure == "huge_observation": row.payload["evidence"]["checks"][0]["observed_at"] = 10**400
    elif failure == "huge_deadline": row.payload["evidence"]["checks"][0]["valid_until"] = 10**400
    elif failure == "huge_created_at": row.created_at = row.payload["created_at"] = 10**400
    elif failure == "bool_expiry": row.payload["expires_at"] = False
    if failure not in {"signature", "removed_key"}: resign(row, settings)
    assert not verify_qualification(row, settings)


def test_metrics_and_api_distinguish_unbounded_and_actual_deadline(setup):
    settings, platform, generation = standalone(setup)
    row = issue_qualification(platform.db, settings, generation, "local", observations(settings, generation), until_change=True)
    with TestClient(create_app(settings, platform)) as client:
        def metrics():
            return {line.split()[0]: float(line.split()[1]) for line in client.get("/metrics").text.splitlines() if not line.startswith("#")}
        values = metrics()
        assert values["sovite_qualified"] == 1
        assert values["sovite_qualification_time_bounded"] == values["sovite_qualification_expiry_timestamp_seconds"] == 0
        record = client.get("/api/qualifications", headers={"Authorization": "Bearer " + OPERATOR}).json()["items"][0]
        assert record["valid"] and record["expires_at"] == 0 and record["effective_expires_at"] is None
        settings.qualification_revoked_keys = [key_digest(row.public_key)]
        values = metrics()
        assert values["sovite_qualified"] == 0 and values["sovite_qualification_expiry_timestamp_seconds"] == 0


def test_deadline_selection_prefers_longest_usable_record(setup):
    settings, platform, generation = standalone(setup)
    short = issue_qualification(platform.db, settings, generation, "local", observations(settings, generation, deadline=time.time() + 100), until_change=True)
    legacy = issue_qualification(platform.db, settings, generation, "local", {"generation": generation, "profile": "local", "scope": "legacy fixture", "checks": [{"name": "receipt-boundary", "status": "passed"}]})
    with platform.db.sessions() as session:
        assert current_qualification(session, settings, generation).id == legacy.id
    unbounded = issue_qualification(platform.db, settings, generation, "local", observations(settings, generation), until_change=True)
    with platform.db.sessions() as session:
        assert current_qualification(session, settings, generation).id == unbounded.id
    settings.qualification_issuers = [{"path": settings.qualification_public_keys[0], "role": "operator", "expires_at": time.time() + 50}]
    assert qualification_deadline(unbounded, settings) < qualification_deadline(short, replace(settings, qualification_issuers=[]))


def test_evidence_expiry_between_admission_and_execution_blocks_work(setup, monkeypatch):
    settings, platform, generation = standalone(setup)
    deadline = time.time() + 100
    issue_qualification(platform.db, settings, generation, "local", observations(settings, generation, deadline=deadline), until_change=True)
    job = platform.submit("incident-forensics", "fixture", "cloud-codex", "before-expiry")
    monkeypatch.setattr(time, "time", lambda: deadline)
    asyncio.run(Worker(platform).run_once())
    assert platform.get_job(job.id).status == "blocked"
    assert platform.public_status(job.receipt, "cloud-codex")["status"] == "unavailable"


def test_signing_and_import_do_not_clear_recovery_inhibit(setup, tmp_path):
    settings, platform, generation = standalone(setup)
    platform.inhibit("incident-forensics", "Restored state requires review")
    row = issue_qualification(None, settings, generation, "local", observations(settings, generation), until_change=True)
    path = tmp_path / "commissioning.json"
    write_qualification(row, str(path))
    import_qualification(platform.db, settings, generation, str(path))
    with pytest.raises(PlatformError) as denied:
        platform.submit("incident-forensics", "fixture", "cloud-codex", "inhibited")
    assert denied.value.code == "grant_inhibited"


@pytest.mark.parametrize("failure", ["missing_platform", "expired_key", "not_yet_valid", "revoked", "wrong_workspace", "wrong_role", "envelope", "runtime", "generation"])
def test_hosted_commissioning_preserves_independent_roles_and_boundaries(hosted, failure):
    settings, platform, _, envelope = hosted
    generation = platform.active()[1]
    rows = {}
    for role in ("platform", "tenant"):
        rows[role] = issue_qualification(None, signing(settings, role), generation, "local", observations(settings, generation, role), role=role, until_change=True)
    if failure == "missing_platform": rows.pop("platform")
    elif failure == "expired_key": settings.qualification_issuers[0]["expires_at"] = time.time() - 1
    elif failure == "not_yet_valid": settings.qualification_issuers[0]["not_before"] = time.time() + 100
    elif failure == "revoked": settings.qualification_revoked_keys = [key_digest(rows["tenant"].public_key)]
    elif failure == "wrong_workspace":
        rows["tenant"].payload["workspace"] = "another-workspace"
        resign(rows["tenant"], signing(settings, "tenant"))
    elif failure == "wrong_role":
        rows["tenant"].payload["role"] = "platform"
        resign(rows["tenant"], signing(settings, "tenant"))
    elif failure == "envelope":
        envelope["revision"] += 1
        Path(settings.hosting_envelope_path).write_text(json.dumps(envelope))
    elif failure == "runtime": settings.public_origin = "https://changed.example"
    elif failure == "generation":
        rows["tenant"].generation = rows["tenant"].payload["generation"] = "other-generation"
        resign(rows["tenant"], signing(settings, "tenant"))
    with platform.db.sessions.begin() as session:
        session.add_all(rows.values())
    with platform.db.sessions() as session:
        assert current_qualification(session, settings, generation) is None
    with pytest.raises(PlatformError):
        platform.submit("incident-forensics", "fixture", "cloud-codex", "denied", input_class="C1")


def test_hosted_pair_roundtrip_uses_earliest_evidence_or_issuer_deadline(hosted, tmp_path):
    settings, platform, _, _ = hosted
    generation = platform.active()[1]
    deadlines = {"platform": time.time() + 100, "tenant": None}
    rows = {}
    for role in ("platform", "tenant"):
        row = issue_qualification(None, signing(settings, role), generation, "local", observations(settings, generation, role, deadlines[role]), role=role, until_change=True)
        path = tmp_path / (role + ".json")
        write_qualification(row, str(path))
        rows[role] = import_qualification(platform.db, settings, generation, str(path))
    with platform.db.sessions() as session:
        assert current_qualification(session, settings, generation).id == rows["platform"].id
    # The tenant issuer can make the other role's effective deadline earlier.
    settings.qualification_issuers[1]["expires_at"] = time.time() + 50
    with platform.db.sessions() as session:
        assert current_qualification(session, settings, generation).id == rows["tenant"].id
    assert platform.submit("incident-forensics", "fixture", "cloud-codex", "allowed", input_class="C1")


def test_cli_validity_flags_are_exclusive(monkeypatch):
    monkeypatch.setattr("sys.argv", ["sovite", "qualify", "--evidence", "unused", "--until-change", "--valid-days", "7"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2


@pytest.mark.parametrize("online", [False, True])
def test_cli_commissioning_reaches_online_and_offline_signers(setup, tmp_path, monkeypatch, online):
    settings, platform, generation = standalone(setup)
    path = tmp_path / "observations.json"
    path.write_text(json.dumps(observations(settings, generation)))
    monkeypatch.setattr(cli.Settings, "from_env", classmethod(lambda cls, **kwargs: settings))
    args = ["sovite", "qualify", "--evidence", str(path), "--until-change"]
    output = tmp_path / "signed.json"
    if not online:
        args += ["--output", str(output)]
        monkeypatch.setattr(cli, "Platform", lambda *args: pytest.fail("Offline signing must not open the runtime DB"))
    monkeypatch.setattr("sys.argv", args)
    cli.main()
    if online:
        with platform.db.sessions() as session:
            assert current_qualification(session, settings, generation).payload["validity"] == "until-change"
    else:
        assert json.loads(output.read_text())["payload"]["schema"] == COMMISSIONING_SCHEMA


def test_until_change_and_explicit_days_rejected_by_owner(setup):
    settings, _, generation = standalone(setup)
    with pytest.raises(ValueError, match="not both"):
        issue_qualification(None, settings, generation, "local", observations(settings, generation), valid_days=7, until_change=True)


def test_timed_commissioning_metrics_expire_with_observations(setup, monkeypatch):
    settings, platform, generation = standalone(setup)
    deadline = time.time() + 100
    issue_qualification(platform.db, settings, generation, "local", observations(settings, generation, deadline=deadline), until_change=True)
    with TestClient(create_app(settings, platform)) as client:
        def values():
            return {line.split()[0]: float(line.split()[1]) for line in client.get("/metrics").text.splitlines() if not line.startswith("#")}
        current = values()
        assert current["sovite_qualification_time_bounded"] == current["sovite_qualified"] == 1
        assert current["sovite_qualification_expiry_timestamp_seconds"] == int(deadline)
        monkeypatch.setattr(time, "time", lambda: deadline)
        expired = values()
        assert expired["sovite_qualified"] == expired["sovite_qualification_expiry_timestamp_seconds"] == 0



def test_current_query_excludes_expired_legacy_history(setup, monkeypatch):
    import sovite.qualification as module
    settings, platform, generation = standalone(setup)
    legacy = {"generation": generation, "profile": "local", "scope": "legacy fixture", "checks": [{"name": "receipt-boundary", "status": "passed"}]}
    old = issue_qualification(platform.db, settings, generation, "local", legacy, valid_days=1)
    current = issue_qualification(platform.db, settings, generation, "local", observations(settings, generation), until_change=True)
    seen = []
    original = module.verify_qualification
    def record_verification(row, *args, **kwargs):
        seen.append(row.id)
        return original(row, *args, **kwargs)
    monkeypatch.setattr(module, 'verify_qualification', record_verification)
    monkeypatch.setattr(time, 'time', lambda: old.expires_at + 1)
    with platform.db.sessions() as session:
        assert current_qualification(session, settings, generation).id == current.id
    assert seen == [current.id]
