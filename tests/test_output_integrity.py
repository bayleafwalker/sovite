"""Stored output approvals bind exact source, content, evidence and authority."""
from copy import deepcopy
import hashlib
import json

import pytest
from sqlalchemy import select

from sovite.db import Job, MetadataRow
from sovite.output_contracts import approve_output, read_output
from sovite.service import PlatformError
from sovite.worker import Worker
from test_workflow_admission import installed as mixed_installed, approval


def finish_source(platform, binding, key):
    job = platform.submit_workflow_role(binding, "implementer", "private source", "workflow-consumer", key)
    worker = Worker(platform)
    assert worker.claim().id == job.id
    worker.finish(job.id, artifact=platform.store.put(job.id, {"answer": "PRIVATE-" + key}))
    return platform.get_job(job.id)


@pytest.fixture
def source(mixed_installed):
    platform, binding = mixed_installed
    return platform, binding, finish_source(platform, binding, "source")


@pytest.fixture
def released(source):
    platform, binding, job = source
    args = approval(platform, job)
    release = approve_output(platform, **args)
    return platform, binding, job, args, release


def change_record(platform, release_key, **changes):
    with platform.db.sessions.begin() as session:
        row = session.get(MetadataRow, release_key)
        row.value = {**deepcopy(row.value), **changes}


def test_exact_approval_replays_and_retains_authenticated_actor_role(released):
    platform, _, _, args, release = released
    assert approve_output(platform, **args) == release
    with platform.db.sessions() as session:
        record = session.get(MetadataRow, release["release_id"]).value
        assert record["actor"] == "approver" and record["actor_role"] == "caller"
    assert read_output(platform, release["release_id"], "approver")["content"] == args["content"]


def test_metadata_changed_during_read_cannot_describe_different_artifact_bytes(released, monkeypatch):
    from sovite import output_contracts

    platform, _, _, _, release = released
    replacement_key, replacement_sha = platform.store.put(release["release_id"],
        {"content": "Different bytes that this decision did not approve."})
    read = platform.store.get
    with platform.db.sessions.begin() as session:
        platform.db.authority_lock(session)
        # Keep the ORM record live, as a caller of the internal transfer/read
        # boundary may. The storage callback changes that same mutable record.
        row = session.get(MetadataRow, release["release_id"])
        original_key = row.value["artifact_key"]

        def replace_record_after_read(key, sha):
            body = read(key, sha)
            if key == original_key:
                row.value.update(artifact_key=replacement_key, artifact_sha256=replacement_sha)
            return body

        monkeypatch.setattr(platform.store, "get", replace_record_after_read)
        with pytest.raises(PlatformError) as denied:
            output_contracts._read(platform, session, release["release_id"])
        assert denied.value.code == "output_release_integrity_failed"
    assert output_contracts._VERIFICATION.get() is None


@pytest.mark.parametrize("mutation", ["foreign-key", "missing-digest", "wrong-projection", "missing-release-time"])
def test_source_release_markers_and_storage_identity_are_required(released, mutation):
    platform, _, source, args, release = released
    with platform.db.sessions.begin() as session:
        row = session.get(Job, source.id)
        if mutation == "foreign-key":
            row.artifact_key, row.artifact_sha256 = platform.store.put("another-job", {"answer": "PRIVATE"})
            # Even when the caller knows the other digest, it cannot stand in for
            # this job's source identity.
            args["source_digest"] = "sha256:" + row.artifact_sha256
        elif mutation == "missing-digest":
            row.artifact_sha256 = None
        elif mutation == "wrong-projection":
            row.public_projection = {"receipt": "another-receipt", "status": "available"}
        else:
            row.released_at = None
    with pytest.raises(PlatformError):
        approve_output(platform, **args)
    with pytest.raises(PlatformError):
        read_output(platform, release["release_id"], "approver")


@pytest.mark.parametrize("field,value", [
    ("output_class", "C3"), ("destinations", ["execution"]),
    ("release_id", "release_" + "0" * 64), ("actor_role", []),
    ("approved_at", "not-a-time"), ("evidence_refs", []),
    ("approved_at", 10 ** 1000), ("source_job_id", "\ud800"),
])
def test_malformed_or_policy_inconsistent_release_metadata_fails_closed(released, field, value):
    platform, _, _, _, release = released
    change_record(platform, release["release_id"], **{field: value})
    with pytest.raises(PlatformError) as denied:
        read_output(platform, release["release_id"], "approver")
    assert denied.value.code == "output_release_integrity_failed"


