"""Net-worth service (F5.1): items, valuations, subtotals, bulk add, seeds and roles."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.db.models.identity import AuditEvent
from assetboard.db.repositories import (
    ItemValuationRepository,
    NetWorthItemRepository,
    NotFound,
    create_owned_workspace,
)
from assetboard.domain.networth import Side
from assetboard.services import networth as service
from assetboard.services.context import Context, PermissionDenied
from assetboard.services.networth import (
    AlreadySeeded,
    BulkRejected,
    DuplicateCategoryName,
    InvalidNetWorth,
    ItemArchived,
    add_valuation,
    archive_category,
    archive_item,
    create_bulk,
    create_category,
    create_item,
    delete_category,
    get_category,
    get_item,
    list_networth,
    move_category,
    preview_bulk,
    restore_category,
    restore_item,
    seed_categories,
    update_category,
    update_item,
    update_valuation,
)
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


class TickingClock(FixedClock):
    """Each ``now()`` is one second later, so same-day valuations have a defined order."""

    def __init__(self) -> None:
        super().__init__("2026-03-01T12:00:00+00:00")
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


def _category(
    db: WorkspaceDb,
    ctx: Context,
    name: str = "Vehicles",
    side: str = "asset",
    color: str = "#16a34a",
) -> service.CategoryView:
    return create_category(db.factory, ctx, side=side, name=name, color=color)


def _item(
    db: WorkspaceDb,
    ctx: Context,
    category: service.CategoryView,
    *,
    name: str = "Car",
    value: str = "12000",
    currency: str = "EUR",
    notes: str = "",
    acquired_on: date | None = None,
    include_in_net_worth: bool = True,
    investable: bool = False,
    estimate_between: bool | None = None,
    as_of: date | None = None,
    valuation_note: str = "",
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
        notes=notes,
        acquired_on=acquired_on,
        include_in_net_worth=include_in_net_worth,
        investable=investable,
        estimate_between=estimate_between,
        as_of=as_of,
        valuation_note=valuation_note,
    )


def _events(db: WorkspaceDb, prefix: str = "networth") -> list[AuditEvent]:
    with db.factory() as session:
        return list(
            session.scalars(
                select(AuditEvent)
                .where(AuditEvent.action.startswith(prefix))
                .order_by(AuditEvent.ts, AuditEvent.id)
            )
        )


def _event(db: WorkspaceDb, action: str, entity_id: UUID | None = None) -> AuditEvent:
    events = [event for event in _events(db, action) if event.action == action]
    if entity_id is not None:
        events = [event for event in events if event.entity_id == str(entity_id)]
    assert len(events) == 1, [(event.action, event.entity_id) for event in events]
    return events[0]


def _side(db: WorkspaceDb, ctx: Context, side: Side = Side.ASSET) -> service.SideView:
    return list_networth(db.factory, ctx).side_view(side)


# Categories


def test_category_create_cleans_names_and_audits(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    first = _category(team, ctx, "  Vehicles  ")
    second = _category(team, ctx, "Electronics", color="#F97316")
    liability = _category(team, ctx, "Mortgage", side="liability")
    assert (first.name, first.sort_order, first.side) == ("Vehicles", 0, "asset")
    assert (second.name, second.sort_order, second.color) == ("Electronics", 1, "#f97316")
    assert (liability.name, liability.sort_order, liability.side) == (
        "Mortgage",
        0,
        "liability",
    )
    event = _event(team, "networth_category.created", first.id)
    assert event.data == {"name": "Vehicles", "side": "asset"}
    assert event.actor_user_id == team.users["ed"]


def test_category_names_are_unique_per_side_and_archive_allows_reuse(
    team: WorkspaceDb,
) -> None:
    ctx = team.ctx("ed", "editor")
    vehicles = _category(team, ctx, "Vehicles")
    _category(team, ctx, "Vehicles", side="liability")
    with pytest.raises(DuplicateCategoryName):
        _category(team, ctx, "VEHICLES")
    archive_category(team.factory, ctx, vehicles.id)
    replacement = _category(team, ctx, "vehicles")
    with pytest.raises(DuplicateCategoryName):
        restore_category(team.factory, ctx, vehicles.id)
    assert replacement.name == "vehicles"


def test_category_update_reorder_archive_and_delete(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    vehicles = _category(team, ctx, "Vehicles")
    electronics = _category(team, ctx, "Electronics")
    _category(team, ctx, "Audio equipment")
    updated = update_category(team.factory, ctx, vehicles.id, name="Cars", color="#0ea5e9")
    assert (updated.name, updated.color) == ("Cars", "#0ea5e9")
    assert _event(team, "networth_category.updated", vehicles.id).data == {
        "name": "Cars",
        "fields": "color,name",
    }
    move_category(team.factory, ctx, electronics.id, "up")
    names = [category.name for category in _side(team, ctx).categories]
    assert names == ["Electronics", "Cars", "Audio equipment"]
    assert [event.action for event in _events(team, "networth_category.")].count(
        "networth_category.updated"
    ) == 1
    archive_category(team.factory, ctx, vehicles.id)
    assert vehicles.id not in {category.id for category in _side(team, ctx).categories}
    restore_category(team.factory, ctx, vehicles.id)
    assert [category.name for category in _side(team, ctx).categories][-1] == "Cars"
    assert delete_category(team.factory, ctx, electronics.id) == "deleted"
    with pytest.raises(NotFound):
        get_category(team.factory, ctx, electronics.id)


def test_archiving_a_category_keeps_its_items_visible_and_counted(
    team: WorkspaceDb,
) -> None:
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx, "Vehicles")
    item = _item(team, ctx, category, name="Car")
    archive_category(team.factory, ctx, category.id)
    view = _side(team, ctx)
    assert view.groups == ()
    assert [group.category.id for group in view.archived_groups] == [category.id]
    group = view.archived_groups[0]
    assert [row.id for row in group.items] == [item.id]
    assert group.subtotal is not None and group.subtotal.amount_minor == 1200000
    assert not view.can_seed
    with pytest.raises(InvalidNetWorth) as caught:
        _item(team, ctx, category, name="Second")
    assert (caught.value.field, caught.value.code) == ("category", "category_archived")
    # Deleting a category with items archives it; the item stays.
    assert delete_category(team.factory, ctx, category.id) == "archived"
    assert _event(team, "networth_category.archived", category.id).data["in_use"] is True


def test_category_delete_only_removes_an_unused_category(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    unused = _category(team, ctx, "Unused")
    used = _category(team, ctx, "Used")
    _item(team, ctx, used)
    assert delete_category(team.factory, ctx, unused.id) == "deleted"
    assert delete_category(team.factory, ctx, used.id) == "archived"
    assert get_category(team.factory, ctx, used.id).archived


SUGGESTED_ASSETS = [
    "Real estate",
    "Vehicles",
    "Electronics",
    "Appliances",
    "Furniture",
    "Jewelry & watches",
    "Art & collectibles",
    "Tools",
    "Other",
]


def _names(view: service.SideView) -> list[str]:
    return [category.name for category in view.categories]


def test_seed_an_empty_side_adds_the_whole_set_with_the_manual_event_shape(
    team: WorkspaceDb,
) -> None:
    ctx = team.ctx("ed", "editor")
    assert seed_categories(team.factory, ctx, side="asset", locale="en") == SUGGESTED_ASSETS
    event = _event(team, "networth_category.seeded")
    assert event.data == {"count": 9, "side": "asset"}
    assert event.actor_user_id == team.users["ed"]
    view = _side(team, ctx)
    assert _names(view) == SUGGESTED_ASSETS
    assert [category.sort_order for category in view.categories] == list(range(9))
    assert not view.can_seed
    liability_view = _side(team, ctx, Side.LIABILITY)
    assert liability_view.categories == ()
    assert liability_view.can_seed
    assert seed_categories(team.factory, ctx, side="liability", locale="pl") == [
        "Raty",
        "Kredyt hipoteczny",
        "Inne",
    ]
    assert _names(_side(team, ctx, Side.LIABILITY)) == ["Raty", "Kredyt hipoteczny", "Inne"]


def test_seed_refuses_a_side_that_already_has_categories(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    _category(team, ctx, "Tools")
    _category(team, ctx, "My boat")
    _category(team, ctx, "Pojazdy")  # Vehicles in Polish
    view = _side(team, ctx)
    assert not view.can_seed

    with pytest.raises(AlreadySeeded):
        seed_categories(team.factory, ctx, side="asset", locale="en")

    assert _names(_side(team, ctx)) == ["Tools", "My boat", "Pojazdy"]
    assert not [e for e in _events(team) if e.action == "networth_category.seeded"]


def test_seed_refuses_when_only_an_archived_category_exists(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx, "Cars")
    archive_category(team.factory, ctx, category.id)
    view = _side(team, ctx)
    assert not view.can_seed
    assert view.categories == ()

    with pytest.raises(AlreadySeeded):
        seed_categories(team.factory, ctx, side="asset", locale="en")

    assert _names(_side(team, ctx)) == []
    assert [category.name for category in _side(team, ctx).archived_categories] == ["Cars"]


def test_a_second_seed_is_refused_and_writes_no_event(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    seed_categories(team.factory, ctx, side="asset", locale="pl")
    with pytest.raises(AlreadySeeded):
        seed_categories(team.factory, ctx, side="asset", locale="en")
    with pytest.raises(AlreadySeeded):
        seed_categories(team.factory, ctx, side="asset", locale="pl")
    assert len(_side(team, ctx).categories) == 9
    assert len([e for e in _events(team) if e.action == "networth_category.seeded"]) == 1


def test_seed_needs_an_editor(team: WorkspaceDb) -> None:
    with pytest.raises(PermissionDenied):
        seed_categories(team.factory, team.ctx("vic", "viewer"), side="asset", locale="en")
    assert _side(team, team.ctx("olga", "owner")).categories == ()


def test_workspace_creation_seed_writes_both_sides_and_one_automatic_event(
    team: WorkspaceDb,
) -> None:
    owner = team.users["olga"]
    with team.factory.begin() as session:
        workspace, scope = create_owned_workspace(
            session,
            name="Second",
            base_currency="PLN",
            country_code="PL",
            timezone="Europe/Warsaw",
            locale="pl",
            owner_user_id=owner,
        )
        count = service.seed_into(
            session,
            scope,
            locale="pl",
            actor_user_id=owner,
            actor_type="user",
            now=team.clock.now(),
            automatic=True,
        )
        workspace_id = workspace.id
    assert count == 12
    event = _event(team, "networth_category.seeded")
    assert event.workspace_id == workspace_id
    assert event.data == {"count": 12, "automatic": True}
    listed = list_networth(team.factory, team.ctx("olga", "owner", workspace_id))
    assert [category.name for category in listed.asset.categories][0] == "Nieruchomości"
    assert len(listed.asset.categories) == 9
    assert len(listed.liability.categories) == 3


# Items and valuations


def test_item_create_appends_the_first_valuation_and_audits(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx, "Vehicles")
    item = _item(
        team,
        ctx,
        category,
        name="  Vintage  car ",
        value="1 200,50",
        notes="  Garage  ",
        acquired_on=date(2020, 5, 1),
        investable=True,
        as_of=date(2026, 2, 1),
        valuation_note="Purchased",
    )
    assert (item.name, item.currency, item.notes) == ("Vintage car", "EUR", "Garage")
    assert item.current is not None and item.current.amount_minor == 120050
    assert item.current_as_of == date(2026, 2, 1)
    assert item.acquired_on == date(2020, 5, 1)
    assert item.investable
    assert len(item.valuations) == 1 and item.valuations[0].note == "Purchased"
    assert item.sparkline
    event = _event(team, "networth_item.created", item.id)
    assert event.data == {
        "name": "Vintage car",
        "side": "asset",
        "category": "Vehicles",
        "currency": "EUR",
        "amount": "1200.50",
        "as_of": "2026-02-01",
    }


def test_item_create_uses_the_base_currency_and_validates_currency(
    team: WorkspaceDb,
) -> None:
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx, "Vehicles")
    item = create_item(
        team.factory,
        ctx,
        locale="en",
        side="asset",
        category_id=category.id,
        name="Car",
        value="100",
        currency="",
    )
    assert item.currency == "EUR"
    usd = _item(team, ctx, category, name="Boat", currency="USD", value="50")
    assert usd.currency == "USD"
    assert usd.non_base
    with pytest.raises(InvalidNetWorth) as caught:
        _item(team, ctx, category, name="Bad", currency="ZZZ")
    assert (caught.value.field, caught.value.code) == ("currency", "invalid_currency")


@pytest.mark.parametrize(
    ("value", "field", "code"),
    [
        ("-1", "value", "value_negative"),
        ("not a number", "value", "invalid_amount"),
        ("12.345", "value", "too_many_decimals"),
        ("9999999999999999", "value", "amount_too_large"),
    ],
)
def test_item_value_validation(team: WorkspaceDb, value: str, field: str, code: str) -> None:
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx, "Vehicles")
    with pytest.raises(InvalidNetWorth) as caught:
        _item(team, ctx, category, value=value)
    assert (caught.value.field, caught.value.code) == (field, code)


def test_extra_decimals_are_refused_for_valuations_and_bulk_lines(
    team: WorkspaceDb,
) -> None:
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx, "Vehicles")
    item = _item(team, ctx, category, name="Car", value="100")
    with pytest.raises(InvalidNetWorth) as caught:
        add_valuation(
            team.factory,
            ctx,
            item.id,
            locale="en",
            value="12.345",
            as_of=date(2026, 3, 1),
        )
    assert (caught.value.field, caught.value.code) == ("value", "too_many_decimals")
    preview = preview_bulk(
        team.factory, ctx, side="asset", text="Boat; 12.345; Vehicles", locale="en"
    )
    assert preview.lines[0].errors == ("too_many_decimals",)
    with pytest.raises(BulkRejected) as rejected:
        create_bulk(
            team.factory,
            ctx,
            side="asset",
            text="Boat; 12.345; Vehicles",
            locale="en",
        )
    assert rejected.value.preview.lines[0].errors == ("too_many_decimals",)
    with team.factory() as session:
        assert len(ItemValuationRepository(session, ctx).for_item(item.id)) == 1


def test_item_category_must_be_active_and_on_the_same_side(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    asset = _category(team, ctx, "Vehicles")
    liability = _category(team, ctx, "Mortgage", side="liability")
    with pytest.raises(InvalidNetWorth) as caught:
        create_item(
            team.factory,
            ctx,
            locale="en",
            side="asset",
            category_id=liability.id,
            name="Car",
            value="1",
        )
    assert (caught.value.field, caught.value.code) == ("category", "wrong_side")
    with pytest.raises(NotFound):
        _item(team, ctx, service.CategoryView(
            id=uuid4(), side="asset", name="Missing", color="#16a34a",
            sort_order=0, archived=False, item_count=0,
        ))  # fmt: skip
    assert asset.id != liability.id


def test_item_update_does_not_append_a_valuation(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx, "Vehicles")
    item = _item(team, ctx, category)
    updated = update_item(
        team.factory,
        ctx,
        item.id,
        category_id=category.id,
        name="Better car",
        currency="EUR",
        notes="New note",
        acquired_on=date(2020, 1, 1),
        include_in_net_worth=False,
        investable=True,
    )
    assert updated.name == "Better car" and updated.notes == "New note"
    assert not updated.include_in_net_worth and updated.investable
    with team.factory() as session:
        assert len(ItemValuationRepository(session, ctx).for_item(item.id)) == 1
    event = _event(team, "networth_item.updated", item.id)
    assert event.data["fields"] == ("acquired_on,include_in_net_worth,investable,name,notes")


def test_item_currency_is_locked_after_a_valuation(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx, "Vehicles")
    item = _item(team, ctx, category)
    with pytest.raises(InvalidNetWorth) as caught:
        update_item(
            team.factory,
            ctx,
            item.id,
            category_id=category.id,
            name="Car",
            currency="USD",
        )
    assert (caught.value.field, caught.value.code) == ("currency", "currency_locked")
    # An item created without a valuation (only possible for a future link flow) may change.
    with team.factory.begin() as session:
        raw = NetWorthItemRepository(session, ctx).create(
            side="asset",
            category_id=category.id,
            name="No value",
            currency="EUR",
            notes="",
            acquired_on=None,
            valuation_mode="manual",
            link_type=None,
            link_id=None,
            include_in_net_worth=True,
            investable=False,
            estimate_between=True,
        )
        raw_id = raw.id
    changed = update_item(
        team.factory,
        ctx,
        raw_id,
        category_id=category.id,
        name="No value",
        currency="USD",
    )
    assert changed.currency == "USD"


def test_archive_and_restore_item_keep_its_history(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx, "Vehicles")
    item = _item(team, ctx, category)
    archive_item(team.factory, ctx, item.id)
    assert _side(team, ctx).groups == ()
    assert [row.id for row in _side(team, ctx).archived_items] == [item.id]
    assert _side(team, ctx).archived_items[0].valuations
    with pytest.raises(ItemArchived):
        add_valuation(team.factory, ctx, item.id, locale="en", value="1", as_of=date(2026, 3, 1))
    restore_item(team.factory, ctx, item.id)
    assert [row.id for row in _side(team, ctx).groups[0].items] == [item.id]
    assert _event(team, "networth_item.archived", item.id).data == {
        "name": "Car",
        "side": "asset",
    }
    assert _event(team, "networth_item.restored", item.id).data["name"] == "Car"


def test_valuation_append_backdate_future_and_same_day_order(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx, "Vehicles")
    item = _item(team, ctx, category, value="100", as_of=date(2026, 3, 1))
    backdated = add_valuation(
        team.factory, ctx, item.id, locale="en", value="90", as_of=date(2026, 1, 1)
    )
    assert backdated.current is not None and backdated.current.amount_minor == 10000
    changed = add_valuation(
        team.factory,
        ctx,
        item.id,
        locale="en",
        value="110",
        as_of=date(2026, 3, 1),
        note="Recheck",
    )
    assert changed.current is not None and changed.current.amount_minor == 11000
    assert changed.valuations[0].note == "Recheck"
    future = add_valuation(
        team.factory,
        ctx,
        item.id,
        locale="en",
        value="500",
        as_of=date(2026, 4, 1),
    )
    assert future.current is not None and future.current.amount_minor == 11000
    assert future.valuations[0].future
    assert [value.as_of for value in future.valuations] == [
        date(2026, 4, 1),
        date(2026, 3, 1),
        date(2026, 3, 1),
        date(2026, 1, 1),
    ]
    events = [
        event
        for event in _events(team, "networth_item.valuation_added")
        if event.entity_id == str(item.id)
    ]
    assert len(events) == 3
    assert events[-1].data["currency"] == "EUR"


def test_update_valuation_corrects_value_date_and_note_in_place(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx, "Vehicles")
    item = _item(
        team,
        ctx,
        category,
        value="100",
        as_of=date(2026, 3, 1),
        valuation_note="First",
    )
    original = item.valuations[0]
    changed = update_valuation(
        team.factory,
        ctx,
        item.id,
        original.id,
        locale="en",
        value="90",
        as_of=date(2026, 1, 1),
        note="Corrected",
    )
    assert len(changed.valuations) == 1
    row = changed.valuations[0]
    assert (row.id, row.as_of, row.value.amount_minor, row.note) == (
        original.id,
        date(2026, 1, 1),
        9000,
        "Corrected",
    )
    assert row.created_at == original.created_at
    assert changed.current is not None and changed.current.amount_minor == 9000
    events = [event for event in _events(team) if event.action == "networth_item.valuation_updated"]
    assert len(events) == 1
    assert events[0].entity_id == str(item.id)
    assert events[0].data == {
        "as_of": "2026-01-01",
        "amount": "90.00",
        "currency": "EUR",
        "previous_as_of": "2026-03-01",
        "previous_amount": "100.00",
        "fields": "as_of,note,value_minor",
    }


def test_update_valuation_recomputes_current_value_and_future_flag(
    team: WorkspaceDb,
) -> None:
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx, "Vehicles")
    item = _item(team, ctx, category, value="100", as_of=date(2026, 3, 1))
    future = add_valuation(
        team.factory,
        ctx,
        item.id,
        locale="en",
        value="200",
        as_of=date(2026, 4, 1),
    )
    assert future.current is not None and future.current.amount_minor == 10000
    future_row = next(row for row in future.valuations if row.as_of == date(2026, 4, 1))
    assert future_row.future

    corrected = update_valuation(
        team.factory,
        ctx,
        item.id,
        future_row.id,
        locale="en",
        value="200",
        as_of=date(2026, 2, 1),
        note="",
    )
    assert corrected.current is not None and corrected.current.amount_minor == 10000
    assert not any(row.future for row in corrected.valuations)

    before = corrected.sparkline
    current_row = next(row for row in corrected.valuations if row.as_of == date(2026, 3, 1))
    updated = update_valuation(
        team.factory,
        ctx,
        item.id,
        current_row.id,
        locale="en",
        value="300",
        as_of=current_row.as_of,
        note="",
    )
    assert updated.current is not None and updated.current.amount_minor == 30000
    assert updated.sparkline != before


def test_update_valuation_rejects_archived_linked_and_missing_rows(
    team: WorkspaceDb,
) -> None:
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx, "Vehicles")
    item = _item(team, ctx, category)
    row = item.valuations[0]

    other = _item(team, ctx, category, name="Other car")
    with pytest.raises(NotFound):
        update_valuation(
            team.factory,
            ctx,
            item.id,
            other.valuations[0].id,
            locale="en",
            value="1",
            as_of=date(2026, 3, 1),
        )

    with team.factory.begin() as session:
        NetWorthItemRepository(session, ctx).update(
            item.id,
            valuation_mode="linked",
            link_type="portfolio_asset_class",
            link_id=uuid4(),
        )
    with pytest.raises(InvalidNetWorth) as linked:
        update_valuation(
            team.factory,
            ctx,
            item.id,
            row.id,
            locale="en",
            value="1",
            as_of=date(2026, 3, 1),
        )
    assert linked.value.code == "linked_value_readonly"

    with team.factory.begin() as session:
        NetWorthItemRepository(session, ctx).update(
            item.id,
            valuation_mode="manual",
            link_type=None,
            link_id=None,
        )
    archive_item(team.factory, ctx, item.id)
    with pytest.raises(ItemArchived):
        update_valuation(
            team.factory,
            ctx,
            item.id,
            row.id,
            locale="en",
            value="1",
            as_of=date(2026, 3, 1),
        )


@pytest.mark.parametrize(
    ("value", "as_of", "note", "code", "field"),
    [
        ("-1", date(2026, 3, 1), "", "value_negative", "value"),
        ("1.001", date(2026, 3, 1), "", "too_many_decimals", "value"),
        ("1", None, "", "invalid_date", "as_of"),
        ("1", date(1899, 12, 31), "", "invalid_date", "as_of"),
        ("1", date(2026, 3, 1), "x" * 201, "invalid_notes", "valuation_note"),
    ],
)
def test_update_valuation_validates_its_inputs(
    team: WorkspaceDb,
    value: str,
    as_of: date | None,
    note: str,
    code: str,
    field: str,
) -> None:
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx, "Vehicles")
    item = _item(team, ctx, category)
    with pytest.raises(InvalidNetWorth) as error:
        update_valuation(
            team.factory,
            ctx,
            item.id,
            item.valuations[0].id,
            locale="en",
            value=value,
            as_of=as_of,
            note=note,
        )
    assert (error.value.code, error.value.field) == (code, field)


def test_update_valuation_without_changes_writes_no_audit_event(
    team: WorkspaceDb,
) -> None:
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx, "Vehicles")
    item = _item(team, ctx, category, value="100", as_of=date(2026, 3, 1))
    row = item.valuations[0]
    before = [event.id for event in _events(team)]
    unchanged = update_valuation(
        team.factory,
        ctx,
        item.id,
        row.id,
        locale="en",
        value="100",
        as_of=date(2026, 3, 1),
        note="",
    )
    assert unchanged.current is not None and unchanged.current.amount_minor == 10000
    assert [event.id for event in _events(team)] == before


def test_a_future_only_item_has_no_current_value(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx, "Vehicles")
    item = _item(team, ctx, category, value="100", as_of=date(2026, 4, 1))
    assert item.current is None
    view = _side(team, ctx)
    assert view.groups[0].subtotal is None
    assert view.groups[0].has_no_value


def test_subtotals_include_only_base_currency_included_items(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    vehicles = _category(team, ctx, "Vehicles")
    _item(team, ctx, vehicles, name="Car", value="12000")
    _item(team, ctx, vehicles, name="Boat", value="5000")
    _item(team, ctx, vehicles, name="Watch", value="300", currency="USD")
    _item(team, ctx, vehicles, name="Excluded", value="999", include_in_net_worth=False)
    _item(team, ctx, vehicles, name="Future", value="100", as_of=date(2026, 4, 1))
    group = _side(team, ctx).groups[0]
    assert group.subtotal is not None and group.subtotal.amount_minor == 1700000
    assert group.has_non_base and group.has_excluded and group.has_no_value
    assert len(group.items) == 5


def test_archived_items_are_excluded_and_archived_categories_count(
    team: WorkspaceDb,
) -> None:
    ctx = team.ctx("ed", "editor")
    vehicles = _category(team, ctx, "Vehicles")
    _item(team, ctx, vehicles, name="Car", value="100")
    old = _category(team, ctx, "Old")
    old_item = _item(team, ctx, old, name="Old thing", value="300")
    archive_category(team.factory, ctx, old.id)
    archived_item = _item(team, ctx, vehicles, name="Archived", value="999")
    archive_item(team.factory, ctx, archived_item.id)
    view = _side(team, ctx)
    assert [group.category.name for group in view.groups] == ["Vehicles"]
    assert view.groups[0].subtotal is not None and view.groups[0].subtotal.amount_minor == 10000
    assert [group.category.name for group in view.archived_groups] == ["Old"]
    assert view.archived_groups[0].subtotal is not None
    assert old_item.id in {row.id for row in view.archived_groups[0].items}
    assert [row.id for row in view.archived_items] == [archived_item.id]
    assert archived_item.id not in {row.id for row in view.groups[0].items}


# Bulk add


def test_bulk_preview_and_create_are_all_or_nothing(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    _category(team, ctx, "Vehicles")
    _category(team, ctx, "Electronics")
    text = "Car; 12000; Vehicles\nBike; 1 200,50; Electronics\n"
    preview = preview_bulk(team.factory, ctx, side="asset", text=text, locale="en")
    assert preview.can_create
    assert [line.value_minor for line in preview.lines] == [1200000, 120050]
    assert list_networth(team.factory, ctx).asset.groups == ()
    assert create_bulk(team.factory, ctx, side="asset", text=text, locale="en") == 2
    view = _side(team, ctx)
    assert sum(len(group.items) for group in view.groups) == 2
    assert _event(team, "networth_item.bulk_created").data == {
        "side": "asset",
        "count": 2,
    }
    bad = "Car; 1; Vehicles\nBike; 2; Missing"
    with pytest.raises(BulkRejected) as caught:
        create_bulk(team.factory, ctx, side="asset", text=bad, locale="en")
    assert [line.errors for line in caught.value.preview.lines] == [
        (),
        ("category_unknown",),
    ]
    assert sum(len(group.items) for group in _side(team, ctx).groups) == 2


def test_bulk_preview_reports_archived_categories_and_duplicate_warnings(
    team: WorkspaceDb,
) -> None:
    ctx = team.ctx("ed", "editor")
    vehicles = _category(team, ctx, "Vehicles")
    _item(team, ctx, vehicles, name="Car")
    archived = _category(team, ctx, "Old")
    archive_category(team.factory, ctx, archived.id)
    preview = preview_bulk(
        team.factory,
        ctx,
        side="asset",
        text="Car; 1; Vehicles\ncar; 2; Vehicles\nBoat; 3; Old",
        locale="en",
    )
    assert [line.warnings for line in preview.lines] == [
        ("existing_name",),
        ("duplicate_name", "existing_name"),
        (),
    ]
    assert preview.lines[2].errors == ("category_archived",)


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("a; 1; Vehicles\n" * 501, "too_many_lines"),
        ("a; 1; Vehicles\n" + "x" * (64 * 1024), "bulk_too_large"),
    ],
)
def test_bulk_limits_are_refused_before_parsing(team: WorkspaceDb, text: str, code: str) -> None:
    ctx = team.ctx("ed", "editor")
    _category(team, ctx, "Vehicles")
    with pytest.raises(InvalidNetWorth) as caught:
        preview_bulk(team.factory, ctx, side="asset", text=text, locale="en")
    assert (caught.value.field, caught.value.code) == ("bulk", code)


# Roles and workspaces


def _prepare_owner_rows(
    db: WorkspaceDb, ctx: Context
) -> tuple[service.CategoryView, service.ItemView]:
    category = _category(db, ctx, "Vehicles")
    item = _item(db, ctx, category)
    return category, item


def _writes(db: WorkspaceDb, ctx: Context) -> dict[str, Callable[[], object]]:
    category, item = _prepare_owner_rows(db, db.ctx("olga", "owner"))
    # Archived targets for the restore actions.
    archived_category = _category(db, db.ctx("olga", "owner"), "Archived category")
    archive_category(db.factory, db.ctx("olga", "owner"), archived_category.id)
    archived_item = _item(db, db.ctx("olga", "owner"), category, name="Archived item")
    archive_item(db.factory, db.ctx("olga", "owner"), archived_item.id)
    other_category = _category(db, db.ctx("olga", "owner"), "Other category")
    return {
        "create_category": lambda: create_category(
            db.factory, ctx, side="asset", name="New", color="#16a34a"
        ),
        "update_category": lambda: update_category(
            db.factory, ctx, category.id, name="Cars", color="#16a34a"
        ),
        "move_category": lambda: move_category(db.factory, ctx, category.id, "down"),
        "archive_category": lambda: archive_category(db.factory, ctx, category.id),
        "restore_category": lambda: restore_category(db.factory, ctx, archived_category.id),
        "delete_category": lambda: delete_category(db.factory, ctx, other_category.id),
        "seed_categories": lambda: seed_categories(db.factory, ctx, side="liability", locale="en"),
        "create_item": lambda: create_item(
            db.factory,
            ctx,
            locale="en",
            side="asset",
            category_id=category.id,
            name="New item",
            value="1",
        ),
        "update_item": lambda: update_item(
            db.factory,
            ctx,
            item.id,
            category_id=category.id,
            name="Changed",
            currency="EUR",
        ),
        "archive_item": lambda: archive_item(db.factory, ctx, item.id),
        "restore_item": lambda: restore_item(db.factory, ctx, archived_item.id),
        "add_valuation": lambda: add_valuation(
            db.factory, ctx, item.id, locale="en", value="2", as_of=date(2026, 3, 1)
        ),
        "update_valuation": lambda: update_valuation(
            db.factory,
            ctx,
            item.id,
            item.valuations[0].id,
            locale="en",
            value="2",
            as_of=date(2026, 3, 1),
        ),
        "preview_bulk": lambda: preview_bulk(
            db.factory, ctx, side="asset", text="Car; 1; Vehicles", locale="en"
        ),
        "create_bulk": lambda: create_bulk(
            db.factory, ctx, side="asset", text="Car; 1; Vehicles", locale="en"
        ),
    }


WRITE_NAMES = (
    "create_category",
    "update_category",
    "move_category",
    "archive_category",
    "restore_category",
    "delete_category",
    "seed_categories",
    "create_item",
    "update_item",
    "archive_item",
    "restore_item",
    "add_valuation",
    "update_valuation",
    "preview_bulk",
    "create_bulk",
)


@pytest.mark.parametrize("write", WRITE_NAMES)
def test_viewers_cannot_write(team: WorkspaceDb, write: str) -> None:
    calls = _writes(team, team.ctx("vic", "viewer"))
    before = [(event.id, event.action) for event in _events(team)]
    with pytest.raises(PermissionDenied):
        calls[write]()
    assert [(event.id, event.action) for event in _events(team)] == before
    assert list_networth(team.factory, team.ctx("vic", "viewer")).asset.groups


@pytest.mark.parametrize("write", tuple(name for name in WRITE_NAMES if name != "preview_bulk"))
def test_a_stale_editor_context_is_refused_inside_the_transaction(
    team: WorkspaceDb, write: str
) -> None:
    team.add_member("dee", "viewer")
    calls = _writes(team, team.ctx("dee", "editor"))
    before = [(event.id, event.action) for event in _events(team)]
    with pytest.raises(PermissionDenied):
        calls[write]()
    assert [(event.id, event.action) for event in _events(team)] == before


def test_viewers_can_read(team: WorkspaceDb) -> None:
    ctx = team.ctx("vic", "viewer")
    category = _category(team, team.ctx("ed", "editor"), "Vehicles")
    item = _item(team, team.ctx("ed", "editor"), category)
    assert get_category(team.factory, ctx, category.id).name == "Vehicles"
    assert get_item(team.factory, ctx, item.id).name == "Car"
    assert _side(team, ctx).groups[0].items[0].id == item.id


def test_another_workspaces_rows_are_not_found(team: WorkspaceDb) -> None:
    owner_ctx = team.ctx("ed", "editor")
    category = _category(team, owner_ctx, "Vehicles")
    item = _item(team, owner_ctx, category)
    other = team.add_workspace("Other")
    team.add_member("ed", "editor", other)
    foreign = team.ctx("ed", "editor", other)
    assert _side(team, foreign).groups == ()
    with pytest.raises(NotFound):
        get_category(team.factory, foreign, category.id)
    with pytest.raises(NotFound):
        get_item(team.factory, foreign, item.id)
    with pytest.raises(NotFound):
        update_category(team.factory, foreign, category.id, name="Cars", color="#16a34a")
    with pytest.raises(NotFound):
        update_item(
            team.factory,
            foreign,
            item.id,
            category_id=category.id,
            name="Cars",
            currency="EUR",
        )
    with pytest.raises(NotFound):
        add_valuation(
            team.factory, foreign, item.id, locale="en", value="1", as_of=date(2026, 3, 1)
        )
    with pytest.raises(NotFound):
        archive_item(team.factory, foreign, item.id)
    assert get_item(team.factory, owner_ctx, item.id).name == "Car"


def test_estimate_between_defaults_by_side_on_create_and_bulk(
    team: WorkspaceDb,
) -> None:
    ctx = team.ctx("ed", "editor")
    asset_category = _category(team, ctx, "Vehicles")
    liability_category = _category(team, ctx, "Mortgage", side="liability")
    asset = _item(team, ctx, asset_category, name="Car")
    liability = _item(team, ctx, liability_category, name="Loan", value="1000")
    assert (asset.estimate_between, liability.estimate_between) == (True, False)
    assert (
        create_bulk(
            team.factory,
            ctx,
            side="asset",
            text="Bike; 100; Vehicles\n",
            locale="en",
        )
        == 1
    )
    assert (
        create_bulk(
            team.factory,
            ctx,
            side="liability",
            text="Card; 50; Mortgage\n",
            locale="en",
        )
        == 1
    )
    with team.factory() as session:
        items = NetWorthItemRepository(session, ctx).ordered()
    assert {row.name: row.estimate_between for row in items} == {
        "Car": True,
        "Loan": False,
        "Bike": True,
        "Card": False,
    }


def test_estimate_between_can_be_unchecked_and_is_audited_by_field_name(
    team: WorkspaceDb,
) -> None:
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx, "Vehicles")
    item = _item(team, ctx, category, name="Car")
    assert item.estimate_between is True
    updated = update_item(
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
        estimate_between=False,
    )
    assert updated.estimate_between is False
    event = _event(team, "networth_item.updated", item.id)
    assert event.data["fields"] == "estimate_between"
    restored = update_item(
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
    assert restored.estimate_between is True


def test_estimate_between_can_be_set_on_a_manual_item_with_no_valuation(
    team: WorkspaceDb,
) -> None:
    ctx = team.ctx("ed", "editor")
    category = _category(team, ctx, "Vehicles")
    with team.factory.begin() as session:
        raw = NetWorthItemRepository(session, ctx).create(
            side="asset",
            category_id=category.id,
            name="No value",
            currency="EUR",
            estimate_between=False,
        )
    updated = update_item(
        team.factory,
        ctx,
        raw.id,
        category_id=category.id,
        name=raw.name,
        currency=raw.currency,
        estimate_between=True,
    )
    assert updated.estimate_between is True
