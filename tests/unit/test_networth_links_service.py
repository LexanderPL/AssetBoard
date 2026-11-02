"""Linked net-worth items (F5.4): creation, resolution, snapshots and unlinking."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.db.models.identity import AuditEvent
from assetboard.db.models.networth import NetWorthSnapshot
from assetboard.db.repositories import ItemValuationRepository
from assetboard.domain.money import Money
from assetboard.domain.networth import Side
from assetboard.services import networth as service
from assetboard.services.context import Context, PermissionDenied
from assetboard.services.networth import (
    InvalidNetWorth,
    archive_item,
    create_category,
    create_item,
    get_item,
    link_options_for_side,
    restore_item,
    unlink_item,
)
from assetboard.services.networth_snapshots import (
    backfill_snapshots,
    take_auto_snapshot,
    take_manual_snapshot,
)
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


def _manual_item(
    db: WorkspaceDb,
    ctx: Context,
    category: service.CategoryView,
    *,
    name: str = "Cash",
    value: str = "0",
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
        as_of=as_of,
    )


def _linked_item(
    db: WorkspaceDb,
    ctx: Context,
    category: service.CategoryView,
    target: FakeTarget,
    *,
    name: str = "Holding",
) -> service.ItemView:
    return create_item(
        db.factory,
        ctx,
        locale="en",
        side=category.side,
        category_id=category.id,
        name=name,
        link_type=LINK_TYPE,
        link_id=target.link_id,
    )


def _target(
    team: WorkspaceDb,
    *,
    label: str = "Holding",
    currency: str = "EUR",
    values: dict[date, int] | None = None,
    href: str | None = "/portfolio/holdings/1",
    resolution_currency: str | None = None,
) -> FakeTarget:
    return FakeTarget(
        workspace_id=team.workspace_id,
        link_id=uuid4(),
        label=label,
        currency=currency,
        href=href,
        values=values or {},
        resolution_currency=resolution_currency,
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


def test_create_linked_item_resolves_read_only_value_and_audits(
    team: WorkspaceDb, fake_links: FakeLinks
) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    target = _target(team, values={date(2026, 3, 15): 125000})
    fake_links.register(targets=[target])
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    item = _linked_item(team, ctx, category, target)
    assert (item.valuation_mode, item.link_type, item.link_id) == (
        "linked",
        LINK_TYPE,
        target.link_id,
    )
    assert (item.link_label, item.link_name, item.link_url) == (
        "Fake source",
        "Holding",
        "/portfolio/holdings/1",
    )
    assert item.current == Money(125000, "EUR")
    assert item.current_as_of == date(2026, 3, 15)
    assert item.valuations == ()
    assert not item.unavailable
    with team.factory() as session:
        assert ItemValuationRepository(session, ctx).for_item(item.id) == []
    created = _events(team, "networth_item.created")[-1]
    assert created.data["currency"] == "EUR"
    assert "amount" not in created.data and "as_of" not in created.data
    linked = _events(team, "networth_item.linked")[-1]
    assert linked.data == {"link_type": LINK_TYPE, "target": "Holding"}


def test_a_linked_item_uses_the_target_currency_and_stays_non_base(
    team: WorkspaceDb, fake_links: FakeLinks
) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    target = _target(team, currency="USD", values={date(2026, 3, 15): 5000})
    fake_links.register(targets=[target])
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    item = _linked_item(team, ctx, category, target)
    assert (item.currency, item.non_base) == ("USD", True)
    assert item.current == Money(5000, "USD")
    summary = networth_summary(team.factory, ctx)
    assert summary.summary.assets == Money(0, "EUR")
    assert [warning.id for warning in summary.non_base_items] == [item.id]


def test_a_link_must_be_a_known_target_in_this_workspace(
    team: WorkspaceDb, fake_links: FakeLinks
) -> None:
    target = FakeTarget(
        workspace_id=uuid4(),
        link_id=uuid4(),
        label="Foreign",
    )
    fake_links.register(targets=[target])
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    with pytest.raises(InvalidNetWorth) as caught:
        _linked_item(team, ctx, category, target)
    assert (caught.value.field, caught.value.code) == ("link", "invalid_link")


def test_wrong_side_link_is_refused(team: WorkspaceDb, fake_links: FakeLinks) -> None:
    target = _target(team)
    fake_links.register(side=Side.ASSET, targets=[target])
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx, "Mortgage", "liability")
    with pytest.raises(InvalidNetWorth) as caught:
        _linked_item(team, ctx, category, target)
    assert (caught.value.field, caught.value.code) == ("link", "link_wrong_side")


def test_link_options_are_only_for_the_item_side(team: WorkspaceDb, fake_links: FakeLinks) -> None:
    target = _target(team)
    fake_links.register(side=Side.ASSET, targets=[target])
    ctx = team.ctx("ed", "editor")
    asset = link_options_for_side(team.factory, ctx, side="asset")
    liability = link_options_for_side(team.factory, ctx, side="liability")
    assert [(option.link_type, option.choice.label) for option in asset] == [(LINK_TYPE, "Holding")]
    assert liability == ()


def test_linked_item_rejects_manual_valuations(team: WorkspaceDb, fake_links: FakeLinks) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    target = _target(team, values={date(2026, 3, 15): 100})
    fake_links.register(targets=[target])
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    item = _linked_item(team, ctx, category, target)
    with pytest.raises(InvalidNetWorth) as caught:
        service.add_valuation(team.factory, ctx, item.id, locale="en", value="200")
    assert (caught.value.field, caught.value.code) == (
        "valuation_mode",
        "linked_value_readonly",
    )


def test_duplicate_active_links_and_restore_are_refused(
    team: WorkspaceDb, fake_links: FakeLinks
) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    target = _target(team, values={date(2026, 3, 15): 100})
    fake_links.register(targets=[target])
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    first = _linked_item(team, ctx, category, target)
    with pytest.raises(InvalidNetWorth) as caught:
        _linked_item(team, ctx, category, target, name="Second")
    assert (caught.value.field, caught.value.code) == ("link", "already_linked")

    archive_item(team.factory, ctx, first.id)
    second = _linked_item(team, ctx, category, target, name="Second")
    with pytest.raises(InvalidNetWorth) as caught:
        restore_item(team.factory, ctx, first.id)
    assert (caught.value.field, caught.value.code) == ("link", "already_linked")
    assert get_item(team.factory, ctx, second.id).valuation_mode == "linked"
    archive_item(team.factory, ctx, second.id)
    restore_item(team.factory, ctx, first.id)
    assert get_item(team.factory, ctx, first.id).valuation_mode == "linked"


def test_unavailable_linked_item_is_flagged_and_snapshot_records_it(
    team: WorkspaceDb, fake_links: FakeLinks
) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    target = _target(team, values={})
    fake_links.register(targets=[target])
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    item = _linked_item(team, ctx, category, target)
    assert item.unavailable and item.current is None
    summary = networth_summary(team.factory, ctx)
    assert [warning.id for warning in summary.unavailable_items] == [item.id]
    take_manual_snapshot(team.factory, ctx)
    with team.factory() as session:
        snapshot = session.scalars(
            select(NetWorthSnapshot).where(NetWorthSnapshot.workspace_id == team.workspace_id)
        ).one()
    assert snapshot.breakdown["not_counted"]["unavailable"] == 1
    assert _events(team, "networth_snapshot.taken")


def test_unlink_keeps_an_available_value_as_the_first_manual_valuation(
    team: WorkspaceDb, fake_links: FakeLinks
) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    target = _target(team, values={date(2026, 3, 15): 125000})
    fake_links.register(targets=[target])
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    item = _linked_item(team, ctx, category, target)
    result = unlink_item(team.factory, ctx, item.id)
    assert result.value_recorded
    assert result.item.valuation_mode == "manual"
    assert result.item.link_type is None and result.item.link_id is None
    assert result.item.current == Money(125000, "EUR")
    with team.factory() as session:
        valuations = ItemValuationRepository(session, ctx).for_item(item.id)
    assert len(valuations) == 1
    assert (valuations[0].as_of, valuations[0].value_minor) == (
        date(2026, 3, 15),
        125000,
    )
    assert _events(team, "networth_item.unlinked")[-1].data == {"value_recorded": True}


def test_unlink_without_a_value_leaves_the_item_with_no_value(
    team: WorkspaceDb, fake_links: FakeLinks
) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    target = _target(team, values={})
    fake_links.register(targets=[target])
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    item = _linked_item(team, ctx, category, target)
    result = unlink_item(team.factory, ctx, item.id)
    assert not result.value_recorded
    assert result.item.valuation_mode == "manual"
    assert result.item.current is None and not result.item.unavailable
    with team.factory() as session:
        assert ItemValuationRepository(session, ctx).for_item(item.id) == []
    assert _events(team, "networth_item.unlinked")[-1].data == {"value_recorded": False}


def test_wrong_currency_resolution_counts_as_unavailable(
    team: WorkspaceDb, fake_links: FakeLinks
) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    target = _target(
        team,
        values={date(2026, 3, 15): 100},
        resolution_currency="USD",
    )
    fake_links.register(targets=[target])
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    item = _linked_item(team, ctx, category, target)
    assert item.unavailable and item.current is None
    result = unlink_item(team.factory, ctx, item.id)
    assert not result.value_recorded
    assert result.item.current is None


def test_unknown_type_on_read_is_unavailable_and_can_be_unlinked(
    team: WorkspaceDb, fake_links: FakeLinks
) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    target = _target(team, values={date(2026, 3, 15): 100})
    fake_links.register(targets=[target])
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    item = _linked_item(team, ctx, category, target)
    fake_links.reset()
    read = get_item(team.factory, ctx, item.id)
    assert read.unavailable and read.current is None
    assert read.link_name is None and read.link_label is None and read.link_url is None
    take_manual_snapshot(team.factory, ctx)
    with team.factory() as session:
        snapshot = session.scalars(
            select(NetWorthSnapshot).where(NetWorthSnapshot.workspace_id == team.workspace_id)
        ).one()
    assert snapshot.breakdown["not_counted"]["unavailable"] == 1
    result = unlink_item(team.factory, ctx, item.id)
    assert not result.value_recorded and result.item.valuation_mode == "manual"


def test_backfill_resolves_linked_values_once_for_all_month_ends(
    team: WorkspaceDb, fake_links: FakeLinks
) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    target = _target(
        team,
        values={
            date(2026, 1, 31): 100,
            date(2026, 2, 28): 200,
        },
    )
    spec = fake_links.register(targets=[target])
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    _manual_item(team, ctx, category, value="0", as_of=date(2026, 1, 1))
    _linked_item(team, ctx, category, target)
    spec.calls.clear()
    result = backfill_snapshots(team.factory, ctx)
    assert (result.from_date, result.to_date) == (
        date(2026, 1, 31),
        date(2026, 2, 28),
    )
    assert len(spec.calls) == 1
    workspace_id, link_ids, dates = spec.calls[0]
    assert workspace_id == team.workspace_id
    assert link_ids == {target.link_id}
    assert dates == (date(2026, 1, 31), date(2026, 2, 28))
    with team.factory() as session:
        rows = list(
            session.scalars(
                select(NetWorthSnapshot)
                .where(NetWorthSnapshot.workspace_id == team.workspace_id)
                .order_by(NetWorthSnapshot.as_of)
            )
        )
    assert [row.assets_minor for row in rows] == [100, 200]
    assert rows[0].breakdown["not_counted"]["unavailable"] == 0


def test_manual_snapshot_uses_the_linked_value_today(
    team: WorkspaceDb, fake_links: FakeLinks
) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    target = _target(team, values={date(2026, 3, 15): 900})
    fake_links.register(targets=[target])
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    _linked_item(team, ctx, category, target)
    take_manual_snapshot(team.factory, ctx)
    with team.factory() as session:
        row = session.scalars(
            select(NetWorthSnapshot).where(NetWorthSnapshot.workspace_id == team.workspace_id)
        ).one()
    assert (row.as_of, row.assets_minor) == (date(2026, 3, 15), 900)


def test_auto_snapshot_uses_the_linked_value_at_the_month_end(
    team: WorkspaceDb, fake_links: FakeLinks
) -> None:
    _at(team, "2026-03-15T12:00:00+00:00")
    target = _target(team, values={date(2026, 2, 28): 700})
    fake_links.register(targets=[target])
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx)
    _linked_item(team, ctx, category, target)
    assert take_auto_snapshot(team.factory, ctx, team.clock, "UTC") is True
    with team.factory() as session:
        row = session.scalars(
            select(NetWorthSnapshot).where(NetWorthSnapshot.workspace_id == team.workspace_id)
        ).one()
    assert (row.as_of, row.assets_minor) == (date(2026, 2, 28), 700)


def test_only_editors_can_link_or_unlink(team: WorkspaceDb, fake_links: FakeLinks) -> None:
    target = _target(team)
    fake_links.register(targets=[target])
    editor = team.ctx("ed", "editor")
    category = _category(team, editor)
    item = _linked_item(team, editor, category, target)
    ctx = team.ctx("vic", "viewer")
    with pytest.raises(PermissionDenied):
        _linked_item(team, ctx, category, target)
    with pytest.raises(PermissionDenied):
        unlink_item(team.factory, ctx, item.id)
