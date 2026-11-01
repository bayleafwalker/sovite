"""The dormant admission boundary cannot create an effect or obligation."""
import pytest

from sovite.program_ledger import ProgramLedgerUnavailable, prepare


def test_prepare_is_unavailable_without_a_configuration_override():
    with pytest.raises(ProgramLedgerUnavailable, match="admission is unavailable"):
        prepare()


def test_unknown_and_overrun_count_the_larger_amount_once():
    from sovite.program_ledger import _obligation_totals
    assert _obligation_totals([("a", "unknown", "10", "7", "12", "2"),
                               ("b", "reserved", "5", "4", None, None)]) == (17, 11)


@pytest.mark.parametrize("amount", ["", "01", "-1", "+1", " 1", "1.0", "1e3", "١", "1" * 129])
def test_canonical_amount_rejects_invalid_or_truncated_input(amount):
    from sovite.program_ledger import _parse_micro_units
    with pytest.raises(ValueError):
        _parse_micro_units(amount)


def test_duplicate_partial_and_aggregate_overflow_fail_closed():
    from sovite.program_ledger import _obligation_totals
    with pytest.raises(ValueError, match="identity"):
        _obligation_totals([("a", "reserved", "1", "1", None, None), ("a", "reserved", "1", "1", None, None)])
    with pytest.raises(ValueError, match="pair"):
        _obligation_totals([("a", "unknown", "1", "1", "2", None)])
    with pytest.raises(ValueError, match="aggregate"):
        _obligation_totals([("a", "reserved", "9" * 128, "1", None, None), ("b", "reserved", "1", "1", None, None)])


def test_settled_counts_verified_actual_below_reservation():
    from sovite.program_ledger import _obligation_totals
    assert _obligation_totals([("a", "settled", "10", "7", "3", "2")]) == (3, 2)
    with pytest.raises(ValueError, match="requires actuals"):
        _obligation_totals([("a", "settled", "10", "7", None, None)])


def test_i64_overflow_has_distinct_exact_result():
    from sovite.program_ledger import _AggregateOverflow, _obligation_totals
    maximum = str((1 << 63) - 1)
    assert _obligation_totals([("a", "reserved", maximum, "0", None, None)]) == ((1 << 63) - 1, 0)
    with pytest.raises(_AggregateOverflow) as raised:
        _obligation_totals([("a", "reserved", maximum, "0", None, None),
                            ("b", "reserved", "1", "0", None, None)])
    assert (raised.value.usd, raised.value.allowance) == (1 << 63, 0)


def test_generation_identity_requires_exact_lowercase_hex():
    from sovite.program_ledger import _is_lower_hex
    assert _is_lower_hex("a" * 32, 32)
    assert not _is_lower_hex("A" * 32, 32)
    assert not _is_lower_hex("a" * 31, 32)
    assert not _is_lower_hex("g" * 32, 32)
    assert not _is_lower_hex(None, 32)


def test_legacy_binding_rejects_job_purpose_scope_and_empty_identity():
    from sovite.program_ledger import _validate_locked_legacy_binding
    h = "a" * 64
    link = dict(scope_id="scope", batch_id="batch", link_id="link", job_id="job", original_attempt_id="attempt",
        original_call_id="source-call", original_slot_id="source-slot", subject_id="job", call_id="call",
        slot_id="slot", original_digest=h)
    batch = dict(scope_id="scope", batch_id="batch")
    registry = dict(scope_id="scope", purpose="work", subject_id="job", binding_digest=h)
    job = dict(id="job")
    reservation = dict(scope_id="scope", subject_id="job", purpose="work", call_id="call")
    slot = dict(scope_id="scope", subject_id="job", call_id="call", slot_id="slot")
    _validate_locked_legacy_binding(link, batch, registry, job, reservation, slot)
    cases = [({**link, "original_call_id": ""}, batch, registry, job, reservation, slot),
             (link, batch, {**registry, "purpose": "collection"}, job, reservation, slot),
             (link, batch, registry, {"id": "other"}, reservation, slot),
             (link, batch, registry, job, reservation, {**slot, "scope_id": "other"})]
    for case in cases:
        with pytest.raises(ValueError):
            _validate_locked_legacy_binding(*case)


def test_private_fixture_core_is_not_imported_by_product_modules():
    import ast
    from pathlib import Path
    import sovite
    root = Path(sovite.__file__).parent
    for path in root.glob("*.py"):
        if path.name == "program_ledger_core.py":
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(not alias.name.endswith("program_ledger_core") for alias in node.names), path
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").endswith("program_ledger_core"), path
                assert all(alias.name != "program_ledger_core" for alias in node.names), path
