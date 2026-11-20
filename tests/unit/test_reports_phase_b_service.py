"""F10.4/F10.5 report series: reuse the derived net-worth and portfolio services."""

from __future__ import annotations

from collections.abc import Collection, Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, insert

from assetboard.clock import FixedClock
from assetboard.db.models.networth import ItemValuation
from assetboard.db.models.prices import PricePoint
from assetboard.db.repositories import (
    AssetClassRepository,
    ItemValuationRepository,
    PricePointRepository,
)
from assetboard.domain.money import Money
from assetboard.domain.performance import SeriesPoint
from assetboard.domain.reports import ReportStep
from assetboard.services.context import Context
from assetboard.services.investment_transactions import create_transaction
from assetboard.services.investments import create_asset_class, create_instrument
from assetboard.services.networth import add_valuation, create_category, create_item
from assetboard.services.networth_history import networth_history
from assetboard.services.performance import PerformancePage, performance_page
from assetboard.services.reports import net_worth_report, portfolio_report, reports_data
from assetboard.web.reports import _portfolio_series
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
    assert [point.date for point in points] == [
        date(2024, 12, 31),
        date(2025, 12, 31),
        date(2026, 12, 15),
    ]
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
    assert [point.date for point in monthly.portfolio.chart] == [
        date(2026, month, 31 if month in (1, 3, 5, 7, 8, 10, 12) else 30)
        if month != 2
        else date(2026, 2, 28)
        for month in range(1, 12)
    ] + [date(2026, 12, 15)]
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


def test_an_incomplete_portfolio_point_is_dashed_and_noted() -> None:
    page = PerformancePage(
        currency="EUR",
        today=date(2026, 12, 15),
        range_code="all",
        range_start=date(2024, 1, 1),
        class_scope=None,
        class_options=(),
        chart=(
            SeriesPoint(
                date=date(2026, 12, 15),
                value=Money(0, "EUR"),
                net_invested=Money(100_000, "EUR"),
                incomplete=True,
            ),
        ),
        periods=(),
        undated_count=1,
        incomplete_points=1,
        first_date=date(2024, 1, 1),
    )
    view = _portfolio_series(page, ReportStep.YEAR, "en", date(2026, 12, 15))
    assert view is not None
    assert view.rows[0].cells == ("—", "—", "—")
    assert view.rows[0].so_far is True
    assert "Incomplete: an instrument has no price or FX rate." in view.rows[0].notes
    assert any("undated legacy transaction" in note for note in view.notes)
    assert any("chart point is incomplete" in note for note in view.notes)
    assert view.chart["value"] == [None]


def test_phase_b_sections_are_absent_for_empty_future_ranges(team: WorkspaceDb) -> None:
    _net_worth_item(team)
    _portfolio(team)
    future = _data(team, from_year="2030", to_year="2030")
    assert future.net_worth is None and future.portfolio is None


def test_portfolio_dates_are_sorted_deduplicated_and_clipped(team: WorkspaceDb) -> None:
    _portfolio(team)
    page = performance_page(
        team.factory,
        team.ctx("vic", "viewer"),
        range_code="all",
        with_periods=False,
        dates=(
            date(2026, 1, 31),
            date(2025, 12, 31),
            date(2025, 12, 31),
            date(2030, 1, 1),
        ),
    )
    assert [point.date for point in page.chart] == [
        date(2025, 12, 31),
        date(2026, 1, 31),
    ]


