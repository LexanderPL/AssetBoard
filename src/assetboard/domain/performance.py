"""Performance series and XIRR (F6.7). Pure: no I/O, no database and no clock.

``portfolio_series`` values the dated transaction history at chosen points. Prices and FX use
the latest observation at or before each point (carry-forward). On each point an instrument with
no price or FX is left out of both value and net invested, matching ``value_portfolio``; the
point is marked incomplete. Undated legacy lots are not part of the series.

XIRR uses an 80-digit Decimal context, actual/365 day count, Newton iteration and a bracketing
bisection fallback. The rate is annualised; callers showing periods shorter than a year convert
it to a period return with ``period_return``.
"""

from __future__ import annotations

import calendar
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, localcontext
from enum import StrEnum
from uuid import UUID

from assetboard.domain.holdings import (
    HoldingTransaction,
    InvestmentTransactionType,
    dividend_amounts,
    quantize_quantity,
    split_factor,
    transaction_total,
)
from assetboard.domain.money import Money
from assetboard.domain.portfolio import PortfolioAllocation, split_by_weights

_DECIMAL_PRECISION = 80
_DAYS_PER_YEAR = Decimal(365)
_XIRR_TOLERANCE = Decimal("1e-12")
_XIRR_MAX_ITERATIONS = 200
_XIRR_FUNCTION_TOLERANCE = Decimal("1e-7")


