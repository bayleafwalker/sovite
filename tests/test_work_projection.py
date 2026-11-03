"""Pure Work projection v1 checks; no owner or runtime authority is involved."""
import hashlib
import json

import pytest

from sovite.work_projection import (
    WorkProjectionError,
    canonical_bytes,
    parse_raw_json,
    projection_digest,
    validate_payload,
    verify_digest,
)


def payload(**changes):
    value = {
        "schema_version": "sovite-work-projection/v1",
        "scope_id": "scope-1",
        "work_job_id": "job-1",
        "subject_id": "job-1",
        "attempt_id": "attempt-1",
        "call_id": "call-1",
        "slot_id": "slot-1",
        "effect_key": "effect-1",
        "receipt_id": None,
        "evidence_id": None,
        "purpose": "work",
        "lease_digest": "a" * 64,
        "request_digest": "b" * 64,
        "policy_digest": "c" * 64,
        "receipt_digest": None,
        "allowance_unit": "units",
        "policy_revision": "7",
        "period_start_ms": "1000",
        "period_end_ms": "2000",
        "projection_revision": "1",
        "reserved_usd_micro": "10",
        "reserved_allowance_micro": "20",
        "audit_sequence": "1",
        "actual_usd_micro": None,
        "actual_allowance_micro": None,
        "intent_state": "committed",
        "reservation_state": "reserved",
        "slot_state": "held",
    }
    value.update(changes)
    return value


@pytest.mark.parametrize("value", [
    payload(),
    payload(
        receipt_id="receipt-1", receipt_digest="d" * 64,
        actual_usd_micro="2", actual_allowance_micro="3",
        intent_state="known", reservation_state="settled", slot_state="released",
    ),
    payload(
        intent_state="unknown", reservation_state="unknown", slot_state="unknown",
    ),
    payload(
        receipt_id="receipt-1", receipt_digest="d" * 64,
        intent_state="unknown", reservation_state="unknown", slot_state="unknown",
    ),
    payload(
        receipt_id="receipt-1", receipt_digest="d" * 64,
        actual_usd_micro="11", actual_allowance_micro="21",
        intent_state="unknown", reservation_state="unknown", slot_state="unknown",
    ),
])
def test_valid_closed_state_vectors_round_trip(value):
    projection = validate_payload(value)
    assert projection.to_payload() == value
    assert parse_raw_json(canonical_bytes(projection)) == projection


def test_raw_json_rejects_duplicate_object_keys():
    raw = b'{"scope_id":"scope-1","scope_id":"scope-2"}'
    with pytest.raises(WorkProjectionError, match="duplicate"):
        parse_raw_json(raw)


@pytest.mark.parametrize("field, value", [("policy_revision", 7), ("audit_sequence", True)])
def test_raw_json_rejects_json_numbers_and_booleans_for_decimal_strings(field, value):
    raw_value = payload(**{field: value})
    with pytest.raises(WorkProjectionError):
        parse_raw_json(json.dumps(raw_value))


@pytest.mark.parametrize("change", [
    {"unknown_field": "x"},
    {"call_id": None},
    {"schema_version": "sovite-work-projection/v2"},
    {"purpose": "collection"},
    {"subject_id": "different-job"},
    {"scope_id": "not an identifier"},
])
def test_wrong_fields_or_identity_are_rejected(change):
    with pytest.raises(WorkProjectionError):
        validate_payload(payload(**change))


def test_missing_required_field_is_rejected():
    value = payload()
    del value["attempt_id"]
    with pytest.raises(WorkProjectionError, match="missing"):
        validate_payload(value)


def test_referenced_audit_integer_uses_signed_i32_bound():
    assert validate_payload(payload(audit_sequence="2147483647")).audit_sequence == "2147483647"
    with pytest.raises(WorkProjectionError, match="audit_sequence"):
        validate_payload(payload(audit_sequence="2147483648"))


