"""Portfolio day/month changes for the Home widget (F9.1)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from pathlib import Path
from uuid import UUID

import pytest

from assetboard.clock import FixedClock
from assetboard.db.repositories import AssetClassRepository
from assetboard.domain.money import Money
from assetboard.services.investment_transactions import create_transaction
from assetboard.services.investments import create_instrument, seed_asset_classes
from assetboard.services.portfolio_changes import portfolio_changes
from assetboard.services.prices import save_manual_price
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


@pytest.fixture
def db(tmp_path: Path) -> Iterator[WorkspaceDb]:
    for workspace in workspace_db(tmp_path):
        workspace.add_member("olga", "owner")
        yield workspace


def _ctx(db: WorkspaceDb):
    return db.ctx("olga", "owner")


def _instrument(db: WorkspaceDb, name: str) -> UUID:
    ctx = _ctx(db)
    with db.factory() as session:
        classes = AssetClassRepository(session, ctx).ordered()
    if not classes:
        seed_asset_classes(db.factory, ctx, locale="en")
        with db.factory() as session:
            classes = AssetClassRepository(session, ctx).ordered()
    class_id = classes[0].id
    return create_instrument(
        db.factory,
        ctx,
        name=name,
        symbol=name[:3].upper(),
        type="etf",
        quote_currency="EUR",
        unit_label="share",
        allocations=[(class_id, 10_000)],
        locale="en",
    ).id


def _buy(db: WorkspaceDb, instrument_id: UUID, quantity: str) -> None:
    create_transaction(
        db.factory,
        _ctx(db),
        instrument_id=instrument_id,
        type_value="buy",
        date_value="2026-01-15",
        quantity_value=quantity,
        unit_price_value="100",
        fee_value="0",
        fx_value="",
        note_value="",
        locale="en",
    )


def _price(db: WorkspaceDb, instrument_id: UUID, day: str, price: str) -> None:
    save_manual_price(
        db.factory,
        _changed_clock(db),
        instrument_id,
        price_id=None,
        date_value=day,
        price_value=price,
        locale="en",
    )


def _changed_clock(db: WorkspaceDb):
    return replace(_ctx(db), clock=FixedClock("2026-03-10T12:00:00+00:00"))


def _two_holdings(db: WorkspaceDb) -> None:
    first = _instrument(db, "Alpha")
    second = _instrument(db, "Beta")
    _buy(db, first, "10")
    _buy(db, second, "5")
    _price(db, first, "2026-02-28", "100")
    _price(db, first, "2026-03-09", "105")
    _price(db, first, "2026-03-10", "110")
    _price(db, second, "2026-02-28", "200")
    _price(db, second, "2026-03-09", "200")
    _price(db, second, "2026-03-10", "205")


def test_changes_since_yesterday_and_since_the_month_start(db: WorkspaceDb) -> None:
    _two_holdings(db)
    changes = portfolio_changes(db.factory, _changed_clock(db))
    assert changes.has_data is True
    assert changes.currency == "EUR"
    assert changes.previous_day == date(2026, 3, 9)
    assert changes.month_start == date(2026, 3, 1)
    assert changes.since_previous_day.amount == Money(7500, "EUR")
    assert changes.since_previous_day.percent_tenths == 37  # 75 / 2050 = 3.7%
    assert changes.since_month_start.amount == Money(12500, "EUR")
    assert changes.since_month_start.percent_tenths == 63  # 125 / 2000 = 6.3%
    assert [(mover.name, mover.change.amount) for mover in changes.movers] == [
        ("Alpha", Money(5000, "EUR")),
        ("Beta", Money(2500, "EUR")),
    ]
    assert changes.movers[0].change.percent_tenths == 48  # 50 / 1050
    assert changes.movers[1].change.percent_tenths == 25  # 25 / 1000


def test_no_investment_transactions_means_no_history(db: WorkspaceDb) -> None:
    changes = portfolio_changes(db.factory, _changed_clock(db))
    assert changes.has_data is False
    assert changes.since_previous_day.amount is None
    assert changes.since_month_start.amount is None
    assert changes.movers == ()


def test_a_flat_day_has_a_zero_change_and_no_percent(db: WorkspaceDb) -> None:
    instrument = _instrument(db, "Flat")
    _buy(db, instrument, "1")
    _price(db, instrument, "2026-03-09", "100")
    _price(db, instrument, "2026-03-10", "100")
    changes = portfolio_changes(db.factory, _changed_clock(db))
    assert changes.since_previous_day.amount == Money(0, "EUR")
    assert changes.since_previous_day.percent_tenths == 0
    assert changes.movers == ()