@pytest.mark.parametrize("resolved", [None, [], {}, {"grant": []}])
def test_malformed_current_source_resolution_has_a_bounded_integrity_failure(released, resolved):
    platform, _, source, args, release = released
    with platform.db.sessions.begin() as session:
        session.get(Job, source.id).resolved = resolved
    for operation in (
        lambda: approve_output(platform, **args),
        lambda: read_output(platform, release["release_id"], "approver"),
    ):
        with pytest.raises(PlatformError) as denied:
            operation()
        assert denied.value.code == "artifact_integrity_failed"


def test_approval_rejects_non_utf8_content_without_a_release(source):
    platform, _, job = source
    args = approval(platform, job)
    args["content"] = "\ud800"
    with pytest.raises(PlatformError) as denied:
        approve_output(platform, **args)
    assert denied.value.code == "invalid_output"
    with platform.db.sessions() as session:
        assert not session.scalars(select(MetadataRow).where(MetadataRow.key.like("release_%"))).all()


@pytest.mark.parametrize("operation", ["read", "replay", "transfer"])
@pytest.mark.parametrize("artifact", ["source", "released"])
def test_qualification_expiry_during_read_blocks_release_and_replay(released, monkeypatch, operation, artifact):
    from sovite import service

    platform, binding, source, args, release = released
    allowed = [True]
    seen = []
    qualified = service.is_qualified
    monkeypatch.setattr(service, "is_qualified",
        lambda *a, **kw: allowed[0] and qualified(*a, **kw))
    get = platform.store.get

    def expire_after_read(key, sha):
        value = get(key, sha)
        selected = (key == source.artifact_key if artifact == "source"
                    else key.startswith("results/" + release["release_id"] + "/"))
        if selected:
            seen.append(key)
            allowed[0] = False
        return value

    monkeypatch.setattr(platform.store, "get", expire_after_read)
    with pytest.raises(PlatformError) as denied:
        if operation == "read":
            read_output(platform, release["release_id"], "approver")
        elif operation == "replay":
            approve_output(platform, **args)
        else:
            platform.submit_workflow_role(binding, "inline_reviewer", "", "workflow-consumer",
                "expired-transfer", source_release=release["release_id"], transfer_id="inline")
    assert seen and denied.value.code == "qualification_required"
    with platform.db.sessions() as session:
        assert session.scalars(select(MetadataRow).where(MetadataRow.key.like("release_%"))).all()
        assert not session.scalars(select(Job).where(Job.id != source.id)).all()


@pytest.mark.parametrize("storage_operation", ["get", "put"])
def test_qualification_expiry_during_approval_io_creates_no_release_authority(source, monkeypatch, storage_operation):
    from sovite import service

    platform, _, job = source
    allowed = [True]
    seen = []
    qualified = service.is_qualified
    monkeypatch.setattr(service, "is_qualified",
        lambda *a, **kw: allowed[0] and qualified(*a, **kw))
    original = getattr(platform.store, storage_operation)

    def expire_after_io(*args):
        result = original(*args)
        seen.append(args[0])
        allowed[0] = False
        return result

    monkeypatch.setattr(platform.store, storage_operation, expire_after_io)
    with pytest.raises(PlatformError) as denied:
        approve_output(platform, **approval(platform, job))
    assert seen and denied.value.code == "qualification_required"
    with platform.db.sessions() as session:
        assert not session.scalars(select(MetadataRow).where(MetadataRow.key.like("release_%"))).all()



@pytest.mark.parametrize("content", ["", " ", "\ud800"])
def test_corrupt_released_content_is_rejected_before_hashing_or_return(released, content):
    platform, _, _, _, release = released
    body = json.dumps({"content": content}, ensure_ascii=True).encode("ascii")
    sha = hashlib.sha256(body).hexdigest()
    key = f"results/{release['release_id']}/{sha}.json"
    path = platform.store.root / key
    path.write_bytes(body)
    change_record(platform, release["release_id"], artifact_key=key, artifact_sha256=sha)
    with pytest.raises(PlatformError) as denied:
        read_output(platform, release["release_id"], "approver")
    assert denied.value.code == "artifact_integrity_failed"


def test_rehashed_artifact_content_cannot_reuse_approved_release_identity(released):
    platform, _, _, _, release = released
    key, sha = platform.store.put(release["release_id"], {"content": "UNAPPROVED-PRIVATE-CONTENT"})
    change_record(platform, release["release_id"], artifact_key=key, artifact_sha256=sha)
    with pytest.raises(PlatformError) as denied:
        read_output(platform, release["release_id"], "approver")
    assert denied.value.code == "output_release_integrity_failed"
    assert "UNAPPROVED" not in denied.value.message


