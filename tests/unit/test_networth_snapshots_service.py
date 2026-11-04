"""Net-worth snapshots and history service (F5.3): sources, dates, roles and currencies."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.db.models.identity import AuditEvent, Workspace
from assetboard.db.models.networth import NetWorthSnapshot
from assetboard.db.repositories import NetWorthSnapshotRepository
from assetboard.domain.money import Money
from assetboard.services import networth as service
from assetboard.services.context import Context, PermissionDenied
from assetboard.services.networth import (
    add_valuation,
    archive_item,
    create_category,
    create_item,
    restore_item,
    update_item,
)
from assetboard.services.networth_snapshots import (
    SnapshotDateInFuture,
    backfill_snapshots,
    networth_history,
    take_auto_snapshot,
    take_manual_snapshot,
)
from tests.tenant.harness import context_for
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
    )


def _rows(team: WorkspaceDb, *, source: str | None = None) -> list[NetWorthSnapshot]:
    with team.factory() as session:
        statement = select(NetWorthSnapshot).where(
            NetWorthSnapshot.workspace_id == team.workspace_id
        )
        if source is not None:
            statement = statement.where(NetWorthSnapshot.source == source)
        return list(
            session.scalars(statement.order_by(NetWorthSnapshot.as_of, NetWorthSnapshot.source))
        )


def _events(team: WorkspaceDb, action: str) -> list[AuditEvent]:
    with team.factory() as session:
        return list(
            session.scalars(
                select(AuditEvent)
                .where(AuditEvent.action == action)
                .order_by(AuditEvent.ts, AuditEvent.id)
            )
        )


def test_auto_snapshot_uses_the_previous_month_end_and_is_idempotent(
    team: WorkspaceDb,
) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    assert take_auto_snapshot(team.factory, ctx, team.clock, "UTC") is False
    assert _rows(team) == []

    category = _category(team, ctx)
    _item(team, ctx, category, value="100", as_of=date(2026, 1, 1))
    assert take_auto_snapshot(team.factory, ctx, team.clock, "UTC") is True
    assert take_auto_snapshot(team.factory, ctx, team.clock, "UTC") is False
    rows = _rows(team)
    assert len(rows) == 1
    row = rows[0]
    assert (row.as_of, row.source, row.currency) == (date(2026, 2, 28), "auto", "EUR")
    assert (row.assets_minor, row.liabilities_minor, row.net_minor) == (10000, 0, 10000)
    assert row.breakdown == {
        "v": 1,
        "categories": [
            {
                "category_id": str(category.id),
                "side": "asset",
                "total_minor": 10000,
            }
        ],
        "not_counted": {"non_base": 0, "no_value": 0, "unavailable": 0},
        "estimated": 0,
    }
    event = _events(team, "networth_snapshot.created")[0]
    assert event.actor_type == "system" and event.actor_user_id is None
    assert event.data == {"as_of": "2026-02-28"}


def test_auto_snapshot_skips_when_no_value_was_effective_at_the_month_end(
    team: WorkspaceDb,
) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    _item(team, ctx, category, value="100", as_of=date(2026, 3, 1))
    assert take_auto_snapshot(team.factory, ctx, team.clock, "UTC") is False
    assert _rows(team) == []


def test_an_archived_item_still_makes_the_workspace_eligible_for_an_auto_snapshot(
    team: WorkspaceDb,
) -> None:
    _at(team, "2026-02-20T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    item = _item(team, ctx, category, value="100", as_of=date(2026, 1, 1))
    archive_item(team.factory, ctx, item.id)
    _at(team, "2026-03-15T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    assert take_auto_snapshot(team.factory, ctx, team.clock, "UTC") is True
    row = _rows(team)[0]
    assert (row.as_of, row.assets_minor) == (date(2026, 2, 28), 0)


def test_backfill_replaces_an_automatic_snapshot_and_the_job_leaves_it(
    team: WorkspaceDb,
) -> None:
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
    add_valuation(team.factory, ctx, item.id, locale="en", value="300", as_of=date(2026, 3, 1))
    assert take_auto_snapshot(team.factory, ctx, team.clock, "UTC") is True
    february = next(row for row in _rows(team) if row.as_of == date(2026, 2, 28))
    assert (february.source, february.assets_minor) == ("auto", 10000)

    backfill_snapshots(team.factory, ctx)
    february = next(row for row in _rows(team) if row.as_of == date(2026, 2, 28))
    assert (february.source, february.assets_minor) == ("backfill", 29661)
    assert february.breakdown["estimated"] == 1
    # The monthly/startup job sees an existing row and leaves the rebuilt history alone.
    assert take_auto_snapshot(team.factory, ctx, team.clock, "UTC") is False
    assert next(row for row in _rows(team) if row.as_of == date(2026, 2, 28)).source == "backfill"


def test_auto_snapshot_does_not_replace_backfill_but_manual_still_wins(
    team: WorkspaceDb,
) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    _item(team, ctx, category, value="100", as_of=date(2026, 1, 1))
    backfill_snapshots(team.factory, ctx)
    assert [row.source for row in _rows(team)] == ["backfill", "backfill"]

    assert take_auto_snapshot(team.factory, ctx, team.clock, "UTC") is False
    february = [row for row in _rows(team) if row.as_of == date(2026, 2, 28)]
    assert [row.source for row in february] == ["backfill"]

    take_manual_snapshot(team.factory, ctx, as_of=date(2026, 2, 28))
    assert take_auto_snapshot(team.factory, ctx, team.clock, "UTC") is False
    february = [row for row in _rows(team) if row.as_of == date(2026, 2, 28)]
    assert [row.source for row in february] == ["manual"]


def test_manual_snapshot_uses_today_and_replaces_the_same_day(team: WorkspaceDb) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    item = _item(team, ctx, category, value="100", as_of=date(2026, 3, 1))
    first, replaced = take_manual_snapshot(team.factory, ctx)
    assert (first, replaced) == (date(2026, 3, 15), False)
    add_valuation(
        team.factory,
        ctx,
        item.id,
        locale="en",
        value="250",
        as_of=date(2026, 3, 10),
    )
    second, replaced = take_manual_snapshot(team.factory, ctx)
    assert (second, replaced) == (date(2026, 3, 15), True)
    rows = _rows(team)
    assert len(rows) == 1
    assert rows[0].source == "manual" and rows[0].assets_minor == 25000
    events = _events(team, "networth_snapshot.taken")
    assert len(events) == 2
    assert {event.data["replaced"] for event in events} == {False, True}
    assert all(event.data["as_of"] == "2026-03-15" for event in events)


def test_manual_snapshot_stores_zero_for_an_empty_workspace(team: WorkspaceDb) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    take_manual_snapshot(team.factory, ctx)
    row = _rows(team)[0]
    assert (row.assets_minor, row.liabilities_minor, row.net_minor) == (0, 0, 0)
    assert row.breakdown["categories"] == []


def test_manual_snapshot_refuses_a_future_date(team: WorkspaceDb) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    with pytest.raises(SnapshotDateInFuture):
        take_manual_snapshot(team.factory, ctx, as_of=date(2026, 3, 16))
    assert _rows(team) == []


def test_backfill_covers_each_month_end_and_recalculates_only_backfill(
    team: WorkspaceDb,
) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    item = _item(team, ctx, category, value="100", as_of=date(2026, 1, 1))
    add_valuation(team.factory, ctx, item.id, locale="en", value="200", as_of=date(2026, 2, 1))
    add_valuation(team.factory, ctx, item.id, locale="en", value="300", as_of=date(2026, 3, 1))
    result = backfill_snapshots(team.factory, ctx)
    assert (result.count, result.from_date, result.to_date) == (
        2,
        date(2026, 1, 31),
        date(2026, 2, 28),
    )
    rows = _rows(team)
    assert [(row.as_of, row.assets_minor) for row in rows] == [
        (date(2026, 1, 31), 10000),
        (date(2026, 2, 28), 20000),  # the 1 March value is after the month-end
    ]
    add_valuation(team.factory, ctx, item.id, locale="en", value="250", as_of=date(2026, 2, 10))
    backfill_snapshots(team.factory, ctx)
    assert [row.assets_minor for row in _rows(team)] == [10000, 25000]
    event = _events(team, "networth_snapshot.backfilled")[-1]
    assert event.data == {"from": "2026-01-31", "to": "2026-02-28", "count": 2}
    assert event.actor_user_id == team.users["ed"]


def test_backfill_and_a_single_snapshot_at_the_same_date_agree(
    team: WorkspaceDb,
) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    item = _item(team, ctx, category, value="100", as_of=date(2026, 1, 1))
    add_valuation(team.factory, ctx, item.id, locale="en", value="200", as_of=date(2026, 2, 1))
    backfill_snapshots(team.factory, ctx)
    february = next(row for row in _rows(team) if row.as_of == date(2026, 2, 28))
    backfilled = (
        february.assets_minor,
        february.liabilities_minor,
        february.net_minor,
        february.currency,
        february.breakdown,
    )
    take_manual_snapshot(team.factory, ctx, as_of=date(2026, 2, 28))
    manual = next(row for row in _rows(team) if row.source == "manual")
    assert (
        manual.assets_minor,
        manual.liabilities_minor,
        manual.net_minor,
        manual.currency,
        manual.breakdown,
    ) == backfilled


def test_an_archived_item_counts_up_to_its_archive_date(team: WorkspaceDb) -> None:
    _at(team, "2026-06-15T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    item = _item(team, ctx, category, value="100", as_of=date(2026, 1, 1))
    archive_item(team.factory, ctx, item.id)
    _at(team, "2026-07-01T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    backfill_snapshots(team.factory, ctx)
    by_date = {row.as_of: row.assets_minor for row in _rows(team)}
    assert by_date[date(2026, 5, 31)] == 10000
    assert by_date[date(2026, 6, 30)] == 0


def test_a_restored_item_counts_in_every_month(team: WorkspaceDb) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    item = _item(team, ctx, category, value="100", as_of=date(2026, 1, 1))
    archive_item(team.factory, ctx, item.id)
    restore_item(team.factory, ctx, item.id)
    _at(team, "2026-07-01T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    backfill_snapshots(team.factory, ctx)
    assert {row.as_of: row.assets_minor for row in _rows(team)} == {
        date(2026, 1, 31): 10000,
        date(2026, 2, 28): 10000,
        date(2026, 3, 31): 10000,
        date(2026, 4, 30): 10000,
        date(2026, 5, 31): 10000,
        date(2026, 6, 30): 10000,
    }


def test_changing_include_changes_backfill_and_replaces_auto_but_not_manual(
    team: WorkspaceDb,
) -> None:
    _at(team, "2026-07-01T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    item = _item(team, ctx, category, value="100", as_of=date(2026, 1, 1))
    take_auto_snapshot(team.factory, ctx, team.clock, "UTC")
    take_manual_snapshot(team.factory, ctx)
    backfill_snapshots(team.factory, ctx)
    assert all(row.assets_minor == 10000 for row in _rows(team) if row.as_of < date(2026, 6, 30))

    update_item(
        team.factory,
        ctx,
        item.id,
        category_id=category.id,
        name=item.name,
        currency=item.currency,
        notes=item.notes,
        acquired_on=item.acquired_on,
        include_in_net_worth=False,
        investable=item.investable,
    )
    backfill_snapshots(team.factory, ctx)
    manual = next(row for row in _rows(team, source="manual"))
    assert manual.assets_minor == 10000
    backfill = [row for row in _rows(team, source="backfill") if row.as_of <= date(2026, 6, 30)]
    assert backfill and all(row.assets_minor == 0 for row in backfill)
    assert [row.source for row in _rows(team) if row.as_of == date(2026, 6, 30)] == ["backfill"]
    assert [row.source for row in _rows(team) if row.as_of == date(2026, 7, 1)] == ["manual"]
    assert _rows(team, source="auto") == []


def test_backfill_exclusions_match_the_summary(team: WorkspaceDb) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    assets = _category(team, ctx, "Vehicles")
    liabilities = _category(team, ctx, "Mortgage", "liability")
    _item(team, ctx, assets, name="Counted", value="100", as_of=date(2026, 1, 1))
    _item(
        team,
        ctx,
        assets,
        name="Excluded",
        value="900",
        include_in_net_worth=False,
        as_of=date(2026, 1, 1),
    )
    _item(team, ctx, assets, name="Foreign", value="800", currency="USD", as_of=date(2026, 1, 1))
    _item(team, ctx, assets, name="Future", value="700", as_of=date(2026, 3, 1))
    _item(team, ctx, liabilities, name="Mortgage", value="40", as_of=date(2026, 1, 1))
    backfill_snapshots(team.factory, ctx)
    january = next(row for row in _rows(team) if row.as_of == date(2026, 1, 31))
    assert (january.assets_minor, january.liabilities_minor, january.net_minor) == (
        10000,
        4000,
        6000,
    )
    assert january.breakdown["not_counted"] == {
        "non_base": 1,
        "no_value": 1,
        "unavailable": 0,
    }


def test_history_prefers_manual_over_auto_over_backfill_and_filters_the_range(
    team: WorkspaceDb,
) -> None:
    _at(team, "2026-11-02T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    scope = context_for(team.workspace_id, team.clock)
    with team.factory() as session, session.begin():
        repository = NetWorthSnapshotRepository(session, scope)
        repository.create(
            as_of=date(2025, 1, 31),
            assets_minor=1,
            liabilities_minor=0,
            currency="EUR",
            breakdown={"v": 1, "categories": []},
            source="manual",
        )
        for amount, source in ((20, "backfill"), (30, "auto"), (40, "manual")):
            repository.create(
                as_of=date(2026, 10, 31),
                assets_minor=amount,
                liabilities_minor=0,
                currency="EUR",
                breakdown={"v": 1, "categories": []},
                source=source,
            )
    default = networth_history(team.factory, ctx, "1y")
    assert [point.as_of for point in default.snapshots] == [date(2026, 10, 31)]
    assert default.snapshots[0].source == "manual"
    assert default.snapshots[0].assets == Money(40, "EUR")
    everything = networth_history(team.factory, ctx, "all")
    assert [point.as_of for point in everything.snapshots] == [
        date(2025, 1, 31),
        date(2026, 10, 31),
    ]


def test_history_keeps_other_currencies_in_the_table_but_not_the_chart(
    team: WorkspaceDb,
) -> None:
    _at(team, "2026-07-01T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    _item(team, ctx, category, value="100", as_of=date(2026, 1, 1))
    take_manual_snapshot(team.factory, ctx, as_of=date(2026, 1, 31))
    with team.factory.begin() as session:
        workspace = session.get(Workspace, team.workspace_id)
        assert workspace is not None
        workspace.base_currency = "USD"
    ctx = team.ctx("ed", "editor")
    take_manual_snapshot(team.factory, ctx, as_of=date(2026, 2, 28))
    data = networth_history(team.factory, ctx, "all")
    assert data.base_currency == "USD"
    assert [point.currency for point in data.snapshots] == ["EUR", "USD"]
    assert [point.charted for point in data.snapshots] == [False, True]
    assert data.other_currency_count == 1


def test_backfill_audit_has_no_amounts(team: WorkspaceDb) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    _item(team, ctx, category, value="123.45", as_of=date(2026, 1, 1))
    backfill_snapshots(team.factory, ctx)
    for event in (
        *_events(team, "networth_snapshot.created"),
        *_events(team, "networth_snapshot.taken"),
        *_events(team, "networth_snapshot.backfilled"),
    ):
        assert "amount" not in event.data
        assert "12345" not in str(event.data) and "123.45" not in str(event.data)


def test_viewers_cannot_take_or_backfill_and_a_stale_editor_is_refused(
    team: WorkspaceDb,
) -> None:
    ctx = team.ctx("vic", "viewer")
    with pytest.raises(PermissionDenied):
        take_manual_snapshot(team.factory, ctx)
    with pytest.raises(PermissionDenied):
        backfill_snapshots(team.factory, ctx)
    team.add_member("dee", "viewer")
    stale = team.ctx("dee", "editor")
    with pytest.raises(PermissionDenied):
        take_manual_snapshot(team.factory, stale)
    assert _rows(team) == []


def test_backfill_estimates_between_values_and_counts_one_estimate(
    team: WorkspaceDb,
) -> None:
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
    add_valuation(team.factory, ctx, item.id, locale="en", value="200", as_of=date(2026, 2, 1))
    add_valuation(team.factory, ctx, item.id, locale="en", value="300", as_of=date(2026, 3, 1))
    backfill_snapshots(team.factory, ctx)
    january = next(row for row in _rows(team) if row.as_of == date(2026, 1, 31))
    february = next(row for row in _rows(team) if row.as_of == date(2026, 2, 28))
    assert (january.assets_minor, february.assets_minor) == (19677, 29643)
    assert january.breakdown["estimated"] == 1
    assert february.breakdown["estimated"] == 1


def test_auto_and_manual_snapshots_ignore_the_switch(team: WorkspaceDb) -> None:
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
    add_valuation(team.factory, ctx, item.id, locale="en", value="300", as_of=date(2026, 3, 1))
    take_auto_snapshot(team.factory, ctx, team.clock, "UTC")
    take_manual_snapshot(team.factory, ctx, as_of=date(2026, 2, 28))
    auto = next(row for row in _rows(team, source="auto"))
    manual = next(row for row in _rows(team, source="manual"))
    assert (auto.assets_minor, manual.assets_minor) == (10000, 10000)
    assert auto.breakdown["estimated"] == manual.breakdown["estimated"] == 0
    backfill_snapshots(team.factory, ctx)
    # Backfill replaces the automatic row; the manual row keeps its recorded value.
    assert _rows(team, source="auto") == []
    manual = next(row for row in _rows(team, source="manual"))
    assert manual.assets_minor == 10000 and manual.breakdown["estimated"] == 0
    assert [row.source for row in _rows(team) if row.as_of == date(2026, 2, 28)] == ["manual"]


def test_changing_the_switch_changes_nothing_until_backfill_is_rerun(
    team: WorkspaceDb,
) -> None:
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
    add_valuation(team.factory, ctx, item.id, locale="en", value="300", as_of=date(2026, 3, 1))
    backfill_snapshots(team.factory, ctx)
    before = [(row.as_of, row.assets_minor) for row in _rows(team)]
    assert before == [
        (date(2026, 1, 31), 10000),
        (date(2026, 2, 28), 10000),
    ]
    update_item(
        team.factory,
        ctx,
        item.id,
        category_id=category.id,
        name=item.name,
        currency=item.currency,
        notes=item.notes,
        acquired_on=item.acquired_on,
        include_in_net_worth=item.include_in_net_worth,
        investable=item.investable,
        estimate_between=True,
    )
    assert [(row.as_of, row.assets_minor) for row in _rows(team)] == before
    backfill_snapshots(team.factory, ctx)
    assert [(row.as_of, row.assets_minor) for row in _rows(team)] == [
        (date(2026, 1, 31), 20169),
        (date(2026, 2, 28), 29661),
    ]


def test_estimated_count_only_includes_items_in_the_snapshot_totals(
    team: WorkspaceDb,
) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    included = _item(
        team,
        ctx,
        category,
        name="Included",
        value="100",
        estimate_between=True,
        as_of=date(2026, 1, 1),
    )
    excluded = _item(
        team,
        ctx,
        category,
        name="Excluded",
        value="200",
        include_in_net_worth=False,
        estimate_between=True,
        as_of=date(2026, 1, 1),
    )
    for item in (included, excluded):
        add_valuation(team.factory, ctx, item.id, locale="en", value="300", as_of=date(2026, 3, 1))
    backfill_snapshots(team.factory, ctx)
    january = next(row for row in _rows(team) if row.as_of == date(2026, 1, 31))
    assert january.assets_minor == 20169
    assert january.breakdown["estimated"] == 1


def test_an_archived_item_is_excluded_even_when_its_value_would_be_estimated(
    team: WorkspaceDb,
) -> None:
    _at(team, "2026-02-10T12:00:00+00:00")
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
    add_valuation(team.factory, ctx, item.id, locale="en", value="300", as_of=date(2026, 3, 1))
    archive_item(team.factory, ctx, item.id)
    _at(team, "2026-03-15T12:00:00+00:00")
    ctx = team.ctx("ed", "editor")
    backfill_snapshots(team.factory, ctx)
    by_date = {row.as_of: row.assets_minor for row in _rows(team)}
    assert by_date[date(2026, 1, 31)] == 20169
    assert by_date[date(2026, 2, 28)] == 0
