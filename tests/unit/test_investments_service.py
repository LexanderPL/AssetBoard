"""Investments service (F6.1): classes, instruments, allocations, seeds and roles."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.db.models.identity import AuditEvent
from assetboard.db.repositories import create_owned_workspace
from assetboard.domain.instruments import ASSET_CLASS_SEED, RiskTier
from assetboard.i18n import gettext
from assetboard.services import investments as service
from assetboard.services.context import Context, PermissionDenied
from assetboard.services.investments import (
    AllocationRejected,
    AlreadySeeded,
    AssetClassArchived,
    DuplicateAssetClassName,
    DuplicateInstrumentName,
    InstrumentArchived,
    InvestmentError,
    apply_suggested_risk_tiers,
    archive_asset_class,
    archive_instrument,
    create_asset_class,
    create_instrument,
    investment_page,
    move_asset_class,
    restore_asset_class,
    restore_instrument,
    seed_asset_classes,
    update_asset_class,
    update_instrument,
)
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


class TickingClock(FixedClock):
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


def _events(db: WorkspaceDb, prefix: str) -> list[AuditEvent]:
    with db.factory() as session:
        return list(
            session.scalars(
                select(AuditEvent)
                .where(AuditEvent.action.startswith(prefix))
                .order_by(AuditEvent.ts, AuditEvent.id)
            )
        )


def _event(db: WorkspaceDb, action: str) -> AuditEvent:
    events = [event for event in _events(db, action) if event.action == action]
    assert len(events) == 1, [(event.action, event.entity_id) for event in events]
    return events[0]


def _class(
    db: WorkspaceDb,
    ctx: Context,
    name: str = "Stocks",
    *,
    color: str = "#2563eb",
    investable: bool = True,
) -> service.AssetClassView:
    return create_asset_class(db.factory, ctx, name=name, color=color, investable=investable)


def _instrument(
    db: WorkspaceDb,
    ctx: Context,
    class_id: UUID,
    *,
    name: str = "World ETF",
    symbol: str = "VWCE",
    type: str = "etf",
    quote_currency: str = "EUR",
    unit_label: str = "",
    allocations: list[tuple[UUID, int]] | None = None,
) -> service.InstrumentView:
    return create_instrument(
        db.factory,
        ctx,
        name=name,
        symbol=symbol,
        type=type,
        quote_currency=quote_currency,
        unit_label=unit_label,
        allocations=allocations if allocations is not None else [(class_id, 10_000)],
        locale="en",
    )


def test_create_asset_class_appends_and_audits(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    first = _class(team, ctx, "Stocks")
    second = _class(team, ctx, "  Bonds  ", color="#16A34A", investable=False)
    assert (first.sort_order, second.name, second.color, second.investable) == (
        0,
        "Bonds",
        "#16a34a",
        False,
    )
    assert [row.id for row in investment_page(team.factory, ctx, locale="en").classes] == [
        first.id,
        second.id,
    ]
    events = _events(team, "asset_class.created")
    assert [event.entity_id for event in events] == [str(first.id), str(second.id)]
    assert events[-1].data == {"name": "Bonds", "investable": False, "risk_tier": ""}


def test_duplicate_active_asset_class_names_are_refused_ignoring_case(
    team: WorkspaceDb,
) -> None:
    ctx = team.ctx("ed", "editor")
    _class(team, ctx, "Stocks")
    with pytest.raises(DuplicateAssetClassName):
        _class(team, ctx, "STOCKS")


def test_archive_and_restore_an_asset_class(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx, "Stocks")
    bonds = _class(team, ctx, "Bonds")
    archive_asset_class(team.factory, ctx, stocks.id)
    page = investment_page(team.factory, ctx, locale="en")
    assert [row.id for row in page.classes] == [bonds.id]
    assert [row.id for row in page.archived_classes] == [stocks.id]
    assert _event(team, "asset_class.archived").data == {"name": "Stocks"}

    restore_asset_class(team.factory, ctx, stocks.id)
    page = investment_page(team.factory, ctx, locale="en")
    assert [row.id for row in page.classes] == [bonds.id, stocks.id]
    assert page.archived_classes == ()
    assert _event(team, "asset_class.restored").data == {"name": "Stocks"}


def test_restoring_a_class_refuses_an_active_name_clash(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx, "Stocks")
    archive_asset_class(team.factory, ctx, stocks.id)
    _class(team, ctx, "Stocks")
    with pytest.raises(DuplicateAssetClassName):
        restore_asset_class(team.factory, ctx, stocks.id)


def test_update_asset_class_changes_fields_and_audits(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    view = _class(team, ctx, "Stocks", color="#2563eb", investable=True)
    updated = update_asset_class(
        team.factory,
        ctx,
        view.id,
        name="Equities",
        color="#16A34A",
        investable=False,
    )
    assert (updated.name, updated.color, updated.investable) == (
        "Equities",
        "#16a34a",
        False,
    )
    event = _event(team, "asset_class.updated")
    assert event.entity_id == str(view.id)
    assert event.data == {"name": "Equities", "fields": "color,investable,name"}


def test_move_asset_class_reorders_only_the_active_list(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    a = _class(team, ctx, "A")
    b = _class(team, ctx, "B")
    c = _class(team, ctx, "C")
    move_asset_class(team.factory, ctx, b.id, "up")
    assert [row.id for row in investment_page(team.factory, ctx, locale="en").classes] == [
        b.id,
        a.id,
        c.id,
    ]
    move_asset_class(team.factory, ctx, b.id, "down")
    assert [row.id for row in investment_page(team.factory, ctx, locale="en").classes] == [
        a.id,
        b.id,
        c.id,
    ]
    with pytest.raises(InvestmentError):
        move_asset_class(team.factory, ctx, a.id, "sideways")


def test_seed_classes_only_when_no_active_class_exists(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    assert seed_asset_classes(team.factory, ctx, locale="en") == len(ASSET_CLASS_SEED)
    page = investment_page(team.factory, ctx, locale="en")
    assert [row.name for row in page.classes] == [
        gettext(seed.name, "en") for seed in ASSET_CLASS_SEED
    ]
    event = _event(team, "asset_class.seeded")
    assert event.data == {"count": len(ASSET_CLASS_SEED)}
    with pytest.raises(AlreadySeeded):
        seed_asset_classes(team.factory, ctx, locale="en")


def test_seed_is_offered_again_when_every_class_is_archived(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    only = _class(team, ctx, "Custom")
    archive_asset_class(team.factory, ctx, only.id)
    assert investment_page(team.factory, ctx, locale="en").can_seed
    assert seed_asset_classes(team.factory, ctx, locale="en") == len(ASSET_CLASS_SEED)
    page = investment_page(team.factory, ctx, locale="en")
    assert len(page.classes) == len(ASSET_CLASS_SEED)
    assert [row.id for row in page.archived_classes] == [only.id]


def test_seed_into_writes_into_its_scope_only(team: WorkspaceDb) -> None:
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
        new_id = workspace.id
    assert count == len(ASSET_CLASS_SEED)
    with team.factory() as session:
        names = session.scalars(
            select(service.AssetClass.name)
            .where(service.AssetClass.workspace_id == new_id)
            .order_by(service.AssetClass.sort_order)
        ).all()
        other = session.scalars(
            select(service.AssetClass.id).where(
                service.AssetClass.workspace_id == team.workspace_id
            )
        ).all()
    assert names == [gettext(seed.name, "pl") for seed in ASSET_CLASS_SEED]
    assert other == []


@pytest.mark.parametrize(
    ("instrument_type", "quote_currency", "unit_label", "expected_unit", "source"),
    [
        ("crypto", "EUR", "", "coin", "manual"),
        ("metal", "EUR", "", "oz", "manual"),
        ("currency", "USD", "", "USD", "manual"),
        ("real_estate", "EUR", "", "property", "manual"),
        ("bond_model", "PLN", "", "bond", "bond_model"),
        ("equity", "EUR", " share ", "share", "manual"),
    ],
)
def test_instrument_type_defaults(
    team: WorkspaceDb,
    instrument_type: str,
    quote_currency: str,
    unit_label: str,
    expected_unit: str,
    source: str,
) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx)
    view = _instrument(
        team,
        ctx,
        stocks.id,
        name=f"Test {instrument_type}",
        symbol="",
        type=instrument_type,
        quote_currency=quote_currency,
        unit_label=unit_label,
    )
    assert (view.unit_label, view.valuation_source) == (expected_unit, source)
    assert view.terms_required is (instrument_type == "bond_model")


def test_a_currency_instrument_is_the_held_currency_with_unit_price_one(
    team: WorkspaceDb,
) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx)
    view = _instrument(
        team,
        ctx,
        stocks.id,
        name="US dollar",
        type="currency",
        quote_currency="usd",
        unit_label="dollar",
    )
    # The unit price of a currency instrument is 1 in its own currency; F6.3 must fetch the
    # FX rate to base, not a market price, so the general valuation path converts it once.
    assert (view.type, view.quote_currency, view.unit_label) == ("currency", "USD", "USD")


def test_duplicate_instrument_names_are_refused_ignoring_case(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx)
    _instrument(team, ctx, stocks.id, name="World ETF")
    with pytest.raises(DuplicateInstrumentName):
        _instrument(team, ctx, stocks.id, name="WORLD ETF", symbol="OTHER")


def test_allocation_must_have_at_least_one_class_and_total_100_percent(
    team: WorkspaceDb,
) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx, "Stocks")
    bonds = _class(team, ctx, "Bonds")
    with pytest.raises(AllocationRejected) as caught:
        _instrument(team, ctx, stocks.id, allocations=[])
    assert caught.value.code == "allocation_required"
    with pytest.raises(AllocationRejected) as caught:
        _instrument(
            team,
            ctx,
            stocks.id,
            allocations=[(stocks.id, 6_000), (bonds.id, 3_999)],
        )
    assert caught.value.code == "allocation_total"
    with pytest.raises(AllocationRejected) as caught:
        _instrument(
            team,
            ctx,
            stocks.id,
            allocations=[(stocks.id, 5_000), (stocks.id, 5_000)],
        )
    assert caught.value.code == "duplicate_class"


def test_allocation_to_an_archived_class_is_refused_for_a_new_instrument(
    team: WorkspaceDb,
) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx, "Stocks")
    archive_asset_class(team.factory, ctx, stocks.id)
    with pytest.raises(AllocationRejected) as caught:
        _instrument(team, ctx, stocks.id)
    assert caught.value.code == "class_archived"


def test_update_instrument_replaces_weights_and_audits_the_allocation(
    team: WorkspaceDb,
) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx, "Stocks")
    bonds = _class(team, ctx, "Bonds")
    view = _instrument(team, ctx, stocks.id, allocations=[(stocks.id, 10_000)])
    updated = update_instrument(
        team.factory,
        ctx,
        view.id,
        name="World ETF",
        symbol="VWCE",
        type="etf",
        quote_currency="EUR",
        unit_label="share",
        allocations=[(stocks.id, 8_000), (bonds.id, 2_000)],
        locale="en",
    )
    assert [(a.asset_class_id, a.weight_bp) for a in updated.allocations] == [
        (stocks.id, 8_000),
        (bonds.id, 2_000),
    ]
    allocation_events = _events(team, "instrument_allocation.updated")
    assert allocation_events[-1].data == {"count": 2}

    # The same class set with a different weight still replaces the rows.
    reweighted = update_instrument(
        team.factory,
        ctx,
        view.id,
        name="World ETF",
        symbol="VWCE",
        type="etf",
        quote_currency="EUR",
        unit_label="share",
        allocations=[(stocks.id, 7_000), (bonds.id, 3_000)],
        locale="en",
    )
    assert [(a.asset_class_id, a.weight_bp) for a in reweighted.allocations] == [
        (stocks.id, 7_000),
        (bonds.id, 3_000),
    ]


def test_update_may_keep_an_archived_allocation_but_may_not_add_one(
    team: WorkspaceDb,
) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx, "Stocks")
    bonds = _class(team, ctx, "Bonds")
    view = _instrument(team, ctx, stocks.id, allocations=[(stocks.id, 8_000), (bonds.id, 2_000)])
    archive_asset_class(team.factory, ctx, bonds.id)
    updated = update_instrument(
        team.factory,
        ctx,
        view.id,
        name="World ETF",
        symbol="VWCE",
        type="etf",
        quote_currency="EUR",
        unit_label="share",
        allocations=[(stocks.id, 8_000), (bonds.id, 2_000)],
        locale="en",
    )
    assert [a.asset_class_id for a in updated.allocations] == [stocks.id, bonds.id]

    cash = _class(team, ctx, "Cash")
    archive_asset_class(team.factory, ctx, cash.id)
    with pytest.raises(AllocationRejected) as caught:
        update_instrument(
            team.factory,
            ctx,
            view.id,
            name="World ETF",
            symbol="VWCE",
            type="etf",
            quote_currency="EUR",
            unit_label="share",
            allocations=[(stocks.id, 9_000), (cash.id, 1_000)],
            locale="en",
        )
    assert caught.value.code == "class_archived"


def test_update_instrument_changes_its_fields(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx)
    view = _instrument(team, ctx, stocks.id, name="World ETF", symbol="VWCE")
    updated = update_instrument(
        team.factory,
        ctx,
        view.id,
        name="Global equity",
        symbol="VWRL",
        type="equity",
        quote_currency="USD",
        unit_label="share",
        allocations=[(stocks.id, 10_000)],
        locale="en",
    )
    assert (
        updated.name,
        updated.symbol,
        updated.type,
        updated.quote_currency,
        updated.unit_label,
    ) == ("Global equity", "VWRL", "equity", "USD", "share")
    event = _event(team, "instrument.updated")
    assert event.data == {
        "name": "Global equity",
        "fields": "name,quote_currency,symbol,type",
    }


def test_archived_instruments_are_read_only_until_restored(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx)
    view = _instrument(team, ctx, stocks.id)
    archive_instrument(team.factory, ctx, view.id)
    page = investment_page(team.factory, ctx, locale="en")
    assert page.instruments == () and [row.id for row in page.archived_instruments] == [view.id]
    with pytest.raises(InstrumentArchived):
        update_instrument(
            team.factory,
            ctx,
            view.id,
            name=view.name,
            symbol=view.symbol or "",
            type=view.type,
            quote_currency=view.quote_currency,
            unit_label=view.unit_label,
            allocations=[(stocks.id, 10_000)],
            locale="en",
        )
    restore_instrument(team.factory, ctx, view.id)
    page = investment_page(team.factory, ctx, locale="en")
    assert [row.id for row in page.instruments] == [view.id]
    assert [allocation.asset_class_id for allocation in page.instruments[0].allocations] == [
        stocks.id
    ]


def test_restoring_an_instrument_refuses_an_active_name_clash(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx)
    view = _instrument(team, ctx, stocks.id, name="World ETF")
    archive_instrument(team.factory, ctx, view.id)
    _instrument(team, ctx, stocks.id, name="World ETF", symbol="OTHER")
    with pytest.raises(DuplicateInstrumentName):
        restore_instrument(team.factory, ctx, view.id)


def test_archived_classes_are_read_only_until_restored(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx)
    archive_asset_class(team.factory, ctx, stocks.id)
    with pytest.raises(AssetClassArchived):
        update_asset_class(
            team.factory,
            ctx,
            stocks.id,
            name="Equities",
            color="#2563eb",
            investable=True,
        )


@pytest.mark.parametrize(
    "operation",
    [
        lambda db, ctx: _class(db, ctx, "Nope"),
        lambda db, ctx: seed_asset_classes(db.factory, ctx, locale="en"),
    ],
)
def test_viewers_cannot_write(
    team: WorkspaceDb, operation: Callable[[WorkspaceDb, Context], object]
) -> None:
    ctx = team.ctx("vic", "viewer")
    with pytest.raises(PermissionDenied):
        operation(team, ctx)


def test_asset_class_risk_tier_is_created_updated_and_cleared(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    created = create_asset_class(
        team.factory,
        ctx,
        name="Stocks",
        color="#2563eb",
        investable=True,
        risk_tier="growth",
    )
    assert created.risk_tier == "growth"
    assert _event(team, "asset_class.created").data["risk_tier"] == "growth"

    updated = update_asset_class(
        team.factory,
        ctx,
        created.id,
        name="Stocks",
        color="#2563eb",
        investable=True,
        risk_tier="",
    )
    assert updated.risk_tier is None
    assert "risk_tier" in _event(team, "asset_class.updated").data["fields"]

    with pytest.raises(InvestmentError) as caught:
        update_asset_class(
            team.factory,
            ctx,
            created.id,
            name="Stocks",
            color="#2563eb",
            investable=True,
            risk_tier="risky",
        )
    assert (caught.value.field, caught.value.code) == ("risk_tier", "invalid_risk_tier")


def test_instrument_risk_tier_override_is_created_updated_and_cleared(
    team: WorkspaceDb,
) -> None:
    ctx = team.ctx("ed", "editor")
    stocks = _class(team, ctx, "Stocks")
    instrument = create_instrument(
        team.factory,
        ctx,
        name="Fund",
        symbol="",
        type="etf",
        quote_currency="EUR",
        unit_label="share",
        allocations=[(stocks.id, 10_000)],
        locale="en",
        risk_tier_override="speculative",
    )
    assert instrument.risk_tier_override == "speculative"
    assert _event(team, "instrument.created").data["risk_tier_override"] == "speculative"

    cleared = update_instrument(
        team.factory,
        ctx,
        instrument.id,
        name="Fund",
        symbol="",
        type="etf",
        quote_currency="EUR",
        unit_label="share",
        allocations=[(stocks.id, 10_000)],
        locale="en",
        risk_tier_override="",
    )
    assert cleared.risk_tier_override is None
    assert "risk_tier_override" in _event(team, "instrument.updated").data["fields"]

    with pytest.raises(InvestmentError) as caught:
        update_instrument(
            team.factory,
            ctx,
            instrument.id,
            name="Fund",
            symbol="",
            type="etf",
            quote_currency="EUR",
            unit_label="share",
            allocations=[(stocks.id, 10_000)],
            locale="en",
            risk_tier_override="risky",
        )
    assert (caught.value.field, caught.value.code) == (
        "risk_tier",
        "invalid_risk_tier",
    )


def test_apply_suggested_risk_tiers_sets_only_empty_matching_classes(
    team: WorkspaceDb,
) -> None:
    ctx = team.ctx("ed", "editor")
    _class(team, ctx, "Stocks")
    bonds = create_asset_class(
        team.factory,
        ctx,
        name="Bonds",
        color="#16a34a",
        investable=True,
        risk_tier="growth",
    )
    other = _class(team, ctx, "My own class")
    assert investment_page(team.factory, ctx, locale="en").can_suggest_risk_tiers

    assert apply_suggested_risk_tiers(team.factory, ctx) == 1
    page = investment_page(team.factory, ctx, locale="en")
    by_name = {row.name: row.risk_tier for row in page.classes}
    assert by_name == {"Stocks": "growth", "Bonds": "growth", "My own class": None}
    assert not page.can_suggest_risk_tiers
    event = _event(team, "asset_class.risk_tiers_suggested")
    assert event.data == {"count": 1, "classes": "Stocks"}

    assert apply_suggested_risk_tiers(team.factory, ctx) == 0
    assert bonds.risk_tier == "growth"
    assert other.id in {row.id for row in page.classes}
    assert len(_events(team, "asset_class.risk_tiers_suggested")) == 1


def test_apply_suggested_risk_tiers_matches_polish_seed_names(team: WorkspaceDb) -> None:
    ctx = team.ctx("ed", "editor")
    _class(team, ctx, gettext("Stocks", "pl"))
    assert apply_suggested_risk_tiers(team.factory, ctx) == 1
    page = investment_page(team.factory, ctx, locale="en")
    assert page.classes[0].risk_tier == RiskTier.GROWTH.value
