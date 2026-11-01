"""Pure arithmetic for proposed program accounting; not authorization or evidence.

All quantities and prices supplied to this module are untrusted calculation
inputs. The caller is responsible for independently establishing their source,
completeness, enforceability, freshness and authority before any use.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import re


MAX_INTEGER = (1 << 63) - 1
MAX_QUANTITY = (1 << 31) - 1
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,18})?\Z", re.ASCII)
CATEGORIES = frozenset({"input", "output", "cache_read", "cache_write_5m", "cache_write_1h", "reasoning", "tool_calls"})
FEE_CATEGORIES = frozenset({"per_call", "surcharge", "fallback"})
SUPPORTED_ALLOWANCE_UNIT = "opencode-go-allowance"


class AccountingValueError(ValueError):
    """An input cannot be used to calculate a bounded accounting value."""


def parse_nonnegative_decimal(value: str) -> Fraction:
    """Parse bounded, nonnegative ASCII decimal text exactly, never a float."""
    if type(value) is not str or not _DECIMAL.fullmatch(value):
        raise AccountingValueError("invalid_decimal")
    integer, separator, fraction = value.partition(".")
    scale = 10 ** len(fraction) if separator else 1
    return Fraction(int(integer) * scale + (int(fraction) if separator else 0), scale)


def _ceil_ratio(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator


def _micro(value: Fraction) -> int:
    if type(value) is not Fraction or value < 0:
        raise AccountingValueError("invalid_amount")
    result = _ceil_ratio(value.numerator * 1_000_000, value.denominator)
    if result > MAX_INTEGER:
        raise AccountingValueError("amount_overflow")
    return result


def decimal_to_micro(value: str) -> int:
    """Convert a nonnegative amount to micro-units, rounding upward."""
    return _micro(parse_nonnegative_decimal(value))


@dataclass(frozen=True)
class WorstCaseCeiling:
    usd_micro: int
    allowance_micro: int
    allowance_unit: str


def worst_case_ceiling(*, currency: str, allowance_unit: str, category_bounds: dict,
                       fees_usd: dict, allowance_multiplier: str,
                       allowance_fixed: str) -> WorstCaseCeiling:
    """Compute a conservative ceiling from explicit per-unit bounds.

    The complete fixed set of categories and fees must be supplied, including
    explicit zero values where appropriate. This pure calculation does not
    establish that the bounds, prices or provider terms are valid or enforced.
    USD prices are per one unit of the associated category, not per million.
    """
    if type(currency) is not str or currency != "USD":
        raise AccountingValueError("unsupported_currency")
    if type(allowance_unit) is not str or allowance_unit != SUPPORTED_ALLOWANCE_UNIT:
        raise AccountingValueError("unsupported_allowance_unit")
    if type(category_bounds) is not dict or set(category_bounds) != CATEGORIES:
        raise AccountingValueError("invalid_categories")
    if type(fees_usd) is not dict or set(fees_usd) != FEE_CATEGORIES:
        raise AccountingValueError("invalid_fees")

    usd = Fraction(0)
    for name in sorted(CATEGORIES):
        item = category_bounds[name]
        if type(item) is not dict or set(item) != {"max_units", "usd_per_unit"}:
            raise AccountingValueError("invalid_category")
        units = item["max_units"]
        if type(units) is not int or not 0 <= units <= MAX_QUANTITY:
            raise AccountingValueError("invalid_quantity")
        usd += units * parse_nonnegative_decimal(item["usd_per_unit"])
    for name in sorted(FEE_CATEGORIES):
        usd += parse_nonnegative_decimal(fees_usd[name])

    multiplier = parse_nonnegative_decimal(allowance_multiplier)
    if multiplier <= 0:
        raise AccountingValueError("invalid_allowance_multiplier")
    fixed = parse_nonnegative_decimal(allowance_fixed)
    allowance = usd * multiplier + fixed
    return WorstCaseCeiling(_micro(usd), _micro(allowance), allowance_unit)