def test_a_future_net_worth_range_does_not_read_the_history(
    team: WorkspaceDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("networth_history must not run for a future-only range")

    monkeypatch.setattr("assetboard.services.reports.networth_history", fail)
    data = _data(team, from_year="2030", to_year="2030")
    assert data.net_worth is None


def _month(day: date, offset: int) -> date:
    year = day.year + (day.month - 1 + offset) // 12
    month = (day.month - 1 + offset) % 12 + 1
    return date(year, month, 28)


def _ten_years(team: WorkspaceDb) -> None:
    """A ten-year net-worth and price history; the report below asks only for 2026."""
    owner = _owner(team)
    category = create_category(
        team.factory, owner, side="asset", name="Long history", color="#16a34a"
    )
    item = create_item(
        team.factory,
        owner,
        locale="en",
        side="asset",
        category_id=category.id,
        name="Long fund",
        value="100",
        currency="EUR",
        include_in_net_worth=True,
        as_of=date(2016, 1, 15),
    )
    instrument_id = _instrument(team, owner)
    create_transaction(
        team.factory,
        owner,
        instrument_id=instrument_id,
        type_value="buy",
        date_value="2016-01-04",
        quantity_value="10",
        unit_price_value="100",
        fee_value="0",
        fx_value="",
        note_value="",
        locale="en",
    )
    with team.factory.begin() as session:
        valuations = [
            {
                "id": uuid4(),
                "workspace_id": team.workspace_id,
                "item_id": item.id,
                "as_of": _month(date(2016, 1, 1), offset),
                "value_minor": 10_000_000 + offset * 10_000,
                "currency": "EUR",
                "note": "",
                "created_at": datetime(2016, 1, 1, tzinfo=UTC),
                "updated_at": datetime(2016, 1, 1, tzinfo=UTC),
            }
            for offset in range(1, 132)
            if _month(date(2016, 1, 1), offset) < date(2026, 12, 15)
        ]
        session.execute(insert(ItemValuation), valuations)
        prices = [
            {
                "id": uuid4(),
                "workspace_id": team.workspace_id,
                "instrument_id": instrument_id,
                "date": _month(date(2016, 1, 1), offset),
                "price": Decimal(100 + offset),
                "source": "manual",
                "provider_key": None,
                "import_key": None,
                "import_batch_id": None,
                "created_at": datetime(2016, 1, 1, tzinfo=UTC),
                "updated_at": datetime(2016, 1, 1, tzinfo=UTC),
            }
            for offset in range(0, 132)
            if _month(date(2016, 1, 1), offset) < date(2026, 12, 15)
        ]
        session.execute(insert(PricePoint), prices)


def test_a_short_report_reads_only_its_range(
    team: WorkspaceDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ten_years(team)
    valuation_rows: list[int] = []
    price_rows: list[int] = []
    original_valuations = ItemValuationRepository.for_items_in_window
    original_prices = PricePointRepository.for_instruments_in_window

    def record_valuations(
        self: ItemValuationRepository, item_ids: Collection[UUID], *, start: date, end: date
    ):
        rows = original_valuations(self, item_ids, start=start, end=end)
        valuation_rows.append(len(rows))
        return rows

    def record_prices(
        self: PricePointRepository,
        instrument_ids: Collection[UUID],
        *,
        start: date,
        end: date,
    ):
        rows = original_prices(self, instrument_ids, start=start, end=end)
        price_rows.append(len(rows))
        return rows

    monkeypatch.setattr(ItemValuationRepository, "for_items_in_window", record_valuations)
    monkeypatch.setattr(PricePointRepository, "for_instruments_in_window", record_prices)
    statements: list[str] = []

    def capture(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        statements.append(statement)

    event.listen(team.engine, "before_cursor_execute", capture)
    started = perf_counter()
    try:
        data = _data(team, from_year="2026", to_year="2026", step="month")
    finally:
        event.remove(team.engine, "before_cursor_execute", capture)
    elapsed = perf_counter() - started

    assert data.net_worth is not None and data.portfolio is not None
    # 12 valuations and 12 prices per series in 2026, not the ten-year history.
    assert valuation_rows and sum(valuation_rows) <= 13
    assert price_rows and sum(price_rows) <= 13
    # Phase A's scoped reads plus the two Phase B sections; no per-month query.
    assert len(statements) <= 25
    assert elapsed < 1.5, f"reports read took {elapsed:.3f}s"


def test_a_short_net_worth_report_matches_the_full_history(team: WorkspaceDb) -> None:
    _ten_years(team)
    ctx = team.ctx("vic", "viewer")
    data = _data(team, from_year="2026", to_year="2026", step="month")
    assert data.net_worth is not None
    full = networth_history(team.factory, ctx, "all")
    expected = {point.as_of: point.net for point in full.points if point.as_of.year == 2026}
    assert {point.as_of: point.net for point in data.net_worth.points} == expected


def test_a_historical_report_keeps_the_estimate_lookahead(team: WorkspaceDb) -> None:
    owner = _owner(team)
    category = create_category(team.factory, owner, side="asset", name="Estimated", color="#16a34a")
    item = create_item(
        team.factory,
        owner,
        locale="en",
        side="asset",
        category_id=category.id,
        name="Flat",
        value="100",
        currency="EUR",
        include_in_net_worth=True,
        estimate_between=True,
        as_of=date(2024, 1, 1),
    )
    add_valuation(team.factory, owner, item.id, locale="en", value="300", as_of=date(2025, 1, 1))
    ctx = team.ctx("vic", "viewer")
    full = networth_history(team.factory, ctx, "all")
    expected = next(point for point in full.points if point.as_of == date(2024, 12, 31))
    data = _data(team, from_year="2024", to_year="2024", step="year")
    assert data.net_worth is not None
    assert data.net_worth.points[-1].as_of == date(2024, 12, 31)
    assert data.net_worth.points[-1].assets == expected.assets
    assert data.net_worth.points[-1].breakdown.estimated == 1


def test_a_historical_portfolio_report_keeps_the_backfill_lookahead(team: WorkspaceDb) -> None:
    owner = _owner(team)
    instrument_id = _instrument(team, owner)
    create_transaction(
        team.factory,
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
    _price(team, owner, instrument_id, "2025-01-15", "150")
    data = _data(team, from_year="2024", to_year="2024", step="year")
    assert data.portfolio is not None
    assert data.portfolio.chart
    assert data.portfolio.chart[-1].date == date(2024, 12, 31)
    assert not data.portfolio.chart[-1].incomplete