def test_changed_evidence_cannot_reuse_existing_approval_identity(released):
    platform, _, _, args, release = released
    changed = deepcopy(args["evidence_refs"])
    changed[0]["revision"] = "a-different-evidence-revision"
    change_record(platform, release["release_id"], evidence_refs=changed)
    with pytest.raises(PlatformError) as denied:
        read_output(platform, release["release_id"], "approver")
    assert denied.value.code == "output_release_integrity_failed"


def test_another_valid_source_cannot_replace_the_approved_source(released):
    platform, binding, _, _, release = released
    other = finish_source(platform, binding, "another-source")
    change_record(platform, release["release_id"], source_job_id=other.id,
                  source_digest="sha256:" + other.artifact_sha256)
    with pytest.raises(PlatformError) as denied:
        read_output(platform, release["release_id"], "approver")
    assert denied.value.code == "output_release_integrity_failed"


def test_release_storage_key_cannot_point_to_another_release(released):
    platform, _, _, args, release = released
    key, sha = platform.store.put("release_" + "f" * 64, {"content": args["content"]})
    change_record(platform, release["release_id"], artifact_key=key, artifact_sha256=sha)
    with pytest.raises(PlatformError) as denied:
        read_output(platform, release["release_id"], "approver")
    assert denied.value.code == "output_release_integrity_failed"


def test_release_artifact_has_exact_content_envelope(released):
    platform, _, _, args, release = released
    key, sha = platform.store.put(release["release_id"], {"content": args["content"], "extra": "UNAPPROVED"})
    change_record(platform, release["release_id"], artifact_key=key, artifact_sha256=sha)
    with pytest.raises(PlatformError) as denied:
        read_output(platform, release["release_id"], "approver")
    assert denied.value.code == "artifact_integrity_failed"


def test_unrelated_reader_cannot_trigger_artifact_reads(released, monkeypatch):
    platform, _, _, _, release = released
    reads = []

    def forbidden_read(*args):
        reads.append(args)
        raise AssertionError("Unrelated reader must be denied before storage access")

    monkeypatch.setattr(platform.store, "get", forbidden_read)
    with pytest.raises(PlatformError) as denied:
        read_output(platform, release["release_id"], "workflow-consumer")
    assert denied.value.code == "declassification_denied" and not reads


def test_operator_is_not_implicitly_an_output_contract_approver(source):
    platform, _, job = source
    with pytest.raises(PlatformError) as denied:
        approve_output(platform, **approval(platform, job, actor="operator", is_operator=True))
    assert denied.value.code == "declassification_denied"


def test_disabling_approver_revokes_future_transfers_but_rotation_preserves_identity(released):
    platform, binding, _, args, release = released
    platform.settings.caller_tokens["approver"] = "rotated-approver-token-000000000000"
    assert approve_output(platform, **args) == release
    assert read_output(platform, release["release_id"], "approver")["content"] == args["content"]
    del platform.settings.caller_tokens["approver"]
    with pytest.raises(PlatformError) as denied:
        platform.submit_workflow_role(binding, "inline_reviewer", "", "workflow-consumer", "revoked",
            source_release=release["release_id"], transfer_id="inline")
    assert denied.value.code == "declassification_denied"


def test_approver_disabled_during_release_read_is_rechecked_before_return(released, monkeypatch):
    platform, _, _, _, release = released
    read = platform.store.get

    def revoke_at_release_read(key, sha):
        body = read(key, sha)
        if key.startswith("results/" + release["release_id"] + "/"):
            del platform.settings.caller_tokens["approver"]
        return body

    monkeypatch.setattr(platform.store, "get", revoke_at_release_read)
    with pytest.raises(PlatformError) as denied:
        read_output(platform, release["release_id"], "approver")
    assert denied.value.code == "declassification_denied"


def test_failed_approval_after_artifact_write_does_not_create_release_authority(source, monkeypatch):
    platform, _, job = source
    put = platform.store.put

    def revoke_after_persistence(key, value):
        stored = put(key, value)
        del platform.settings.caller_tokens["approver"]
        return stored

    monkeypatch.setattr(platform.store, "put", revoke_after_persistence)
    with pytest.raises(PlatformError) as denied:
        approve_output(platform, **approval(platform, job))
    assert denied.value.code == "declassification_denied"
    with platform.db.sessions() as session:
        assert not session.scalars(select(MetadataRow).where(MetadataRow.key.like("release_%"))).all()


