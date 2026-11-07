"""Investment holdings and cost basis (F6.2). Pure: no I/O, no database and no clock.

A holding is computed from the instrument's live buy/sell history. Undated lots (imported
legacy lots, F6.9) count in current holdings but are excluded from a dated ``as_of`` and from
time series. Buys add cost in the workspace base currency; sells remove cost and realize P&L.

Average method: buys move the average purchase cost; a sell removes
``round_half_up(remaining_cost × quantity_sold / remaining_quantity)`` and keeps the displayed
average purchase cost unchanged. FIFO: each buy is a lot; a sell consumes the oldest lots, and
only a partly used lot is rounded once. Every per-transaction amount is rounded half-up to the
base currency's minor unit exactly once.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, localcontext
from enum import StrEnum
from uuid import UUID

from assetboard.domain.money import Money, minor_unit_for

# Enough room for 18-decimal quantities and prices multiplied together, without float.
_DECIMAL_PRECISION = 80
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

    def __init__(self, on: date | None) -> None:
        super().__init__("oversell")
        self.on = on


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


@dataclass(frozen=True, slots=True)
class Holding:
    instrument_id: UUID
    quantity: Decimal
    cost_basis_base: Money
    avg_cost: Decimal  # base currency per unit
    realized_pnl: Money


@dataclass(slots=True)
class _Lot:
    quantity: Decimal
    cost_minor: int


def _type(value: InvestmentTransactionType | str) -> InvestmentTransactionType:
    try:
        return InvestmentTransactionType(value)
    except ValueError:
        raise InvalidHoldingTransaction("invalid_type") from None


def _validate(transaction: HoldingTransaction, base_currency: str) -> None:
    if _type(transaction.type) not in (
        InvestmentTransactionType.BUY,
        InvestmentTransactionType.SELL,
    ):
        raise InvalidHoldingTransaction("unsupported_type")
    if type(transaction.quantity) is not Decimal or not transaction.quantity.is_finite():
        raise InvalidHoldingTransaction("invalid_quantity")
    if transaction.quantity <= 0:
        raise InvalidHoldingTransaction("invalid_quantity")
    if type(transaction.unit_price) is not Decimal or not transaction.unit_price.is_finite():
        raise InvalidHoldingTransaction("invalid_price")
    if transaction.unit_price < 0:
        raise InvalidHoldingTransaction("invalid_price")
    if type(transaction.fee_minor) is not int or transaction.fee_minor < 0:
        raise InvalidHoldingTransaction("invalid_fee")
    if type(transaction.fx_to_base) is not Decimal or not transaction.fx_to_base.is_finite():
        raise InvalidHoldingTransaction("invalid_fx")
    if transaction.fx_to_base <= 0:
        raise InvalidHoldingTransaction("invalid_fx")
    minor_unit_for(transaction.quote_currency)
    minor_unit_for(base_currency)


def _order_key(transaction: HoldingTransaction) -> tuple[bool, date, datetime, str]:
    # Undated lots come first; they exist only for imported legacy history.
    return (
        transaction.date is not None,
        transaction.date or date.min,
        transaction.created_at,
        str(transaction.id),
    )


def _fee_decimal(fee_minor: int, quote_currency: str) -> Decimal:
    return Decimal(fee_minor).scaleb(-minor_unit_for(quote_currency))


def _base_minor(amount: Decimal, base_currency: str) -> int:
    with localcontext() as context:
        context.prec = max(context.prec, _DECIMAL_PRECISION)
        return Money.from_decimal(amount, base_currency).amount_minor


def _buy_cost_minor(transaction: HoldingTransaction, base_currency: str) -> int:
    with localcontext() as context:
        context.prec = max(context.prec, _DECIMAL_PRECISION)
        gross = transaction.quantity * transaction.unit_price
        amount = (gross + _fee_decimal(transaction.fee_minor, transaction.quote_currency)) * (
            transaction.fx_to_base
        )
    return _base_minor(amount, base_currency)


def _sell_proceeds_minor(transaction: HoldingTransaction, base_currency: str) -> int:
    with localcontext() as context:
        context.prec = max(context.prec, _DECIMAL_PRECISION)
        gross = transaction.quantity * transaction.unit_price
        amount = (gross - _fee_decimal(transaction.fee_minor, transaction.quote_currency)) * (
            transaction.fx_to_base
        )
    return _base_minor(amount, base_currency)


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
) -> Holding:
    with localcontext() as context:
        context.prec = max(context.prec, _DECIMAL_PRECISION)
        return _compute_instrument_rows(transactions, method, base_currency=base_currency)


def _compute_instrument_rows(
    transactions: list[HoldingTransaction],
    method: CostBasisMethod,
    *,
    base_currency: str,
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

        if transaction.quantity > quantity:
            raise Oversell(transaction.date)
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
        quantity=quantity,
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
    try:
        wanted_method = CostBasisMethod(method)
    except ValueError:
        raise InvalidHoldingTransaction("invalid_method") from None
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
