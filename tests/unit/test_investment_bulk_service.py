"""Bulk add of investment transactions (F6.2a part B): preview, re-validation, all-or-nothing."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.db.models.identity import AuditEvent
from assetboard.db.models.investments import InvestmentTransaction
from assetboard.db.repositories import FxRateRepository
from assetboard.domain.money import Money
from assetboard.services.context import Context, PermissionDenied
from assetboard.services.investment_bulk import (
    BulkRejected,
    InvalidBulk,
    create_bulk,
    preview_bulk,
)
from assetboard.services.investment_transactions import create_transaction, instrument_history
from assetboard.services.investments import (
    archive_instrument,
    create_asset_class,
    create_instrument,
)
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


class TickingClock(FixedClock):
    def __init__(self) -> None:
        super().__init__("2026-03-10T12:00:00+00:00")
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


@pytest.fixture
def ctx(team: WorkspaceDb) -> Context:
    return team.ctx("ed", "editor")


def _instrument(
    db: WorkspaceDb,
    ctx: Context,
    name: str,
    *,
    symbol: str = "",
    quote_currency: str = "EUR",
    type: str = "etf",
) -> UUID:
    stocks = create_asset_class(
        db.factory, ctx, name=f"Class {name}", color="#2563eb", investable=True
    )
    return create_instrument(
        db.factory,
        ctx,
        name=name,
        symbol=symbol,
        type=type,
        quote_currency=quote_currency,
        unit_label="share",
        allocations=[(stocks.id, 10_000)],
        locale="en",
    ).id


def _rows(db: WorkspaceDb) -> list[InvestmentTransaction]:
    with db.factory() as session:
        return list(
            session.scalars(
                select(InvestmentTransaction).order_by(
                    InvestmentTransaction.date, InvestmentTransaction.created_at
                )
            )
        )


def _events(db: WorkspaceDb, action: str) -> list[AuditEvent]:
    with db.factory() as session:
        return list(session.scalars(select(AuditEvent).where(AuditEvent.action == action)))


def _stored_rate(db: WorkspaceDb, ctx: Context, quote: str, day: date, rate: str) -> None:
    with db.factory.begin() as session:
        FxRateRepository(session, ctx).create(
            base_currency="EUR", quote_currency=quote, date=day, rate=Decimal(rate), source="ecb"
        )


def test_preview_lists_parsed_values_totals_and_writes_nothing(
    team: WorkspaceDb, ctx: Context
) -> None:
    _instrument(team, ctx, "World ETF", symbol="VWCE")
    _instrument(team, ctx, "US ETF", symbol="SPY", quote_currency="USD")
    text = (
        "# my broker export\n"
        "2026-01-05; vwce; buy; 10; 100.5; 2\n"
        "2026-01-06; US ETF; buy; 4; 1600.00; 5; 4.0123; EUR\n"
        "2026-01-07; spy; sell; 1; 400; ; 4; usd\n"
    )
    preview = preview_bulk(team.factory, ctx, text=text, locale="en")
    assert preview.can_create
    first, second, third = preview.lines
    assert (first.instrument_name, first.type, first.quantity) == ("World ETF", "buy", Decimal(10))
    assert first.total == Money(100_700, "EUR")
    assert (second.amounts, second.entry_currency, second.unit_price) == (
        "base",
        "EUR",
        Decimal("1600.00"),
    )
    assert second.total == Money(640_500, "EUR")
    assert (third.amounts, third.entry_currency, third.fee) == ("quote", "USD", Money(0, "USD"))
    assert third.total == Money(160_000, "EUR")
    assert _rows(team) == []


def test_create_writes_every_row_with_one_batch_id_and_one_audit_event(
    team: WorkspaceDb, ctx: Context
) -> None:
    world = _instrument(team, ctx, "World ETF", symbol="VWCE")
    us = _instrument(team, ctx, "US ETF", quote_currency="USD")
    text = (
        "2026-01-05; World ETF; buy; 10; 100\n"
        "2026-01-05; World ETF; sell; 10; 110\n"
        "2026-01-06; US ETF; kupno; 4; 1600; 5; 4.0123; eur\n"
    )
    assert create_bulk(team.factory, ctx, text=text, locale="en") == 3
    rows = _rows(team)
    assert len(rows) == 3
    assert len({row.import_batch_id for row in rows}) == 1 and rows[0].import_batch_id
    # Same-day pasted rows keep their line order (the buy before the sell).
    assert [row.type for row in rows[:2]] == ["buy", "sell"]
    settled = next(row for row in rows if row.instrument_id == us)
    assert (settled.settlement_currency, settled.settlement_fee_minor) == ("EUR", 500)
    history = instrument_history(team.factory, ctx, world, locale="en")
    assert history.holding.quantity == 0
    assert history.holding.realized_pnl == Money(10_000, "EUR")
    [event] = _events(team, "investment_transaction.bulk_created")
    assert event.data["count"] == 3
    assert set(str(event.data["instruments"]).split(",")) == {str(world), str(us)}
    assert event.data["batch"] == str(rows[0].import_batch_id)
    assert "1600" not in str(event.data) and "price" not in event.data
    assert _events(team, "investment_transaction.created") == []


@pytest.mark.parametrize(
    ("line", "code"),
    [
        ("2026-01-05; Nope; buy; 1; 1", "instrument_unknown"),
        ("2026-01-05; ETF; buy; 1; 1", "instrument_ambiguous"),
        ("2026-01-05; Old ETF; buy; 1; 1", "instrument_archived"),
        ("2026-01-05; World ETF; buy; 1", "price_required"),
        ("2026-01-05; World ETF; buy; 0; 1", "invalid_quantity"),
        ("2026-01-05; World ETF; buy; 1; 1; 0.001", "too_many_decimals"),
        ("2026-01-05; World ETF; buy; 1; 1; 0; 2", "fx_not_one"),
        ("2026-01-05; World ETF; buy; 1; 1; 0; 1; GBP", "invalid_amounts_currency"),
        ("2026-01-05; US ETF; buy; 1; 1", "fx_needed"),
        ("2026-01-05; Cash USD; buy; 100; ; 0; 4; EUR", "settlement_not_allowed"),
        ("2026-01-05; World ETF; sell; 6; 1", "oversell"),
    ],
)
def test_any_line_error_refuses_the_whole_batch(
    team: WorkspaceDb, ctx: Context, line: str, code: str
) -> None:
    _instrument(team, ctx, "World ETF", symbol="ETF")
    _instrument(team, ctx, "Second", symbol="etf")
    _instrument(team, ctx, "US ETF", quote_currency="USD")
    _instrument(team, ctx, "Cash USD", quote_currency="USD", type="currency")
    old = _instrument(team, ctx, "Old ETF")
    archive_instrument(team.factory, ctx, old)
    text = f"2026-01-04; World ETF; buy; 5; 10\n{line}\n"
    preview = preview_bulk(team.factory, ctx, text=text, locale="en")
    assert not preview.can_create
    assert preview.lines[0].errors == ()
    assert code in preview.lines[1].errors
    with pytest.raises(BulkRejected) as caught:
        create_bulk(team.factory, ctx, text=text, locale="en")
    assert code in caught.value.preview.lines[1].errors
    assert _rows(team) == []


def test_currency_instruments_ignore_the_price_and_base_quoted_fx_may_be_one(
    team: WorkspaceDb, ctx: Context
) -> None:
    _instrument(team, ctx, "World ETF")
    _instrument(team, ctx, "Cash USD", quote_currency="USD", type="currency")
    text = "2026-01-05; Cash USD; buy; 100; ; ; 0.9\n2026-01-05; World ETF; buy; 1; 50; 0; 1; EUR\n"
    assert create_bulk(team.factory, ctx, text=text, locale="en") == 2
    cash = next(row for row in _rows(team) if row.quote_currency == "USD")
    assert cash.unit_price == Decimal(1) and cash.settlement_currency is None


def test_a_blank_fx_is_filled_from_stored_fx_and_marked_as_stored(
    team: WorkspaceDb, ctx: Context
) -> None:
    _instrument(team, ctx, "US ETF", quote_currency="USD")
    _stored_rate(team, ctx, "USD", date(2026, 1, 2), "0.9")
    preview = preview_bulk(team.factory, ctx, text="2026-01-05; US ETF; buy; 2; 100", locale="en")
    [line] = preview.lines
    assert (line.fx_to_base, line.fx_stored) == (Decimal("0.9"), True)
    assert line.total == Money(18_000, "EUR")


def test_a_transaction_fx_rate_never_fills_a_blank_bulk_rate(
    team: WorkspaceDb, ctx: Context
) -> None:
    us = _instrument(team, ctx, "US ETF", quote_currency="USD")
    create_transaction(
        team.factory,
        ctx,
        instrument_id=us,
        type_value="buy",
        date_value="2026-01-02",
        quantity_value="1",
        unit_price_value="10",
        fee_value="0",
        fx_value="0.7",
        note_value="",
        locale="en",
    )
    preview = preview_bulk(team.factory, ctx, text="2026-01-05; US ETF; buy; 2; 100", locale="en")
    assert preview.lines[0].errors == ("fx_needed",)


def test_polish_locale_numbers_dates_and_aliases(team: WorkspaceDb, ctx: Context) -> None:
    _instrument(team, ctx, "Fundusz")
    text = (
        "5.01.2026; fundusz; zakup; 1,5; 1 000,50; 0,5\n6.01.2026; FUNDUSZ; sprzedaż; 0,5; 1 100\n"
    )
    preview = preview_bulk(team.factory, ctx, text=text, locale="pl")
    assert preview.can_create
    assert preview.lines[0].quantity == Decimal("1.5")
    assert preview.lines[0].unit_price == Decimal("1000.50")
    assert preview.lines[1].type == "sell"


def test_an_oversell_only_from_the_combination_of_pasted_rows(
    team: WorkspaceDb, ctx: Context
) -> None:
    _instrument(team, ctx, "World ETF")
    text = (
        "2026-01-05; World ETF; buy; 5; 10\n"
        "2026-01-06; World ETF; sell; 3; 10\n"
        "2026-01-07; World ETF; sell; 3; 10\n"
    )
    preview = preview_bulk(team.factory, ctx, text=text, locale="en")
    assert [line.errors for line in preview.lines] == [(), (), ("oversell",)]
    with pytest.raises(BulkRejected):
        create_bulk(team.factory, ctx, text=text, locale="en")
    assert _rows(team) == []


def test_a_pasted_back_dated_sell_that_breaks_an_existing_sell_is_refused(
    team: WorkspaceDb, ctx: Context
) -> None:
    world = _instrument(team, ctx, "World ETF")
    for type_value, day in (("buy", "2026-01-05"), ("sell", "2026-02-05")):
        create_transaction(
            team.factory,
            ctx,
            instrument_id=world,
            type_value=type_value,
            date_value=day,
            quantity_value="5",
            unit_price_value="10",
            fee_value="0",
            fx_value="",
            note_value="",
            locale="en",
        )
    text = "2026-01-10; World ETF; sell; 1; 10"
    preview = preview_bulk(team.factory, ctx, text=text, locale="en")
    assert preview.batch_error == "oversell"
    assert preview.batch_error_on == date(2026, 2, 5)
    with pytest.raises(BulkRejected):
        create_bulk(team.factory, ctx, text=text, locale="en")
    assert len(_rows(team)) == 2


def test_confirm_re_validates_the_text_against_the_current_data(
    team: WorkspaceDb, ctx: Context
) -> None:
    world = _instrument(team, ctx, "World ETF")
    text = "2026-01-05; World ETF; buy; 5; 10"
    assert preview_bulk(team.factory, ctx, text=text, locale="en").can_create
    archive_instrument(team.factory, ctx, world)
    with pytest.raises(BulkRejected) as caught:
        create_bulk(team.factory, ctx, text=text, locale="en")
    assert caught.value.preview.lines[0].errors == ("instrument_archived",)
    assert _rows(team) == []


def test_limits_empty_input_and_roles(team: WorkspaceDb, ctx: Context) -> None:
    _instrument(team, ctx, "World ETF")
    with pytest.raises(InvalidBulk) as caught:
        preview_bulk(
            team.factory, ctx, text="2026-01-05; World ETF; buy; 1; 1\n" * 501, locale="en"
        )
    assert caught.value.code == "too_many_lines"
    with pytest.raises(InvalidBulk) as caught:
        create_bulk(team.factory, ctx, text="#" * (64 * 1024 + 1), locale="en")
    assert caught.value.code == "bulk_too_large"
    empty = preview_bulk(team.factory, ctx, text="# only a comment\n\n", locale="en")
    assert empty.input_error == "empty" and not empty.can_create
    viewer = team.ctx("vic", "viewer")
    with pytest.raises(PermissionDenied):
        preview_bulk(team.factory, viewer, text="2026-01-05; World ETF; buy; 1; 1", locale="en")
    with pytest.raises(PermissionDenied):
        create_bulk(team.factory, viewer, text="2026-01-05; World ETF; buy; 1; 1", locale="en")
    # A stale editor context is re-checked inside the write.
    stale = team.ctx("vic", "editor")
    with pytest.raises(PermissionDenied):
        create_bulk(team.factory, stale, text="2026-01-05; World ETF; buy; 1; 1", locale="en")
    assert _rows(team) == []
