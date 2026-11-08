"""Amounts in the base currency (F6.2a): derived quote values and the settlement cost basis.

The oracle is exact ``Decimal`` arithmetic written out in each test (and ``Fraction`` in the
property test), independent of ``domain.holdings`` and ``domain.settlement``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext
from fractions import Fraction
from uuid import UUID, uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from assetboard.domain.holdings import (
    HoldingTransaction,
    InvalidHoldingTransaction,
    compute_holdings,
    realized_events,
    transaction_total,
)
from assetboard.domain.money import Money
from assetboard.domain.settlement import (
    base_values_from_quote,
    derive_quote_values,
    needs_fx_check,
)

BASE = "PLN"
QUOTE = "USD"
_START = datetime(2026, 3, 1, 12, tzinfo=UTC)


def _row(
    instrument_id: UUID,
    kind: str,
    quantity: str,
    *,
    base_price: str | None = None,
    base_fee: int = 0,
    quote_price: str = "0",
    quote_fee: int = 0,
    fx: str = "4.0123",
    day: date = date(2026, 1, 5),
    order: int = 0,
) -> HoldingTransaction:
    """A base-mode row when ``base_price`` is given (quote values derived), else quote mode."""
    if base_price is not None:
        unit_price, fee_minor = derive_quote_values(
            Decimal(base_price), base_fee, Decimal(fx), base_currency=BASE, quote_currency=QUOTE
        )
        return HoldingTransaction(
            id=uuid4(),
            instrument_id=instrument_id,
            date=day,
            type=kind,
            quantity=Decimal(quantity),
            unit_price=unit_price,
            fee_minor=fee_minor,
            quote_currency=QUOTE,
            fx_to_base=Decimal(fx),
            created_at=_START + timedelta(seconds=order),
            settlement_currency=BASE,
            settlement_unit_price=Decimal(base_price),
            settlement_fee_minor=base_fee,
        )
    return HoldingTransaction(
        id=uuid4(),
        instrument_id=instrument_id,
        date=day,
        type=kind,
        quantity=Decimal(quantity),
        unit_price=Decimal(quote_price),
        fee_minor=quote_fee,
        quote_currency=QUOTE,
        fx_to_base=Decimal(fx),
        created_at=_START + timedelta(seconds=order),
    )


def _half_up_minor(value: Decimal) -> int:
    with localcontext() as context:
        context.prec = 80
        return int((value * 100).to_integral_value(rounding=ROUND_HALF_UP))


def test_settlement_golden_buy_4_at_1600_pln_with_fee_5_and_fx_4_0123() -> None:
    instrument = uuid4()
    row = _row(instrument, "buy", "4", base_price="1600.00", base_fee=500)
    with localcontext() as context:
        context.prec = 80
        expected_price = (Decimal("1600.00") / Decimal("4.0123")).quantize(
            Decimal(1).scaleb(-18), rounding=ROUND_HALF_UP
        )
        expected_fee = int(
            (Decimal("5.00") / Decimal("4.0123") * 100).to_integral_value(rounding=ROUND_HALF_UP)
        )
    assert row.unit_price == expected_price
    # The spec example first said 398.774767…; 1600 / 4.0123 is 398.773770655…
    assert str(row.unit_price) == "398.773770655235151908"
    assert row.unit_price.as_tuple().exponent == -18
    assert row.fee_minor == expected_fee == 125
    [holding] = compute_holdings([row], "average", base_currency=BASE)
    assert holding.cost_basis_base == Money(640_500, BASE)
    assert holding.quantity == Decimal(4)
    assert transaction_total(row, BASE) == Money(640_500, BASE)


@pytest.mark.parametrize("method", ["average", "fifo"])
def test_a_partial_sell_and_a_full_sell_of_base_mode_rows(method: str) -> None:
    instrument = uuid4()
    buy_one = _row(instrument, "buy", "3", base_price="100.01", base_fee=101, order=1)
    buy_two = _row(
        instrument, "buy", "2", base_price="110", base_fee=0, fx="4.2", day=date(2026, 1, 6)
    )
    partial = _row(
        instrument,
        "sell",
        "4",
        base_price="120.5",
        base_fee=250,
        fx="4.3",
        day=date(2026, 1, 7),
    )
    rows = [buy_one, buy_two, partial]
    cost_one = _half_up_minor(Decimal(3) * Decimal("100.01") + Decimal("1.01"))
    cost_two = _half_up_minor(Decimal(2) * Decimal(110))
    proceeds = _half_up_minor(Decimal(4) * Decimal("120.5") - Decimal("2.50"))
    if method == "average":
        removed = _half_up_minor(Decimal(cost_one + cost_two) / 100 * Decimal(4) / Decimal(5))
    else:
        removed = cost_one + _half_up_minor(Decimal(cost_two) / 100 * Decimal(1) / Decimal(2))
    [holding] = compute_holdings(rows, method, base_currency=BASE)
    assert holding.quantity == Decimal(1)
    assert holding.cost_basis_base == Money(cost_one + cost_two - removed, BASE)
    assert holding.realized_pnl == Money(proceeds - removed, BASE)
    [event] = realized_events(rows, method, base_currency=BASE)
    assert event.realized == holding.realized_pnl

    rest = _row(instrument, "sell", "1", base_price="1", day=date(2026, 1, 8))
    [closed] = compute_holdings([*rows, rest], method, base_currency=BASE)
    assert closed.quantity == 0
    assert closed.cost_basis_base == Money(0, BASE)


@pytest.mark.parametrize("method", ["average", "fifo"])
def test_mixed_quote_and_base_mode_rows_on_one_instrument(method: str) -> None:
    instrument = uuid4()
    quote_buy = _row(instrument, "buy", "2", quote_price="400", quote_fee=150, fx="4", order=1)
    base_buy = _row(instrument, "buy", "2", base_price="1650", base_fee=700, day=date(2026, 1, 6))
    quote_cost = _half_up_minor((Decimal(2) * Decimal(400) + Decimal("1.50")) * Decimal(4))
    base_cost = _half_up_minor(Decimal(2) * Decimal(1650) + Decimal("7.00"))
    [holding] = compute_holdings([quote_buy, base_buy], method, base_currency=BASE)
    assert holding.cost_basis_base == Money(quote_cost + base_cost, BASE)
    sell = _row(instrument, "sell", "4", base_price="1700", day=date(2026, 1, 9))
    [closed] = compute_holdings([quote_buy, base_buy, sell], method, base_currency=BASE)
    assert closed.cost_basis_base == Money(0, BASE)
    assert closed.realized_pnl == Money(
        _half_up_minor(Decimal(4) * Decimal(1700)) - quote_cost - base_cost, BASE
    )


def test_a_settlement_must_be_complete_and_in_the_base_currency() -> None:
    instrument = uuid4()
    row = _row(instrument, "buy", "1", base_price="10")
    partial = HoldingTransaction(
        id=row.id,
        instrument_id=instrument,
        date=row.date,
        type="buy",
        quantity=row.quantity,
        unit_price=row.unit_price,
        fee_minor=row.fee_minor,
        quote_currency=QUOTE,
        fx_to_base=row.fx_to_base,
        created_at=row.created_at,
        settlement_currency=BASE,
        settlement_unit_price=Decimal(10),
    )
    with pytest.raises(InvalidHoldingTransaction) as caught:
        compute_holdings([partial], "average", base_currency=BASE)
    assert caught.value.code == "invalid_settlement"
    with pytest.raises(InvalidHoldingTransaction):
        compute_holdings([row], "average", base_currency="EUR")


def test_quote_to_base_switch_values_and_the_fx_check_flag() -> None:
    price, fee = base_values_from_quote(
        Decimal("398.5"), 125, Decimal("4.0123"), base_currency=BASE, quote_currency=QUOTE
    )
    assert price == Decimal("1598.90155").quantize(Decimal(1).scaleb(-18))
    assert fee == _half_up_minor(Decimal("1.25") * Decimal("4.0123")) == 502
    with pytest.raises(ValueError):
        derive_quote_values(Decimal(1), 0, Decimal(0), base_currency=BASE, quote_currency=QUOTE)
    assert needs_fx_check(quote_currency="USD", base_currency="PLN", fx_to_base=Decimal("1.000"))
    assert not needs_fx_check(quote_currency="USD", base_currency="PLN", fx_to_base=Decimal("4"))
    assert not needs_fx_check(quote_currency="PLN", base_currency="PLN", fx_to_base=Decimal(1))


_prices = st.decimals(min_value=Decimal("0"), max_value=Decimal("100000"), places=4)
_quantities = st.decimals(min_value=Decimal("0.0001"), max_value=Decimal("1000"), places=4)
_rates = st.decimals(min_value=Decimal("0.0001"), max_value=Decimal("1000"), places=6)
_fees = st.integers(min_value=0, max_value=100_000)


@given(
    st.lists(st.tuples(_quantities, _prices, _fees, _rates), min_size=1, max_size=8),
    st.sampled_from(["average", "fifo"]),
)
@settings(deadline=None, max_examples=100)
def test_base_mode_cost_basis_is_the_sum_of_rounded_settlement_totals_independent_of_fx(
    buys: list[tuple[Decimal, Decimal, int, Decimal]], method: str
) -> None:
    instrument = uuid4()
    rows = [
        _row(
            instrument,
            "buy",
            str(quantity),
            base_price=str(price),
            base_fee=fee,
            fx=str(rate),
            order=index,
        )
        for index, (quantity, price, fee, rate) in enumerate(buys)
    ]
    expected = 0
    for quantity, price, fee, _rate in buys:
        exact = Fraction(quantity) * Fraction(price) * 100 + fee
        floor = exact.numerator // exact.denominator
        expected += floor + (1 if exact - floor >= Fraction(1, 2) else 0)
    [holding] = compute_holdings(rows, method, base_currency=BASE)
    assert holding.cost_basis_base == Money(expected, BASE)
    # The same rows at a different rate give the same cost basis.
    other = [
        _row(
            instrument,
            "buy",
            str(quantity),
            base_price=str(price),
            base_fee=fee,
            fx="7.5",
            order=index,
        )
        for index, (quantity, price, fee, _rate) in enumerate(buys)
    ]
    [same] = compute_holdings(other, method, base_currency=BASE)
    assert same.cost_basis_base == holding.cost_basis_base
