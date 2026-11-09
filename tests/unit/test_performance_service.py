"""Performance service (F6.7): ranges, periods, classes, FX and missing data."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from assetboard.clock import FixedClock
from assetboard.db.repositories import (
    AssetClassRepository,
    BondTermsRepository,
    FxRateRepository,
    InstrumentRepository,
    InvestmentTransactionRepository,
    PricePointRepository,
)
from assetboard.domain.performance import period_return
from assetboard.services.bonds import BondTermsInput, save_bond_terms
from assetboard.services.context import Context
from assetboard.services.investment_transactions import create_transaction
from assetboard.services.investments import create_asset_class, create_instrument
from assetboard.services.performance import (
    DEFAULT_RANGE,
    bond_observations,
    performance_page,
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


def _owner(db: WorkspaceDb) -> Context:
    return db.ctx("olga", "owner")


def _class_id(db: WorkspaceDb, ctx: Context, name: str = "Stocks") -> UUID:
    with db.factory() as session:
        existing = AssetClassRepository(session, ctx).active_by_name_key(name.casefold())
        if existing is not None:
            return existing.id
    return create_asset_class(db.factory, ctx, name=name, color="#2563eb", investable=True).id


def _instrument(
    db: WorkspaceDb,
    ctx: Context,
    *,
    name: str = "World ETF",
    type_value: str = "etf",
    quote_currency: str = "USD",
    allocations: list[tuple[UUID, int]] | None = None,
):
    return create_instrument(
        db.factory,
        ctx,
        name=name,
        symbol="",
        type=type_value,
        quote_currency=quote_currency,
        unit_label="share" if type_value == "etf" else "bond",
        allocations=allocations or [(_class_id(db, ctx), 10_000)],
        locale="en",
    )


def _tx(
    db: WorkspaceDb,
    ctx: Context,
    instrument_id: UUID,
    *,
    day: str = "2024-01-01",
    type_value: str = "buy",
    quantity: str = "10",
    unit_price: str = "100",
    fx: str = "0.9",
):
    return create_transaction(
        db.factory,
        ctx,
        instrument_id=instrument_id,
        type_value=type_value,
        date_value=day,
        quantity_value=quantity,
        unit_price_value=unit_price,
        fee_value="0",
        fx_value=fx,
        note_value="",
        locale="en",
    )


def _price(db: WorkspaceDb, ctx: Context, instrument_id: UUID, day: str, value: str) -> None:
    with db.factory.begin() as session:
        PricePointRepository(session, ctx).create(
            instrument_id=instrument_id,
            date=date.fromisoformat(day),
            price=Decimal(value),
            source="manual",
        )


def _fx(db: WorkspaceDb, ctx: Context, day: str, rate: str) -> None:
    with db.factory.begin() as session:
        FxRateRepository(session, ctx).create(
            base_currency="EUR",
            quote_currency="USD",
            date=date.fromisoformat(day),
            rate=Decimal(rate),
            source="manual",
        )


def test_default_one_year_chart_and_periods(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = _owner(db)
    instrument = _instrument(db, ctx)
    _tx(db, ctx, instrument.id)
    _price(db, ctx, instrument.id, "2024-01-01", "100")
    _price(db, ctx, instrument.id, "2026-03-10", "110")
    _fx(db, ctx, "2024-01-01", "0.9")
    _fx(db, ctx, "2026-03-10", "0.92")

    page = performance_page(db.factory, ctx)

    assert page.range_code == DEFAULT_RANGE == "1y"
    assert page.range_start == date(2025, 3, 10)
    assert page.first_date == date(2024, 1, 1)
    assert [period.code for period in page.periods] == ["1m", "3m", "ytd", "1y", "all"]
    assert page.chart
    assert page.chart[-1].date == date(2026, 3, 10)
    assert page.chart[-1].value.amount_minor == 101_200
    assert page.chart[-1].net_invested.amount_minor == 90_000
    assert page.incomplete_points == 0
    assert page.undated_count == 0
    year = next(period for period in page.periods if period.code == "1y")
    assert year.start_value.amount_minor > 0
    assert year.end_value.amount_minor == 101_200
    assert year.change_excluding_flows.amount_minor == year.change.amount_minor
    assert year.annualized and year.display_rate == year.xirr.rate
    month = next(period for period in page.periods if period.code == "1m")
    assert not month.annualized
    assert month.xirr.rate is not None and month.display_rate is not None
    assert month.display_rate != month.xirr.rate
    all_time = next(period for period in page.periods if period.code == "all")
    assert all_time.start == date(2023, 12, 31)
    assert all_time.start_value.amount_minor == 0
    assert all_time.change_excluding_flows == all_time.change - all_time.net_flows


def test_short_ranges_use_daily_points(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = _owner(db)
    instrument = _instrument(db, ctx)
    _tx(db, ctx, instrument.id)
    _price(db, ctx, instrument.id, "2026-03-10", "110")
    _fx(db, ctx, "2026-03-10", "0.92")
    page = performance_page(db.factory, ctx, range_code="ytd")
    assert page.chart
    assert (page.chart[1].date - page.chart[0].date).days == 1
    assert len(page.chart) >= 60


def test_short_all_time_history_shows_the_period_return(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = _owner(db)
    instrument = _instrument(db, ctx)
    _tx(db, ctx, instrument.id, day="2026-01-10", fx="0.9")
    _price(db, ctx, instrument.id, "2026-03-10", "110")
    page = performance_page(db.factory, ctx)
    all_time = next(period for period in page.periods if period.code == "all")
    assert not all_time.annualized
    assert all_time.start == date(2026, 1, 9)
    assert all_time.xirr.rate is not None and all_time.display_rate is not None
    days = (page.today - all_time.start).days
    assert all_time.display_rate == period_return(all_time.xirr.rate, days)


def test_chart_drops_the_zero_prefix_before_the_first_transaction(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    instrument = _instrument(db, ctx)
    _tx(db, ctx, instrument.id, day="2026-01-10", fx="0.9")

    page = performance_page(db.factory, ctx, range_code="all")

    assert page.chart
    assert page.chart[0].date == date(2026, 1, 10)
    assert all(
        point.value.amount_minor != 0 or point.net_invested.amount_minor != 0
        for point in page.chart
    )


def test_class_scope_uses_look_through_weights(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = _owner(db)
    stocks = _class_id(db, ctx, "Stocks")
    bonds = _class_id(db, ctx, "Bonds")
    instrument = _instrument(db, ctx, allocations=[(stocks, 8_000), (bonds, 2_000)])
    _tx(db, ctx, instrument.id)
    _price(db, ctx, instrument.id, "2026-03-10", "110")
    _fx(db, ctx, "2026-03-10", "0.92")

    total = performance_page(db.factory, ctx)
    stocks_page = performance_page(db.factory, ctx, asset_class_id=stocks)
    bonds_page = performance_page(db.factory, ctx, asset_class_id=bonds)

    assert total.chart[-1].value.amount_minor == 101_200
    assert (
        stocks_page.chart[-1].value.amount_minor + bonds_page.chart[-1].value.amount_minor
        == 101_200
    )
    assert stocks_page.chart[-1].value.amount_minor == 80_960
    assert bonds_page.chart[-1].value.amount_minor == 20_240
    assert (
        stocks_page.chart[-1].net_invested.amount_minor
        + bonds_page.chart[-1].net_invested.amount_minor
        == 90_000
    )


def test_undated_rows_are_counted_but_excluded(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = _owner(db)
    instrument = _instrument(db, ctx)
    _tx(db, ctx, instrument.id)
    with db.factory.begin() as session:
        InvestmentTransactionRepository(session, ctx).create(
            instrument_id=instrument.id,
            date=None,
            type="buy",
            quantity=Decimal("1"),
            unit_price=Decimal("100"),
            quote_currency="USD",
            fee_minor=0,
            fx_to_base=Decimal("0.9"),
        )
    page = performance_page(db.factory, ctx)
    assert page.undated_count == 1
    assert page.chart[-1].net_invested.amount_minor == 90_000


def test_bond_model_is_priced_from_terms_at_each_point(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = _owner(db)
    instrument = _instrument(db, ctx, type_value="bond_model", quote_currency="PLN")
    save_bond_terms(
        db.factory,
        ctx,
        instrument.id,
        values=BondTermsInput(
            series_code="OTS",
            face_value="100",
            maturity_date="2027-01-01",
            interest_mode="capitalized_annually",
            rate_periods=(("2026-01-01", "5"),),
            tax_rate="0",
        ),
        locale="en",
    )
    _tx(db, ctx, instrument.id, day="2026-01-01", unit_price="100", fx="1")
    page = performance_page(db.factory, ctx)
    assert page.chart
    assert page.chart[-1].date == date(2026, 3, 10)
    assert page.chart[-1].value.amount_minor > 100_000  # accrued above face
    assert page.chart[-1].net_invested.amount_minor == 100_000


def test_transaction_prices_and_fx_keep_the_series_complete(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    instrument = _instrument(db, ctx)
    _tx(db, ctx, instrument.id, fx="0.9")
    page = performance_page(db.factory, ctx)
    assert page.chart
    assert page.incomplete_points == 0
    assert all(point.value.amount_minor > 0 for point in page.chart)


def test_a_split_adjusts_the_carried_forward_transaction_price(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    instrument = _instrument(db, ctx, quote_currency="EUR")
    _tx(db, ctx, instrument.id, day="2025-03-01", unit_price="100", fx="1")
    _tx(
        db,
        ctx,
        instrument.id,
        day="2025-06-01",
        type_value="split",
        quantity="2",
        unit_price="1",
        fx="",
    )
    page = performance_page(db.factory, ctx)
    before = [point for point in page.chart if point.date < date(2025, 6, 1)]
    after = [point for point in page.chart if point.date >= date(2025, 6, 1)]
    assert before and after
    assert before[-1].value.amount_minor == after[0].value.amount_minor == 100_000


def test_a_dividend_reduces_net_invested_without_changing_value(
    db: WorkspaceDb, team: WorkspaceDb
) -> None:
    ctx = _owner(db)
    instrument = _instrument(db, ctx, quote_currency="EUR")
    _tx(db, ctx, instrument.id, day="2025-03-01", unit_price="100", fx="1")
    _tx(
        db,
        ctx,
        instrument.id,
        day="2025-06-01",
        type_value="dividend",
        quantity="1",
        unit_price="50",
        fx="1",
    )
    page = performance_page(db.factory, ctx)
    after = [point for point in page.chart if point.date >= date(2025, 6, 1)]
    assert after
    assert after[0].value.amount_minor == 100_000
    assert after[0].net_invested.amount_minor == 95_000  # 1 000.00 − 50.00 net


def test_bond_observations_show_a_coupon_reset(db: WorkspaceDb, team: WorkspaceDb) -> None:
    ctx = _owner(db)
    instrument = _instrument(db, ctx, type_value="bond_model", quote_currency="PLN")
    save_bond_terms(
        db.factory,
        ctx,
        instrument.id,
        values=BondTermsInput(
            series_code="OTS",
            face_value="100",
            maturity_date="2026-01-01",
            interest_mode="paid_out_annually",
            rate_periods=(("2025-01-01", "5"),),
            tax_rate="0",
        ),
        locale="en",
    )
    _tx(db, ctx, instrument.id, day="2025-01-01", unit_price="100", fx="1")
    with db.factory() as session:
        model = InstrumentRepository(session, ctx).get_or_raise(instrument.id)
        terms = BondTermsRepository(session, ctx).for_instrument(instrument.id)
        assert terms is not None
        rows = InvestmentTransactionRepository(session, ctx).for_instrument(instrument.id)
        observations = bond_observations(model, terms, rows, {date(2025, 12, 31), date(2026, 1, 1)})
    values = {observation.date: observation.price for observation in observations}
    assert values[date(2025, 12, 31)] > Decimal("100")
    assert values[date(2026, 1, 1)] == Decimal("100")


def test_empty_workspace_and_viewer_access(db: WorkspaceDb, team: WorkspaceDb) -> None:
    viewer = db.ctx("vic", "viewer")
    page = performance_page(db.factory, viewer)
    assert page.first_date is None
    assert page.chart == ()
    assert page.periods
    assert page.class_options == ()
