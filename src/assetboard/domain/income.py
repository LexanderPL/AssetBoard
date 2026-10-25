from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, localcontext
from enum import StrEnum

from assetboard.domain.money import Money
from assetboard.domain.transactions import MAX_AMOUNT_MINOR


class IncomeKind(StrEnum):
    FIXED = "fixed"
    VARIABLE = "variable"


class ExpectedUnitsMode(StrEnum):
    WORKING_DAYS = "working_days"
    FIXED = "fixed"
    AVERAGE = "average"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class IncomeEstimate:
    net: Money
    amount_state: str
    units: Decimal | None


def pay_date(work_month: date, pay_day: int, lag_months: int) -> date:
    if work_month.day != 1 or not 1 <= pay_day <= 31 or lag_months < 0:
        raise ValueError("invalid_pay_schedule")
    index = work_month.year * 12 + work_month.month - 1 + lag_months
    year, month_index = divmod(index, 12)
    month = month_index + 1
    return date(year, month, min(pay_day, monthrange(year, month)[1]))


def expected_units(
    mode: ExpectedUnitsMode,
    *,
    work_month: date,
    fixed: Decimal | None = None,
    average: Decimal | None = None,
    working_days: int = 0,
) -> Decimal | None:
    if mode == ExpectedUnitsMode.WORKING_DAYS:
        return Decimal(working_days)
    if mode == ExpectedUnitsMode.FIXED:
        return fixed
    if mode == ExpectedUnitsMode.AVERAGE:
        return average
    if mode == ExpectedUnitsMode.MANUAL:
        return None
    raise ValueError("invalid_expected_mode")


def estimate_income(
    *,
    kind: IncomeKind,
    currency: str,
    fixed_minor: int | None,
    rate_minor: int | None,
    units: Decimal | None,
    confirmed: bool,
) -> IncomeEstimate:
    if kind == IncomeKind.FIXED:
        if fixed_minor is None or fixed_minor <= 0:
            raise ValueError("invalid_fixed_amount")
        return IncomeEstimate(Money(fixed_minor, currency), "confirmed", None)
    if rate_minor is None or rate_minor <= 0:
        raise ValueError("invalid_rate")
    if units is None:
        return IncomeEstimate(Money(0, currency), "estimated", None)
    if units < 0 or not units.is_finite():
        raise ValueError("invalid_units")
    precision = max(28, len(units.as_tuple().digits) + 20)
    with localcontext() as context:
        context.prec = precision
        minor = int((Decimal(rate_minor) * units).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if minor < 0 or minor > MAX_AMOUNT_MINOR:
        raise ValueError("amount_too_large")
    return IncomeEstimate(
        Money(minor, currency),
        "confirmed" if confirmed else "estimated",
        units,
    )
