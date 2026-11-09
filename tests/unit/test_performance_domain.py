"""Performance series and XIRR (F6.7): carry-forward, cashflows, periods and properties."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from time import perf_counter
from uuid import UUID, uuid4

from hypothesis import given
from hypothesis import strategies as st
from scipy.optimize import brentq

from assetboard.domain.holdings import HoldingTransaction
from assetboard.domain.money import Money
from assetboard.domain.performance import (
    CashFlow,
    PriceObservation,
    RateObservation,
    Step,
    XirrFailure,
    add_months,
    cashflows_for_period,
    period_return,
    portfolio_series,
    portfolio_value_at,
    step_dates,
    xirr,
    years_before,
)
from assetboard.domain.portfolio import PortfolioAllocation

BASE = "EUR"
DAY = date(2020, 1, 1)


def _tx(
    *,
    instrument_id: UUID,
    day: date | None = DAY,
    type: str = "buy",
    quantity: str = "10",
    unit_price: str = "100",
    fee: int = 0,
    quote_currency: str = BASE,
    fx: str = "1",
    settlement: tuple[str, str, int] | None = None,
) -> HoldingTransaction:
    return HoldingTransaction(
        id=uuid4(),
        instrument_id=instrument_id,
        date=day,
        type=type,
        quantity=Decimal(quantity),
        unit_price=Decimal(unit_price),
        fee_minor=fee,
        quote_currency=quote_currency,
        fx_to_base=Decimal(fx),
        created_at=datetime(2020, 1, 1, 12, tzinfo=UTC),
        settlement_currency=settlement[0] if settlement else None,
        settlement_unit_price=Decimal(settlement[1]) if settlement else None,
        settlement_fee_minor=settlement[2] if settlement else None,
    )


def _price(*rows: tuple[date, str]) -> list[PriceObservation]:
    return [PriceObservation(day, Decimal(value)) for day, value in rows]


def _rate(*rows: tuple[date, str]) -> list[RateObservation]:
    return [RateObservation(day, Decimal(value)) for day, value in rows]


def test_xirr_golden_vector_and_period_return() -> None:
    result = xirr(
        (
            CashFlow(date(2020, 1, 1), Money(-100_000, "PLN")),
            CashFlow(date(2021, 1, 1), Money(110_000, "PLN")),
        )
    )
    assert result.defined
    assert result.rate is not None
    assert abs(result.rate - Decimal("0.0997")) < Decimal("0.0001")
    assert abs(period_return(result.rate, 366) - Decimal("0.1")) < Decimal("1e-12")


def test_xirr_handles_multiple_flows_and_a_negative_rate() -> None:
    profitable = xirr(
        (
            CashFlow(date(2020, 1, 1), Money(-100_000, "EUR")),
            CashFlow(date(2020, 7, 1), Money(-50_000, "EUR")),
            CashFlow(date(2021, 1, 1), Money(170_000, "EUR")),
        )
    )
    assert profitable.defined and profitable.rate is not None
    assert profitable.rate > 0
    losing = xirr(
        (
            CashFlow(date(2020, 1, 1), Money(-100_000, "EUR")),
            CashFlow(date(2021, 1, 1), Money(90_000, "EUR")),
        )
    )
    assert losing.defined and losing.rate is not None
    assert losing.rate < 0


def test_xirr_undefined_reasons() -> None:
    assert xirr(()).reason is XirrFailure.NO_CASHFLOWS
    same_sign = xirr(
        (
            CashFlow(date(2020, 1, 1), Money(-100, "EUR")),
            CashFlow(date(2021, 1, 1), Money(-100, "EUR")),
        )
    )
    assert same_sign.reason is XirrFailure.NO_SIGN_CHANGE


def test_xirr_matches_scipy_brentq_as_an_independent_oracle() -> None:
    vectors = (
        (
            CashFlow(date(2020, 1, 1), Money(-100_000, "EUR")),
            CashFlow(date(2021, 1, 1), Money(110_000, "EUR")),
        ),
        (
            CashFlow(date(2020, 1, 1), Money(-100_000, "EUR")),
            CashFlow(date(2020, 7, 1), Money(-50_000, "EUR")),
            CashFlow(date(2021, 1, 1), Money(170_000, "EUR")),
        ),
        (
            CashFlow(date(2020, 1, 1), Money(-100_000, "EUR")),
            CashFlow(date(2020, 4, 1), Money(20_000, "EUR")),
            CashFlow(date(2021, 1, 1), Money(70_000, "EUR")),
        ),
    )
    for flows in vectors:
        result = xirr(flows)
        assert result.rate is not None
        first = flows[0].date

        def equation(rate: float, flows=flows, first=first) -> float:
            return sum(
                float(flow.amount.amount_minor) / (1 + rate) ** ((flow.date - first).days / 365)
                for flow in flows
            )

        oracle = float(brentq(equation, -0.999, 10))  # type: ignore[arg-type]
        assert abs(float(result.rate) - oracle) < 1e-9


def test_period_cashflows_do_not_double_count_the_start_day() -> None:
    instrument_id = uuid4()
    transactions = (
        _tx(instrument_id=instrument_id, day=DAY, quantity="10"),
        _tx(
            instrument_id=instrument_id,
            day=date(2020, 2, 1),
            type="sell",
            quantity="2",
            unit_price="110",
        ),
    )
    flows = cashflows_for_period(
        transactions,
        DAY,
        date(2020, 3, 1),
        start_value=Money(100_000, "EUR"),
        end_value=Money(90_000, "EUR"),
        base_currency="EUR",
    )
    assert flows[0] == CashFlow(DAY, Money(-100_000, "EUR"))
    assert flows[1].date == date(2020, 2, 1)
    assert flows[1].amount == Money(22_000, "EUR")
    assert flows[-1] == CashFlow(date(2020, 3, 1), Money(90_000, "EUR"))


def test_series_carries_prices_and_reports_value_and_net_invested() -> None:
    instrument_id = uuid4()
    points = portfolio_series(
        (_tx(instrument_id=instrument_id, quantity="10", fee=500),),
        {instrument_id: _price((DAY, "100"), (date(2020, 1, 2), "110"))},
        {},
        DAY,
        date(2020, 1, 3),
        Step.DAY,
        base_currency="EUR",
    )
    assert [(point.date, point.value.amount_minor) for point in points] == [
        (DAY, 100_000),
        (date(2020, 1, 2), 110_000),
        (date(2020, 1, 3), 110_000),
    ]
    assert all(point.net_invested == Money(100_500, "EUR") for point in points)
    assert all(not point.incomplete for point in points)


def test_series_converts_fx_and_uses_settlement_totals() -> None:
    instrument_id = uuid4()
    transaction = _tx(
        instrument_id=instrument_id,
        quantity="10",
        unit_price="100",
        quote_currency="USD",
        fx="0.9",
    )
    point = portfolio_value_at(
        (transaction,),
        {instrument_id: _price((DAY, "110"))},
        {"USD": _rate((DAY, "0.92"))},
        DAY,
        base_currency="EUR",
    )
    assert point.value == Money(101_200, "EUR")  # 10 × 110 × 0.92
    assert point.net_invested == Money(90_000, "EUR")

    settled = _tx(
        instrument_id=instrument_id,
        quantity="10",
        unit_price="4",
        settlement=("EUR", "4", 100),
    )
    settled_point = portfolio_value_at(
        (settled,),
        {instrument_id: _price((DAY, "4"))},
        {},
        DAY,
        base_currency="EUR",
    )
    assert settled_point.value == Money(4_000, "EUR")
    assert settled_point.net_invested == Money(4_100, "EUR")


def test_series_adjusts_a_pre_split_price_and_has_no_jump() -> None:
    instrument_id = uuid4()
    transactions = (
        _tx(instrument_id=instrument_id, day=DAY, quantity="10", unit_price="100"),
        _tx(
            instrument_id=instrument_id,
            day=date(2020, 1, 2),
            type="split",
            quantity="2",
            unit_price="1",
        ),
    )
    points = portfolio_series(
        transactions,
        {instrument_id: _price((DAY, "100"))},
        {},
        DAY,
        date(2020, 1, 3),
        Step.DAY,
        base_currency="EUR",
    )
    assert [point.value for point in points] == [Money(100_000, "EUR")] * 3


def test_a_dividend_is_a_positive_cash_flow_and_a_split_is_not() -> None:
    instrument_id = uuid4()
    transactions = (
        _tx(instrument_id=instrument_id, quantity="10", unit_price="100"),
        _tx(
            instrument_id=instrument_id,
            day=date(2020, 7, 1),
            type="dividend",
            quantity="1",
            unit_price="50",
        ),
        _tx(
            instrument_id=instrument_id,
            day=date(2020, 8, 1),
            type="split",
            quantity="2",
            unit_price="1",
        ),
    )
    flows = cashflows_for_period(
        transactions,
        DAY,
        date(2021, 1, 1),
        start_value=Money(100_000, "EUR"),
        end_value=Money(100_000, "EUR"),
        base_currency="EUR",
    )
    assert [flow for flow in flows if flow.date == date(2020, 7, 1)] == [
        CashFlow(date(2020, 7, 1), Money(5_000, "EUR"))
    ]
    assert all(flow.date != date(2020, 8, 1) for flow in flows)


def test_xirr_counts_a_dividend_as_an_inflow() -> None:
    instrument_id = uuid4()
    transactions = (
        _tx(instrument_id=instrument_id, quantity="10", unit_price="100"),
        _tx(
            instrument_id=instrument_id,
            day=date(2020, 7, 1),
            type="dividend",
            quantity="1",
            unit_price="50",
            quote_currency="EUR",
        ),
    )
    flows = cashflows_for_period(
        transactions,
        DAY,
        date(2021, 1, 1),
        start_value=Money(100_000, "EUR"),
        end_value=Money(100_000, "EUR"),
        base_currency="EUR",
    )
    result = xirr(flows)
    assert result.defined and result.rate is not None
    assert result.rate > 0

    def equation(rate: float) -> float:
        return sum(
            float(flow.amount.amount_minor) / (1 + rate) ** ((flow.date - DAY).days / 365)
            for flow in flows
        )

    oracle = float(brentq(equation, -0.999, 10))  # type: ignore[arg-type]
    assert abs(float(result.rate) - oracle) < 1e-9


def test_series_excludes_undated_and_marks_missing_data() -> None:
    instrument_id = uuid4()
    undated = _tx(instrument_id=instrument_id, day=None)
    timed = _tx(instrument_id=instrument_id)
    missing = portfolio_series(
        (undated, timed),
        {},
        {},
        DAY,
        DAY,
        Step.DAY,
        base_currency="EUR",
    )
    assert missing[0].value == Money(0, "EUR")
    assert missing[0].net_invested == Money(0, "EUR")
    assert missing[0].incomplete


def test_series_backfills_a_leading_price_and_fx_observation() -> None:
    instrument_id = uuid4()
    transactions = (
        _tx(
            instrument_id=instrument_id,
            quantity="10",
            unit_price="100",
            quote_currency="USD",
            fx="0.9",
        ),
    )
    prices = {instrument_id: _price((date(2020, 1, 3), "110"))}
    fx = {"USD": _rate((date(2020, 1, 3), "0.92"))}
    end = date(2020, 1, 3)

    conservative = portfolio_series(
        transactions, prices, fx, DAY, end, Step.DAY, base_currency="EUR"
    )
    assert conservative[0].incomplete
    assert conservative[0].value == Money(0, "EUR")
    assert conservative[0].net_invested == Money(0, "EUR")

    backfilled = portfolio_series(
        transactions,
        prices,
        fx,
        DAY,
        end,
        Step.DAY,
        base_currency="EUR",
        backfill_leading=True,
    )
    assert [point.value for point in backfilled] == [Money(101_200, "EUR")] * 3
    assert [point.net_invested for point in backfilled] == [Money(90_000, "EUR")] * 3
    assert not any(point.incomplete for point in backfilled)


def test_series_backfill_keeps_carry_forward_as_the_primary_rule() -> None:
    instrument_id = uuid4()
    transactions = (_tx(instrument_id=instrument_id, quantity="10"),)
    prices = {instrument_id: _price((DAY, "100"), (date(2020, 1, 3), "200"))}

    points = portfolio_series(
        transactions,
        prices,
        {},
        date(2020, 1, 2),
        date(2020, 1, 2),
        Step.DAY,
        base_currency="EUR",
        backfill_leading=True,
    )

    assert points[0].value == Money(100_000, "EUR")


def test_series_backfill_does_not_invent_missing_data() -> None:
    instrument_id = uuid4()
    transactions = (_tx(instrument_id=instrument_id, quantity="10"),)

    points = portfolio_series(
        transactions,
        {},
        {},
        DAY,
        DAY,
        Step.DAY,
        base_currency="EUR",
        backfill_leading=True,
    )

    assert points[0].incomplete
    assert points[0].value == Money(0, "EUR")
    assert points[0].net_invested == Money(0, "EUR")


def test_class_series_sums_exactly_to_the_total_series() -> None:
    instrument_id = uuid4()
    stocks, bonds = uuid4(), uuid4()
    allocations = {
        instrument_id: (
            PortfolioAllocation(instrument_id, stocks, 8_000, 0),
            PortfolioAllocation(instrument_id, bonds, 2_000, 1),
        )
    }
    transactions = (_tx(instrument_id=instrument_id, quantity="10", fee=0),)
    prices = {instrument_id: _price((DAY, "100"))}
    total = portfolio_series(transactions, prices, {}, DAY, DAY, Step.DAY, base_currency="EUR")[0]
    stocks_point = portfolio_series(
        transactions,
        prices,
        {},
        DAY,
        DAY,
        Step.DAY,
        base_currency="EUR",
        allocations=allocations,
        asset_class_id=stocks,
    )[0]
    bonds_point = portfolio_series(
        transactions,
        prices,
        {},
        DAY,
        DAY,
        Step.DAY,
        base_currency="EUR",
        allocations=allocations,
        asset_class_id=bonds,
    )[0]
    assert stocks_point.value == Money(80_000, "EUR")
    assert bonds_point.value == Money(20_000, "EUR")
    assert stocks_point.value + bonds_point.value == total.value
    assert stocks_point.net_invested + bonds_point.net_invested == total.net_invested


def test_class_net_invested_uses_each_transaction_total() -> None:
    instrument_id = uuid4()
    stocks, bonds = uuid4(), uuid4()
    allocations = {
        instrument_id: (
            PortfolioAllocation(instrument_id, stocks, 8_000, 0),
            PortfolioAllocation(instrument_id, bonds, 2_000, 1),
        )
    }
    transactions = (
        _tx(instrument_id=instrument_id, quantity="10", fee=0),
        _tx(instrument_id=instrument_id, quantity="10", fee=0),
        _tx(
            instrument_id=instrument_id,
            type="sell",
            quantity="5",
            unit_price="100",
        ),
    )
    prices = {instrument_id: _price((DAY, "100"))}
    point = portfolio_series(
        transactions,
        prices,
        {},
        DAY,
        DAY,
        Step.DAY,
        base_currency="EUR",
        allocations=allocations,
        asset_class_id=stocks,
    )[0]
    assert point.net_invested == Money(120_000, "EUR")


@given(
    stocks_weight=st.integers(min_value=0, max_value=10_000),
    quantity=st.decimals(min_value=Decimal("1"), max_value=Decimal("100"), places=2),
    price=st.decimals(min_value=Decimal("1"), max_value=Decimal("1000"), places=2),
)
def test_class_series_sums_to_the_total_property(
    stocks_weight: int, quantity: Decimal, price: Decimal
) -> None:
    instrument_id = uuid4()
    stocks, bonds = uuid4(), uuid4()
    allocations = {
        instrument_id: (
            PortfolioAllocation(instrument_id, stocks, stocks_weight, 0),
            PortfolioAllocation(instrument_id, bonds, 10_000 - stocks_weight, 1),
        )
    }
    transactions = (
        _tx(
            instrument_id=instrument_id,
            quantity=str(quantity),
            unit_price=str(price),
            fee=0,
        ),
    )
    prices = {instrument_id: _price((DAY, str(price)))}
    total = portfolio_series(transactions, prices, {}, DAY, DAY, Step.DAY, base_currency="EUR")[0]
    stocks_point = portfolio_series(
        transactions,
        prices,
        {},
        DAY,
        DAY,
        Step.DAY,
        base_currency="EUR",
        allocations=allocations,
        asset_class_id=stocks,
    )[0]
    bonds_point = portfolio_series(
        transactions,
        prices,
        {},
        DAY,
        DAY,
        Step.DAY,
        base_currency="EUR",
        allocations=allocations,
        asset_class_id=bonds,
    )[0]
    assert stocks_point.value + bonds_point.value == total.value
    assert stocks_point.net_invested + bonds_point.net_invested == total.net_invested


def test_step_generation_includes_the_end_and_handles_calendar_months() -> None:
    assert step_dates(DAY, date(2020, 1, 15), Step.WEEK) == [
        DAY,
        date(2020, 1, 8),
        date(2020, 1, 15),
    ]
    assert step_dates(date(2020, 1, 31), date(2020, 3, 31), Step.MONTH) == [
        date(2020, 1, 31),
        date(2020, 2, 29),
        date(2020, 3, 31),
    ]
    assert add_months(date(2020, 1, 31), 1) == date(2020, 2, 29)
    assert years_before(date(2020, 2, 29), 1) == date(2019, 2, 28)


def test_period_return_is_the_short_period_conversion() -> None:
    annual = Decimal("0.25")
    expected = Decimal("1.25") ** (Decimal(90) / 365) - 1
    assert abs(period_return(annual, 90) - expected) < Decimal("1e-25")


@given(
    day_offset=st.integers(min_value=0, max_value=60),
    price=st.decimals(min_value=Decimal("1"), max_value=Decimal("1000"), places=2),
)
def test_series_value_is_non_negative_and_carry_forward_is_stable(
    day_offset: int, price: Decimal
) -> None:
    instrument_id = uuid4()
    day = DAY + timedelta(days=day_offset)
    point = portfolio_value_at(
        (_tx(instrument_id=instrument_id, day=DAY),),
        {instrument_id: _price((DAY, "100"), (day, str(price)))},
        {},
        day,
        base_currency="EUR",
    )
    assert point.value.amount_minor >= 0
    assert point.net_invested == Money(100_000, "EUR")


def test_five_year_weekly_series_is_fast() -> None:
    instrument_ids = [uuid4() for _ in range(30)]
    transactions = []
    prices: dict[UUID, list[PriceObservation]] = {}
    for index, instrument_id in enumerate(instrument_ids):
        transactions.append(_tx(instrument_id=instrument_id, quantity="10"))
        prices[instrument_id] = _price(
            (DAY, "100"), (DAY + timedelta(days=5 * 365), f"{100 + index}")
        )
    started = perf_counter()
    points = portfolio_series(
        transactions,
        prices,
        {},
        DAY,
        DAY + timedelta(days=5 * 365),
        Step.WEEK,
        base_currency="EUR",
    )
    elapsed = perf_counter() - started
    print(f"five-year weekly series: {elapsed * 1000:.1f} ms, {len(points)} points")
    assert elapsed < 2
    assert len(points) >= 260