@pytest.fixture
def approved_eight_link_chain(mixed_installed):
    from pathlib import Path
    import yaml

    platform, previous = mixed_installed
    bundle = deepcopy(platform.active()[0])
    bundle["output_contracts"][0]["approvers"].append("ancestor-approver")
    platform.settings.caller_tokens["ancestor-approver"] = "ancestor-approver-token-00000000000"
    Path(platform.settings.bundle_path).write_text(yaml.safe_dump(bundle))
    platform.reload(platform.settings.bundle_path)
    document = deepcopy(previous["definition"])
    document["transfers"].extend([
        {"id": "review-to-holistic", "from_role": "inline_reviewer",
         "target": {"kind": "role", "role": "holistic_reviewer"},
         "output_contract": "public-review"},
        {"id": "holistic-to-review", "from_role": "holistic_reviewer",
         "target": {"kind": "role", "role": "inline_reviewer"},
         "output_contract": "public-review"},
    ])
    binding = platform.bind_workflow(document, previous["requirements_ref"],
        previous["session_policy_ref"], platform.active()[1], input_class="C3")
    source = finish_source(platform, binding, "chain-source-0")
    worker = Worker(platform)
    for depth in range(8):
        args = approval(platform, source, content=f"Approved review output {depth}.",
            actor="ancestor-approver" if depth == 0 else "approver")
        release = approve_output(platform, **args)
        if depth < 7:
            next_role = "inline_reviewer" if depth % 2 == 0 else "holistic_reviewer"
            transfer_id = "inline" if depth == 0 else (
                "holistic-to-review" if depth % 2 == 0 else "review-to-holistic")
            source = platform.submit_workflow_role(binding, next_role, "", "workflow-consumer",
                f"chain-source-{depth + 1}", source_release=release["release_id"],
                transfer_id=transfer_id)
            assert worker.claim().id == source.id
            worker.finish(source.id, artifact=platform.store.put(source.id, {"answer": f"Review output {depth + 1}"}))
            source = platform.get_job(source.id)
    return platform, release, args["content"]


@pytest.mark.parametrize("revocation", [None, "qualification", "ancestor-approver"])
def test_eight_link_revalidation_is_linear_current_and_call_scoped(approved_eight_link_chain, monkeypatch, revocation):
    from sovite import output_contracts, service

    platform, release, expected_content = approved_eight_link_chain
    read = platform.store.get
    qualified = service.is_qualified
    allowed = [True]
    reads = []
    qualification_checks = []

    def check_qualification(*args, **kwargs):
        qualification_checks.append(allowed[0])
        return allowed[0] and qualified(*args, **kwargs)

    def observe_read(key, sha):
        body = read(key, sha)
        reads.append(key)
        if key.startswith("results/" + release["release_id"] + "/"):
            if revocation == "qualification":
                allowed[0] = False
            elif revocation == "ancestor-approver":
                del platform.settings.caller_tokens["ancestor-approver"]
        return body

    monkeypatch.setattr(service, "is_qualified", check_qualification)
    monkeypatch.setattr(platform.store, "get", observe_read)
    if revocation is None:
        assert read_output(platform, release["release_id"], "approver")["content"] == expected_content
        # Every ancestor is checked before and after I/O, while its source and
        # approved artifact are read exactly once. There is no recursive doubling.
        assert len(qualification_checks) == 16
    else:
        with pytest.raises(PlatformError) as denied:
            read_output(platform, release["release_id"], "approver")
        assert denied.value.code == ("qualification_required" if revocation == "qualification"
                                     else "declassification_denied")
    assert len(reads) == 16 and len(set(reads)) == 16
    assert output_contracts._VERIFICATION.get() is None

    # Neither successful nor failed calls leave proofs for a later request.
    allowed[0] = True
    platform.settings.caller_tokens["ancestor-approver"] = "ancestor-approver-token-00000000000"
    reads.clear()
    qualification_checks.clear()

    def observe_again(key, sha):
        reads.append(key)
        return read(key, sha)

    monkeypatch.setattr(platform.store, "get", observe_again)
    assert read_output(platform, release["release_id"], "approver")["content"] == expected_content
    assert len(reads) == 16 and len(qualification_checks) == 16
    assert output_contracts._VERIFICATION.get() is None
