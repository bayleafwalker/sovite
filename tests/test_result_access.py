"""Class-aware release regressions rebuilt after workspace recovery.

These tests exercise restored result code and current authority at both sides of
artifact I/O. They are a fresh verification suite, not an exact-byte recovery of
the previous test file.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

from sovite import policy
from sovite.db import AuditEvent, BundleRevision, Job
from sovite.results import result_status, scoped_read
from sovite.service import Platform, PlatformError
from sovite.settings import Settings
from sovite.worker import Worker
from test_workflow_admission import installed as mixed_installed, approval


ROOT = Path(__file__).resolve().parents[1]
CANARY = "PRIVATE-RESULT-CANARY"
BODY = {"answer": CANARY, "evidence": ["verified-fixture"]}


@pytest.fixture
def installed(tmp_path):
    bundle, _ = policy.load_bundle(ROOT / "config/platform.local.yaml")
    bundle["grants"][0].update(return_contract="class-aware", caller_clearance="C3",
                               result_readers=["reader"])
    path = tmp_path / "bundle.yaml"
    path.write_text(yaml.safe_dump(bundle))
    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/control.db",
        bundle_path=str(path), artifact_path=str(tmp_path / "artifacts"),
        operator_token="operator-result-test-0000000000000000",
        caller_tokens={
            "cloud-codex": "caller-result-test-0000000000000000",
            "operations": "operations-result-test-000000000000",
            "reader": "reader-result-test-0000000000000000",
            "stranger": "stranger-result-test-00000000000000",
        },
        require_qualification=False,
    )
    platform = Platform(settings)
    platform.db.migrate()
    platform.initialize()
    return platform, bundle


def configure(platform, bundle, **fields):
    for name, value in fields.items():
        if value is None:
            bundle["grants"][0].pop(name, None)
        else:
            bundle["grants"][0][name] = value
    Path(platform.settings.bundle_path).write_text(yaml.safe_dump(bundle))
    platform.reload(platform.settings.bundle_path)


def complete(platform, *, caller="cloud-codex", grant="incident-forensics", key="result", input_class=None):
    row = platform.submit(grant, "private task", caller, key, input_class=input_class)
    worker = Worker(platform)
    assert worker.claim().id == row.id
    worker.finish(row.id, artifact=platform.store.put(row.id, BODY))
    row = platform.get_job(row.id)
    assert row.status == "completed"
    return row


def change_current_grant(platform, job, **changes):
    """Simulate current owner revocation independently of the pinned grant."""
    with platform.db.sessions.begin() as session:
        revision = session.get(BundleRevision, job.generation)
        bundle = deepcopy(revision.bundle)
        grant = next(value for value in bundle["grants"] if value["id"] == job.grant_id)
        grant.update(changes)
        revision.bundle = bundle


def assert_withheld(operation):
    try:
        value = operation()
    except PlatformError:
        return
    assert "result" not in value
    assert value["status"] == "unavailable"
    assert CANARY not in json.dumps(value)


def test_cleared_caller_receives_exact_verified_class_aware_content(installed):
    platform, _ = installed
    row = complete(platform)
    assert result_status(platform, row.receipt, row.caller) == {
        "receipt": row.receipt, "status": "available", "result": BODY,
    }


@pytest.mark.parametrize("clearance", ["C1", None])
def test_uncleared_caller_gets_receipt_and_scoped_reader_reference(installed, clearance):
    platform, bundle = installed
    configure(platform, bundle, caller_clearance=clearance)
    row = complete(platform)
    result = result_status(platform, row.receipt, row.caller)
    assert result == {"receipt": row.receipt, "status": "available",
                      "reader": {"tool": "read_result", "receipt": row.receipt}}
    assert CANARY not in json.dumps(result)


def test_legacy_reference_contract_remains_exact_two_field_receipt(installed):
    platform, bundle = installed
    configure(platform, bundle, return_contract="reference")
    row = complete(platform)
    assert result_status(platform, row.receipt, row.caller) == {
        "receipt": row.receipt, "status": "available",
    }


@pytest.mark.parametrize("cleared", [True, False])
def test_pending_content_has_reader_only_when_caller_lacks_clearance(installed, cleared):
    platform, bundle = installed
    if not cleared:
        configure(platform, bundle, caller_clearance="C1")
    row = platform.submit("incident-forensics", "pending", "cloud-codex", "pending")
    expected = {"receipt": row.receipt, "status": "pending"}
    if not cleared:
        expected["reader"] = {"tool": "read_result", "receipt": row.receipt}
    assert result_status(platform, row.receipt, row.caller) == expected
    assert scoped_read(platform, row.receipt, "reader") == {
        "receipt": row.receipt, "status": "pending",
    }


def test_explicit_scoped_reader_gets_result_without_submission_or_operator_access(installed):
    platform, _ = installed
    row = complete(platform)
    assert scoped_read(platform, row.receipt, "reader") == {
        "receipt": row.receipt, "status": "available", "result": BODY,
    }
    with pytest.raises(PlatformError):
        platform.submit(row.grant_id, "reader cannot execute", "reader", "forbidden")
    with pytest.raises(PlatformError):
        result_status(platform, row.receipt, "reader")


@pytest.mark.parametrize("identity", ["stranger", "operations", "operator", "missing"])
def test_receipt_knowledge_and_operator_name_do_not_grant_scoped_read(installed, identity):
    platform, _ = installed
    row = complete(platform)
    with pytest.raises(PlatformError) as denied:
        scoped_read(platform, row.receipt, identity)
    assert denied.value.code == "not_found"
    assert CANARY not in denied.value.message


def test_legacy_contract_only_has_reader_access_when_explicitly_installed(installed):
    platform, bundle = installed
    configure(platform, bundle, return_contract="reference", result_readers=[])
    row = complete(platform)
    with pytest.raises(PlatformError):
        scoped_read(platform, row.receipt, "reader")


def test_reader_cannot_cross_to_a_different_grant(installed):
    platform, _ = installed
    row = complete(platform, grant="change-assessment", caller="operations", input_class="C2")
    with pytest.raises(PlatformError) as denied:
        scoped_read(platform, row.receipt, "reader")
    assert denied.value.code == "not_found"


@pytest.mark.parametrize("identity,reader", [("cloud-codex", False), ("reader", True)])
def test_removed_principal_cannot_reuse_receipt(installed, identity, reader):
    platform, _ = installed
    row = complete(platform)
    del platform.settings.caller_tokens[identity]
    operation = scoped_read if reader else result_status
    with pytest.raises(PlatformError):
        operation(platform, row.receipt, identity)


def test_reader_must_appear_in_both_pinned_and_current_grant(installed):
    platform, _ = installed
    row = complete(platform)
    change_current_grant(platform, row, result_readers=[])
    with pytest.raises(PlatformError):
        scoped_read(platform, row.receipt, "reader")
    change_current_grant(platform, row, result_readers=["reader", "stranger"])
    with pytest.raises(PlatformError):
        scoped_read(platform, row.receipt, "stranger")
    assert scoped_read(platform, row.receipt, "reader")["result"] == BODY


@pytest.mark.parametrize("revocation", ["grant", "qualification", "generation"])
def test_current_authority_is_required_for_caller_and_scoped_reader(installed, revocation):
    platform, bundle = installed
    row = complete(platform)
    if revocation == "grant":
        platform.inhibit(row.grant_id, "Result revoked")
    elif revocation == "qualification":
        platform.settings.require_qualification = True
    else:
        bundle["metadata"]["version"] = "new-generation"
        Path(platform.settings.bundle_path).write_text(yaml.safe_dump(bundle))
        platform.reload(platform.settings.bundle_path)
    assert_withheld(lambda: result_status(platform, row.receipt, row.caller))
    with pytest.raises(PlatformError):
        scoped_read(platform, row.receipt, "reader")


@pytest.mark.parametrize("mutation", ["missing", "changed-bytes", "foreign-job-key"])
def test_result_artifact_digest_and_job_storage_identity_are_verified(installed, mutation):
    platform, _ = installed
    row = complete(platform)
    path = platform.store.root / row.artifact_key
    if mutation == "missing":
        path.unlink()
    elif mutation == "changed-bytes":
        path.write_bytes(b'{"answer":"UNVERIFIED"}')
    else:
        key, sha = platform.store.put("different-job", BODY)
        with platform.db.sessions.begin() as session:
            stored = session.get(Job, row.id)
            stored.artifact_key, stored.artifact_sha256 = key, sha
    for read in (
        lambda: result_status(platform, row.receipt, row.caller),
        lambda: scoped_read(platform, row.receipt, "reader"),
    ):
        with pytest.raises(PlatformError) as denied:
            read()
        assert denied.value.code == "artifact_integrity_failed"
        assert CANARY not in denied.value.message


@pytest.mark.parametrize("body", [
    b'["not-an-object"]', b'{"value":NaN}', b'{"value":Infinity}',
    b'{"answer":"one","answer":"two"}', b"\xff",
])
def test_result_content_requires_finite_unique_utf8_json_object(installed, body):
    platform, _ = installed
    row = complete(platform)
    sha = hashlib.sha256(body).hexdigest()
    key = f"results/{row.id}/{sha}.json"
    (platform.store.root / key).write_bytes(body)
    with platform.db.sessions.begin() as session:
        stored = session.get(Job, row.id)
        stored.artifact_key, stored.artifact_sha256 = key, sha
    with pytest.raises(PlatformError) as denied:
        scoped_read(platform, row.receipt, "reader")
    assert denied.value.code == "artifact_integrity_failed"


@pytest.mark.parametrize("mutation", ["projection", "release-time", "digest"])
def test_artifact_persistence_without_release_markers_never_exposes_content(installed, mutation):
    platform, _ = installed
    row = complete(platform)
    with platform.db.sessions.begin() as session:
        stored = session.get(Job, row.id)
        if mutation == "projection":
            stored.public_projection = None
        elif mutation == "release-time":
            stored.released_at = None
        else:
            stored.artifact_sha256 = None
    assert_withheld(lambda: result_status(platform, row.receipt, row.caller))
    assert_withheld(lambda: scoped_read(platform, row.receipt, "reader"))


@pytest.mark.parametrize("reader", [False, True])
def test_grant_revocation_during_storage_io_is_rechecked(installed, monkeypatch, reader):
    platform, _ = installed
    row = complete(platform)
    get = platform.store.get

    def revoke(key, sha):
        body = get(key, sha)
        platform.inhibit(row.grant_id, "Revoked while result bytes were read")
        return body

    monkeypatch.setattr(platform.store, "get", revoke)
    if reader:
        with pytest.raises(PlatformError):
            scoped_read(platform, row.receipt, "reader")
    else:
        assert_withheld(lambda: result_status(platform, row.receipt, row.caller))


@pytest.mark.parametrize("reader", [False, True])
def test_principal_removal_during_storage_io_is_rechecked(installed, monkeypatch, reader):
    platform, _ = installed
    row = complete(platform)
    identity = "reader" if reader else row.caller
    get = platform.store.get

    def revoke(key, sha):
        body = get(key, sha)
        del platform.settings.caller_tokens[identity]
        return body

    monkeypatch.setattr(platform.store, "get", revoke)
    operation = scoped_read if reader else result_status
    with pytest.raises(PlatformError):
        operation(platform, row.receipt, identity)


def test_replaced_artifact_identity_after_io_cannot_be_released(installed, monkeypatch):
    platform, _ = installed
    row = complete(platform)
    get = platform.store.get
    other_key, other_sha = platform.store.put("another-job", BODY)

    def replace_identity(key, sha):
        body = get(key, sha)
        with platform.db.sessions.begin() as session:
            stored = session.get(Job, row.id)
            stored.artifact_key, stored.artifact_sha256 = other_key, other_sha
        return body

    monkeypatch.setattr(platform.store, "get", replace_identity)
    with pytest.raises(PlatformError):
        scoped_read(platform, row.receipt, "reader")


def test_access_audit_contains_only_bounded_metadata(installed):
    platform, _ = installed
    row = complete(platform)
    result_status(platform, row.receipt, row.caller)
    scoped_read(platform, row.receipt, "reader")
    with platform.db.sessions() as session:
        events = session.scalars(select(AuditEvent)).all()
        values = [event.__dict__ for event in events]
        assert CANARY not in repr(values)


def complete_workflow_chain(platform, binding):
    from sovite.output_contracts import approve_output

    source = platform.submit_workflow_role(binding, "implementer", "Private source",
        "workflow-consumer", "source-chain")
    worker = Worker(platform)
    assert worker.claim().id == source.id
    worker.finish(source.id, artifact=platform.store.put(source.id, {"answer": "PRIVATE-SOURCE"}))
    source = platform.get_job(source.id)
    release = approve_output(platform, **approval(platform, source))
    target = platform.submit_workflow_role(binding, "inline_reviewer", "", "workflow-consumer",
        "target-chain", source_release=release["release_id"], transfer_id="inline")
    assert worker.claim().id == target.id
    worker.finish(target.id, artifact=platform.store.put(target.id, BODY))
    return source, platform.get_job(target.id)


@pytest.mark.parametrize("reader", [False, True])
@pytest.mark.parametrize("during_io", [False, True])
def test_source_grant_revocation_blocks_completed_downstream_result(mixed_installed, monkeypatch, reader, during_io):
    platform, binding = mixed_installed
    source, target = complete_workflow_chain(platform, binding)
    assert result_status(platform, target.receipt, target.caller)["result"] == BODY
    assert scoped_read(platform, target.receipt, "approver")["result"] == BODY
    observed = []

    if during_io:
        get = platform.store.get

        def revoke_at_target_read(key, sha):
            value = get(key, sha)
            if key == target.artifact_key:
                observed.append(key)
                platform.inhibit(source.grant_id, "Source revoked during downstream storage read")
            return value

        monkeypatch.setattr(platform.store, "get", revoke_at_target_read)
    else:
        platform.inhibit(source.grant_id, "Source revoked after downstream execution")

    if reader:
        with pytest.raises(PlatformError):
            scoped_read(platform, target.receipt, "approver")
    else:
        assert_withheld(lambda: result_status(platform, target.receipt, target.caller))
    if during_io:
        assert observed == [target.artifact_key]