class Step(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


class XirrFailure(StrEnum):
    NO_CASHFLOWS = "no_cashflows"
    NO_SIGN_CHANGE = "no_sign_change"
    NO_SOLUTION = "no_solution"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class PriceObservation:
    date: date
    price: Decimal


@dataclass(frozen=True, slots=True)
class RateObservation:
    date: date
    rate: Decimal


@dataclass(frozen=True, slots=True)
class SeriesPoint:
    date: date
    value: Money
    net_invested: Money
    incomplete: bool = False


@dataclass(frozen=True, slots=True)
class CashFlow:
    date: date
    amount: Money


@dataclass(frozen=True, slots=True)
class XirrResult:
    rate: Decimal | None
    reason: XirrFailure | None = None

    @property
    def defined(self) -> bool:
        return self.rate is not None


@dataclass(frozen=True, slots=True)
class Observations:
    dates: tuple[date, ...]
    values: tuple[Decimal, ...]

    def latest(self, on: date) -> Decimal | None:
        index = bisect_right(self.dates, on) - 1
        return self.values[index] if index >= 0 else None

    def latest_with_date(self, on: date) -> tuple[date, Decimal] | None:
        index = bisect_right(self.dates, on) - 1
        if index < 0:
            return None
        return self.dates[index], self.values[index]

    def earliest_with_date(self, on: date) -> tuple[date, Decimal] | None:
        """The first observation on or after ``on``; used to backfill a leading gap."""
        index = bisect_left(self.dates, on)
        if index >= len(self.dates):
            return None
        return self.dates[index], self.values[index]


def _observations(
    values: Iterable[PriceObservation | RateObservation],
) -> Observations:
    rows = sorted(
        (
            (row.date, row.price if isinstance(row, PriceObservation) else row.rate)
            for row in values
        ),
        key=lambda row: row[0],
    )
    return Observations(dates=tuple(row[0] for row in rows), values=tuple(row[1] for row in rows))


def step_dates(start: date, end: date, step: Step) -> list[date]:
    if start > end:
        return []
    dates = [start]
    index = 1
    while True:
        if step is Step.DAY:
            candidate = start + timedelta(days=index)
        elif step is Step.WEEK:
            candidate = start + timedelta(days=7 * index)
        else:
            candidate = add_months(start, index)
        if candidate >= end:
            if dates[-1] != end:
                dates.append(end)
            return dates
        dates.append(candidate)
        index += 1


def add_months(day: date, months: int) -> date:
    month_index = day.year * 12 + day.month - 1 + months
    year, month = divmod(month_index, 12)
    month += 1
    day_number = min(day.day, calendar.monthrange(year, month)[1])
    return date(year, month, day_number)


def years_before(day: date, years: int) -> date:
    """The same calendar day ``years`` earlier; 29 February falls on 28 February."""
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return date(day.year - years, 2, 28)


def _split_signed(amount_minor: int, allocations: Sequence[PortfolioAllocation]) -> dict[UUID, int]:
    if amount_minor == 0:
        return {}
    if amount_minor > 0:
        return split_by_weights(amount_minor, allocations)
    return {key: -value for key, value in split_by_weights(-amount_minor, allocations).items()}


def _class_amount(
    amount_minor: int,
    allocations: Sequence[PortfolioAllocation],
    asset_class_id: UUID | None,
) -> int:
    if asset_class_id is None:
        return amount_minor
    if not allocations:
        return 0
    return _split_signed(amount_minor, allocations).get(asset_class_id, 0)


def _value_minor(quantity: Decimal, price: Decimal, rate: Decimal, currency: str) -> int:
    with localcontext() as context:
        context.prec = max(context.prec, _DECIMAL_PRECISION)
        exact = quantity * price * rate
    return Money.from_decimal(exact, currency).amount_minor


def portfolio_series(
    transactions: Sequence[HoldingTransaction],
    prices: Mapping[UUID, Sequence[PriceObservation]],
    fx: Mapping[str, Sequence[RateObservation]],
    start: date,
    end: date,
    step: Step | str,
    *,
    base_currency: str,
    allocations: Mapping[UUID, Sequence[PortfolioAllocation]] | None = None,
    asset_class_id: UUID | None = None,
    backfill_leading: bool = False,
) -> tuple[SeriesPoint, ...]:
    """Value the dated transaction history at the step dates in ``[start, end]``.

    Transactions before ``start`` still establish the opening quantity and net invested.
    ``asset_class_id`` selects a look-through class; values and transaction totals are split
    with the same largest-remainder rule as the portfolio page. A point is incomplete when a
    held instrument has no price or FX; that instrument is omitted from both lines there.
    ``backfill_leading`` values a leading gap (no observation on or before the point) from the
    earliest later observation. Carry-forward stays the primary rule; genuinely missing data
    stays incomplete.
    """
    if start > end:
        raise ValueError("start must not be after end")
    if not base_currency:
        raise ValueError("base currency is required")
    wanted_step = Step(step)
    price_series = {instrument_id: _observations(rows) for instrument_id, rows in prices.items()}
    fx_series = {currency: _observations(rows) for currency, rows in fx.items()}
    allocation_map = {key: tuple(value) for key, value in (allocations or {}).items()}

    dated = sorted(
        (row for row in transactions if row.date is not None and row.date <= end),
        key=lambda row: (row.date, row.created_at, str(row.id)),
    )
    by_instrument: dict[UUID, list[HoldingTransaction]] = {}
    for row in dated:
        by_instrument.setdefault(row.instrument_id, []).append(row)
    splits_by_instrument: dict[UUID, list[HoldingTransaction]] = {}
    for row in dated:
        if InvestmentTransactionType(row.type) is InvestmentTransactionType.SPLIT:
            splits_by_instrument.setdefault(row.instrument_id, []).append(row)
    quantities: dict[UUID, Decimal] = {}
    net_invested: dict[UUID, int] = {}
    class_invested: dict[UUID, int] = {}
    pointer = 0

    points: list[SeriesPoint] = []
    for point in step_dates(start, end, wanted_step):
        while pointer < len(dated):
            row = dated[pointer]
            row_date = row.date
            assert row_date is not None
            if row_date > point:
                break
            pointer += 1
            kind = InvestmentTransactionType(row.type)
            if kind is InvestmentTransactionType.SPLIT:
                with localcontext() as context:
                    context.prec = max(context.prec, _DECIMAL_PRECISION)
                    ratio = row.quantity / row.unit_price
                    quantities[row.instrument_id] = quantize_quantity(
                        quantities.get(row.instrument_id, Decimal(0)) * ratio
                    )
                continue
            if kind not in (
                InvestmentTransactionType.BUY,
                InvestmentTransactionType.SELL,
                InvestmentTransactionType.DIVIDEND,
            ):
                continue
            if kind is InvestmentTransactionType.DIVIDEND:
                signed_total = -dividend_amounts(row, base_currency).net.amount_minor
            else:
                total = transaction_total(row, base_currency)
                if kind is InvestmentTransactionType.BUY:
                    quantities[row.instrument_id] = (
                        quantities.get(row.instrument_id, Decimal(0)) + row.quantity
                    )
                    signed_total = total.amount_minor
                else:
                    quantities[row.instrument_id] = (
                        quantities.get(row.instrument_id, Decimal(0)) - row.quantity
                    )
                    signed_total = -total.amount_minor
            net_invested[row.instrument_id] = net_invested.get(row.instrument_id, 0) + signed_total
            if asset_class_id is not None:
                class_invested[row.instrument_id] = class_invested.get(
                    row.instrument_id, 0
                ) + _class_amount(
                    signed_total,
                    allocation_map.get(row.instrument_id, ()),
                    asset_class_id,
                )

        value_total = 0
        invested_total = 0
        incomplete = False
        for instrument_id, rows in by_instrument.items():
            quantity = quantities.get(instrument_id, Decimal(0))
            invested = (
                class_invested.get(instrument_id, 0)
                if asset_class_id is not None
                else net_invested.get(instrument_id, 0)
            )
            parts = allocation_map.get(instrument_id, ())
            weight_bp = (
                10_000
                if asset_class_id is None
                else next(
                    (part.weight_bp for part in parts if part.asset_class_id == asset_class_id),
                    0,
                )
            )
            if quantity == 0 and invested == 0:
                continue
            if weight_bp == 0:
                continue
            price = price_series.get(instrument_id)
            currency = rows[0].quote_currency
            rate: Decimal | None
            if currency == base_currency:
                rate = Decimal(1)
            else:
                rate_series = fx_series.get(currency)
                rate_observation = (
                    rate_series.latest_with_date(point) if rate_series is not None else None
                )
                if rate_observation is None and backfill_leading and rate_series is not None:
                    rate_observation = rate_series.earliest_with_date(point)
                rate = rate_observation[1] if rate_observation is not None else None
            observation = price.latest_with_date(point) if price is not None else None
            if observation is None and backfill_leading and price is not None:
                observation = price.earliest_with_date(point)
            price_value: Decimal | None = None
            if observation is not None:
                splits = splits_by_instrument.get(instrument_id, ())
                if observation[0] > point:
                    # A backfilled observation is dated after the point. The split factor is
                    # old/new for (from, to]; walking backwards needs its inverse, so a
                    # post-split price is turned back into a pre-split price.
                    with localcontext() as context:
                        context.prec = max(context.prec, _DECIMAL_PRECISION)
                        price_value = observation[1] / split_factor(splits, point, observation[0])
                else:
                    price_value = observation[1] * split_factor(splits, observation[0], point)
            if quantity > 0 and (price_value is None or rate is None):
                incomplete = True
                continue
            if quantity != 0:
                assert price_value is not None and rate is not None
                value_total += _class_amount(
                    _value_minor(quantity, price_value, rate, base_currency),
                    parts,
                    asset_class_id,
                )
            invested_total += invested

        points.append(
            SeriesPoint(
                date=point,
                value=Money(value_total, base_currency),
                net_invested=Money(invested_total, base_currency),
                incomplete=incomplete,
            )
        )
    return tuple(points)


def portfolio_value_at(
    transactions: Sequence[HoldingTransaction],
    prices: Mapping[UUID, Sequence[PriceObservation]],
    fx: Mapping[str, Sequence[RateObservation]],
    on: date,
    *,
    base_currency: str,
    allocations: Mapping[UUID, Sequence[PortfolioAllocation]] | None = None,
    asset_class_id: UUID | None = None,
) -> SeriesPoint:
    """The series point at one date, using the same valuation rules as ``portfolio_series``."""
    return portfolio_series(
        transactions,
        prices,
        fx,
        on,
        on,
        Step.DAY,
        base_currency=base_currency,
        allocations=allocations,
        asset_class_id=asset_class_id,
    )[0]


def cashflows_for_period(
    transactions: Sequence[HoldingTransaction],
    start: date,
    end: date,
    *,
    start_value: Money,
    end_value: Money,
    base_currency: str,
    allocations: Mapping[UUID, Sequence[PortfolioAllocation]] | None = None,
    asset_class_id: UUID | None = None,
) -> tuple[CashFlow, ...]:
    """XIRR cashflows: opening value, external trades after the start, closing value.

    The opening value is measured after the start date's trades, so those trades are not
    counted twice. The opening/ending Money and every flow must use ``base_currency``.
    """
    if start > end:
        raise ValueError("start must not be after end")
    if start_value.currency != base_currency or end_value.currency != base_currency:
        raise ValueError("cashflows must use the base currency")
    allocation_map = {key: tuple(value) for key, value in (allocations or {}).items()}
    flows: list[CashFlow] = []
    if start_value.amount_minor:
        flows.append(CashFlow(start, Money(-start_value.amount_minor, base_currency)))
    for row in transactions:
        if row.date is None or not (start < row.date <= end):
            continue
        kind = InvestmentTransactionType(row.type)
        if kind is InvestmentTransactionType.SPLIT:
            continue
        if kind is InvestmentTransactionType.DIVIDEND:
            amount = _class_amount(
                dividend_amounts(row, base_currency).net.amount_minor,
                allocation_map.get(row.instrument_id, ()),
                asset_class_id,
            )
            if amount != 0:
                flows.append(CashFlow(row.date, Money(amount, base_currency)))
            continue
        if kind not in (InvestmentTransactionType.BUY, InvestmentTransactionType.SELL):
            continue
        total = transaction_total(row, base_currency)
        amount = _class_amount(
            total.amount_minor,
            allocation_map.get(row.instrument_id, ()),
            asset_class_id,
        )
        if amount == 0:
            continue
        flows.append(
            CashFlow(
                row.date,
                Money(-amount if kind is InvestmentTransactionType.BUY else amount, base_currency),
            )
        )
    if end_value.amount_minor:
        flows.append(CashFlow(end, end_value))
    return tuple(flows)


def xirr(cashflows: Sequence[CashFlow], *, day_count: int = 365) -> XirrResult:
    """Annualised internal rate of return, or a clear reason when it is undefined."""
    if day_count <= 0:
        raise ValueError("day_count must be positive")
    rows = sorted(cashflows, key=lambda flow: flow.date)
    if not rows:
        return XirrResult(None, XirrFailure.NO_CASHFLOWS)
    currency = rows[0].amount.currency
    if any(row.amount.currency != currency for row in rows):
        raise ValueError("cashflows must use one currency")
    if not any(row.amount.amount_minor < 0 for row in rows) or not any(
        row.amount.amount_minor > 0 for row in rows
    ):
        return XirrResult(None, XirrFailure.NO_SIGN_CHANGE)
    first = rows[0].date
    with localcontext() as context:
        context.prec = max(context.prec, _DECIMAL_PRECISION)

        def npv(rate: Decimal) -> Decimal:
            total = Decimal(0)
            for flow in rows:
                years = Decimal((flow.date - first).days) / Decimal(day_count)
                total += Decimal(flow.amount.amount_minor) * (Decimal(1) + rate) ** (-years)
            return total

        def derivative(rate: Decimal) -> Decimal:
            total = Decimal(0)
            for flow in rows:
                years = Decimal((flow.date - first).days) / Decimal(day_count)
                total -= (
                    years * Decimal(flow.amount.amount_minor) * (Decimal(1) + rate) ** (-years - 1)
                )
            return total

        rate = Decimal("0.1")
        for _ in range(_XIRR_MAX_ITERATIONS):
            value = npv(rate)
            if abs(value) < _XIRR_FUNCTION_TOLERANCE:
                return _xirr_result(rate)
            slope = derivative(rate)
            if slope == 0:
                break
            candidate = rate - value / slope
            if candidate <= -1 or not candidate.is_finite():
                break
            if abs(candidate - rate) < _XIRR_TOLERANCE:
                return _xirr_result(candidate)
            rate = candidate

        low = Decimal("-0.999999")
        high = Decimal("0.1")
        low_value = npv(low)
        high_value = npv(high)
        while (low_value > 0) == (high_value > 0) and high < Decimal("1000000"):
            high *= 2
            high_value = npv(high)
        if (low_value > 0) == (high_value > 0):
            return XirrResult(None, XirrFailure.NO_SOLUTION)
        for _ in range(_XIRR_MAX_ITERATIONS):
            middle = (low + high) / 2
            middle_value = npv(middle)
            if abs(middle_value) < _XIRR_FUNCTION_TOLERANCE or high - low < _XIRR_TOLERANCE:
                return _xirr_result(middle)
            if (low_value > 0) == (middle_value > 0):
                low, low_value = middle, middle_value
            else:
                high, high_value = middle, middle_value
        return XirrResult(None, XirrFailure.NO_SOLUTION)


def _xirr_result(rate: Decimal) -> XirrResult:
    if not rate.is_finite() or rate <= -1:
        return XirrResult(None, XirrFailure.NO_SOLUTION)
    return XirrResult(rate)


def period_return(annualized_rate: Decimal, days: int) -> Decimal:
    """Convert an annualised rate to the return over ``days`` (actual/365)."""
    if days < 0:
        raise ValueError("days must not be negative")
    with localcontext() as context:
        context.prec = max(context.prec, _DECIMAL_PRECISION)
        return (Decimal(1) + annualized_rate) ** (Decimal(days) / _DAYS_PER_YEAR) - 1
