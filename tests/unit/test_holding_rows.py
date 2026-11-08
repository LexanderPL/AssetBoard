"""The shared stored-transaction → holdings input mapper (F6.2, used by F6.4 and F6.6)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from assetboard.db.models.investments import InvestmentTransaction
from assetboard.domain.holdings import CostBasisMethod
from assetboard.services.holding_rows import cost_basis_method, holding_transaction


def _row(fx: Decimal | None) -> InvestmentTransaction:
    return InvestmentTransaction(
        id=uuid4(),
        instrument_id=uuid4(),
        date=date(2026, 3, 1),
        type="buy",
        quantity=Decimal("1.5"),
        unit_price=Decimal("10.25"),
        fee_minor=99,
        quote_currency="USD",
        fx_to_base=fx,
        created_at=datetime(2026, 3, 1, 12, tzinfo=UTC),
    )


def test_holding_transaction_copies_the_fields_and_defaults_fx_to_one() -> None:
    row = _row(Decimal("4.0123"))
    mapped = holding_transaction(row)
    assert (mapped.id, mapped.instrument_id, mapped.date, mapped.type) == (
        row.id,
        row.instrument_id,
        row.date,
        "buy",
    )
    assert (mapped.quantity, mapped.unit_price, mapped.fee_minor) == (
        Decimal("1.5"),
        Decimal("10.25"),
        99,
    )
    assert (mapped.quote_currency, mapped.fx_to_base, mapped.created_at) == (
        "USD",
        Decimal("4.0123"),
        row.created_at,
    )
    assert holding_transaction(_row(None)).fx_to_base == Decimal(1)


def test_cost_basis_method_falls_back_to_average() -> None:
    assert cost_basis_method(SimpleNamespace(investment_cost_basis="fifo")) is CostBasisMethod.FIFO
    assert cost_basis_method(SimpleNamespace(investment_cost_basis="average")) is (
        CostBasisMethod.AVERAGE
    )
    assert cost_basis_method(SimpleNamespace(investment_cost_basis="lifo")) is (
        CostBasisMethod.AVERAGE
    )
    assert cost_basis_method(object()) is CostBasisMethod.AVERAGE
