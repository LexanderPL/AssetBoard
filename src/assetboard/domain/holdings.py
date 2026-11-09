"""Investment holdings and cost basis (F6.2). Pure: no I/O, no database and no clock.

A holding is computed from the instrument's live buy/sell history. Undated lots (imported
legacy lots, F6.9) count in current holdings but are excluded from a dated ``as_of`` and from
time series. Buys add cost in the workspace base currency; sells remove cost and realize P&L.

Average method: buys move the average purchase cost; a sell removes
``round_half_up(remaining_cost × quantity_sold / remaining_quantity)`` and keeps the displayed
average purchase cost unchanged. FIFO: each buy is a lot; a sell consumes the oldest lots, and
only a partly used lot is rounded once. Every per-transaction amount is rounded half-up to the
base currency's minor unit exactly once.

Settlement (F6.2a): a row entered in the base currency carries ``settlement_unit_price`` and
``settlement_fee_minor`` in ``settlement_currency`` (the base). Its buy cost is then
``round_half_up(quantity × settlement_unit_price + settlement_fee)`` and its sell proceeds
``round_half_up(quantity × settlement_unit_price − settlement_fee)``, without the FX rate; the
broker's FX spread is part of what left the account.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, localcontext
from enum import StrEnum
from uuid import UUID

from assetboard.domain.money import Money, minor_unit_for

# Enough room for 18-decimal quantities and prices multiplied together, without float.
_DECIMAL_PRECISION = 80
_DECIMAL_QUANTUM = Decimal("1e-18")
MAX_NOTE_LENGTH = 200


class CostBasisMethod(StrEnum):
    AVERAGE = "average"
    FIFO = "fifo"


class InvestmentTransactionType(StrEnum):
    BUY = "buy"
    SELL = "sell"
    DIVIDEND = "dividend"
    FEE = "fee"
    SPLIT = "split"


class InvalidHoldingTransaction(ValueError):
    """A transaction cannot be used in holdings; ``code`` is a stable token for the form."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Oversell(ValueError):
    """A sell exceeds the quantity held at its point in the ordered history."""

    def __init__(self, on: date | None, transaction_id: UUID | None = None) -> None:
        super().__init__("oversell")
        self.on = on
        self.transaction_id = transaction_id


@dataclass(frozen=True, slots=True)
class HoldingTransaction:
    """The fields holdings need; services build these from their rows."""

    id: UUID
    instrument_id: UUID
    date: date | None
    type: InvestmentTransactionType | str
    quantity: Decimal
    unit_price: Decimal
    fee_minor: int
    quote_currency: str
    fx_to_base: Decimal
    created_at: datetime
    # F6.2a: all three set (amounts entered in the base currency) or all None.
    settlement_currency: str | None = None
    settlement_unit_price: Decimal | None = None
    settlement_fee_minor: int | None = None

    @property
    def settled(self) -> bool:
        return self.settlement_currency is not None


@dataclass(frozen=True, slots=True)
class Holding:
    instrument_id: UUID
    quantity: Decimal
    cost_basis_base: Money
    avg_cost: Decimal  # base currency per unit
    realized_pnl: Money


@dataclass(frozen=True, slots=True)
class DividendAmounts:
    gross: Money
    withholding: Money
    net: Money


@dataclass(frozen=True, slots=True)
class RealizedEvent:
    """The P&L one sell realized, in the base currency (rounded exactly as in ``Holding``)."""

    transaction_id: UUID
    instrument_id: UUID
    date: date | None
    realized: Money


@dataclass(slots=True)
class _Lot:
    quantity: Decimal
    cost_minor: int


def _type(value: InvestmentTransactionType | str) -> InvestmentTransactionType:
    try:
        return InvestmentTransactionType(value)
    except ValueError:
        raise InvalidHoldingTransaction("invalid_type") from None


def _validate_amount_precision(amount: Decimal, currency: str) -> None:
    minor_unit = minor_unit_for(currency)
    quantum = Decimal(1).scaleb(-minor_unit)
    with localcontext() as context:
        context.prec = max(context.prec, _DECIMAL_PRECISION)
        if amount != amount.quantize(quantum):
            raise InvalidHoldingTransaction("invalid_amount_precision")


