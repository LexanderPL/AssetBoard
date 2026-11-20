"""Derived net-worth history service (F5.9): points, clipping, estimates, links and scoping."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from assetboard.clock import FixedClock
from assetboard.db.models.identity import Workspace
from assetboard.db.models.networth import NetWorthSnapshot
from assetboard.db.repositories import ItemValuationRepository, NetWorthSnapshotRepository
from assetboard.domain.money import Money
from assetboard.services import networth as service
from assetboard.services.context import Context, TwoFactorRequired
from assetboard.services.networth import (
    archive_item,
    create_category,
    create_item,
    update_item,
    update_valuation,
)
from assetboard.services.networth_history import networth_history
from assetboard.services.networth_summary import networth_summary
from tests.fake_links import FakeLinks, FakeTarget
from tests.tenant.workspace_db import WorkspaceDb, workspace_db

LINK_TYPE = "portfolio_asset_class"


@pytest.fixture
def db(tmp_path: Path) -> Iterator[WorkspaceDb]:
    yield from workspace_db(tmp_path)


@pytest.fixture
def team(db: WorkspaceDb) -> WorkspaceDb:
    db.add_member("olga", "owner")
    db.add_member("ed", "editor")
    db.add_member("vic", "viewer")
    return db


def _at(team: WorkspaceDb, iso: str) -> None:
    clock = FixedClock(iso)
    team.clock = clock
    team.factory.kw["info"]["clock"] = clock


def _category(
    db: WorkspaceDb, ctx: Context, name: str = "Vehicles", side: str = "asset"
) -> service.CategoryView:
    return create_category(db.factory, ctx, side=side, name=name, color="#16a34a")


def _item(
    db: WorkspaceDb,
    ctx: Context,
    category: service.CategoryView,
    *,
    name: str = "Car",
    value: str = "100",
    currency: str = "EUR",
    include_in_net_worth: bool = True,
    estimate_between: bool = False,
    as_of: date | None = None,
    link_type: str | None = None,
    link_id=None,
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
        estimate_between=estimate_between,
        as_of=as_of,
        link_type=link_type,
        link_id=link_id,
    )


@contextmanager
def _queries(engine) -> Iterator[list[str]]:
    statements: list[str] = []

    def before_cursor_execute(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)


def test_history_has_one_point_per_completed_month_end_plus_today(team: WorkspaceDb) -> None:
    _at(team, "2026-11-16T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    _item(team, ctx, category, value="100", as_of=date(2024, 1, 15))

    data = networth_history(team.factory, ctx, "1y")
    assert data.range_code == "1y" and data.today == date(2026, 11, 16)
    assert len(data.points) == 13
    assert data.points[0].as_of == date(2025, 11, 30)
    assert data.points[-1].as_of == date(2026, 11, 16)
    assert all(not point.live for point in data.points[:-1])
    assert data.points[-1].live
    assert all(point.assets == Money(10_000, "EUR") for point in data.points)
    assert data.has_chart


def test_fixed_ranges_clip_to_the_first_value(team: WorkspaceDb) -> None:
    _at(team, "2026-11-16T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    _item(team, ctx, category, value="100", as_of=date(2026, 9, 15))

    data = networth_history(team.factory, ctx, "1y")
    assert [point.as_of for point in data.points] == [
        date(2026, 9, 30),
        date(2026, 10, 31),
        date(2026, 11, 16),
    ]


def test_all_starts_at_the_earliest_value_and_ignores_future_values(
    team: WorkspaceDb,
) -> None:
    _at(team, "2026-11-16T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    _item(team, ctx, category, value="100", as_of=date(2024, 1, 15))
    _item(team, ctx, category, name="Future", value="900", as_of=date(2199, 1, 1))

    data = networth_history(team.factory, ctx, "all")
    assert data.points[0].as_of == date(2024, 1, 31)
    assert data.points[-1].as_of == date(2026, 11, 16)
    assert len(data.points) == 35


def test_a_later_item_has_no_value_in_earlier_points(team: WorkspaceDb) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    _item(team, ctx, category, name="Older", value="100", as_of=date(2024, 1, 15))
    _item(team, ctx, category, name="Newer", value="50", as_of=date(2026, 2, 1))

    data = networth_history(team.factory, ctx, "all")
    january = data.points[0]
    february = next(point for point in data.points if point.as_of == date(2026, 2, 28))
    assert january.as_of == date(2024, 1, 31)
    assert january.assets == Money(10_000, "EUR")
    assert [row.total_minor for row in january.breakdown.categories] == [10_000]
    assert february.assets == Money(15_000, "EUR")


def test_no_data_returns_no_points_and_no_chart(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    data = networth_history(team.factory, ctx, "1y")
    assert data.points == ()
    assert data.has_values is False
    assert data.has_chart is False


def test_items_without_values_return_no_points_and_no_chart(team: WorkspaceDb) -> None:
    _at(team, "2026-11-16T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    _item(team, ctx, category, value="900", as_of=date(2199, 1, 1))
    data = networth_history(team.factory, ctx, "1y")
    assert data.points == () and data.has_values is False


def test_past_month_ends_use_estimate_between(team: WorkspaceDb) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    item = _item(
        team,
        ctx,
        category,
        value="100",
        estimate_between=True,
        as_of=date(2026, 1, 1),
    )
    service.add_valuation(
        team.factory, ctx, item.id, locale="en", value="300", as_of=date(2026, 3, 1)
    )

    data = networth_history(team.factory, ctx, "1y")
    february = next(point for point in data.points if point.as_of == date(2026, 2, 28))
    assert february.assets == Money(29_661, "EUR")
    assert february.breakdown.estimated == 1
    assert data.points[-1].live
    assert data.points[-1].assets == Money(30_000, "EUR")


def test_today_is_live_and_never_estimated(team: WorkspaceDb) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    item = _item(
        team,
        ctx,
        category,
        value="100",
        estimate_between=True,
        as_of=date(2026, 1, 1),
    )
    service.add_valuation(
        team.factory, ctx, item.id, locale="en", value="300", as_of=date(2026, 3, 1)
    )

    data = networth_history(team.factory, ctx, "1y")
    today = data.points[-1]
    assert today.as_of == date(2026, 3, 15) and today.live
    assert today.assets == Money(30_000, "EUR")
    assert today.breakdown.estimated == 0


def test_estimate_switch_off_carries_the_last_value(team: WorkspaceDb) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    item = _item(
        team,
        ctx,
        category,
        value="100",
        estimate_between=False,
        as_of=date(2026, 1, 1),
    )
    service.add_valuation(
        team.factory, ctx, item.id, locale="en", value="300", as_of=date(2026, 3, 1)
    )

    data = networth_history(team.factory, ctx, "1y")
    february = next(point for point in data.points if point.as_of == date(2026, 2, 28))
    assert february.assets == Money(10_000, "EUR")
    assert february.breakdown.estimated == 0


def test_explicit_dates_before_the_first_manual_value_are_dropped(team: WorkspaceDb) -> None:
    _at(team, "2026-12-15T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    _item(team, ctx, category, value="100", as_of=date(2025, 6, 30))
    data = networth_history(
        team.factory,
        ctx,
        dates=(date(2024, 12, 31), date(2025, 6, 30), date(2025, 12, 31)),
    )
    assert [point.as_of for point in data.points] == [
        date(2025, 6, 30),
        date(2025, 12, 31),
    ]


def test_explicit_dates_before_a_linked_item_creation_are_dropped(
    team: WorkspaceDb, fake_links: FakeLinks
) -> None:
    _at(team, "2025-01-01T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    target = FakeTarget(
        workspace_id=team.workspace_id,
        link_id=uuid4(),
        label="Holding",
        currency="EUR",
        values={date(2025, 1, 31): 100, date(2026, 1, 31): 200},
    )
    fake_links.register(targets=[target])
    _item(team, ctx, category, name="Holding", link_type=LINK_TYPE, link_id=target.link_id)
    _at(team, "2026-03-15T12:00:00+00:00")
    data = networth_history(
        team.factory,
        team.ctx("ed", "editor"),
        dates=(date(2024, 12, 31), date(2025, 1, 31), date(2026, 1, 31)),
    )
    assert [point.as_of for point in data.points] == [date(2025, 1, 31), date(2026, 1, 31)]


def test_linked_values_use_resolver_history_and_live_today(
    team: WorkspaceDb, fake_links: FakeLinks
) -> None:
    _at(team, "2025-01-01T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    target = FakeTarget(
        workspace_id=team.workspace_id,
        link_id=uuid4(),
        label="Holding",
        values={
            date(2026, 1, 31): 100,
            date(2026, 2, 28): 200,
            date(2026, 3, 15): 300,
        },
    )
    fake_links.register(targets=[target])
    _item(team, ctx, category, name="Holding", link_type=LINK_TYPE, link_id=target.link_id)
    _at(team, "2026-03-15T12:00:00+00:00")

    data = networth_history(team.factory, team.ctx("ed", "editor"), "1y")
    february = next(point for point in data.points if point.as_of == date(2026, 2, 28))
    today = data.points[-1]
    assert february.assets == Money(200, "EUR")
    assert today.assets == Money(300, "EUR") and today.live


def test_linked_item_creation_is_the_earliest_supported_start(
    team: WorkspaceDb, fake_links: FakeLinks
) -> None:
    _at(team, "2025-01-01T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    target = FakeTarget(
        workspace_id=team.workspace_id,
        link_id=uuid4(),
        label="Holding",
        values={
            date(2025, 1, 31): 100,
            date(2025, 2, 28): 200,
            date(2026, 3, 15): 300,
        },
    )
    fake_links.register(targets=[target])
    _item(team, ctx, category, name="Holding", link_type=LINK_TYPE, link_id=target.link_id)
    _at(team, "2026-03-15T12:00:00+00:00")

    data = networth_history(team.factory, team.ctx("ed", "editor"), "all")
    # The adapter cannot resolve a date before the item existed, so January 2025 is the start.
    assert data.points[0].as_of == date(2025, 1, 31)
    assert data.points[0].assets == Money(100, "EUR")
    assert data.points[1].as_of == date(2025, 2, 28)
    assert data.points[1].assets == Money(200, "EUR")
    assert data.points[-1].as_of == date(2026, 3, 15)
    assert data.points[-1].assets == Money(300, "EUR")


def test_linked_resolution_is_one_batch_call_for_all_dates(
    team: WorkspaceDb, fake_links: FakeLinks
) -> None:
    _at(team, "2025-01-01T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    target = FakeTarget(
        workspace_id=team.workspace_id,
        link_id=uuid4(),
        label="Holding",
        values={date(2026, 1, 31): 100, date(2026, 2, 28): 200, date(2026, 3, 15): 300},
    )
    spec = fake_links.register(targets=[target])
    _item(team, ctx, category, name="Holding", link_type=LINK_TYPE, link_id=target.link_id)
    spec.calls.clear()
    _at(team, "2026-03-15T12:00:00+00:00")

    networth_history(team.factory, team.ctx("ed", "editor"), "1y")
    assert len(spec.calls) == 1
    workspace_id, link_ids, dates = spec.calls[0]
    assert workspace_id == team.workspace_id
    assert link_ids == {target.link_id}
    assert date(2026, 1, 31) in dates and date(2026, 2, 28) in dates
    assert dates[-1] == date(2026, 3, 15)


def test_unavailable_linked_item_is_not_counted_and_is_flagged(
    team: WorkspaceDb, fake_links: FakeLinks
) -> None:
    _at(team, "2026-01-01T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    target = FakeTarget(workspace_id=team.workspace_id, link_id=uuid4(), label="Gone")
    fake_links.register(targets=[target])
    _item(team, ctx, category, name="Holding", link_type=LINK_TYPE, link_id=target.link_id)
    _at(team, "2026-03-15T12:00:00+00:00")

    data = networth_history(team.factory, team.ctx("ed", "editor"), "1y")
    assert data.points
    assert all(point.assets == Money(0, "EUR") for point in data.points)
    assert all(point.breakdown.unavailable == 1 for point in data.points)
    assert data.has_values is False
    assert data.has_chart is False


def test_editing_a_valuation_changes_the_derived_points(team: WorkspaceDb) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    item = _item(team, ctx, category, value="100", as_of=date(2026, 1, 1))
    with team.factory() as session:
        valuation = ItemValuationRepository(session, ctx).for_item(item.id)[0]

    before = networth_history(team.factory, ctx, "1y")
    january = next(point for point in before.points if point.as_of == date(2026, 1, 31))
    assert january.assets == Money(10_000, "EUR")

    update_valuation(
        team.factory,
        ctx,
        item.id,
        valuation.id,
        locale="en",
        value="250",
        as_of=date(2026, 2, 15),
        note="corrected",
    )
    after = networth_history(team.factory, ctx, "1y")
    assert after.points[0].as_of == date(2026, 2, 28)
    assert after.points[0].assets == Money(25_000, "EUR")
    assert after.points[-1].assets == Money(25_000, "EUR")


def test_archived_item_counts_up_to_its_archive_date(team: WorkspaceDb) -> None:
    _at(team, "2026-03-01T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    item = _item(team, ctx, category, value="100", as_of=date(2025, 1, 1))
    archive_item(team.factory, ctx, item.id)

    data = networth_history(team.factory, team.ctx("ed", "editor"), "1y")
    february = next(point for point in data.points if point.as_of == date(2026, 2, 28))
    march = data.points[-1]
    assert february.assets == Money(10_000, "EUR")
    assert march.assets == Money(0, "EUR")


def test_including_an_item_toggle_changes_every_point(team: WorkspaceDb) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    item = _item(team, ctx, category, value="100", as_of=date(2025, 1, 1))
    update_item(
        team.factory,
        ctx,
        item.id,
        category_id=category.id,
        name=item.name,
        currency=item.currency,
        notes="",
        acquired_on=None,
        include_in_net_worth=False,
        investable=False,
        estimate_between=False,
    )
    data = networth_history(team.factory, ctx, "1y")
    assert all(point.assets == Money(0, "EUR") for point in data.points)
    assert data.has_recorded_values is True
    assert data.has_values is False and data.has_chart is False

    update_item(
        team.factory,
        ctx,
        item.id,
        category_id=category.id,
        name=item.name,
        currency=item.currency,
        notes="",
        acquired_on=None,
        include_in_net_worth=True,
        investable=False,
        estimate_between=False,
    )
    data = networth_history(team.factory, ctx, "1y")
    assert all(point.assets == Money(10_000, "EUR") for point in data.points)
    assert data.has_values is True and data.has_chart is True


def test_a_zero_recorded_value_still_counts_as_data(team: WorkspaceDb) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    _item(team, ctx, category, value="0", as_of=date(2025, 1, 1))

    data = networth_history(team.factory, ctx, "1y")
    assert data.has_values is True and data.has_chart is True
    assert all(point.assets == Money(0, "EUR") for point in data.points)


def test_non_base_items_are_excluded_from_every_point(team: WorkspaceDb) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    _item(team, ctx, category, name="Car", value="100", as_of=date(2025, 1, 1))
    _item(
        team,
        ctx,
        category,
        name="Boat",
        value="500",
        currency="USD",
        as_of=date(2025, 1, 1),
    )

    data = networth_history(team.factory, ctx, "1y")
    assert all(point.assets == Money(10_000, "EUR") for point in data.points)
    assert all(point.breakdown.non_base == 1 for point in data.points)

    with team.factory.begin() as session:
        workspace = session.get(Workspace, team.workspace_id)
        assert workspace is not None
        workspace.base_currency = "USD"
    data = networth_history(team.factory, ctx, "1y")
    assert all(point.assets == Money(50_000, "USD") for point in data.points)
    assert all(point.breakdown.non_base == 1 for point in data.points)


def test_non_base_only_records_values_but_has_no_chart(team: WorkspaceDb) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    _item(
        team,
        ctx,
        category,
        name="Boat",
        value="500",
        currency="USD",
        as_of=date(2025, 1, 1),
    )

    data = networth_history(team.factory, ctx, "1y")
    assert data.points
    assert data.has_recorded_values is True
    assert data.has_values is False
    assert data.has_chart is False
    assert all(point.assets == Money(0, "EUR") for point in data.points)
    assert all(point.breakdown.non_base == 1 for point in data.points)


def test_history_ignores_stored_snapshots(team: WorkspaceDb) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    _item(team, ctx, category, value="100", as_of=date(2025, 1, 1))
    with team.factory.begin() as session:
        NetWorthSnapshotRepository(session, ctx).create(
            as_of=date(2026, 2, 28),
            assets_minor=999_999,
            liabilities_minor=0,
            currency="EUR",
            breakdown={"v": 1, "categories": []},
            source="auto",
        )

    data = networth_history(team.factory, ctx, "1y")
    february = next(point for point in data.points if point.as_of == date(2026, 2, 28))
    assert february.assets == Money(10_000, "EUR")


def test_history_creates_no_snapshot_rows(team: WorkspaceDb) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    _item(team, ctx, category, value="100", as_of=date(2025, 1, 1))
    with team.factory() as session:
        before = session.scalars(select(NetWorthSnapshot)).all()
    networth_history(team.factory, ctx, "all")
    with team.factory() as session:
        assert len(session.scalars(select(NetWorthSnapshot)).all()) == len(before) == 0


def test_today_totals_match_the_summary_service(team: WorkspaceDb) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    assets = _category(team, ctx, "Vehicles", "asset")
    liabilities = _category(team, ctx, "Mortgage", "liability")
    _item(team, ctx, assets, name="Car", value="100", as_of=date(2025, 1, 1))
    _item(team, ctx, liabilities, name="Loan", value="40", as_of=date(2025, 1, 1))

    history = networth_history(team.factory, ctx, "1y")
    summary = networth_summary(team.factory, ctx).summary
    assert history.points[-1].assets == summary.assets
    assert history.points[-1].liabilities == summary.liabilities
    assert history.points[-1].net == summary.net


def test_history_is_workspace_scoped(team: WorkspaceDb) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    other_id = team.add_workspace("Other")
    team.add_member("olga", "owner", other_id)
    home = team.ctx("olga", "owner")
    other = team.ctx("olga", "owner", other_id)
    home_category = _category(team, home, "Vehicles")
    other_category = _category(team, other, "Vehicles")
    _item(team, home, home_category, name="Home car", value="100", as_of=date(2025, 1, 1))
    _item(team, other, other_category, name="Other car", value="900", as_of=date(2025, 1, 1))

    home_data = networth_history(team.factory, home, "1y")
    other_data = networth_history(team.factory, other, "1y")
    assert all(point.assets == Money(10_000, "EUR") for point in home_data.points)
    assert all(point.assets == Money(90_000, "EUR") for point in other_data.points)


def test_viewers_can_read(team: WorkspaceDb) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    _item(team, ctx, category, value="100", as_of=date(2025, 1, 1))
    reader = team.ctx("vic", "viewer")
    assert networth_history(team.factory, reader, "1y").has_chart


def test_a_two_factor_requirement_refuses_a_password_only_read(team: WorkspaceDb) -> None:
    ctx = replace(
        team.ctx("vic", "viewer"),
        workspace=replace(team.ctx("vic", "viewer").workspace, require_two_factor=True),
    )
    with pytest.raises(TwoFactorRequired):
        networth_history(team.factory, ctx, "1y")


def test_query_count_does_not_grow_with_months(team: WorkspaceDb) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    _item(team, ctx, category, value="100", as_of=date(2014, 1, 1))

    with _queries(team.engine) as one_year:
        networth_history(team.factory, ctx, "1y")
    with _queries(team.engine) as all_time:
        networth_history(team.factory, ctx, "all")
    assert len(all_time) == len(one_year)
    assert len(all_time) <= 12


def test_unknown_range_falls_back_to_one_year(team: WorkspaceDb) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    _item(team, ctx, category, value="100", as_of=date(2014, 1, 1))
    data = networth_history(team.factory, ctx, "nonsense")
    assert data.range_code == "1y"
    assert data.points[0].as_of == date(2025, 3, 31)
