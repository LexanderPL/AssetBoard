"""Holdings and cost basis (F6.2): golden scenario, rounding, FIFO, oversell and properties."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from assetboard.domain.holdings import (
    CostBasisMethod,
    HoldingTransaction,
    InvalidHoldingTransaction,
    InvestmentTransactionType,
    Oversell,
    compute_holdings,
)
from assetboard.domain.money import Money

BASE = "EUR"
QUOTE = "EUR"


def _tx(
    *,
    day: date | None,
    type: str,
    quantity: str,
    price: str,
    fee: int = 0,
    fx: str = "1",
    instrument_id: UUID | None = None,
    quote_currency: str = QUOTE,
    created_at: datetime | None = None,
) -> HoldingTransaction:
    return HoldingTransaction(
        id=uuid4(),
        instrument_id=instrument_id or uuid4(),
        date=day,
        type=type,
        quantity=Decimal(quantity),
        unit_price=Decimal(price),
        fee_minor=fee,
        quote_currency=quote_currency,
        fx_to_base=Decimal(fx),
        created_at=created_at or datetime(2026, 3, 1, 12, tzinfo=UTC),
    )


def _buy(
    instrument_id: UUID,
    quantity: str,
    price: str,
    *,
    fee: int = 0,
    day: date = date(2026, 1, 1),
    fx: str = "1",
    quote_currency: str = QUOTE,
) -> HoldingTransaction:
    return _tx(
        day=day,
        type="buy",
        quantity=quantity,
        price=price,
        fee=fee,
        fx=fx,
        instrument_id=instrument_id,
        quote_currency=quote_currency,
    )


def _sell(
    instrument_id: UUID,
    quantity: str,
    price: str,
    *,
    fee: int = 0,
    day: date = date(2026, 2, 1),
    fx: str = "1",
    quote_currency: str = QUOTE,
) -> HoldingTransaction:
    return _tx(
        day=day,
        type="sell",
        quantity=quantity,
        price=price,
        fee=fee,
        fx=fx,
        instrument_id=instrument_id,
        quote_currency=quote_currency,
    )


def _holding(rows: list[HoldingTransaction], method: str = "average"):
    return compute_holdings(rows, method, base_currency=BASE)[0]


def test_the_golden_scenario_uses_the_sum_of_transaction_costs() -> None:
    instrument = uuid4()
    rows = [
        _buy(instrument, "46", "489.5409", fee=6530, day=date(2026, 1, 1)),
        _buy(instrument, "4", "496.3093", fee=1400, day=date(2026, 2, 1)),
    ]
    holding = _holding(rows)
    assert holding.quantity == Decimal("50")
    assert holding.cost_basis_base == Money(2_458_342, BASE)
    assert holding.avg_cost == Decimal("491.6684")
    assert holding.realized_pnl == Money(0, BASE)


def test_average_cost_is_unchanged_by_a_partial_sale_and_removes_exact_cost() -> None:
    instrument = uuid4()
    rows = [
        _buy(instrument, "46", "489.5409", fee=6530, day=date(2026, 1, 1)),
        _buy(instrument, "4", "496.3093", fee=1400, day=date(2026, 2, 1)),
        _sell(instrument, "10", "500", day=date(2026, 3, 1)),
    ]
    holding = _holding(rows)
    # 2 458 342 × 10 / 50 = 491 668.4 → 4 916.68 removed; the displayed average is unchanged.
    assert holding.quantity == Decimal("40")
    assert holding.cost_basis_base == Money(1_966_674, BASE)
    assert holding.avg_cost == Decimal("491.6684")
    assert holding.realized_pnl == Money(8_332, BASE)


def test_fifo_consumes_the_first_lot_and_rounds_only_the_partial_lot() -> None:
    instrument = uuid4()
    rows = [
        _buy(instrument, "46", "489.5409", fee=6530, day=date(2026, 1, 1)),
        _buy(instrument, "4", "496.3093", fee=1400, day=date(2026, 2, 1)),
        _sell(instrument, "10", "500", day=date(2026, 3, 1)),
    ]
    holding = _holding(rows, "fifo")
    assert holding.quantity == Decimal("40")
    # First lot 22 584.18 × 10 / 46 = 4 909.6043 → 4 909.60 removed.
    assert holding.cost_basis_base == Money(1_967_382, BASE)
    assert holding.realized_pnl == Money(9_040, BASE)
    assert holding.avg_cost == Decimal("491.8455")


def test_selling_everything_returns_zero_cost_for_both_methods() -> None:
    instrument = uuid4()
    rows = [
        _buy(instrument, "46", "489.5409", fee=6530),
        _buy(instrument, "4", "496.3093", fee=1400),
        _sell(instrument, "50", "600", fee=123),
    ]
    for method in ("average", "fifo"):
        holding = _holding(rows, method)
        assert holding.quantity == 0
        assert holding.cost_basis_base == Money(0, BASE)


def test_fractional_quantity_is_kept_exactly() -> None:
    instrument = uuid4()
    rows = [_buy(instrument, "0.00924", "100000")]
    holding = _holding(rows)
    assert holding.quantity == Decimal("0.00924")
    assert holding.cost_basis_base == Money(92_400, BASE)


def test_a_sell_fee_can_exceed_the_gross_and_produce_a_loss() -> None:
    instrument = uuid4()
    rows = [
        _buy(instrument, "1", "100"),
        _sell(instrument, "1", "100", fee=500),
    ]
    holding = _holding(rows)
    assert holding.realized_pnl == Money(-500, BASE)


def test_fx_converts_the_whole_transaction_once() -> None:
    instrument = uuid4()
    rows = [
        _buy(instrument, "10", "100", fx="0.9", quote_currency="USD"),
        _sell(instrument, "10", "100", fx="0.8", quote_currency="USD"),
    ]
    holding = _holding(rows)
    assert holding.cost_basis_base == Money(0, BASE)
    assert holding.realized_pnl == Money(-10_000, BASE)  # 800.00 proceeds − 900.00 cost


def test_oversell_names_the_failing_sale_date() -> None:
    instrument = uuid4()
    rows = [
        _buy(instrument, "1", "100", day=date(2026, 1, 1)),
        _sell(instrument, "2", "100", day=date(2026, 2, 3)),
    ]
    with pytest.raises(Oversell) as caught:
        compute_holdings(rows, "average", base_currency=BASE)
    assert caught.value.on == date(2026, 2, 3)


def test_undated_lots_count_now_but_not_in_a_dated_as_of() -> None:
    instrument = uuid4()
    rows = [
        _tx(day=None, type="buy", quantity="1", price="100", instrument_id=instrument),
        _buy(instrument, "1", "200", day=date(2026, 1, 1)),
    ]
    current = compute_holdings(rows, "average", base_currency=BASE)[0]
    assert current.quantity == Decimal("2")
    dated = compute_holdings(rows, "average", date(2026, 6, 1), base_currency=BASE)
    assert dated[0].quantity == Decimal("1")


def test_compute_holdings_returns_one_holding_per_instrument() -> None:
    first, second = uuid4(), uuid4()
    rows = [
        _buy(first, "1", "100"),
        _buy(second, "2", "50"),
    ]
    holdings = compute_holdings(rows, "average", base_currency=BASE)
    by_instrument = {holding.instrument_id: holding.quantity for holding in holdings}
    assert by_instrument == {first: Decimal("1"), second: Decimal("2")}


def test_invalid_transaction_fields_are_refused() -> None:
    with pytest.raises(InvalidHoldingTransaction):
        compute_holdings(
            [_tx(day=date(2026, 1, 1), type="dividend", quantity="1", price="1")],
            "average",
            base_currency=BASE,
        )
    with pytest.raises(InvalidHoldingTransaction):
        compute_holdings(
            [_tx(day=date(2026, 1, 1), type="buy", quantity="0", price="1")],
            "average",
            base_currency=BASE,
        )


def test_the_accounting_identity_holds_for_a_buy_and_a_sell() -> None:
    instrument = uuid4()
    buy = _buy(instrument, "10", "12.3456", fee=123)
    sell = _sell(instrument, "4", "20", fee=50)
    holding = _holding([buy, sell])
    total_bought = 12_469  # round((10 × 12.3456 + 1.23) × 1)
    proceeds = 7_950  # round((4 × 20 − 0.50) × 1)
    cost_removed = total_bought - holding.cost_basis_base.amount_minor
    assert holding.realized_pnl.amount_minor + cost_removed == proceeds


@given(
    quantity=st.decimals(min_value=Decimal("0.000001"), max_value=Decimal("100000")),
    price=st.decimals(min_value=Decimal("0"), max_value=Decimal("100000")),
)
def test_selling_everything_property_zero_cost(quantity: Decimal, price: Decimal) -> None:
    instrument = uuid4()
    rows = [
        _tx(
            day=date(2026, 1, 1),
            type="buy",
            quantity=str(quantity),
            price=str(price),
            instrument_id=instrument,
        ),
        _tx(
            day=date(2026, 2, 1),
            type="sell",
            quantity=str(quantity),
            price="1",
            instrument_id=instrument,
        ),
    ]
    for method in (CostBasisMethod.AVERAGE, CostBasisMethod.FIFO):
        holding = compute_holdings(rows, method, base_currency=BASE)[0]
        assert holding.quantity == 0
        assert holding.cost_basis_base == Money(0, BASE)
        assert holding.realized_pnl.amount_minor >= -(10**18)


@given(
    buys=st.lists(
        st.tuples(
            st.decimals(min_value=Decimal("0.01"), max_value=Decimal("100")),
            st.decimals(min_value=Decimal("0"), max_value=Decimal("1000")),
        ),
        min_size=1,
        max_size=5,
    )
)
def test_quantity_and_cost_basis_are_never_negative(
    buys: list[tuple[Decimal, Decimal]],
) -> None:
    instrument = uuid4()
    rows = [
        _tx(
            day=date(2026, 1, 1 + index),
            type="buy",
            quantity=str(quantity),
            price=str(price),
            instrument_id=instrument,
        )
        for index, (quantity, price) in enumerate(buys)
    ]
    for method in (CostBasisMethod.AVERAGE, CostBasisMethod.FIFO):
        holding = compute_holdings(rows, method, base_currency=BASE)[0]
        assert holding.quantity >= 0
        assert holding.cost_basis_base.amount_minor >= 0
        assert holding.avg_cost >= 0


def test_transaction_type_values_are_the_spec_set() -> None:
    assert {kind.value for kind in InvestmentTransactionType} == {
        "buy",
        "sell",
        "dividend",
        "fee",
        "split",
    }