def _validate(transaction: HoldingTransaction, base_currency: str) -> None:
    kind = _type(transaction.type)
    if kind not in (
        InvestmentTransactionType.BUY,
        InvestmentTransactionType.SELL,
        InvestmentTransactionType.DIVIDEND,
        InvestmentTransactionType.SPLIT,
    ):
        raise InvalidHoldingTransaction("unsupported_type")
    if type(transaction.quantity) is not Decimal or not transaction.quantity.is_finite():
        raise InvalidHoldingTransaction("invalid_quantity")
    if kind is InvestmentTransactionType.DIVIDEND:
        if transaction.quantity != 1:
            raise InvalidHoldingTransaction("invalid_quantity")
    elif transaction.quantity <= 0:
        raise InvalidHoldingTransaction("invalid_quantity")
    if type(transaction.unit_price) is not Decimal or not transaction.unit_price.is_finite():
        raise InvalidHoldingTransaction("invalid_price")
    if kind is InvestmentTransactionType.DIVIDEND:
        if transaction.unit_price <= 0:
            raise InvalidHoldingTransaction("invalid_price")
    elif transaction.unit_price < 0:
        raise InvalidHoldingTransaction("invalid_price")
    if type(transaction.fee_minor) is not int or transaction.fee_minor < 0:
        raise InvalidHoldingTransaction("invalid_fee")
    if type(transaction.fx_to_base) is not Decimal or not transaction.fx_to_base.is_finite():
        raise InvalidHoldingTransaction("invalid_fx")
    if transaction.fx_to_base <= 0:
        raise InvalidHoldingTransaction("invalid_fx")
    minor_unit_for(transaction.quote_currency)
    minor_unit_for(base_currency)
    if kind is InvestmentTransactionType.DIVIDEND:
        _validate_amount_precision(transaction.unit_price, transaction.quote_currency)
        gross_minor = int(transaction.unit_price.scaleb(minor_unit_for(transaction.quote_currency)))
        if transaction.fee_minor > gross_minor:
            raise InvalidHoldingTransaction("invalid_fee")
        if any(
            value is not None
            for value in (
                transaction.settlement_currency,
                transaction.settlement_unit_price,
                transaction.settlement_fee_minor,
            )
        ):
            raise InvalidHoldingTransaction("invalid_settlement")
        return
    if kind is InvestmentTransactionType.SPLIT:
        if transaction.unit_price <= 0 or transaction.quantity == transaction.unit_price:
            raise InvalidHoldingTransaction("invalid_split")
        if transaction.fee_minor != 0:
            raise InvalidHoldingTransaction("invalid_fee")
        if transaction.fx_to_base != 1:
            raise InvalidHoldingTransaction("invalid_fx")
        if any(
            value is not None
            for value in (
                transaction.settlement_currency,
                transaction.settlement_unit_price,
                transaction.settlement_fee_minor,
            )
        ):
            raise InvalidHoldingTransaction("invalid_settlement")
        return
    _validate_settlement(transaction, base_currency)


def _validate_settlement(transaction: HoldingTransaction, base_currency: str) -> None:
    values = (
        transaction.settlement_currency,
        transaction.settlement_unit_price,
        transaction.settlement_fee_minor,
    )
    if all(value is None for value in values):
        return
    if any(value is None for value in values):
        raise InvalidHoldingTransaction("invalid_settlement")
    if transaction.settlement_currency != base_currency:
        raise InvalidHoldingTransaction("invalid_settlement")
    price = transaction.settlement_unit_price
    if type(price) is not Decimal or not price.is_finite() or price < 0:
        raise InvalidHoldingTransaction("invalid_settlement")
    fee = transaction.settlement_fee_minor
    if type(fee) is not int or fee < 0:
        raise InvalidHoldingTransaction("invalid_settlement")


def _order_key(transaction: HoldingTransaction) -> tuple[bool, date, datetime, str]:
    # Undated lots come first; they exist only for imported legacy history.
    return (
        transaction.date is not None,
        transaction.date or date.min,
        transaction.created_at,
        str(transaction.id),
    )


