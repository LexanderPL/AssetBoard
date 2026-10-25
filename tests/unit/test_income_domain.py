from datetime import date
from decimal import Decimal

import pytest

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
