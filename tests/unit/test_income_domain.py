from calendar import monthrange
from datetime import date
from decimal import Decimal
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from assetboard.domain.income import (
    ExpectedUnitsMode,
    IncomeKind,
    estimate_income,
    expected_units,
    pay_date,
)
from assetboard.domain.money import Money


@pytest.mark.parametrize(
    ("month", "day", "lag", "expected"),
    [
        (date(2026, 8, 1), 10, 0, date(2026, 8, 10)),
        (date(2026, 8, 1), 10, 1, date(2026, 9, 10)),
        (date(2026, 8, 1), 10, 2, date(2026, 10, 10)),
        (date(2026, 1, 1), 31, 1, date(2026, 2, 28)),
    ],
)
def test_pay_date_lag_and_short_month(month: date, day: int, lag: int, expected: date) -> None:
    assert pay_date(month, day, lag) == expected


def test_expected_units_modes() -> None:
    month = date(2026, 8, 1)
    assert expected_units(
        ExpectedUnitsMode.WORKING_DAYS, work_month=month, working_days=21
    ) == Decimal(21)
    assert expected_units(
        ExpectedUnitsMode.FIXED, work_month=month, fixed=Decimal("7.5")
    ) == Decimal("7.5")
    assert expected_units(
        ExpectedUnitsMode.AVERAGE, work_month=month, average=Decimal("18.25")
    ) == Decimal("18.25")
    assert expected_units(ExpectedUnitsMode.MANUAL, work_month=month) is None


def test_estimate_income_uses_half_up_minor_unit_rounding() -> None:
    estimate = estimate_income(
        kind=IncomeKind.VARIABLE,
        currency="EUR",
        fixed_minor=None,
        rate_minor=1,
        units=Decimal("2.5"),
        confirmed=True,
    )
    assert estimate.net == Money(3, "EUR")
    assert estimate.amount_state == "confirmed"


def test_manual_expected_units_are_zero_and_remain_estimated() -> None:
    estimate = estimate_income(
        kind=IncomeKind.VARIABLE,
        currency="EUR",
        fixed_minor=None,
        rate_minor=100,
        units=None,
        confirmed=False,
    )
    assert estimate.net == Money(0, "EUR")
    assert estimate.amount_state == "estimated"


def test_fixed_income_is_confirmed_without_work_units() -> None:
    estimate = estimate_income(
        kind=IncomeKind.FIXED,
        currency="EUR",
        fixed_minor=250_000,
        rate_minor=None,
        units=None,
        confirmed=False,
    )
    assert estimate.net == Money(250_000, "EUR")
    assert estimate.amount_state == "confirmed"


@given(
    year=st.integers(1900, 2190),
    month=st.integers(1, 12),
    pay_day=st.integers(1, 31),
    lag=st.integers(0, 24),
)
def test_pay_date_is_the_clamped_pay_day_of_the_lagged_month(
    year: int, month: int, pay_day: int, lag: int
) -> None:
    result = pay_date(date(year, month, 1), pay_day, lag)
    assert (result.year * 12 + result.month) - (year * 12 + month) == lag
    assert result.day == min(pay_day, monthrange(result.year, result.month)[1])


@given(
    rate_minor=st.integers(1, 10_000_000),
    units=st.decimals(min_value=0, max_value=1000, places=4, allow_nan=False),
)
def test_variable_income_is_rate_times_units_rounded_half_up_once(
    rate_minor: int, units: Decimal
) -> None:
    estimate = estimate_income(
        kind=IncomeKind.VARIABLE,
        currency="EUR",
        fixed_minor=None,
        rate_minor=rate_minor,
        units=units,
        confirmed=True,
    )
    exact = Fraction(rate_minor) * Fraction(units)
    # Independent oracle: half-up on the exact rational product.
    assert estimate.net.amount_minor == int(exact + Fraction(1, 2))
