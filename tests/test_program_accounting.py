"""Synthetic pure arithmetic tests; values are not provider pricing evidence."""
from fractions import Fraction

import pytest

from sovite import program_accounting as accounting


def inputs():
    bounds = {name: {"max_units": 0, "usd_per_unit": "0"} for name in accounting.CATEGORIES}
    bounds["input"] = {"max_units": 3, "usd_per_unit": "0.0000015"}
    bounds["output"] = {"max_units": 2, "usd_per_unit": "0.0000031"}
    return dict(currency="USD", allowance_unit="opencode-go-allowance", category_bounds=bounds,
                fees_usd={"per_call": "0.0000001", "surcharge": "0.0000002", "fallback": "0.0000003"},
                allowance_multiplier="2.5", allowance_fixed="0.0000004")


@pytest.mark.parametrize(("value", "expected"), [
    ("0", 0), ("0.000000000000000001", 1), ("0.000001", 1), ("0.000001000000000001", 2),
    ("1", 1000000), ("9223372036854.775807", accounting.MAX_INTEGER),
])
def test_micro_conversion_rounds_up_exactly(value, expected):
    assert accounting.decimal_to_micro(value) == expected


@pytest.mark.parametrize("value", [True, False, None, 0, 1.0, float("nan"), float("inf"),
    "NaN", "Infinity", "-0", "+0", "-1", "1e3", " 1", "1 ", "", ".1", "1.",
    "01", "١", "1.0000000000000000001", "1000000000000000000"])
def test_invalid_decimal_inputs_fail(value):
    with pytest.raises(accounting.AccountingValueError):
        accounting.decimal_to_micro(value)


def test_micro_overflow_is_denied():
    with pytest.raises(accounting.AccountingValueError, match="amount_overflow"):
        accounting.decimal_to_micro("9223372036854.775808")


def test_worst_case_uses_all_categories_fees_and_allowance():
    ceiling = accounting.worst_case_ceiling(**inputs())
    # Exact USD 0.0000113 => 12 micro; allowance 0.00002865 => 29 micro.
    assert ceiling == accounting.WorstCaseCeiling(12, 29, "opencode-go-allowance")
    values = inputs()
    values["fees_usd"]["fallback"] = "0.0000013"
    assert accounting.worst_case_ceiling(**values).usd_micro == 13


@pytest.mark.parametrize("mutation", ["category_missing", "category_extra", "field_missing", "field_extra",
    "fee_missing", "fee_extra", "currency", "allowance", "allowance_multiple", "bool_quantity",
    "float_quantity", "negative_quantity", "huge_quantity", "float_price", "invalid_multiplier", "zero_multiplier", "negative_fixed"])
def test_incomplete_or_unsupported_bound_fails(mutation):
    values = inputs()
    if mutation == "category_missing": values["category_bounds"].pop("input")
    elif mutation == "category_extra": values["category_bounds"]["other"] = {}
    elif mutation == "field_missing": values["category_bounds"]["input"].pop("usd_per_unit")
    elif mutation == "field_extra": values["category_bounds"]["input"]["extra"] = "0"
    elif mutation == "fee_missing": values["fees_usd"].pop("fallback")
    elif mutation == "fee_extra": values["fees_usd"]["other"] = "0"
    elif mutation == "currency": values["currency"] = "EUR"
    elif mutation == "allowance": values["allowance_unit"] = "unknown"
    elif mutation == "allowance_multiple": values["allowance_unit"] = ["opencode-go-allowance", "other"]
    elif mutation == "bool_quantity": values["category_bounds"]["input"]["max_units"] = True
    elif mutation == "float_quantity": values["category_bounds"]["input"]["max_units"] = 3.0
    elif mutation == "negative_quantity": values["category_bounds"]["input"]["max_units"] = -1
    elif mutation == "huge_quantity": values["category_bounds"]["input"]["max_units"] = accounting.MAX_QUANTITY + 1
    elif mutation == "float_price": values["category_bounds"]["input"]["usd_per_unit"] = 0.1
    elif mutation == "invalid_multiplier": values["allowance_multiplier"] = "NaN"
    elif mutation == "zero_multiplier": values["allowance_multiplier"] = "0"
    elif mutation == "negative_fixed": values["allowance_fixed"] = "-1"
    with pytest.raises(accounting.AccountingValueError):
        accounting.worst_case_ceiling(**values)


def test_bound_overflow_in_either_dimension_fails():
    values = inputs()
    values["category_bounds"]["input"] = {"max_units": accounting.MAX_QUANTITY, "usd_per_unit": "999999999999999999"}
    with pytest.raises(accounting.AccountingValueError, match="amount_overflow"):
        accounting.worst_case_ceiling(**values)
    values = inputs()
    values["allowance_multiplier"] = "999999999999999999"
    with pytest.raises(accounting.AccountingValueError, match="amount_overflow"):
        accounting.worst_case_ceiling(**values)


def test_rounding_negative_control_detects_floor_mutant(monkeypatch):
    monkeypatch.setattr(accounting, "_ceil_ratio", lambda n, d: n // d)
    with pytest.raises(AssertionError):
        assert accounting.decimal_to_micro("0.0000001") == 1


def test_fee_negative_control_detects_omission_mutant(monkeypatch):
    original = accounting.parse_nonnegative_decimal
    monkeypatch.setattr(accounting, "parse_nonnegative_decimal", lambda value: Fraction(0) if value == "0.0000003" else original(value))
    values = inputs()
    # This fixture straddles a micro-unit boundary only if fallback is counted.
    values["fees_usd"] = {"per_call": "0.0000001", "surcharge": "0.0000001", "fallback": "0.0000003"}
    with pytest.raises(AssertionError):
        assert accounting.worst_case_ceiling(**values).usd_micro == 12
