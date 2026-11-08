"""Portfolio valuation and look-through (F6.6): golden cases, rounding, exclusions, properties.

Oracle: exact rational arithmetic (``fractions.Fraction``) with half-up rounding to the minor
unit, independent of the Decimal implementation.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from fractions import Fraction
from uuid import UUID, uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from assetboard.domain.holdings import RealizedEvent
from assetboard.domain.money import Money
from assetboard.domain.portfolio import (
    Missing,
    PortfolioAllocation,
    PortfolioHolding,
    PortfolioPrice,
    portfolio_slices,
    realized_totals,
    split_by_weights,
    value_portfolio,
)

BASE = "PLN"
AS_OF = date(2026, 3, 10)
STOCKS, BONDS, GOLD = (
    UUID("00000000-0000-0000-0000-00000000000a"),
    UUID("00000000-0000-0000-0000-00000000000b"),
    UUID("00000000-0000-0000-0000-00000000000c"),
)
ORDER = {STOCKS: 0, BONDS: 1, GOLD: 2}


def _holding(
    quantity: str, cost_minor: int, currency: str = BASE, instrument_id: UUID | None = None
) -> PortfolioHolding:
    return PortfolioHolding(
        instrument_id=instrument_id or uuid4(),
        quantity=Decimal(quantity),
        cost_basis=Money(cost_minor, BASE),
        quote_currency=currency,
    )


def _weights(instrument_id: UUID, *pairs: tuple[UUID, int]) -> list[PortfolioAllocation]:
    return [
        PortfolioAllocation(instrument_id, class_id, weight, ORDER.get(class_id, 9))
        for class_id, weight in pairs
    ]


def _round_half_up(value: Fraction) -> int:
    floor = value.numerator // value.denominator
    return floor + (1 if value - floor >= Fraction(1, 2) else 0)


def test_golden_80_20_fund_splits_1000_into_800_and_200() -> None:
    fund = _holding("10", 90_000)
    valuation = value_portfolio(
        [fund],
        [PortfolioPrice(fund.instrument_id, Decimal("100.00"))],
        {},
        _weights(fund.instrument_id, (STOCKS, 8000), (BONDS, 2000)),
        AS_OF,
        base_currency=BASE,
    )
    assert valuation.value == Money(100_000, BASE)
    totals = {total.asset_class_id: total for total in valuation.classes}
    assert totals[STOCKS].value == Money(80_000, BASE)
    assert totals[BONDS].value == Money(20_000, BASE)
    assert (totals[STOCKS].cost, totals[BONDS].cost) == (Money(72_000, BASE), Money(18_000, BASE))
    assert (totals[STOCKS].gain_tenths, totals[BONDS].gain_tenths) == (111, 111)
    [row] = valuation.rows
    assert (row.value, row.gain, row.gain_tenths, row.share_tenths) == (
        Money(100_000, BASE),
        Money(10_000, BASE),
        111,
        1000,
    )
    assert row.avg_cost == Decimal(90)


def test_remainder_goes_to_the_largest_weight() -> None:
    allocations = _weights(uuid4(), (STOCKS, 3333), (BONDS, 3333), (GOLD, 3334))
    assert split_by_weights(100, allocations) == {STOCKS: 33, BONDS: 33, GOLD: 34}
    assert split_by_weights(1, allocations) == {STOCKS: 0, BONDS: 0, GOLD: 1}
    assert split_by_weights(0, allocations) == {STOCKS: 0, BONDS: 0, GOLD: 0}


def test_equal_largest_weights_break_on_sort_order_then_id() -> None:
    instrument = uuid4()
    tied = [
        PortfolioAllocation(instrument, BONDS, 5000, 1),
        PortfolioAllocation(instrument, STOCKS, 5000, 0),
    ]
    assert split_by_weights(101, tied) == {BONDS: 50, STOCKS: 51}
    same_order = [
        PortfolioAllocation(instrument, BONDS, 5000, 0),
        PortfolioAllocation(instrument, STOCKS, 5000, 0),
    ]
    assert split_by_weights(101, same_order) == {BONDS: 50, STOCKS: 51}  # STOCKS id is lower
    with pytest.raises(ValueError):
        split_by_weights(-1, tied)
    with pytest.raises(ValueError):
        split_by_weights(1, [])


def test_value_is_quantity_times_price_times_fx_rounded_once() -> None:
    usd = _holding("3", 10_000, currency="USD")
    valuation = value_portfolio(
        [usd],
        [PortfolioPrice(usd.instrument_id, Decimal("10.005"))],
        {"USD": Decimal("4.0001")},
        _weights(usd.instrument_id, (STOCKS, 10_000)),
        AS_OF,
        base_currency=BASE,
    )
    exact = Fraction("3") * Fraction("10.005") * Fraction("4.0001") * 100
    assert valuation.value == Money(_round_half_up(exact), BASE)  # 120.063 → 120.06 → 12006


@pytest.mark.parametrize("missing", [Missing.PRICE, Missing.FX])
def test_missing_price_or_fx_excludes_value_and_cost_from_every_total(missing: Missing) -> None:
    kept = _holding("1", 5_000)
    gone = _holding("2", 7_000, currency="USD")
    prices = [PortfolioPrice(kept.instrument_id, Decimal("60"))]
    fx: dict[str, Decimal] = {}
    if missing is Missing.FX:
        prices.append(PortfolioPrice(gone.instrument_id, Decimal("10")))
    else:
        fx["USD"] = Decimal("4")
    valuation = value_portfolio(
        [kept, gone],
        prices,
        fx,
        [
            *_weights(kept.instrument_id, (STOCKS, 10_000)),
            *_weights(gone.instrument_id, (STOCKS, 10_000)),
        ],
        AS_OF,
        base_currency=BASE,
    )
    assert (valuation.value, valuation.cost, valuation.gain) == (
        Money(6_000, BASE),
        Money(5_000, BASE),
        Money(1_000, BASE),
    )
    assert valuation.excluded == 1
    assert [total.value for total in valuation.classes] == [Money(6_000, BASE)]
    assert [total.cost for total in valuation.classes] == [Money(5_000, BASE)]
    row = valuation.rows[1]
    assert (row.missing, row.value, row.gain, row.share_tenths, row.parts) == (
        missing,
        None,
        None,
        None,
        (),
    )
    assert row.cost_basis == Money(7_000, BASE)  # still shown on the row
    assert row.avg_cost == Decimal(35)
    assert valuation.rows[0].share_tenths == 1000
    assert [s.category_id for s in portfolio_slices(valuation)] == [kept.instrument_id]


def test_zero_cost_gives_no_gain_percentage() -> None:
    gift = _holding("1", 0)
    valuation = value_portfolio(
        [gift],
        [PortfolioPrice(gift.instrument_id, Decimal("5"))],
        {},
        _weights(gift.instrument_id, (GOLD, 10_000)),
        AS_OF,
        base_currency=BASE,
    )
    assert valuation.rows[0].gain_tenths is None
    assert valuation.classes[0].gain_tenths is None
    assert valuation.gain_tenths is None
    assert valuation.gain == Money(500, BASE)


def test_negative_gain_and_an_instrument_without_allocations() -> None:
    loser = _holding("1", 10_000)
    valuation = value_portfolio(
        [loser],
        [PortfolioPrice(loser.instrument_id, Decimal("75"))],
        {},
        [],
        AS_OF,
        base_currency=BASE,
    )
    assert (valuation.rows[0].gain, valuation.rows[0].gain_tenths) == (Money(-2_500, BASE), -250)
    assert [(total.asset_class_id, total.value) for total in valuation.classes] == [
        (None, Money(7_500, BASE))
    ]


def test_inputs_are_validated() -> None:
    with pytest.raises(ValueError):
        value_portfolio([_holding("0", 0)], [], {}, [], AS_OF, base_currency=BASE)
    wrong = PortfolioHolding(uuid4(), Decimal(1), Money(1, "EUR"), "EUR")
    with pytest.raises(ValueError):
        value_portfolio([wrong], [], {}, [], AS_OF, base_currency=BASE)


def _three_funds() -> tuple[
    list[PortfolioHolding], list[PortfolioPrice], list[PortfolioAllocation]
]:
    world = _holding("1", 0)
    bonds = _holding("1", 0)
    tiny = _holding("1", 0)
    holdings = [world, bonds, tiny]
    prices = [
        PortfolioPrice(world.instrument_id, Decimal("600")),
        PortfolioPrice(bonds.instrument_id, Decimal("390")),
        PortfolioPrice(tiny.instrument_id, Decimal("10")),
    ]
    allocations = [
        *_weights(world.instrument_id, (STOCKS, 10_000)),
        *_weights(bonds.instrument_id, (STOCKS, 2500), (BONDS, 7500)),
        *_weights(tiny.instrument_id, (STOCKS, 10_000)),
    ]
    return holdings, prices, allocations


def test_whole_portfolio_slices_use_full_values_and_group_small_ones() -> None:
    holdings, prices, allocations = _three_funds()
    valuation = value_portfolio(holdings, prices, {}, allocations, AS_OF, base_currency=BASE)
    slices = portfolio_slices(valuation)
    assert [(s.category_id, s.amount.amount_minor, s.share_tenths) for s in slices] == [
        (holdings[0].instrument_id, 60_000, 600),
        (holdings[1].instrument_id, 39_000, 390),
        (None, 1_000, 10),  # 1% < 2%: grouped into "Other"
    ]
    assert slices[-1].grouped == 1


def test_class_scope_slices_use_each_instrument_look_through_part() -> None:
    holdings, prices, allocations = _three_funds()
    valuation = value_portfolio(holdings, prices, {}, allocations, AS_OF, base_currency=BASE)
    stocks = portfolio_slices(valuation, STOCKS)
    assert [(s.category_id, s.amount.amount_minor) for s in stocks] == [
        (holdings[0].instrument_id, 60_000),
        (holdings[1].instrument_id, 9_750),  # its 25% Stocks part of 390.00
        (None, 1_000),  # the third fund: 10.00 / 707.50 = 1.4% < 2%, grouped
    ]
    bonds = portfolio_slices(valuation, BONDS)
    assert [(s.category_id, s.amount.amount_minor, s.share_tenths) for s in bonds] == [
        (holdings[1].instrument_id, 29_250, 1000)
    ]
    assert portfolio_slices(valuation, GOLD) == ()


def test_realized_totals_split_by_year_and_keep_undated_sells_all_time() -> None:
    instrument = uuid4()
    events = [
        RealizedEvent(uuid4(), instrument, date(2025, 12, 31), Money(1_000, BASE)),
        RealizedEvent(uuid4(), instrument, date(2026, 1, 1), Money(-300, BASE)),
        RealizedEvent(uuid4(), instrument, date(2026, 3, 10), Money(50, BASE)),
        RealizedEvent(uuid4(), instrument, None, Money(7, BASE)),
    ]
    totals = realized_totals(events, date(2026, 3, 10), base_currency=BASE)
    assert (totals.year_to_date, totals.all_time) == (Money(-250, BASE), Money(757, BASE))
    with pytest.raises(ValueError):
        realized_totals(
            [RealizedEvent(uuid4(), instrument, None, Money(1, "EUR"))],
            AS_OF,
            base_currency=BASE,
        )


_CLASSES = (STOCKS, BONDS, GOLD)


@st.composite
def _portfolio(draw: st.DrawFn):
    count = draw(st.integers(min_value=1, max_value=6))
    holdings: list[PortfolioHolding] = []
    prices: list[PortfolioPrice] = []
    allocations: list[PortfolioAllocation] = []
    fx = {"USD": Decimal("3.987654")}
    for _ in range(count):
        currency = draw(st.sampled_from([BASE, "USD", "EUR"]))  # EUR has no rate: missing FX
        holding = PortfolioHolding(
            uuid4(),
            draw(st.decimals(min_value=Decimal("0.0001"), max_value=Decimal("5000"), places=4)),
            Money(draw(st.integers(min_value=0, max_value=10**9)), BASE),
            currency,
        )
        holdings.append(holding)
        if draw(st.booleans()) or currency != "EUR":
            prices.append(
                PortfolioPrice(
                    holding.instrument_id,
                    draw(st.decimals(min_value=Decimal(0), max_value=Decimal("10000"), places=6))
                    if draw(st.integers(0, 9))
                    else None,
                )
            )
        classes = draw(st.lists(st.sampled_from(_CLASSES), min_size=1, max_size=3, unique=True))
        cuts = sorted(
            draw(
                st.lists(
                    st.integers(min_value=0, max_value=10_000),
                    min_size=len(classes) - 1,
                    max_size=len(classes) - 1,
                )
            )
        )
        bounds = [0, *cuts, 10_000]
        for index, class_id in enumerate(classes):
            allocations.append(
                PortfolioAllocation(
                    holding.instrument_id,
                    class_id,
                    bounds[index + 1] - bounds[index],
                    ORDER[class_id],
                )
            )
    return holdings, prices, fx, allocations


@given(_portfolio())
@settings(deadline=None, max_examples=150)
def test_class_totals_equal_the_portfolio_totals(portfolio) -> None:
    holdings, prices, fx, allocations = portfolio
    valuation = value_portfolio(holdings, prices, fx, allocations, AS_OF, base_currency=BASE)
    assert sum(t.value.amount_minor for t in valuation.classes) == valuation.value.amount_minor
    assert sum(t.cost.amount_minor for t in valuation.classes) == valuation.cost.amount_minor
    for row in valuation.rows:
        if row.value is None:
            continue
        assert sum(part.value_minor for part in row.parts) == row.value.amount_minor
        assert sum(part.cost_minor for part in row.parts) == row.cost_basis.amount_minor


@given(_portfolio())
@settings(deadline=None, max_examples=150)
def test_allocation_weights_are_respected(portfolio) -> None:
    holdings, prices, fx, allocations = portfolio
    valuation = value_portfolio(holdings, prices, fx, allocations, AS_OF, base_currency=BASE)
    for row in valuation.rows:
        if row.value is None:
            continue
        weights = [item for item in allocations if item.instrument_id == row.instrument_id]
        largest = min(
            weights,
            key=lambda item: (-item.weight_bp, item.class_sort_order, str(item.asset_class_id)),
        )
        by_class = {part.asset_class_id: part for part in row.parts}
        for item in weights:
            for amount, part in (
                (row.value.amount_minor, by_class[item.asset_class_id].value_minor),
                (row.cost_basis.amount_minor, by_class[item.asset_class_id].cost_minor),
            ):
                exact = Fraction(amount * item.weight_bp, 10_000)
                if item is largest:
                    # The largest class absorbs the other classes' floors: under 1 unit each.
                    assert 0 <= part - exact < len(weights)
                else:
                    assert 0 <= exact - part < 1


@given(_portfolio())
@settings(deadline=None, max_examples=150)
def test_excluded_rows_never_affect_totals_and_values_match_the_oracle(portfolio) -> None:
    holdings, prices, fx, allocations = portfolio
    valuation = value_portfolio(holdings, prices, fx, allocations, AS_OF, base_currency=BASE)
    included = [row.instrument_id for row in valuation.rows if row.missing is None]
    alone = value_portfolio(
        [holding for holding in holdings if holding.instrument_id in included],
        prices,
        fx,
        allocations,
        AS_OF,
        base_currency=BASE,
    )
    assert (alone.value, alone.cost, alone.classes) == (
        valuation.value,
        valuation.cost,
        valuation.classes,
    )
    price_by_id = {item.instrument_id: item.price for item in prices}
    for holding, row in zip(holdings, valuation.rows, strict=True):
        price = price_by_id.get(holding.instrument_id)
        rate = Decimal(1) if holding.quote_currency == BASE else fx.get(holding.quote_currency)
        if price is None or rate is None:
            assert row.value is None and row.missing is not None
            continue
        exact = Fraction(holding.quantity) * Fraction(price) * Fraction(rate) * 100
        assert row.value == Money(_round_half_up(exact), BASE)
