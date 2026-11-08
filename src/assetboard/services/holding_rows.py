"""Shared mapping from stored investment transactions to the pure holdings input (F6.2).

The holdings domain (``domain.holdings``) never sees ORM rows. Every service that computes
holdings maps its rows here, so the instrument page, the price refresh and the portfolio page
agree on the inputs (the F6.2a settlement amounts included): a missing ``fx_to_base`` (a
base-currency trade) is 1, and an unknown cost basis setting falls back to the average method.
"""

from __future__ import annotations

from decimal import Decimal

from assetboard.db.models.investments import InvestmentTransaction
from assetboard.domain.holdings import CostBasisMethod, HoldingTransaction


def holding_transaction(row: InvestmentTransaction) -> HoldingTransaction:
    """The holdings view of one stored investment transaction."""
    return HoldingTransaction(
        id=row.id,
        instrument_id=row.instrument_id,
        date=row.date,
        type=row.type,
        quantity=row.quantity,
        unit_price=row.unit_price,
        fee_minor=row.fee_minor,
        quote_currency=row.quote_currency,
        fx_to_base=row.fx_to_base if row.fx_to_base is not None else Decimal(1),
        created_at=row.created_at,
        settlement_currency=row.settlement_currency,
        settlement_unit_price=row.settlement_unit_price,
        settlement_fee_minor=row.settlement_fee_minor,
    )


def cost_basis_method(workspace: object) -> CostBasisMethod:
    """The workspace's cost basis method; the average method when unset or unknown."""
    value = getattr(workspace, "investment_cost_basis", "average")
    try:
        return CostBasisMethod(value)
    except ValueError:
        return CostBasisMethod.AVERAGE
