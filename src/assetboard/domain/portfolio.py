"""Portfolio valuation and asset-class look-through (F6.6). Pure: no I/O, no clock.

``value_portfolio`` values current holdings in the workspace base currency:

- An instrument's value is ``quantity × price × fx_to_base``, computed exactly in an 80-digit
  context and rounded half up to the base currency's minor unit **once**. ``fx_to_base`` is base
  units per one quote unit; the base currency itself is 1.
- The cost basis is the holdings' ``cost_basis_base`` (F6.2), already in the base currency.
- A missing price or a missing FX rate excludes the instrument's value **and** its cost basis
  from every total (portfolio, classes, shares, chart). The row stays, marked ``missing``, so
  the page can warn; it is never valued at zero. A stale price or rate is not missing.
- Look-through: each included instrument's value and cost are split over its asset classes by
  allocation weight (basis points). Every class except the largest-weight one gets
  ``floor(amount × weight / 10 000)``; the largest gets the remainder, so the parts add up to the
  instrument's amount exactly. Equal largest weights are broken by the class's sort order, then
  its id. Integer arithmetic only.
- Shares of the portfolio and gain percentages are tenths of a percent, rounded once from the
  exact integer ratio; the displayed shares need not add up to 100.0%.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
from enum import StrEnum
from uuid import UUID

from assetboard.domain.charts import CategoryAmount, Slice, category_slices, share_tenths
from assetboard.domain.holdings import RealizedEvent
from assetboard.domain.instruments import ALLOCATION_TOTAL_BP
from assetboard.domain.money import Money
from assetboard.domain.summary import ratio_tenths

_DECIMAL_PRECISION = 80
SMALL_SLICE_BP = 200  # chart slices below 2% of the chart's total are grouped into "Other"


class Missing(StrEnum):
    PRICE = "price"
    FX = "fx"


@dataclass(frozen=True, slots=True)
class PortfolioHolding:
    """One instrument held now (quantity > 0) and its cost basis in the base currency."""

    instrument_id: UUID
    quantity: Decimal
    cost_basis: Money
    quote_currency: str


@dataclass(frozen=True, slots=True)
class PortfolioPrice:
    """The unit price used for an instrument, in its quote currency; ``None``: no price."""

    instrument_id: UUID
    price: Decimal | None


@dataclass(frozen=True, slots=True)
class PortfolioAllocation:
    """One allocation weight, with the class's sort order for the remainder tie-break."""

    instrument_id: UUID
    asset_class_id: UUID
    weight_bp: int
    class_sort_order: int


@dataclass(frozen=True, slots=True)
class ClassPart:
    asset_class_id: UUID | None  # None: an instrument without any allocation
    value_minor: int
    cost_minor: int


@dataclass(frozen=True, slots=True)
class PortfolioRow:
    instrument_id: UUID
    quantity: Decimal
    cost_basis: Money
    avg_cost: Decimal  # base currency per unit, exact (cost basis / quantity)
    price: Decimal | None
    fx_to_base: Decimal | None
    missing: Missing | None
    value: Money | None  # None when excluded
    gain: Money | None
    gain_tenths: int | None  # None when excluded or when the cost basis is 0
    share_tenths: int | None  # share of the included portfolio value; None when excluded
    parts: tuple[ClassPart, ...]  # empty when excluded

    @property
    def included(self) -> bool:
        return self.missing is None


@dataclass(frozen=True, slots=True)
class ClassTotal:
    asset_class_id: UUID | None
    value: Money
    cost: Money
    gain: Money
    gain_tenths: int | None  # None when the class's cost is 0


@dataclass(frozen=True, slots=True)
class PortfolioValuation:
    as_of: date
    currency: str
    rows: tuple[PortfolioRow, ...]
    classes: tuple[ClassTotal, ...]
    value: Money
    cost: Money
    gain: Money
    gain_tenths: int | None
    excluded: int  # rows left out of the totals (missing price or FX)

    def class_value(self, asset_class_id: UUID | None) -> int:
        for total in self.classes:
            if total.asset_class_id == asset_class_id:
                return total.value.amount_minor
        return 0


