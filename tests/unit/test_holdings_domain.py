"""Holdings and cost basis (F6.2): golden scenario, rounding, FIFO, oversell and properties."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from uuid import UUID, uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from assetboard.domain.holdings import (
    HoldingTransaction,
    InvalidHoldingTransaction,
    InvestmentTransactionType,
    Oversell,
    RealizedEvent,
    compute_holdings,
    dividend_amounts,
    realized_events,
    split_factor,
)
from assetboard.domain.money import Money, minor_unit_for

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


def _split(
    instrument_id: UUID,
    new_units: str,
    old_units: str,
    *,
    day: date = date(2026, 2, 1),
) -> HoldingTransaction:
    return _tx(
        day=day,
        type="split",
        quantity=new_units,
        price=old_units,
        instrument_id=instrument_id,
    )


def _dividend(
    instrument_id: UUID,
    gross: str,
    *,
    withholding: int = 0,
    day: date = date(2026, 2, 1),
    fx: str = "1",
    quote_currency: str = QUOTE,
) -> HoldingTransaction:
    return _tx(
        day=day,
        type="dividend",
        quantity="1",
        price=gross,
        fee=withholding,
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


def test_a_2_for_1_split_scales_quantity_and_keeps_cost_basis() -> None:
    instrument = uuid4()
    rows = [
        _buy(instrument, "10", "100", day=date(2026, 1, 1)),
        _split(instrument, "2", "1", day=date(2026, 2, 1)),
    ]
    holding = _holding(rows)
    assert holding.quantity == Decimal("20")
    assert holding.cost_basis_base == Money(100_000, BASE)
    assert holding.avg_cost == Decimal("50")


def test_a_1_for_3_reverse_split_is_exact_to_the_decimal_type_scale() -> None:
    instrument = uuid4()
    rows = [
        _buy(instrument, "10", "100", day=date(2026, 1, 1)),
        _split(instrument, "1", "3", day=date(2026, 2, 1)),
    ]
    holding = _holding(rows)
    assert holding.quantity == Decimal("3.333333333333333333")
    assert holding.quantity.as_tuple().exponent == -18
    assert holding.cost_basis_base == Money(100_000, BASE)


def test_a_3_for_2_split_scales_fifo_lots_without_changing_cost() -> None:
    instrument = uuid4()
    rows = [
        _buy(instrument, "4", "100", day=date(2026, 1, 1)),
        _buy(instrument, "6", "200", day=date(2026, 1, 2)),
        _split(instrument, "3", "2", day=date(2026, 2, 1)),
        _sell(instrument, "10", "100", day=date(2026, 3, 1)),
    ]
    holding = _holding(rows, "fifo")
    assert holding.quantity == Decimal("5")
    assert holding.cost_basis_base == Money(66_667, BASE)
    assert holding.realized_pnl == Money(6_667, BASE)


def test_a_dividend_does_not_change_holdings() -> None:
    instrument = uuid4()
    rows = [
        _buy(instrument, "10", "100", day=date(2026, 1, 1)),
        _dividend(instrument, "5", withholding=50, day=date(2026, 2, 1)),
    ]
    holding = _holding(rows)
    assert holding.quantity == Decimal("10")
    assert holding.cost_basis_base == Money(100_000, BASE)
    assert holding.realized_pnl == Money(0, BASE)


def test_split_factor_uses_old_over_new_in_the_interval() -> None:
    instrument = uuid4()
    rows = [
        _split(instrument, "2", "1", day=date(2026, 2, 1)),
        _split(instrument, "1", "3", day=date(2026, 3, 1)),
    ]
    assert split_factor(rows, date(2026, 1, 1), date(2026, 2, 1)) == Decimal("0.5")
    assert split_factor(rows, date(2026, 1, 1), date(2026, 3, 1)) == Decimal("1.5")
    assert split_factor(rows, date(2026, 2, 1), date(2026, 3, 1)) == Decimal("3")


def test_a_split_applies_to_undated_legacy_lots_first() -> None:
    instrument = uuid4()
    rows = [
        _tx(day=None, type="buy", quantity="10", price="100", instrument_id=instrument),
        _split(instrument, "2", "1", day=date(2026, 2, 1)),
    ]
    holding = _holding(rows)
    assert holding.quantity == Decimal("20")
    assert holding.cost_basis_base == Money(100_000, BASE)


def test_dividend_amounts_round_gross_and_withholding_once() -> None:
    instrument = uuid4()
    row = _dividend(instrument, "10.01", withholding=5, fx="1.2345", quote_currency="USD")
    amounts = dividend_amounts(row, BASE)
    assert amounts.gross == Money(1_236, BASE)  # 12.357345 -> 12.36
    assert amounts.withholding == Money(6, BASE)  # 0.061725 -> 0.06
    assert amounts.net == Money(1_230, BASE)


def test_dividend_and_split_encoding_is_validated() -> None:
    instrument = uuid4()
    with pytest.raises(InvalidHoldingTransaction, match="invalid_quantity"):
        _holding([replace(_dividend(instrument, "10"), quantity=Decimal("2"))])
    with pytest.raises(InvalidHoldingTransaction, match="invalid_amount_precision"):
        _holding([_dividend(instrument, "1.234")])
    with pytest.raises(InvalidHoldingTransaction, match="invalid_fee"):
        _holding([_dividend(instrument, "1", withholding=101)])
    with pytest.raises(InvalidHoldingTransaction, match="invalid_settlement"):
        _holding(
            [
                replace(
                    _dividend(instrument, "1"),
                    settlement_currency=BASE,
                    settlement_unit_price=Decimal("1"),
                    settlement_fee_minor=0,
                )
            ]
        )
    with pytest.raises(InvalidHoldingTransaction, match="invalid_split"):
        _holding([_split(instrument, "2", "2")])
    with pytest.raises(InvalidHoldingTransaction, match="invalid_fee"):
        _holding([replace(_split(instrument, "2", "1"), fee_minor=1)])
    with pytest.raises(InvalidHoldingTransaction, match="invalid_fx"):
        _holding([replace(_split(instrument, "2", "1"), fx_to_base=Decimal("1.5"))])
    with pytest.raises(InvalidHoldingTransaction, match="invalid_settlement"):
        _holding(
            [
                replace(
                    _split(instrument, "2", "1"),
                    settlement_currency=BASE,
                    settlement_unit_price=Decimal("1"),
                    settlement_fee_minor=0,
                )
            ]
        )


@given(st.integers(min_value=1, max_value=100), st.integers(min_value=1, max_value=100))
@settings(max_examples=50)
def test_a_split_and_its_inverse_restore_quantity_and_cost(new_units: int, old_units: int) -> None:
    if new_units == old_units:
        return
    instrument = uuid4()
    rows = [
        _buy(instrument, "10", "100", day=date(2026, 1, 1)),
        _split(instrument, str(new_units), str(old_units), day=date(2026, 2, 1)),
        _split(instrument, str(old_units), str(new_units), day=date(2026, 3, 1)),
    ]
    holding = _holding(rows)
    assert abs(holding.quantity - Decimal("10")) <= Decimal("1e-18")
    assert holding.cost_basis_base == Money(100_000, BASE)


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
            [
                _tx(
                    day=date(2026, 1, 1),
                    type="dividend",
                    quantity="2",
                    price="1",
                )
            ],
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


# Independent oracle: Fraction-based, plain lots; it does not call compute_holdings.
def _round_half_up(value: Fraction) -> int:
    if value >= 0:
        return (value + Fraction(1, 2)).__floor__()
    return -((-value + Fraction(1, 2)).__floor__())


def _fee(transaction: HoldingTransaction) -> Fraction:
    return Fraction(transaction.fee_minor, 10 ** minor_unit_for(transaction.quote_currency))


def _buy_cost_oracle(transaction: HoldingTransaction) -> int:
    gross = Fraction(transaction.quantity) * Fraction(transaction.unit_price)
    amount = (gross + _fee(transaction)) * Fraction(transaction.fx_to_base)
    return _round_half_up(amount * 10 ** minor_unit_for(BASE))


def _sell_proceeds_oracle(transaction: HoldingTransaction) -> int:
    gross = Fraction(transaction.quantity) * Fraction(transaction.unit_price)
    amount = (gross - _fee(transaction)) * Fraction(transaction.fx_to_base)
    return _round_half_up(amount * 10 ** minor_unit_for(BASE))


@dataclass(slots=True)
class _OracleLot:
    quantity: Fraction
    cost_minor: int


def _oracle_holdings(rows: list[HoldingTransaction], method: str) -> tuple[Fraction, int, int]:
    held = Fraction(0)
    remaining_cost = 0
    realized = 0
    lots: list[_OracleLot] = []
    ordered = sorted(
        rows,
        key=lambda row: (
            row.date is not None,
            row.date or date.min,
            row.created_at,
            str(row.id),
        ),
    )
    for transaction in ordered:
        quantity = Fraction(transaction.quantity)
        if transaction.type == "buy":
            cost = _buy_cost_oracle(transaction)
            held += quantity
            remaining_cost += cost
            if method == "fifo":
                lots.append(_OracleLot(quantity, cost))
            continue
        proceeds = _sell_proceeds_oracle(transaction)
        if method == "fifo":
            removed = 0
            left = quantity
            while left > 0:
                lot = lots[0]
                if left >= lot.quantity:
                    removed += lot.cost_minor
                    left -= lot.quantity
                    lots.pop(0)
                else:
                    part = _round_half_up(Fraction(lot.cost_minor) * left / lot.quantity)
                    removed += part
                    lot.cost_minor -= part
                    lot.quantity -= left
                    break
            held -= quantity
            remaining_cost -= removed
        else:
            removed = (
                remaining_cost
                if quantity == held
                else _round_half_up(Fraction(remaining_cost) * quantity / held)
            )
            held -= quantity
            remaining_cost -= removed
        realized += proceeds - removed
    return held, remaining_cost, realized


@st.composite
def _mixed_history(draw: st.DrawFn) -> tuple[list[HoldingTransaction], Decimal]:
    steps = draw(
        st.lists(
            st.tuples(
                st.decimals(min_value=Decimal("0.001"), max_value=Decimal("100"), places=6),
                st.decimals(min_value=Decimal("0.01"), max_value=Decimal("1000"), places=4),
                st.integers(min_value=0, max_value=10_000),
                st.decimals(min_value=Decimal("0.5"), max_value=Decimal("2"), places=6),
                st.decimals(min_value=Decimal("0.01"), max_value=Decimal("1"), places=2),
                st.decimals(min_value=Decimal("0.01"), max_value=Decimal("1000"), places=4),
                st.integers(min_value=0, max_value=10_000),
                st.decimals(min_value=Decimal("0.5"), max_value=Decimal("2"), places=6),
            ),
            min_size=1,
            max_size=6,
        )
    )
    instrument = uuid4()
    rows: list[HoldingTransaction] = []
    held = Decimal(0)
    for index, (
        buy_quantity,
        buy_price,
        buy_fee,
        buy_fx,
        sell_fraction,
        sell_price,
        sell_fee,
        sell_fx,
    ) in enumerate(steps):
        day = date(2026, 1, 1) + timedelta(days=index)
        created = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=index)
        rows.append(
            _tx(
                day=day,
                type="buy",
                quantity=str(buy_quantity),
                price=str(buy_price),
                fee=buy_fee,
                fx=str(buy_fx),
                instrument_id=instrument,
                created_at=created,
            )
        )
        held += buy_quantity
        sold = held * sell_fraction
        if sold > 0:
            rows.append(
                _tx(
                    day=day,
                    type="sell",
                    quantity=str(sold),
                    price=str(sell_price),
                    fee=sell_fee,
                    fx=str(sell_fx),
                    instrument_id=instrument,
                    created_at=created + timedelta(milliseconds=1),
                )
            )
            held -= sold
    return rows, held


@given(_mixed_history())
@settings(deadline=None, max_examples=100)
def test_mixed_histories_match_the_independent_oracle(
    history: tuple[list[HoldingTransaction], Decimal],
) -> None:
    rows, _held = history
    for method in ("average", "fifo"):
        holding = compute_holdings(rows, method, base_currency=BASE)[0]
        held, remaining, realized = _oracle_holdings(rows, method)
        assert Fraction(holding.quantity) == held
        assert holding.cost_basis_base.amount_minor == remaining
        assert holding.realized_pnl.amount_minor == realized


@given(_mixed_history())
@settings(deadline=None, max_examples=100)
def test_mixed_histories_stay_non_negative_and_keep_the_identity(
    history: tuple[list[HoldingTransaction], Decimal],
) -> None:
    rows, _held = history
    total_bought = sum(_buy_cost_oracle(row) for row in rows if row.type == "buy")
    total_proceeds = sum(_sell_proceeds_oracle(row) for row in rows if row.type == "sell")
    for method in ("average", "fifo"):
        holding = compute_holdings(rows, method, base_currency=BASE)[0]
        assert holding.quantity >= 0
        assert holding.cost_basis_base.amount_minor >= 0
        cost_removed = total_bought - holding.cost_basis_base.amount_minor
        assert holding.realized_pnl.amount_minor + cost_removed == total_proceeds
        assert holding.cost_basis_base.amount_minor == total_bought - cost_removed


@given(_mixed_history())
@settings(deadline=None, max_examples=100)
def test_a_mixed_history_sold_to_zero_has_zero_cost(
    history: tuple[list[HoldingTransaction], Decimal],
) -> None:
    rows, held = history
    if held > 0:
        rows.append(
            _tx(
                day=date(2027, 1, 1),
                type="sell",
                quantity=str(held),
                price="1",
                instrument_id=rows[0].instrument_id,
                created_at=datetime(2027, 1, 1, tzinfo=UTC),
            )
        )
    for method in ("average", "fifo"):
        holding = compute_holdings(rows, method, base_currency=BASE)[0]
        assert holding.quantity == 0
        assert holding.cost_basis_base == Money(0, BASE)


@given(_mixed_history())
@settings(deadline=None, max_examples=100)
def test_realized_events_sum_to_the_holding_realized_pnl(
    history: tuple[list[HoldingTransaction], Decimal],
) -> None:
    rows, _held = history
    sells = [row for row in rows if row.type == "sell"]
    for method in ("average", "fifo"):
        holding = compute_holdings(rows, method, base_currency=BASE)[0]
        events = realized_events(rows, method, base_currency=BASE)
        assert [event.transaction_id for event in events] == [row.id for row in sells]
        assert sum(event.realized.amount_minor for event in events) == (
            holding.realized_pnl.amount_minor
        )


def test_realized_events_name_each_sell_and_its_date() -> None:
    instrument = uuid4()
    buy = _buy(instrument, "10", "10", day=date(2025, 6, 1))
    first = _sell(instrument, "4", "12", day=date(2025, 12, 31))
    second = _sell(instrument, "6", "9", fee=100, day=date(2026, 1, 2))
    events = realized_events([second, buy, first], "average", base_currency=BASE)
    assert events == [
        RealizedEvent(first.id, instrument, date(2025, 12, 31), Money(800, BASE)),
        RealizedEvent(second.id, instrument, date(2026, 1, 2), Money(-700, BASE)),
    ]


def test_a_dated_sell_of_an_undated_lot_has_an_event_and_does_not_oversell() -> None:
    instrument = uuid4()
    legacy = _tx(day=None, type="buy", quantity="5", price="20", instrument_id=instrument)
    sell = _sell(instrument, "5", "30", day=date(2026, 2, 1))
    for method in ("average", "fifo"):
        [event] = realized_events([legacy, sell], method, base_currency=BASE)
        assert (event.date, event.realized) == (date(2026, 2, 1), Money(5000, BASE))
    with pytest.raises(Oversell):  # why the portfolio must not subtract dated holdings
        compute_holdings([legacy, sell], "average", date(2026, 12, 31), base_currency=BASE)


def test_realized_events_cover_every_instrument_and_refuse_a_bad_method() -> None:
    first, second = uuid4(), uuid4()
    rows = [
        _buy(first, "1", "10"),
        _sell(first, "1", "11"),
        _buy(second, "2", "10"),
        _sell(second, "1", "8"),
    ]
    events = realized_events(rows, "fifo", base_currency=BASE)
    assert {event.instrument_id: event.realized for event in events} == {
        first: Money(100, BASE),
        second: Money(-200, BASE),
    }
    assert realized_events([_buy(first, "1", "10")], "average", base_currency=BASE) == []
    with pytest.raises(InvalidHoldingTransaction):
        realized_events(rows, "lifo", base_currency=BASE)


def test_transaction_type_values_are_the_spec_set() -> None:
    assert {kind.value for kind in InvestmentTransactionType} == {
        "buy",
        "sell",
        "dividend",
        "fee",
        "split",
    }