@pytest.mark.parametrize("change", [
    {"policy_revision": "01"},
    {"period_start_ms": "-1"},
    {"period_end_ms": "1.0"},
    {"projection_revision": "0"},
    {"reserved_usd_micro": str(1 << 63)},
    {"actual_usd_micro": "1"},
    {"period_start_ms": "2000", "period_end_ms": "1000"},
])
def test_numeric_grammar_ranges_and_pairs_are_strict(change):
    with pytest.raises(WorkProjectionError):
        validate_payload(payload(**change))


@pytest.mark.parametrize("change", [
    {"intent_state": "committed", "reservation_state": "settled", "slot_state": "held"},
    {"intent_state": "known", "reservation_state": "settled", "slot_state": "released"},
    {"receipt_id": "receipt-1"},
    {"evidence_id": "evidence-1"},
    {"intent_state": "unknown", "reservation_state": "unknown", "slot_state": "unknown",
     "actual_usd_micro": "1", "actual_allowance_micro": "1"},
    {"receipt_id": "receipt-1", "receipt_digest": "d" * 64,
     "actual_usd_micro": "11", "actual_allowance_micro": "1",
     "intent_state": "known", "reservation_state": "settled", "slot_state": "released"},
])
def test_incoherent_cross_field_or_state_branches_fail_closed(change):
    with pytest.raises(WorkProjectionError):
        validate_payload(payload(**change))


def test_changed_valid_payload_fails_original_digest_verification():
    original = payload()
    expected = projection_digest(original)
    tampered = {**original, "call_id": "call-2"}
    with pytest.raises(WorkProjectionError, match="digest mismatch"):
        verify_digest(tampered, expected)


def test_canonical_digest_matches_independent_literal_byte_vector():
    value = payload(
        receipt_id="receipt-1", receipt_digest="d" * 64,
        actual_usd_micro="2", actual_allowance_micro="3",
        projection_revision="2", audit_sequence="2147483647",
        intent_state="known", reservation_state="settled", slot_state="released",
    )
    # Independently specified canonical UTF-8 bytes, not produced by the module.
    expected_bytes = (
        b'{"actual_allowance_micro":"3","actual_usd_micro":"2","allowance_unit":"units",'
        b'"attempt_id":"attempt-1","audit_sequence":"2147483647","call_id":"call-1",'
        b'"effect_key":"effect-1","evidence_id":null,"intent_state":"known",'
        b'"lease_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        b'"period_end_ms":"2000","period_start_ms":"1000",'
        b'"policy_digest":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",'
        b'"policy_revision":"7","projection_revision":"2","purpose":"work",'
        b'"receipt_digest":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",'
        b'"receipt_id":"receipt-1","request_digest":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
        b'"reservation_state":"settled","reserved_allowance_micro":"20","reserved_usd_micro":"10",'
        b'"schema_version":"sovite-work-projection/v1","scope_id":"scope-1","slot_id":"slot-1",'
        b'"slot_state":"released","subject_id":"job-1","work_job_id":"job-1"}'
    )
    expected_digest = hashlib.sha256(b"SOVITE:WORK-PROJECTION:v1\x00" + expected_bytes).hexdigest()
    assert canonical_bytes(value) == expected_bytes
    assert projection_digest(value) == expected_digest == "0e4bed4bccb5d2d587b8e0a3acc6e37aa771e76626f985f8904f30146d112238"
    assert verify_digest(value, expected_digest).to_payload() == value


@pytest.mark.parametrize("state", [("known", "settled", "released"), ("unknown", "unknown", "unknown")])
def test_overflow_evidence_is_unrepresentable_even_with_actuals(state):
    with pytest.raises(WorkProjectionError, match="not representable"):
        validate_payload(payload(intent_state=state[0], reservation_state=state[1], slot_state=state[2],
                                 receipt_id="receipt-1", receipt_digest="d" * 64, evidence_id="evidence-1",
                                 actual_usd_micro="1", actual_allowance_micro="1"))
