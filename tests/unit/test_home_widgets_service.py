"""Home widget performance (F11.12): bounded reads, caching and write invalidation."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Any
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
from assetboard.services.dashboard import (
    home_net_worth_history,
    home_portfolio_history,
    home_portfolio_page,
)
from assetboard.services.home_cache import clear_home_cache
from assetboard.services.investment_transactions import create_transaction
from assetboard.services.investments import create_instrument, seed_asset_classes
from assetboard.services.networth import add_valuation, create_category, create_item
from assetboard.services.prices import save_manual_price
from tests.tenant.workspace_db import WorkspaceDb, workspace_db

TODAY = "2026-12-15T12:00:00+00:00"


@pytest.fixture
def team(tmp_path: Path) -> Iterator[WorkspaceDb]:
    for prepared in workspace_db(tmp_path):
        clock = FixedClock(TODAY)
        prepared.clock = clock
        prepared.factory.configure(info={"clock": clock})
        prepared.add_member("olga", "owner")
        clear_home_cache()
        yield prepared
    clear_home_cache()


@contextmanager
def _queries(engine: Any) -> Iterator[list[str]]:
    statements: list[str] = []

    def before_cursor_execute(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: Any,
    ) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)


def _month(day: date, offset: int) -> date:
    year = day.year + (day.month - 1 + offset) // 12
    month = (day.month - 1 + offset) % 12 + 1
    return date(year, month, 28)


def _seed(team: WorkspaceDb) -> tuple[UUID, UUID]:
    """Ten years of net-worth valuations and monthly prices for one item and one instrument."""
    ctx = team.ctx("olga", "owner")
    category = create_category(team.factory, ctx, side="asset", name="Assets", color="#16a34a")
    item = create_item(
        team.factory,
        ctx,
        locale="en",
        side="asset",
        category_id=category.id,
        name="Fund",
        value="100",
        currency="EUR",
        include_in_net_worth=True,
        as_of=date(2016, 1, 15),
    )
    seed_asset_classes(team.factory, ctx, locale="en")
    with team.factory() as session:
        class_id = AssetClassRepository(session, ctx).ordered()[0].id
    instrument = create_instrument(
        team.factory,
        ctx,
        name="World ETF",
        symbol="WLD",
        type="etf",
        quote_currency="EUR",
        unit_label="share",
        allocations=[(class_id, 10_000)],
        locale="en",
    )
    create_transaction(
        team.factory,
        ctx,
        instrument_id=instrument.id,
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
        valuations = []
        for offset in range(1, 130):
            day = _month(date(2016, 1, 28), offset)
            if day >= date(2026, 12, 15):
                break
            valuations.append(
                {
                    "id": uuid4(),
                    "workspace_id": team.workspace_id,
                    "item_id": item.id,
                    "as_of": day,
                    "value_minor": 10_000_000 + offset * 10_000,
                    "currency": "EUR",
                    "note": "",
                    "created_at": datetime(2016, 1, 1, tzinfo=UTC),
                    "updated_at": datetime(2016, 1, 1, tzinfo=UTC),
                }
            )
        session.execute(insert(ItemValuation), valuations)
        prices = []
        for offset in range(0, 130):
            day = _month(date(2016, 1, 4), offset)
            if day > date(2026, 12, 1):
                break
            prices.append(
                {
                    "id": uuid4(),
                    "workspace_id": team.workspace_id,
                    "instrument_id": instrument.id,
                    "date": day,
                    "price": Decimal(100 + offset),
                    "source": "manual",
                    "provider_key": None,
                    "import_key": None,
                    "import_batch_id": None,
                    "created_at": datetime(2016, 1, 1, tzinfo=UTC),
                    "updated_at": datetime(2016, 1, 1, tzinfo=UTC),
                }
            )
        session.execute(insert(PricePoint), prices)
    return item.id, instrument.id


def test_home_net_worth_reads_only_the_widget_range(
    team: WorkspaceDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(team)
    ctx = team.ctx("olga", "owner")
    loaded: list[tuple[date, date, int]] = []
    original = ItemValuationRepository.for_items_in_window

    def recording(self: ItemValuationRepository, item_ids: Any, *, start: date, end: date):
        rows = original(self, item_ids, start=start, end=end)
        loaded.append((start, end, len(rows)))
        return rows

    monkeypatch.setattr(ItemValuationRepository, "for_items_in_window", recording)
    started = perf_counter()
    with _queries(team.engine) as statements:
        data = home_net_worth_history(team.factory, ctx, "1y")
    elapsed = perf_counter() - started

    assert data.points
    assert loaded and sum(row[2] for row in loaded) <= 14
    assert len(statements) <= 12
    assert elapsed < 1.0, f"net-worth widget took {elapsed:.3f}s"


def test_home_net_worth_cache_hit_uses_no_sql(team: WorkspaceDb) -> None:
    _seed(team)
    ctx = team.ctx("olga", "owner")
    first = home_net_worth_history(team.factory, ctx, "1y")
    with _queries(team.engine) as statements:
        second = home_net_worth_history(team.factory, ctx, "1y")
    assert second is first
    assert statements == []


def test_home_net_worth_cache_is_invalidated_by_a_write(team: WorkspaceDb) -> None:
    item_id, _instrument_id = _seed(team)
    ctx = team.ctx("olga", "owner")
    before = home_net_worth_history(team.factory, ctx, "1y").points[-1].net
    add_valuation(
        team.factory,
        ctx,
        item_id,
        locale="en",
        value="999999",
        as_of=date(2026, 12, 15),
    )
    after = home_net_worth_history(team.factory, ctx, "1y").points[-1].net
    assert after != before


def test_home_net_worth_cache_is_invalidated_by_a_bulk_write(team: WorkspaceDb) -> None:
    item_id, _instrument_id = _seed(team)
    ctx = team.ctx("olga", "owner")
    before = home_net_worth_history(team.factory, ctx, "1y").points[-1].net
    with team.factory.begin() as session:
        session.execute(
            insert(ItemValuation),
            [
                {
                    "id": uuid4(),
                    "workspace_id": team.workspace_id,
                    "item_id": item_id,
                    "as_of": date(2026, 12, 15),
                    "value_minor": 77_000_000,
                    "currency": "EUR",
                    "note": "",
                    "created_at": datetime(2026, 12, 15, tzinfo=UTC),
                    "updated_at": datetime(2026, 12, 15, tzinfo=UTC),
                }
            ],
        )
    after = home_net_worth_history(team.factory, ctx, "1y").points[-1].net
    assert after != before


def test_home_portfolio_history_reads_only_the_widget_range(
    team: WorkspaceDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(team)
    ctx = team.ctx("olga", "owner")
    loaded: list[int] = []
    original = PricePointRepository.for_instruments_in_window

    def recording(self: PricePointRepository, instrument_ids: Any, *, start: date, end: date):
        rows = original(self, instrument_ids, start=start, end=end)
        loaded.append(len(rows))
        return rows

    monkeypatch.setattr(PricePointRepository, "for_instruments_in_window", recording)
    started = perf_counter()
    with _queries(team.engine) as statements:
        page = home_portfolio_history(team.factory, ctx, "1y")
    elapsed = perf_counter() - started

    assert page.chart
    assert loaded and sum(loaded) <= 14
    assert len(statements) <= 12
    assert elapsed < 1.0, f"portfolio widget took {elapsed:.3f}s"


def test_home_portfolio_cache_is_invalidated_by_a_price_write(team: WorkspaceDb) -> None:
    _item_id, instrument_id = _seed(team)
    ctx = team.ctx("olga", "owner")
    before = home_portfolio_history(team.factory, ctx, "1y").chart[-1].value
    save_manual_price(
        team.factory,
        ctx,
        instrument_id,
        price_id=None,
        date_value="2026-12-15",
        price_value="999",
        locale="en",
    )
    after = home_portfolio_history(team.factory, ctx, "1y").chart[-1].value
    assert after != before


def test_home_portfolio_page_cache_hit_uses_no_sql(team: WorkspaceDb) -> None:
    _seed(team)
    ctx = team.ctx("olga", "owner")
    first = home_portfolio_page(team.factory, ctx)
    with _queries(team.engine) as statements:
        second = home_portfolio_page(team.factory, ctx)
    assert second is first
    assert statements == []
