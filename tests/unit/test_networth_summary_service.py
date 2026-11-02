"""Net-worth summary service (F5.2): current values, exclusions and historical as-of reads."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from assetboard.domain.money import Money
from assetboard.services import networth as service
from assetboard.services.context import Context
from assetboard.services.networth import (
    add_valuation,
    archive_category,
    archive_item,
    create_category,
    create_item,
    current_values,
)
from assetboard.services.networth_summary import networth_summary
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


@pytest.fixture
def db(tmp_path: Path) -> Iterator[WorkspaceDb]:
    yield from workspace_db(tmp_path)


@pytest.fixture
def team(db: WorkspaceDb) -> WorkspaceDb:
    db.add_member("olga", "owner")
    db.add_member("ed", "editor")
    db.add_member("vic", "viewer")
    return db


def _category(
    db: WorkspaceDb, ctx: Context, name: str, side: str = "asset"
) -> service.CategoryView:
    return create_category(db.factory, ctx, side=side, name=name, color="#16a34a")


def _item(
    db: WorkspaceDb,
    ctx: Context,
    category: service.CategoryView,
    *,
    name: str = "Item",
    value: str = "100",
    currency: str = "EUR",
    include_in_net_worth: bool = True,
    as_of: date | None = None,
) -> service.ItemView:
    return create_item(
        db.factory,
        ctx,
        locale="en",
        side=category.side,
        category_id=category.id,
        name=name,
        value=value,
        currency=currency,
        include_in_net_worth=include_in_net_worth,
        as_of=as_of,
    )


def test_current_values_uses_the_requested_as_of_not_the_clock(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx, "Vehicles")
    item = _item(team, ctx, category, name="Car", value="100", as_of=date(2026, 1, 1))
    add_valuation(
        team.factory,
        ctx,
        item.id,
        locale="en",
        value="200",
        as_of=date(2026, 3, 1),
    )
    with team.factory() as session:
        february = current_values(session, ctx, as_of=date(2026, 2, 1), base_currency="EUR")
        march = current_values(session, ctx, as_of=date(2026, 3, 1), base_currency="EUR")
        history = current_values(
            session,
            ctx,
            as_of=date(2026, 2, 1),
            base_currency="EUR",
            include_history=True,
        )
    assert february[0].current is not None
    assert february[0].current.amount_minor == 10000
    assert march[0].current is not None and march[0].current.amount_minor == 20000
    assert february[0].valuations == () and february[0].sparkline == ""
    assert len(history[0].valuations) == 2
    assert history[0].sparkline


def test_summary_uses_current_values_and_category_metadata(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    vehicles = _category(team, ctx, "Vehicles")
    mortgage = _category(team, ctx, "Mortgage", side="liability")
    _item(team, ctx, vehicles, name="Car", value="12000")
    _item(team, ctx, mortgage, name="Mortgage", value="5000")
    data = networth_summary(team.factory, ctx)
    assert (data.summary.assets, data.summary.liabilities, data.summary.net) == (
        Money(1_200_000, "EUR"),
        Money(500_000, "EUR"),
        Money(700_000, "EUR"),
    )
    assert data.categories[vehicles.id].name == "Vehicles"
    assert data.categories[vehicles.id].color == "#16a34a"
    assert data.summary.asset_categories[0].share_tenths == 1000
    assert data.summary.liability_categories[0].share_tenths == 1000


def test_category_ties_keep_the_f5_1_category_order(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    first = _category(team, ctx, "First")
    second = _category(team, ctx, "Second")
    _item(team, ctx, first, name="A", value="100")
    _item(team, ctx, second, name="B", value="100")
    data = networth_summary(team.factory, ctx)
    assert [row.category_id for row in data.summary.asset_categories] == [
        first.id,
        second.id,
    ]


def test_summary_reports_excluded_missing_and_non_base_items(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    vehicles = _category(team, ctx, "Vehicles")
    _item(team, ctx, vehicles, name="Car", value="100")
    excluded = _item(team, ctx, vehicles, name="Excluded", value="200", include_in_net_worth=False)
    missing = _item(team, ctx, vehicles, name="Later", value="300", as_of=date(2026, 4, 1))
    foreign = _item(team, ctx, vehicles, name="Boat", value="400", currency="USD")
    data = networth_summary(team.factory, ctx)
    assert data.summary.assets == Money(10000, "EUR")
    assert [item.id for item in data.excluded_items] == [excluded.id]
    assert [item.id for item in data.no_value_items] == [missing.id]
    assert [item.id for item in data.non_base_items] == [foreign.id]
    assert [row.category_id for row in data.summary.asset_categories] == [vehicles.id]
    assert data.summary.net == Money(10000, "EUR")


def test_archived_items_are_excluded_but_archived_categories_count(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    vehicles = _category(team, ctx, "Vehicles")
    old = _category(team, ctx, "Old")
    archived_item = _item(team, ctx, vehicles, name="Sold", value="900")
    archive_item(team.factory, ctx, archived_item.id)
    old_item = _item(team, ctx, old, name="Antique", value="300")
    archive_category(team.factory, ctx, old.id)
    data = networth_summary(team.factory, ctx)
    assert data.summary.assets == Money(30000, "EUR")
    assert [row.category_id for row in data.summary.asset_categories] == [old.id]
    assert old_item.id not in {item.id for item in data.no_value_items}
    assert archived_item.id not in {item.id for item in data.excluded_items}


def test_viewers_can_read_the_summary(team: WorkspaceDb) -> None:
    ctx = team.ctx("vic", "viewer")
    data = networth_summary(team.factory, ctx)
    assert data.summary.assets == Money(0, "EUR")


def test_another_workspaces_items_do_not_leak(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx, "Vehicles")
    _item(team, ctx, category, name="Car", value="100")
    other = team.add_workspace("Other")
    team.add_member("ed", "editor", other)
    foreign = team.ctx("ed", "editor", other)
    data = networth_summary(team.factory, foreign)
    assert data.summary.assets == Money(0, "EUR")
    assert data.summary.asset_categories == ()
    assert data.categories == {}