def split_factor(
    splits: Iterable[HoldingTransaction],
    from_date: date,
    to_date: date,
) -> Decimal:
    """The cumulative old/new factor for splits in ``(from_date, to_date]``.

    A raw price per unit observed at ``from_date`` is multiplied by this factor when it is
    carried forward to ``to_date``. ``quantity`` is the new units and ``unit_price`` the old
    units of a split, so the factor is ``unit_price / quantity`` (a 2-for-1 split is 0.5).
    """
    factor = Decimal(1)
    with localcontext() as context:
        context.prec = max(context.prec, _DECIMAL_PRECISION)
        for row in splits:
            if InvestmentTransactionType(row.type) is not InvestmentTransactionType.SPLIT:
                continue
            if row.date is None or not (from_date < row.date <= to_date):
                continue
            factor *= row.unit_price / row.quantity
    return factor


def quantize_quantity(value: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = max(context.prec, _DECIMAL_PRECISION)
        return value.quantize(_DECIMAL_QUANTUM, rounding=ROUND_HALF_UP)


def _fee_decimal(fee_minor: int, quote_currency: str) -> Decimal:
    return Decimal(fee_minor).scaleb(-minor_unit_for(quote_currency))


def _base_minor(amount: Decimal, base_currency: str) -> int:
    with localcontext() as context:
        context.prec = max(context.prec, _DECIMAL_PRECISION)
        return Money.from_decimal(amount, base_currency).amount_minor


def _settlement_amount(transaction: HoldingTransaction, sign: int) -> Decimal:
    assert transaction.settlement_unit_price is not None
    assert transaction.settlement_fee_minor is not None
    assert transaction.settlement_currency is not None
    with localcontext() as context:
        context.prec = max(context.prec, _DECIMAL_PRECISION)
        gross = transaction.quantity * transaction.settlement_unit_price
        fee = _fee_decimal(transaction.settlement_fee_minor, transaction.settlement_currency)
        return gross + sign * fee


def _buy_cost_minor(transaction: HoldingTransaction, base_currency: str) -> int:
    if transaction.settled:
        return _base_minor(_settlement_amount(transaction, 1), base_currency)
    with localcontext() as context:
        context.prec = max(context.prec, _DECIMAL_PRECISION)
        gross = transaction.quantity * transaction.unit_price
        amount = (gross + _fee_decimal(transaction.fee_minor, transaction.quote_currency)) * (
            transaction.fx_to_base
        )
    return _base_minor(amount, base_currency)


def _sell_proceeds_minor(transaction: HoldingTransaction, base_currency: str) -> int:
    if transaction.settled:
        return _base_minor(_settlement_amount(transaction, -1), base_currency)
    with localcontext() as context:
        context.prec = max(context.prec, _DECIMAL_PRECISION)
        gross = transaction.quantity * transaction.unit_price
        amount = (gross - _fee_decimal(transaction.fee_minor, transaction.quote_currency)) * (
            transaction.fx_to_base
        )
    return _base_minor(amount, base_currency)


def transaction_total(transaction: HoldingTransaction, base_currency: str) -> Money:
    """A buy's total cost or a sell's proceeds in the base currency, rounded once (F6.2/F6.2a).

    Proceeds may be negative when a sell fee exceeds the gross amount.
    """
    _validate(transaction, base_currency)
    kind = _type(transaction.type)
    if kind is InvestmentTransactionType.BUY:
        return Money(_buy_cost_minor(transaction, base_currency), base_currency)
    if kind is InvestmentTransactionType.SELL:
        return Money(_sell_proceeds_minor(transaction, base_currency), base_currency)
    raise InvalidHoldingTransaction("unsupported_type")


def dividend_amounts(transaction: HoldingTransaction, base_currency: str) -> DividendAmounts:
    """Gross, withholding and net in the base currency, each rounded half-up once."""
    _validate(transaction, base_currency)
    if _type(transaction.type) is not InvestmentTransactionType.DIVIDEND:
        raise InvalidHoldingTransaction("unsupported_type")
    with localcontext() as context:
        context.prec = max(context.prec, _DECIMAL_PRECISION)
        gross = Money.from_decimal(
            transaction.quantity * transaction.unit_price * transaction.fx_to_base,
            base_currency,
        )
        withholding_quote = Decimal(transaction.fee_minor).scaleb(
            -minor_unit_for(transaction.quote_currency)
        )
        withholding = Money.from_decimal(withholding_quote * transaction.fx_to_base, base_currency)
    return DividendAmounts(
        gross=gross,
        withholding=withholding,
        net=Money(gross.amount_minor - withholding.amount_minor, base_currency),
    )


def _rounded_cost(remaining_cost_minor: int, quantity: Decimal, sold: Decimal) -> int:
    with localcontext() as context:
        context.prec = max(context.prec, _DECIMAL_PRECISION)
        exact = Decimal(remaining_cost_minor) * sold / quantity
        rounded = exact.to_integral_value(rounding=ROUND_HALF_UP)
    return int(rounded)


def _compute_instrument(
    transactions: list[HoldingTransaction],
    method: CostBasisMethod,
    *,
    base_currency: str,
    events: list[RealizedEvent] | None = None,
) -> Holding:
    with localcontext() as context:
        context.prec = max(context.prec, _DECIMAL_PRECISION)
        return _compute_instrument_rows(
            transactions, method, base_currency=base_currency, events=events
        )


def _compute_instrument_rows(
    transactions: list[HoldingTransaction],
    method: CostBasisMethod,
    *,
    base_currency: str,
    events: list[RealizedEvent] | None = None,
) -> Holding:
    instrument_id = transactions[0].instrument_id
    quantity = Decimal(0)
    remaining_cost_minor = 0
    realized_minor = 0
    average_cost = Decimal(0)
    lots: list[_Lot] = []

    for transaction in sorted(transactions, key=_order_key):
        _validate(transaction, base_currency)
        kind = _type(transaction.type)
        if kind is InvestmentTransactionType.BUY:
            cost_minor = _buy_cost_minor(transaction, base_currency)
            quantity += transaction.quantity
            remaining_cost_minor += cost_minor
            if method is CostBasisMethod.FIFO:
                lots.append(_Lot(transaction.quantity, cost_minor))
            else:
                with localcontext() as context:
                    context.prec = max(context.prec, _DECIMAL_PRECISION)
                    average_cost = (
                        Decimal(remaining_cost_minor).scaleb(-minor_unit_for(base_currency))
                        / quantity
                    )
            continue

        if kind is InvestmentTransactionType.DIVIDEND:
            continue

        if kind is InvestmentTransactionType.SPLIT:
            with localcontext() as context:
                context.prec = max(context.prec, _DECIMAL_PRECISION)
                ratio = transaction.quantity / transaction.unit_price
                if method is CostBasisMethod.FIFO:
                    for lot in lots:
                        lot.quantity = quantize_quantity(lot.quantity * ratio)
                    quantity = sum((lot.quantity for lot in lots), Decimal(0))
                else:
                    quantity = quantize_quantity(quantity * ratio)
                if quantity > 0:
                    average_cost = (
                        Decimal(remaining_cost_minor).scaleb(-minor_unit_for(base_currency))
                        / quantity
                    )
                else:
                    average_cost = Decimal(0)
            continue

        if transaction.quantity > quantity:
            raise Oversell(transaction.date, transaction.id)
        proceeds_minor = _sell_proceeds_minor(transaction, base_currency)
        if method is CostBasisMethod.FIFO:
            cost_removed_minor = _consume_lots(lots, transaction.quantity)
            quantity -= transaction.quantity
            remaining_cost_minor -= cost_removed_minor
        else:
            if transaction.quantity == quantity:
                cost_removed_minor = remaining_cost_minor
            else:
                cost_removed_minor = _rounded_cost(
                    remaining_cost_minor, quantity, transaction.quantity
                )
            quantity -= transaction.quantity
            remaining_cost_minor -= cost_removed_minor
            if quantity == 0:
                average_cost = Decimal(0)
            # The displayed average purchase cost is unchanged by a partial sale.
        realized_minor += proceeds_minor - cost_removed_minor
        if events is not None:
            events.append(
                RealizedEvent(
                    transaction_id=transaction.id,
                    instrument_id=instrument_id,
                    date=transaction.date,
                    realized=Money(proceeds_minor - cost_removed_minor, base_currency),
                )
            )

        if method is CostBasisMethod.FIFO:
            if quantity == 0:
                average_cost = Decimal(0)
                lots.clear()
            else:
                with localcontext() as context:
                    context.prec = max(context.prec, _DECIMAL_PRECISION)
                    average_cost = (
                        Decimal(remaining_cost_minor).scaleb(-minor_unit_for(base_currency))
                        / quantity
                    )

    if remaining_cost_minor < 0:
        # Defensive: rounding must never make a position owe cost.
        remaining_cost_minor = 0
    if quantity == 0:
        remaining_cost_minor = 0
        average_cost = Decimal(0)
    return Holding(
        instrument_id=instrument_id,
        quantity=quantize_quantity(quantity),
        cost_basis_base=Money(remaining_cost_minor, base_currency),
        avg_cost=average_cost,
        realized_pnl=Money(realized_minor, base_currency),
    )


def _consume_lots(lots: list[_Lot], sold: Decimal) -> int:
    """Consume ``sold`` from the oldest lots; a partial lot is rounded once."""
    removed = 0
    remaining = sold
    while remaining > 0:
        lot = lots[0]
        if remaining >= lot.quantity:
            removed += lot.cost_minor
            remaining -= lot.quantity
            lots.pop(0)
            continue
        with localcontext() as context:
            context.prec = max(context.prec, _DECIMAL_PRECISION)
            exact = Decimal(lot.cost_minor) * remaining / lot.quantity
            part = int(exact.to_integral_value(rounding=ROUND_HALF_UP))
        removed += part
        lot.cost_minor -= part
        lot.quantity -= remaining
        break
    return removed


def _wanted_method(method: CostBasisMethod | str) -> CostBasisMethod:
    try:
        return CostBasisMethod(method)
    except ValueError:
        raise InvalidHoldingTransaction("invalid_method") from None


def compute_holdings(
    transactions: list[HoldingTransaction],
    method: CostBasisMethod | str,
    as_of: date | None = None,
    *,
    base_currency: str,
) -> list[Holding]:
    """Holdings per instrument after applying ``transactions`` up to ``as_of``.

    ``as_of=None`` means current holdings and includes undated lots; a concrete date excludes
    every undated lot. Raises ``Oversell`` at the first sell that exceeds the quantity held.
    """
    wanted_method = _wanted_method(method)
    by_instrument: dict[UUID, list[HoldingTransaction]] = {}
    for transaction in transactions:
        if as_of is not None and (transaction.date is None or transaction.date > as_of):
            continue
        by_instrument.setdefault(transaction.instrument_id, []).append(transaction)
    holdings = [
        _compute_instrument(rows, wanted_method, base_currency=base_currency)
        for rows in by_instrument.values()
    ]
    return sorted(holdings, key=lambda holding: str(holding.instrument_id))


def realized_events(
    transactions: list[HoldingTransaction],
    method: CostBasisMethod | str,
    *,
    base_currency: str,
) -> list[RealizedEvent]:
    """Every sell's realized P&L, from the same ordered pass as ``compute_holdings``.

    The whole history is used (undated legacy lots included), so a dated sell that consumes an
    undated lot is costed exactly as in the current holdings. The events of one instrument sum
    to its ``Holding.realized_pnl``. Callers pick a period by the events' dates (an undated sell
    belongs to no period). Ordered by instrument, then by the holdings order of each instrument.
    """
    wanted_method = _wanted_method(method)
    by_instrument: dict[UUID, list[HoldingTransaction]] = {}
    for transaction in transactions:
        by_instrument.setdefault(transaction.instrument_id, []).append(transaction)
    events: list[RealizedEvent] = []
    for instrument_id in sorted(by_instrument, key=str):
        _compute_instrument(
            by_instrument[instrument_id],
            wanted_method,
            base_currency=base_currency,
            events=events,
        )
    return events
