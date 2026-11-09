"""Investment transactions service (F6.2): holdings, oversell guards, locks and filters."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.db.models.identity import AuditEvent
from assetboard.db.models.investments import InvestmentTransaction
from assetboard.db.repositories import (
    CategoryRepository,
    InvestmentTransactionRepository,
    TransactionRepository,
)
from assetboard.domain.money import Money
from assetboard.domain.months import YearMonth
from assetboard.services.context import Context, PermissionDenied
from assetboard.services.investment_income import (
    default_income_category_id,
    investment_income_page,
)
from assetboard.services.investment_transactions import (
    InstrumentArchived,
    InvalidInvestmentTransaction,
    InvestmentTransactionView,
    InvestmentUndoExpired,
    OversellRejected,
    TransactionDeleted,
    create_transaction,
    delete_transaction,
    instrument_history,
    list_transactions,
    undo_delete,
    update_transaction,
    workspace_has_investment_transactions,
)
from assetboard.services.investments import (
    InvalidInvestment,
    create_asset_class,
    create_instrument,
    update_instrument,
)
from assetboard.services.months import MonthClosed, close_month
from assetboard.services.transactions import (
    InvestmentIncomeLinked,
)
from assetboard.services.transactions import (
    delete_transaction as delete_ledger_transaction,
)
from assetboard.services.transactions import (
    get_transaction as get_ledger_transaction,
)
from assetboard.services.transactions import (
    update_transaction as update_ledger_transaction,
)
from assetboard.services.workspaces import (
    BaseCurrencyLocked,
    update_settings,
    workspace_settings,
)
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


class TickingClock(FixedClock):
    def __init__(self) -> None:
        super().__init__("2026-03-01T12:00:00+00:00")
        self._ticks = 0

    def now(self) -> datetime:
        self._ticks += 1
        return super().now() + timedelta(seconds=self._ticks)

    def advance(self, delta: timedelta) -> None:
        self._value += delta


@pytest.fixture
def db(tmp_path: Path) -> Iterator[WorkspaceDb]:
    for prepared in workspace_db(tmp_path):
        clock = TickingClock()
        prepared.clock = clock
        prepared.factory.kw["info"]["clock"] = clock
        yield prepared


@pytest.fixture
def team(db: WorkspaceDb) -> WorkspaceDb:
    db.add_member("olga", "owner")
    db.add_member("ed", "editor")
    db.add_member("vic", "viewer")
    return db


def _class(db: WorkspaceDb, ctx: Context, name: str = "Stocks"):
    return create_asset_class(db.factory, ctx, name=name, color="#2563eb", investable=True)


def _instrument(
    db: WorkspaceDb,
    ctx: Context,
    class_id: UUID,
    *,
    name: str = "World ETF",
    type: str = "etf",
    quote_currency: str = "EUR",
    unit_label: str = "share",
):
    return create_instrument(
        db.factory,
        ctx,
        name=name,
        symbol="",
        type=type,
        quote_currency=quote_currency,
        unit_label=unit_label,
        allocations=[(class_id, 10_000)],
        locale="en",
    )


def _tx(
    db: WorkspaceDb,
    ctx: Context,
    instrument_id: UUID,
    *,
    type_value: str = "buy",
    date_value: str = "2026-01-10",
    quantity: str = "10",
    unit_price: str = "100",
    fee: str = "0",
    fx: str = "",
    note: str = "",
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
        note_value=note,
        locale="en",
    )


def _income_category(
    db: WorkspaceDb, ctx: Context, name: str = "Interest", *, archived: bool = False
) -> UUID:
    with db.factory.begin() as session:
        row = CategoryRepository(session, ctx).create(
            name=name,
            name_key=name.casefold(),
            kind="income",
            treatment="regular",
            color="#16a34a",
            sort_order=0,
            archived_at=db.clock.now() if archived else None,
        )
        return row.id


def _linked_dividend(
    db: WorkspaceDb,
    ctx: Context,
    instrument_id: UUID,
    category_id: UUID | None,
    *,
    date_value: str = "2026-02-01",
    gross: str = "10",
    withholding: str = "0",
    fx: str = "1",
) -> InvestmentTransactionView:
    return create_transaction(
        db.factory,
        ctx,
        instrument_id=instrument_id,
        type_value="dividend",
        date_value=date_value,
        quantity_value="1",
        unit_price_value=gross,
        fee_value=withholding,
        fx_value=fx,
        note_value="",
        locale="en",
        record_income=True,
        income_category_id=category_id,
    )


def _ledger_row(db: WorkspaceDb, ctx: Context, transaction_id: UUID | None):
    assert transaction_id is not None
    with db.factory() as session:
        row = TransactionRepository(session, ctx).get(transaction_id)
    assert row is not None
    return row


def _events(db: WorkspaceDb, prefix: str) -> list[AuditEvent]:
    with db.factory() as session:
        return list(
            session.scalars(
                select(AuditEvent)
                .where(AuditEvent.action.startswith(prefix))
                .order_by(AuditEvent.ts, AuditEvent.id)
            )
        )


def _set_method(db: WorkspaceDb, ctx: Context, method: str) -> None:
    current = workspace_settings(db.factory, ctx)
    update_settings(
        db.factory,
        ctx,
        name=current.name,
        base_currency=current.base_currency,
        country_code=current.country_code,
        timezone=current.timezone,
        locale=current.locale,
        auto_copy_plan=current.auto_copy_plan,
        require_two_factor=current.require_two_factor,
        upcoming_days=current.upcoming_days,
        investment_cost_basis=method,
    )


def test_buy_computes_the_holding_and_audits(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx)
    instrument = _instrument(team, ctx, stocks.id)
    view = _tx(team, ctx, instrument.id, quantity="10", unit_price="100", fee="2.50")
    assert (view.type, view.quantity, view.unit_price) == ("buy", Decimal("10"), Decimal("100"))
    history = instrument_history(team.factory, ctx, instrument.id, locale="en")
    assert history.holding.quantity == Decimal("10")
    assert history.holding.cost_basis_base == Money(100_250, "EUR")
    assert history.holding.avg_cost == Decimal("100.25")
    assert history.holding.realized_pnl == Money(0, "EUR")
    event = _events(team, "investment_transaction.created")[-1]
    assert event.data["instrument"] == "World ETF"
    assert event.data["transaction_type"] == "buy"
    assert event.data["quantity"] == "10"


def test_average_and_fifo_can_be_switched_and_recompute_holdings(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx)
    instrument = _instrument(team, ctx, stocks.id)
    _tx(
        team,
        ctx,
        instrument.id,
        date_value="2026-01-01",
        quantity="46",
        unit_price="489.5409",
        fee="65.30",
    )
    _tx(
        team,
        ctx,
        instrument.id,
        date_value="2026-02-01",
        quantity="4",
        unit_price="496.3093",
        fee="14.00",
    )
    _tx(
        team,
        ctx,
        instrument.id,
        date_value="2026-03-01",
        type_value="sell",
        quantity="10",
        unit_price="500",
    )
    average = instrument_history(team.factory, ctx, instrument.id, locale="en").holding
    assert average.cost_basis_base == Money(1_966_674, "EUR")
    owner = team.ctx("olga", "owner")
    _set_method(team, owner, "fifo")
    fifo = instrument_history(team.factory, ctx, instrument.id, locale="en").holding
    assert fifo.cost_basis_base == Money(1_967_382, "EUR")
    assert fifo.realized_pnl != average.realized_pnl
    _set_method(team, owner, "average")
    assert instrument_history(
        team.factory, ctx, instrument.id, locale="en"
    ).holding.cost_basis_base == Money(1_966_674, "EUR")


def test_a_non_base_instrument_requires_an_fx_rate_and_a_base_one_stores_one(
    team: WorkspaceDb,
) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx)
    usd = _instrument(team, ctx, stocks.id, name="US ETF", quote_currency="USD")
    with pytest.raises(InvalidInvestmentTransaction) as caught:
        _tx(team, ctx, usd.id, fx="")
    # F6.2a: a blank rate falls back to stored FX; with none stored the rate is needed.
    assert caught.value.code == "fx_needed"
    with pytest.raises(InvalidInvestmentTransaction) as caught:
        _tx(team, ctx, usd.id, fx="0")
    assert caught.value.code == "invalid_fx"
    view = _tx(team, ctx, usd.id, fx="0.9")
    assert view.fx_to_base == Decimal("0.9")
    eur = _instrument(team, ctx, stocks.id, name="EU ETF", quote_currency="EUR")
    assert _tx(team, ctx, eur.id, fx="999").fx_to_base == Decimal(1)


def test_a_currency_instrument_forces_price_one(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx)
    cash = _instrument(
        team, ctx, stocks.id, name="US dollar", type="currency", quote_currency="USD"
    )
    view = _tx(team, ctx, cash.id, quantity="100", unit_price="2.5", fx="0.9")
    assert view.unit_price == Decimal(1)
    assert view.quantity == Decimal("100")


def test_a_fee_must_fit_the_currency_minor_unit(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx)
    instrument = _instrument(team, ctx, stocks.id)
    with pytest.raises(InvalidInvestmentTransaction) as caught:
        _tx(team, ctx, instrument.id, fee="0.001")
    assert (caught.value.field, caught.value.code) == (
        "fee",
        "too_many_decimals",
    )


def test_future_dates_are_refused(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx)
    instrument = _instrument(team, ctx, stocks.id)
    with pytest.raises(InvalidInvestmentTransaction) as caught:
        _tx(team, ctx, instrument.id, date_value="2026-03-02")
    assert (caught.value.field, caught.value.code) == ("date", "future_date")


def test_oversell_is_refused_and_names_the_sale_date(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx)
    instrument = _instrument(team, ctx, stocks.id)
    _tx(team, ctx, instrument.id, date_value="2026-01-10", quantity="2")
    with pytest.raises(OversellRejected) as caught:
        _tx(
            team,
            ctx,
            instrument.id,
            date_value="2026-02-10",
            type_value="sell",
            quantity="3",
        )
    assert caught.value.on == date(2026, 2, 10)


def test_editing_an_earlier_buy_down_to_an_oversell_is_refused(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx)
    instrument = _instrument(team, ctx, stocks.id)
    first = _tx(team, ctx, instrument.id, date_value="2026-01-01", quantity="10")
    _tx(team, ctx, instrument.id, date_value="2026-01-02", quantity="5")
    _tx(
        team,
        ctx,
        instrument.id,
        date_value="2026-02-01",
        type_value="sell",
        quantity="12",
    )
    with pytest.raises(OversellRejected):
        update_transaction(
            team.factory,
            ctx,
            first.id,
            type_value="buy",
            date_value="2026-01-01",
            quantity_value="1",
            unit_price_value="100",
            fee_value="0",
            fx_value="",
            note_value="",
            locale="en",
        )
    assert instrument_history(
        team.factory, ctx, instrument.id, locale="en"
    ).holding.quantity == Decimal("3")


def test_backdating_a_sell_before_enough_buys_is_refused(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx)
    instrument = _instrument(team, ctx, stocks.id)
    _tx(team, ctx, instrument.id, date_value="2026-02-01", quantity="10")
    with pytest.raises(OversellRejected):
        _tx(
            team,
            ctx,
            instrument.id,
            date_value="2026-01-01",
            type_value="sell",
            quantity="1",
        )


def test_deleting_a_buy_that_would_oversell_is_refused(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx)
    instrument = _instrument(team, ctx, stocks.id)
    buy = _tx(team, ctx, instrument.id, date_value="2026-01-01", quantity="10")
    _tx(
        team,
        ctx,
        instrument.id,
        date_value="2026-02-01",
        type_value="sell",
        quantity="8",
    )
    with pytest.raises(OversellRejected):
        delete_transaction(team.factory, ctx, buy.id)
    assert instrument_history(
        team.factory, ctx, instrument.id, locale="en"
    ).holding.quantity == Decimal("2")


def test_restoring_a_deleted_sell_that_would_oversell_is_refused(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx)
    instrument = _instrument(team, ctx, stocks.id)
    buy = _tx(team, ctx, instrument.id, date_value="2026-01-01", quantity="10")
    sell = _tx(
        team,
        ctx,
        instrument.id,
        date_value="2026-02-01",
        type_value="sell",
        quantity="8",
    )
    delete_transaction(team.factory, ctx, sell.id)
    delete_transaction(team.factory, ctx, buy.id)
    with pytest.raises(OversellRejected):
        undo_delete(team.factory, ctx, sell.id)


def test_delete_and_undo_recompute_holdings(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx)
    instrument = _instrument(team, ctx, stocks.id)
    _tx(team, ctx, instrument.id, date_value="2026-01-01", quantity="10")
    sell = _tx(
        team,
        ctx,
        instrument.id,
        date_value="2026-02-01",
        type_value="sell",
        quantity="4",
    )
    assert instrument_history(
        team.factory, ctx, instrument.id, locale="en"
    ).holding.quantity == Decimal("6")
    delete_transaction(team.factory, ctx, sell.id)
    assert instrument_history(
        team.factory, ctx, instrument.id, locale="en"
    ).holding.quantity == Decimal("10")
    undo_delete(team.factory, ctx, sell.id)
    assert instrument_history(
        team.factory, ctx, instrument.id, locale="en"
    ).holding.quantity == Decimal("6")


def test_undo_expires_after_ten_seconds(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx)
    instrument = _instrument(team, ctx, stocks.id)
    _tx(team, ctx, instrument.id, quantity="1")
    sell = _tx(team, ctx, instrument.id, type_value="sell", quantity="1")
    delete_transaction(team.factory, ctx, sell.id)
    cast(TickingClock, team.clock).advance(timedelta(seconds=11))
    with pytest.raises(InvestmentUndoExpired):
        undo_delete(team.factory, ctx, sell.id)


def test_archived_instruments_refuse_create_update_delete_and_undo(
    team: WorkspaceDb,
) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx)
    instrument = _instrument(team, ctx, stocks.id)
    live = _tx(team, ctx, instrument.id, date_value="2026-01-01")
    deleted = _tx(team, ctx, instrument.id, date_value="2026-01-02")
    delete_transaction(team.factory, ctx, deleted.id)
    from assetboard.services.investments import archive_instrument

    archive_instrument(team.factory, ctx, instrument.id)
    with pytest.raises(InstrumentArchived):
        _tx(team, ctx, instrument.id, date_value="2026-01-03")
    with pytest.raises(InstrumentArchived):
        update_transaction(
            team.factory,
            ctx,
            live.id,
            type_value="buy",
            date_value="2026-01-01",
            quantity_value="1",
            unit_price_value="100",
            fee_value="0",
            fx_value="",
            note_value="",
            locale="en",
        )
    with pytest.raises(InstrumentArchived):
        delete_transaction(team.factory, ctx, live.id)
    with pytest.raises(InstrumentArchived):
        undo_delete(team.factory, ctx, deleted.id)


def test_instrument_currency_and_type_are_locked_after_a_transaction(
    team: WorkspaceDb,
) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx)
    instrument = _instrument(team, ctx, stocks.id)
    _tx(team, ctx, instrument.id)
    with pytest.raises(InvalidInvestment) as currency:
        update_instrument(
            team.factory,
            ctx,
            instrument.id,
            name=instrument.name,
            symbol=instrument.symbol or "",
            type=instrument.type,
            quote_currency="USD",
            unit_label=instrument.unit_label,
            allocations=[(stocks.id, 10_000)],
            locale="en",
        )
    assert currency.value.code == "currency_locked"
    with pytest.raises(InvalidInvestment) as kind:
        update_instrument(
            team.factory,
            ctx,
            instrument.id,
            name=instrument.name,
            symbol=instrument.symbol or "",
            type="equity",
            quote_currency=instrument.quote_currency,
            unit_label=instrument.unit_label,
            allocations=[(stocks.id, 10_000)],
            locale="en",
        )
    assert kind.value.code == "type_locked"
    with pytest.raises(IntegrityError), team.factory.begin() as session:
        InvestmentTransactionRepository(session, ctx).create(
            instrument_id=instrument.id,
            date=date(2026, 3, 1),
            type="buy",
            quantity=Decimal("1"),
            unit_price=Decimal("1"),
            quote_currency="USD",
            fee_minor=0,
            fx_to_base=Decimal("1"),
        )


def test_list_filters_and_pagination(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx)
    first = _instrument(team, ctx, stocks.id, name="First")
    second = _instrument(team, ctx, stocks.id, name="Second")
    _tx(team, ctx, first.id, date_value="2026-01-01", quantity="1")
    _tx(team, ctx, first.id, date_value="2026-02-01", type_value="sell", quantity="1")
    _tx(team, ctx, second.id, date_value="2026-03-01", quantity="2")
    page = list_transactions(
        team.factory,
        ctx,
        instrument_id=first.id,
        type_value="",
        date_from="",
        date_to="",
        page=1,
    )
    assert page.total == 2
    assert [row.instrument_name for row in page.rows] == ["First", "First"]
    buys = list_transactions(
        team.factory,
        ctx,
        instrument_id=None,
        type_value="buy",
        date_from="2026-01-15",
        date_to="",
        page=1,
    )
    assert buys.total == 1
    assert [row.instrument_name for row in buys.rows] == ["Second"]


def test_viewers_read_but_cannot_write(team: WorkspaceDb) -> None:
    editor = team.ctx("ed", "editor")
    stocks = _class(team, editor)
    instrument = _instrument(team, editor, stocks.id)
    _tx(team, editor, instrument.id)
    viewer = team.ctx("vic", "viewer")
    assert (
        list_transactions(
            team.factory,
            viewer,
            instrument_id=None,
            type_value="",
            date_from="",
            date_to="",
            page=1,
        ).total
        == 1
    )
    assert instrument_history(
        team.factory, viewer, instrument.id, locale="en"
    ).holding.quantity == Decimal("10")
    with pytest.raises(PermissionDenied):
        _tx(team, viewer, instrument.id)


def test_investment_transactions_lock_the_workspace_base_currency(
    team: WorkspaceDb,
) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx)
    instrument = _instrument(team, ctx, stocks.id)
    _tx(team, ctx, instrument.id)
    owner = team.ctx("olga", "owner")
    current = workspace_settings(team.factory, owner)
    assert current.base_currency_locked
    with pytest.raises(BaseCurrencyLocked) as locked:
        update_settings(
            team.factory,
            owner,
            name=current.name,
            base_currency="PLN",
            country_code=current.country_code,
            timezone=current.timezone,
            locale=current.locale,
            auto_copy_plan=current.auto_copy_plan,
            require_two_factor=current.require_two_factor,
            upcoming_days=current.upcoming_days,
            investment_cost_basis=current.investment_cost_basis,
        )
    assert locked.value.code == "base_currency_locked"
    with team.factory() as session:
        row_id = session.scalars(select(InvestmentTransaction.id)).one()
    delete_transaction(team.factory, ctx, row_id)
    assert workspace_settings(team.factory, owner).base_currency_locked


def test_workspace_has_investment_transactions_counts_deleted_rows(
    team: WorkspaceDb,
) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx)
    instrument = _instrument(team, ctx, stocks.id)
    with team.factory.begin() as session:
        assert not workspace_has_investment_transactions(session, ctx)
    _tx(team, ctx, instrument.id)
    row_id = _events(team, "investment_transaction.created")[-1].entity_id
    delete_transaction(team.factory, ctx, UUID(row_id))
    with team.factory.begin() as session:
        assert workspace_has_investment_transactions(session, ctx)


def test_deleted_and_undated_editing_rules(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx)
    instrument = _instrument(team, ctx, stocks.id)
    deleted = _tx(team, ctx, instrument.id)
    delete_transaction(team.factory, ctx, deleted.id)
    with pytest.raises(TransactionDeleted):
        update_transaction(
            team.factory,
            ctx,
            deleted.id,
            type_value="buy",
            date_value="2026-01-10",
            quantity_value="1",
            unit_price_value="100",
            fee_value="0",
            fx_value="",
            note_value="",
            locale="en",
        )
    with team.factory.begin() as session:
        row = InvestmentTransactionRepository(session, ctx).create(
            instrument_id=instrument.id,
            date=None,
            type="buy",
            quantity=Decimal("1"),
            unit_price=Decimal("100"),
            quote_currency="EUR",
            fee_minor=0,
            fx_to_base=Decimal("1"),
        )
        imported_id = row.id
    updated = update_transaction(
        team.factory,
        ctx,
        imported_id,
        type_value="buy",
        date_value="2026-02-01",
        quantity_value="1",
        unit_price_value="100",
        fee_value="0",
        fx_value="",
        note_value="",
        locale="en",
    )
    assert updated.date == date(2026, 2, 1)


def test_a_dividend_can_create_the_linked_net_income_row(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx)
    instrument = _instrument(team, ctx, stocks.id, quote_currency="USD")
    category = _income_category(team, ctx)

    view = _linked_dividend(
        team, ctx, instrument.id, category, gross="10.01", withholding="0.05", fx="1.2345"
    )
    assert view.quantity == Decimal("1")
    assert view.unit_price == Decimal("10.01")
    assert view.fee == Money(5, "USD")
    assert view.income_transaction_id is not None

    income = _ledger_row(team, ctx, view.income_transaction_id)
    assert income.direction == "income"
    assert income.amount_minor == 1_230  # 12.36 gross − 0.06 withholding
    assert income.currency == "EUR"
    assert income.category_id == category
    assert income.source == "investment"
    assert income.date == date(2026, 2, 1)
    assert income.description == "Investment income: World ETF"
    assert (
        get_ledger_transaction(
            team.factory, ctx, view.income_transaction_id
        ).investment_transaction_id
        == view.id
    )
    assert _events(team, "transaction.created")[-1].data["source"] == "investment"

    page = investment_income_page(team.factory, ctx)
    assert page.years == (
        type(page.years[0])(
            year=2026,
            gross=Money(1_236, "EUR"),
            withholding=Money(6, "EUR"),
            net=Money(1_230, "EUR"),
            payments=1,
        ),
    )
    with team.factory() as session:
        assert default_income_category_id(session, ctx) == category


def test_recording_income_requires_an_active_income_category(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx)
    instrument = _instrument(team, ctx, stocks.id)
    category = _income_category(team, ctx)

    with pytest.raises(InvalidInvestmentTransaction) as missing:
        _linked_dividend(team, ctx, instrument.id, None)
    assert (missing.value.field, missing.value.code) == (
        "income_category",
        "income_category_required",
    )

    with pytest.raises(InvalidInvestmentTransaction) as archived:
        _linked_dividend(
            team, ctx, instrument.id, _income_category(team, ctx, "Old", archived=True)
        )
    assert archived.value.code == "income_category_invalid"

    with pytest.raises(InvalidInvestmentTransaction) as zero:
        _linked_dividend(team, ctx, instrument.id, category, gross="1", withholding="1")
    assert zero.value.code == "income_zero"

    with pytest.raises(InvalidInvestmentTransaction) as wrong_type:
        create_transaction(
            team.factory,
            ctx,
            instrument_id=instrument.id,
            type_value="buy",
            date_value="2026-02-01",
            quantity_value="1",
            unit_price_value="10",
            fee_value="0",
            fx_value="",
            note_value="",
            locale="en",
            record_income=True,
            income_category_id=category,
        )
    assert wrong_type.value.code == "invalid_income"


def test_editing_unticking_and_deleting_a_linked_dividend_keep_the_ledger_in_sync(
    team: WorkspaceDb,
) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx)
    instrument = _instrument(team, ctx, stocks.id)
    first_category = _income_category(team, ctx, "Interest")
    second_category = _income_category(team, ctx, "Dividends")
    view = _linked_dividend(team, ctx, instrument.id, first_category, gross="10", withholding="1")
    income_id = view.income_transaction_id
    assert _ledger_row(team, ctx, income_id).amount_minor == 900

    edited = update_transaction(
        team.factory,
        ctx,
        view.id,
        type_value="dividend",
        date_value="2026-02-03",
        quantity_value="1",
        unit_price_value="20",
        fee_value="2",
        fx_value="1",
        note_value="",
        locale="en",
        record_income=True,
        income_category_id=second_category,
    )
    assert edited.income_transaction_id == income_id
    income = _ledger_row(team, ctx, income_id)
    assert income.amount_minor == 1_800
    assert income.category_id == second_category
    assert income.date == date(2026, 2, 3)

    unticked = update_transaction(
        team.factory,
        ctx,
        view.id,
        type_value="dividend",
        date_value="2026-02-03",
        quantity_value="1",
        unit_price_value="30",
        fee_value="0",
        fx_value="1",
        note_value="",
        locale="en",
        record_income=False,
        income_category_id=None,
    )
    assert unticked.income_transaction_id is None
    assert _ledger_row(team, ctx, income_id).deleted_at is not None

    reticked = update_transaction(
        team.factory,
        ctx,
        view.id,
        type_value="dividend",
        date_value="2026-02-04",
        quantity_value="1",
        unit_price_value="40",
        fee_value="0",
        fx_value="1",
        note_value="",
        locale="en",
        record_income=True,
        income_category_id=first_category,
    )
    new_id = reticked.income_transaction_id
    assert new_id is not None and new_id != income_id
    assert _ledger_row(team, ctx, new_id).amount_minor == 4_000

    delete_transaction(team.factory, ctx, view.id)
    assert _ledger_row(team, ctx, new_id).deleted_at is not None
    undo_delete(team.factory, ctx, view.id)
    assert _ledger_row(team, ctx, new_id).deleted_at is None


def test_closed_months_refuse_only_linked_dividend_writes(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx)
    instrument = _instrument(team, ctx, stocks.id)
    category = _income_category(team, ctx)

    close_month(team.factory, ctx, YearMonth(2026, 1))
    with pytest.raises(MonthClosed):
        _linked_dividend(team, ctx, instrument.id, category, date_value="2026-01-15")
    # Unlinked dividends and splits do not touch the ledger: closed months allow them.
    _tx(
        team,
        ctx,
        instrument.id,
        type_value="dividend",
        date_value="2026-01-15",
        unit_price="10",
    )
    _tx(
        team,
        ctx,
        instrument.id,
        type_value="split",
        date_value="2026-01-15",
        quantity="2",
        unit_price="1",
    )

    movable = _linked_dividend(team, ctx, instrument.id, category, date_value="2026-02-20")
    with pytest.raises(MonthClosed):
        update_transaction(
            team.factory,
            ctx,
            movable.id,
            type_value="dividend",
            date_value="2026-01-15",
            quantity_value="1",
            unit_price_value="10",
            fee_value="0",
            fx_value="1",
            note_value="",
            locale="en",
            record_income=True,
            income_category_id=category,
        )

    edited = _linked_dividend(team, ctx, instrument.id, category, date_value="2026-02-01")
    deleted = _linked_dividend(team, ctx, instrument.id, category, date_value="2026-02-05")
    undone = _linked_dividend(team, ctx, instrument.id, category, date_value="2026-02-10")
    delete_transaction(team.factory, ctx, undone.id)
    close_month(team.factory, ctx, YearMonth(2026, 2))

    with pytest.raises(MonthClosed):
        update_transaction(
            team.factory,
            ctx,
            edited.id,
            type_value="dividend",
            date_value="2026-02-02",
            quantity_value="1",
            unit_price_value="11",
            fee_value="0",
            fx_value="1",
            note_value="",
            locale="en",
            record_income=True,
            income_category_id=category,
        )
    with pytest.raises(MonthClosed):
        delete_transaction(team.factory, ctx, deleted.id)
    with pytest.raises(MonthClosed):
        undo_delete(team.factory, ctx, undone.id)


def test_the_ledger_refuses_changes_to_a_linked_income_row(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx)
    instrument = _instrument(team, ctx, stocks.id)
    category = _income_category(team, ctx)
    view = _linked_dividend(team, ctx, instrument.id, category)
    income_id = view.income_transaction_id
    assert income_id is not None

    with pytest.raises(InvestmentIncomeLinked) as caught:
        update_ledger_transaction(
            team.factory,
            ctx,
            income_id,
            settings=Settings(_env_file=None),
            month=YearMonth(2026, 2),
            day="2026-02-02",
            amount="1",
            description="Edited",
            category_id=category,
            locale="en",
        )
    assert caught.value.investment_transaction_id == view.id

    with pytest.raises(InvestmentIncomeLinked) as deleted:
        delete_ledger_transaction(team.factory, ctx, income_id)
    assert deleted.value.investment_transaction_id == view.id
    assert _ledger_row(team, ctx, income_id).deleted_at is None


def test_splits_change_the_holding_and_take_part_in_oversell_checks(
    team: WorkspaceDb,
) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx)
    instrument = _instrument(team, ctx, stocks.id)
    _tx(team, ctx, instrument.id, date_value="2026-01-01", quantity="10", unit_price="100")
    split = _tx(
        team,
        ctx,
        instrument.id,
        type_value="split",
        date_value="2026-02-01",
        quantity="2",
        unit_price="1",
    )
    assert instrument_history(
        team.factory, ctx, instrument.id, locale="en"
    ).holding.quantity == Decimal("20")
    _tx(
        team,
        ctx,
        instrument.id,
        type_value="sell",
        date_value="2026-03-01",
        quantity="20",
        unit_price="60",
    )

    with pytest.raises(OversellRejected):
        delete_transaction(team.factory, ctx, split.id)
    with pytest.raises(OversellRejected):
        update_transaction(
            team.factory,
            ctx,
            split.id,
            type_value="split",
            date_value="2026-02-01",
            quantity_value="1",
            unit_price_value="2",
            fee_value="0",
            fx_value="",
            note_value="",
            locale="en",
        )

    with pytest.raises(InvalidInvestmentTransaction) as equal:
        update_transaction(
            team.factory,
            ctx,
            split.id,
            type_value="split",
            date_value="2026-02-01",
            quantity_value="1",
            unit_price_value="1",
            fee_value="0",
            fx_value="",
            note_value="",
            locale="en",
        )
    assert equal.value.code == "invalid_split"


def test_splits_are_refused_for_currency_and_bond_instruments(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx)
    currency = _instrument(team, ctx, stocks.id, name="Euro", type="currency", unit_label="EUR")
    bond = _instrument(team, ctx, stocks.id, name="Bond", type="bond_model", unit_label="bond")
    with pytest.raises(InvalidInvestmentTransaction) as currency_error:
        _tx(
            team,
            ctx,
            currency.id,
            type_value="split",
            date_value="2026-02-01",
            quantity="2",
            unit_price="1",
        )
    assert currency_error.value.code == "invalid_split"
    with pytest.raises(InvalidInvestmentTransaction) as bond_error:
        _tx(
            team,
            ctx,
            bond.id,
            type_value="split",
            date_value="2026-02-01",
            quantity="2",
            unit_price="1",
        )
    assert bond_error.value.code == "invalid_split"

    # A split without a holding is a no-op and is accepted.
    _tx(
        team,
        ctx,
        _instrument(team, ctx, stocks.id, name="Empty").id,
        type_value="split",
        date_value="2026-02-01",
        quantity="2",
        unit_price="1",
    )


def test_the_income_report_groups_by_year_and_is_viewer_readable(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx)
    instrument = _instrument(team, ctx, stocks.id)
    category = _income_category(team, ctx)
    _linked_dividend(
        team,
        ctx,
        instrument.id,
        category,
        date_value="2025-12-01",
        gross="10",
        withholding="1",
    )
    _linked_dividend(
        team,
        ctx,
        instrument.id,
        category,
        date_value="2026-01-15",
        gross="20",
        withholding="2",
    )
    _tx(
        team,
        ctx,
        instrument.id,
        type_value="dividend",
        date_value="2026-02-01",
        unit_price="5",
    )
    with team.factory.begin() as session:
        InvestmentTransactionRepository(session, ctx).create(
            instrument_id=instrument.id,
            date=None,
            type="dividend",
            quantity=Decimal("1"),
            unit_price=Decimal("7"),
            quote_currency="EUR",
            fee_minor=0,
            fx_to_base=Decimal("1"),
        )

    viewer = team.ctx("vic", "viewer")
    page = investment_income_page(team.factory, viewer)
    assert [year.year for year in page.years] == [2026, 2025]
    assert page.years[0].gross == Money(2_500, "EUR")
    assert page.years[0].withholding == Money(200, "EUR")
    assert page.years[0].net == Money(2_300, "EUR")
    assert page.years[0].payments == 2
    assert page.years[1].gross == Money(1_000, "EUR")
    assert page.years[1].withholding == Money(100, "EUR")
    assert page.years[1].net == Money(900, "EUR")
    assert page.years[1].payments == 1
    assert page.undated_count == 1