def split_by_weights(
    amount_minor: int, allocations: Sequence[PortfolioAllocation]
) -> dict[UUID, int]:
    """Split a non-negative amount over the allocations (see the module docstring)."""
    if amount_minor < 0:
        raise ValueError("amount must not be negative")
    if not allocations:
        raise ValueError("at least one allocation is needed")
    largest = min(
        allocations,
        key=lambda item: (-item.weight_bp, item.class_sort_order, str(item.asset_class_id)),
    )
    parts: dict[UUID, int] = {}
    for allocation in allocations:
        if allocation is largest:
            continue
        parts[allocation.asset_class_id] = (
            parts.get(allocation.asset_class_id, 0)
            + amount_minor * allocation.weight_bp // ALLOCATION_TOTAL_BP
        )
    parts[largest.asset_class_id] = parts.get(largest.asset_class_id, 0) + (
        amount_minor - sum(parts.values())
    )
    return parts


def _value_minor(quantity: Decimal, price: Decimal, fx: Decimal, currency: str) -> int:
    with localcontext() as context:
        context.prec = max(context.prec, _DECIMAL_PRECISION)
        exact = quantity * price * fx
    return Money.from_decimal(exact, currency).amount_minor


def _avg_cost(cost: Money, quantity: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = max(context.prec, _DECIMAL_PRECISION)
        return cost.to_decimal() / quantity


def value_portfolio(
    holdings: Iterable[PortfolioHolding],
    prices: Iterable[PortfolioPrice],
    fx: Mapping[str, Decimal],
    allocations: Iterable[PortfolioAllocation],
    as_of: date,
    *,
    base_currency: str,
) -> PortfolioValuation:
    """Value ``holdings`` at ``as_of`` (see the module docstring).

    ``fx`` maps a quote currency to base units per one unit; a currency absent from it (other
    than the base) has no rate. Rows keep the order of ``holdings``; class totals are returned
    in first-seen order (callers order them for display).
    """
    price_by_id = {item.instrument_id: item.price for item in prices}
    allocations_by_id: dict[UUID, list[PortfolioAllocation]] = {}
    for allocation in allocations:
        allocations_by_id.setdefault(allocation.instrument_id, []).append(allocation)

    staged: list[tuple[PortfolioHolding, Decimal | None, Decimal | None, Missing | None, int]] = []
    for holding in holdings:
        if holding.quantity <= 0:
            raise ValueError("only held instruments (quantity > 0) can be valued")
        if holding.cost_basis.currency != base_currency:
            raise ValueError("the cost basis must be in the base currency")
        price = price_by_id.get(holding.instrument_id)
        rate = (
            Decimal(1)
            if holding.quote_currency == base_currency
            else fx.get(holding.quote_currency)
        )
        missing: Missing | None = None
        if price is None:
            missing = Missing.PRICE
        elif rate is None:
            missing = Missing.FX
        value_minor = (
            _value_minor(holding.quantity, price, rate, base_currency)
            if price is not None and rate is not None
            else 0
        )
        staged.append((holding, price, rate, missing, value_minor))

    total_value = sum(item[4] for item in staged if item[3] is None)
    total_cost = sum(item[0].cost_basis.amount_minor for item in staged if item[3] is None)
    class_values: dict[UUID | None, int] = {}
    class_costs: dict[UUID | None, int] = {}
    rows: list[PortfolioRow] = []
    for holding, price, rate, missing, value_minor in staged:
        cost_minor = holding.cost_basis.amount_minor
        avg_cost = _avg_cost(holding.cost_basis, holding.quantity)
        if missing is not None:
            rows.append(
                PortfolioRow(
                    instrument_id=holding.instrument_id,
                    quantity=holding.quantity,
                    cost_basis=holding.cost_basis,
                    avg_cost=avg_cost,
                    price=price,
                    fx_to_base=rate,
                    missing=missing,
                    value=None,
                    gain=None,
                    gain_tenths=None,
                    share_tenths=None,
                    parts=(),
                )
            )
            continue
        weights = allocations_by_id.get(holding.instrument_id, [])
        if weights:
            value_parts = split_by_weights(value_minor, weights)
            cost_parts = split_by_weights(cost_minor, weights)
            parts = tuple(
                ClassPart(class_id, value_parts[class_id], cost_parts[class_id])
                for class_id in value_parts
            )
        else:
            parts = (ClassPart(None, value_minor, cost_minor),)
        for part in parts:
            class_values[part.asset_class_id] = (
                class_values.get(part.asset_class_id, 0) + part.value_minor
            )
            class_costs[part.asset_class_id] = (
                class_costs.get(part.asset_class_id, 0) + part.cost_minor
            )
        gain_minor = value_minor - cost_minor
        rows.append(
            PortfolioRow(
                instrument_id=holding.instrument_id,
                quantity=holding.quantity,
                cost_basis=holding.cost_basis,
                avg_cost=avg_cost,
                price=price,
                fx_to_base=rate,
                missing=None,
                value=Money(value_minor, base_currency),
                gain=Money(gain_minor, base_currency),
                gain_tenths=ratio_tenths(gain_minor, cost_minor),
                share_tenths=share_tenths(value_minor, total_value),
                parts=parts,
            )
        )

    classes = tuple(
        ClassTotal(
            asset_class_id=class_id,
            value=Money(class_values[class_id], base_currency),
            cost=Money(class_costs[class_id], base_currency),
            gain=Money(class_values[class_id] - class_costs[class_id], base_currency),
            gain_tenths=ratio_tenths(
                class_values[class_id] - class_costs[class_id], class_costs[class_id]
            ),
        )
        for class_id in class_values
    )
    return PortfolioValuation(
        as_of=as_of,
        currency=base_currency,
        rows=tuple(rows),
        classes=classes,
        value=Money(total_value, base_currency),
        cost=Money(total_cost, base_currency),
        gain=Money(total_value - total_cost, base_currency),
        gain_tenths=ratio_tenths(total_value - total_cost, total_cost),
        excluded=sum(1 for row in rows if row.missing is not None),
    )


def portfolio_slices(
    valuation: PortfolioValuation, asset_class_id: UUID | None = None
) -> tuple[Slice, ...]:
    """The donut slices: instruments by value, largest first, slices under 2% in "Other".

    With ``asset_class_id`` each instrument contributes only its look-through part in that
    class (instruments without a part there have no slice); without it, its full value.
    A slice's ``category_id`` is the instrument id (``None`` for "Other").
    """
    amounts: list[CategoryAmount] = []
    for row in valuation.rows:
        if row.value is None:
            continue
        if asset_class_id is None:
            amount = row.value.amount_minor
        else:
            amount = sum(
                part.value_minor for part in row.parts if part.asset_class_id == asset_class_id
            )
        if amount > 0:
            amounts.append(CategoryAmount(row.instrument_id, Money(amount, valuation.currency)))
    return category_slices(
        amounts, currency=valuation.currency, max_slices=None, min_share_bp=SMALL_SLICE_BP
    )


@dataclass(frozen=True, slots=True)
class RealizedTotals:
    year_to_date: Money
    all_time: Money


def realized_totals(
    events: Iterable[RealizedEvent], today: date, *, base_currency: str
) -> RealizedTotals:
    """Realized P&L: all time, and dated from 1 January of ``today``'s year through ``today``.

    An undated sell (an imported legacy row) counts in the all-time total only.
    """
    year_start = date(today.year, 1, 1)
    all_time = 0
    year_to_date = 0
    for event in events:
        if event.realized.currency != base_currency:
            raise ValueError("realized P&L must be in the base currency")
        all_time += event.realized.amount_minor
        if event.date is not None and year_start <= event.date <= today:
            year_to_date += event.realized.amount_minor
    return RealizedTotals(
        year_to_date=Money(year_to_date, base_currency),
        all_time=Money(all_time, base_currency),
    )
