"""F10.4/F10.5 report series: reuse the derived net-worth and portfolio services."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from assetboard.clock import FixedClock
from assetboard.db.repositories import AssetClassRepository, PricePointRepository
from assetboard.domain.money import Money
from assetboard.domain.reports import ReportStep
from assetboard.services.context import Context
from assetboard.services.investment_transactions import create_transaction
from assetboard.services.investments import create_asset_class, create_instrument
from assetboard.services.networth import add_valuation, create_category, create_item
from assetboard.services.reports import net_worth_report, portfolio_report, reports_data
from tests.tenant.workspace_db import WorkspaceDb, workspace_db

TODAY = "2026-12-15T12:00:00+00:00"


@pytest.fixture
def team(tmp_path: Path) -> Iterator[WorkspaceDb]:
    for prepared in workspace_db(tmp_path):
        clock = FixedClock(TODAY)
        prepared.clock = clock
        prepared.factory.configure(info={"clock": clock})
        prepared.add_member("olga", "owner")
        prepared.add_member("vic", "viewer")
        yield prepared


def _owner(db: WorkspaceDb) -> Context:
    return db.ctx("olga", "owner")


def _data(
    db: WorkspaceDb,
    *,
    from_year: str = "2024",
    to_year: str = "2026",
    step: str = "year",
    currency: str = "",
):
    return reports_data(
        db.factory,
        db.ctx("vic", "viewer"),
        from_year=from_year,
        to_year=to_year,
        step=step,
        currency=currency,
    )


def _net_worth_item(db: WorkspaceDb) -> UUID:
    owner = _owner(db)
    category = create_category(db.factory, owner, side="asset", name="Investments", color="#16a34a")
    item = create_item(
        db.factory,
        owner,
        locale="en",
        side="asset",
        category_id=category.id,
        name="Fund",
        value="100",
        currency="EUR",
        include_in_net_worth=True,
        as_of=date(2024, 12, 31),
    )
    add_valuation(db.factory, owner, item.id, locale="en", value="150", as_of=date(2025, 12, 31))
    add_valuation(db.factory, owner, item.id, locale="en", value="200", as_of=date(2026, 6, 30))
    return item.id


def test_net_worth_report_keeps_the_last_point_of_each_year(team: WorkspaceDb) -> None:
    _net_worth_item(team)
    data = _data(team)
    assert data.net_worth is not None
    points = data.net_worth.points
    assert [point.as_of for point in points] == [
        date(2024, 12, 31),
        date(2025, 12, 31),
        date(2026, 12, 15),
    ]
    assert [point.net.amount_minor for point in points] == [10_000, 15_000, 20_000]


def test_net_worth_report_month_step_and_base_currency_only(team: WorkspaceDb) -> None:
    _net_worth_item(team)
    monthly = _data(team, from_year="2026", to_year="2026", step="month")
    assert monthly.net_worth is not None
    assert len(monthly.net_worth.points) == 12
    assert monthly.net_worth.points[-1].as_of == date(2026, 12, 15)
    assert (
        net_worth_report(
            team.factory,
            team.ctx("vic", "viewer"),
            start_year=2024,
            end_year=2026,
            step=ReportStep.YEAR,
            currency="USD",
        )
        is None
    )


def _class_id(db: WorkspaceDb, ctx: Context) -> UUID:
    with db.factory() as session:
        existing = AssetClassRepository(session, ctx).active_by_name_key("stocks")
        if existing is not None:
            return existing.id
    return create_asset_class(db.factory, ctx, name="Stocks", color="#2563eb", investable=True).id


def _instrument(db: WorkspaceDb, ctx: Context) -> UUID:
    return create_instrument(
        db.factory,
        ctx,
        name="World ETF",
        symbol="",
        type="etf",
        quote_currency="EUR",
        unit_label="share",
        allocations=[(_class_id(db, ctx), 10_000)],
        locale="en",
    ).id


def _price(db: WorkspaceDb, ctx: Context, instrument_id: UUID, day: str, value: str) -> None:
    with db.factory.begin() as session:
        PricePointRepository(session, ctx).create(
            instrument_id=instrument_id,
            date=date.fromisoformat(day),
            price=Decimal(value),
            source="manual",
        )


def _portfolio(db: WorkspaceDb) -> None:
    owner = _owner(db)
    instrument_id = _instrument(db, owner)
    create_transaction(
        db.factory,
        owner,
        instrument_id=instrument_id,
        type_value="buy",
        date_value="2024-01-01",
        quantity_value="10",
        unit_price_value="100",
        fee_value="0",
        fx_value="",
        note_value="",
        locale="en",
    )
    _price(db, owner, instrument_id, "2024-12-01", "110")
    _price(db, owner, instrument_id, "2025-12-01", "120")
    _price(db, owner, instrument_id, "2026-12-01", "130")


def test_portfolio_report_keeps_the_last_point_of_each_year(team: WorkspaceDb) -> None:
    _portfolio(team)
    data = _data(team)
    assert data.portfolio is not None
    points = data.portfolio.chart
    assert [point.date.year for point in points] == [2024, 2025, 2026]
    assert [point.value for point in points] == [
        Money(110_000, "EUR"),
        Money(120_000, "EUR"),
        Money(130_000, "EUR"),
    ]
    assert [point.net_invested for point in points] == [Money(100_000, "EUR")] * 3


def test_portfolio_report_month_step_and_base_currency_only(team: WorkspaceDb) -> None:
    _portfolio(team)
    monthly = _data(team, from_year="2026", to_year="2026", step="month")
    assert monthly.portfolio is not None
    assert len(monthly.portfolio.chart) == 12
    assert monthly.portfolio.chart[-1].date == date(2026, 12, 15)
    assert (
        portfolio_report(
            team.factory,
            team.ctx("vic", "viewer"),
            start_year=2024,
            end_year=2026,
            step=ReportStep.YEAR,
            currency="USD",
        )
        is None
    )


def test_phase_b_sections_are_absent_without_data(team: WorkspaceDb) -> None:
    data = _data(team)
    assert data.net_worth is None and data.portfolio is None
    assert data.filters.step is ReportStep.YEAR
