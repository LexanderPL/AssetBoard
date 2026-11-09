"""Amounts in the base currency (F6.2a): service writes, refusals, FX pre-fill."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from assetboard.clock import FixedClock
from assetboard.db.models.identity import AuditEvent
from assetboard.db.models.investments import InvestmentTransaction
from assetboard.db.repositories import FxRateRepository, InvestmentTransactionRepository, NotFound
from assetboard.domain.money import Money
from assetboard.services.context import Context
from assetboard.services.investment_transactions import (
    InvalidInvestmentTransaction,
    InvestmentTransactionView,
    create_transaction,
    get_transaction,
    instrument_fx_currency,
    instrument_history,
    update_transaction,
)
from assetboard.services.investments import create_asset_class, create_instrument
from assetboard.services.prices import fx_rate, latest_prices
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


class TickingClock(FixedClock):
    def __init__(self) -> None:
        super().__init__("2026-03-01T12:00:00+00:00")
        self._ticks = 0

    def now(self) -> datetime:
        self._ticks += 1
        return super().now() + timedelta(seconds=self._ticks)


@pytest.fixture
def team(tmp_path: Path) -> Iterator[WorkspaceDb]:
    for prepared in workspace_db(tmp_path):
        clock = TickingClock()
        prepared.clock = clock
        prepared.factory.kw["info"]["clock"] = clock
        prepared.add_member("ed", "editor")
        prepared.add_member("vic", "viewer")
        yield prepared


def _instrument(
    db: WorkspaceDb,
    ctx: Context,
    *,
    name: str = "US ETF",
    type: str = "etf",
    quote_currency: str = "USD",
) -> UUID:
    stocks = create_asset_class(
        db.factory, ctx, name=f"Class {name}", color="#2563eb", investable=True
    )
    return create_instrument(
        db.factory,
        ctx,
        name=name,
        symbol="",
        type=type,
        quote_currency=quote_currency,
        unit_label="share",
        allocations=[(stocks.id, 10_000)],
        locale="en",
    ).id


def _tx(
    db: WorkspaceDb,
    ctx: Context,
    instrument_id: UUID,
    *,
    amounts: str = "base",
    type_value: str = "buy",
    date_value: str = "2026-01-10",
    quantity: str = "4",
    unit_price: str = "",
    fee: str = "0",
    base_price: str = "1600.00",
    base_fee: str = "5.00",
    fx: str = "4.0123",
) -> InvestmentTransactionView:
    return create_transaction(
        db.factory,
        ctx,
        instrument_id=instrument_id,
        type_value=type_value,
        date_value=date_value,
        quantity_value=quantity,
        unit_price_value=unit_price,
        fee_value=fee,
        fx_value=fx,
        note_value="",
        locale="en",
        amounts_value=amounts,
        base_unit_price_value=base_price,
        base_fee_value=base_fee,
    )


def _stored_rate(
    db: WorkspaceDb, ctx: Context, quote: str, day: date, rate: str, source: str = "ecb"
) -> None:
    with db.factory.begin() as session:
        FxRateRepository(session, ctx).create(
            base_currency="EUR", quote_currency=quote, date=day, rate=Decimal(rate), source=source
        )


@pytest.mark.parametrize(
    ("currency", "expected"),
    [
        ("USD", Decimal("398.77")),
        ("PLN", Decimal("398.77")),
        ("EUR", Decimal("398.77")),
        ("JPY", Decimal("399")),
        ("BTC", Decimal("398.77377066")),
    ],
)
def test_derived_price_display_uses_the_quote_currencys_minor_unit(
    currency: str, expected: Decimal
) -> None:
    view = InvestmentTransactionView(
        id=uuid4(),
        instrument_id=uuid4(),
        instrument_name="Fund",
        instrument_symbol=None,
        instrument_type="etf",
        unit_label="share",
        quote_currency=currency,
        date=None,
        type="buy",
        quantity=Decimal(1),
        unit_price=Decimal("398.773770655235151908"),
        fee=Money(0, currency),
        fx_to_base=Decimal(1),
        note="",
        instrument_archived=False,
    )
    assert view.derived_price_display == expected


def test_base_mode_stores_the_settlement_and_derived_quote_values(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    usd = _instrument(team, ctx)
    view = _tx(team, ctx, usd)
    with localcontext() as context:
        context.prec = 80
        expected_price = (Decimal("1600.00") / Decimal("4.0123")).quantize(
            Decimal(1).scaleb(-18), rounding=ROUND_HALF_UP
        )
    assert view.amounts == "base"
    assert view.settlement_unit_price == Decimal("1600.00")
    assert view.settlement_fee == Money(500, "EUR")
    assert view.unit_price == expected_price
    assert view.fee == Money(125, "USD")
    assert view.fx_to_base == Decimal("4.0123")
    assert view.derived_price_display == Decimal("398.77")
    with team.factory() as session:
        row = InvestmentTransactionRepository(session, ctx).get(view.id)
        assert row is not None
        assert (row.settlement_currency, row.settlement_fee_minor) == ("EUR", 500)
    history = instrument_history(team.factory, ctx, usd, locale="en")
    assert history.holding.cost_basis_base == Money(640_500, "EUR")


def test_f63_transaction_price_and_fx_of_a_base_mode_row_are_the_derived_values(
    team: WorkspaceDb,
) -> None:
    ctx = team.ctx("ed", "editor")
    usd = _instrument(team, ctx)
    view = _tx(team, ctx, usd)
    price = latest_prices(team.factory, ctx, (usd,), date(2026, 1, 10))[usd]
    assert price.source == "transaction"
    assert price.price == view.unit_price != Decimal("1600.00")
    rate = fx_rate(team.factory, ctx, "USD", "EUR", date(2026, 1, 10))
    assert rate.rate == Decimal("4.0123")
    assert rate.source == "transaction"


@pytest.mark.parametrize(
    ("quote", "type_value"),
    [("EUR", "etf"), ("USD", "currency")],
)
def test_a_settlement_on_a_base_quoted_or_currency_instrument_is_refused(
    team: WorkspaceDb, quote: str, type_value: str
) -> None:
    ctx = team.ctx("ed", "editor")
    item = _instrument(
        team, ctx, name=f"{quote} {type_value}", type=type_value, quote_currency=quote
    )
    with pytest.raises(InvalidInvestmentTransaction) as caught:
        _tx(team, ctx, item)
    assert (caught.value.field, caught.value.code) == ("amounts", "settlement_not_allowed")


def test_unknown_amounts_mode_and_bad_base_values_are_refused(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    usd = _instrument(team, ctx)
    with pytest.raises(InvalidInvestmentTransaction) as caught:
        _tx(team, ctx, usd, amounts="gbp")
    assert caught.value.code == "invalid_amounts"
    with pytest.raises(InvalidInvestmentTransaction) as caught:
        _tx(team, ctx, usd, base_price="abc")
    assert (caught.value.field, caught.value.code) == ("base_unit_price", "invalid_unit_price")
    with pytest.raises(InvalidInvestmentTransaction) as caught:
        _tx(team, ctx, usd, base_fee="1.234")
    assert (caught.value.field, caught.value.code) == ("base_fee", "too_many_decimals")
    with pytest.raises(InvalidInvestmentTransaction) as caught:
        _tx(team, ctx, usd, fx="0")
    assert caught.value.code == "invalid_fx"


def test_the_database_refuses_a_partial_settlement(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    usd = _instrument(team, ctx)
    view = _tx(team, ctx, usd)
    with pytest.raises(IntegrityError), team.factory.begin() as session:
        session.execute(
            update(InvestmentTransaction)
            .where(InvestmentTransaction.id == view.id)
            .values(settlement_fee_minor=None)
        )
    with pytest.raises(IntegrityError), team.factory.begin() as session:
        session.execute(
            update(InvestmentTransaction)
            .where(InvestmentTransaction.id == view.id)
            .values(settlement_fee_minor=-1)
        )


def test_a_blank_rate_on_the_form_is_needed_even_with_a_stored_rate(team: WorkspaceDb) -> None:
    # The form pre-fills stored FX (#243); a rate the user cleared is not silently re-taken.
    ctx = team.ctx("ed", "editor")
    usd = _instrument(team, ctx)
    _stored_rate(team, ctx, "USD", date(2026, 1, 8), "0.91", source="manual")
    with pytest.raises(InvalidInvestmentTransaction) as caught:
        _tx(team, ctx, usd, fx="")
    assert (caught.value.field, caught.value.code) == ("fx_to_base", "fx_needed")


def test_the_fx_currency_lookup_is_workspace_scoped(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    usd = _instrument(team, ctx)
    eur = _instrument(team, ctx, name="EU ETF", quote_currency="EUR")
    assert instrument_fx_currency(team.factory, ctx, usd) == "USD"
    assert instrument_fx_currency(team.factory, ctx, eur) is None
    other_workspace = team.add_workspace("Other")
    team.add_member("ed", "owner", other_workspace)
    outsider = team.ctx("ed", "owner", other_workspace)
    with pytest.raises(NotFound):
        instrument_fx_currency(team.factory, outsider, usd)


def test_switching_modes_on_edit_sets_or_clears_the_settlement(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    usd = _instrument(team, ctx)
    quote = _tx(team, ctx, usd, amounts="quote", unit_price="1600", fee="5", fx="1")
    # quote -> base: the amounts were base amounts entered at rate 1; take the real rate.
    based = update_transaction(
        team.factory,
        ctx,
        quote.id,
        type_value="buy",
        date_value="2026-01-10",
        quantity_value="4",
        unit_price_value="1600",
        fee_value="5",
        fx_value="4.0123",
        note_value="",
        locale="en",
        amounts_value="base",
        base_unit_price_value="1600",
        base_fee_value="5",
    )
    assert based.amounts == "base"
    assert based.fee == Money(125, "USD")
    events = _events(team, "investment_transaction.updated")
    changed = str(events[-1].data["changed"]).split(",")
    assert {"settlement_currency", "settlement_unit_price", "fx_to_base"} <= set(changed)
    assert "1600" not in str(events[-1].data["changed"])
    # base -> quote: the derived quote values are kept and the settlement cleared.
    back = update_transaction(
        team.factory,
        ctx,
        quote.id,
        type_value="buy",
        date_value="2026-01-10",
        quantity_value="4",
        unit_price_value=format(based.unit_price, "f"),
        fee_value="1.25",
        fx_value="4.0123",
        note_value="",
        locale="en",
        amounts_value="quote",
    )
    assert back.amounts == "quote"
    assert back.settlement_fee is None and back.settlement_unit_price is None
    assert get_transaction(team.factory, ctx, quote.id, locale="en").unit_price == based.unit_price


def _events(db: WorkspaceDb, prefix: str) -> list[AuditEvent]:
    with db.factory() as session:
        return list(
            session.scalars(
                select(AuditEvent)
                .where(AuditEvent.action.startswith(prefix))
                .order_by(AuditEvent.ts, AuditEvent.id)
            )
        )
