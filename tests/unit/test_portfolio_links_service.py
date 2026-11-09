"""Portfolio net-worth links (F6.8): current/historical resolution, helper, warnings and jobs."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Any
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from assetboard.config import Settings
from assetboard.db.models.identity import AuditEvent, Workspace
from assetboard.db.models.networth import NetWorthCategory, NetWorthItem, NetWorthSnapshot
from assetboard.db.repositories import FxRateRepository, InvestmentTransactionRepository
from assetboard.domain.money import Money
from assetboard.domain.networth import Side, Unavailable
from assetboard.recurring_jobs import RecurringJobs
from assetboard.services.context import Context, PermissionDenied
from assetboard.services.investment_transactions import create_transaction
from assetboard.services.investments import (
    archive_asset_class,
    archive_instrument,
    create_asset_class,
    create_instrument,
    update_instrument,
)
from assetboard.services.networth import (
    archive_category,
    archive_item,
    create_category,
    create_item,
    list_networth,
)
from assetboard.services.networth_links import UNAVAILABLE, registry
from assetboard.services.networth_snapshots import take_auto_snapshot, take_manual_snapshot
from assetboard.services.portfolio import portfolio_page
from assetboard.services.portfolio_links import (
    create_networth_items_from_asset_classes,
    ensure_networth_items_for_asset_classes,
    portfolio_link_candidates,
    register_portfolio_resolvers,
)
from assetboard.services.prices import save_manual_price
from assetboard.services.risk import save_risk_policy
from tests.tenant.workspace_db import WorkspaceDb, workspace_db

TODAY = date(2026, 3, 1)  # the workspace_db test clock
MONTH_END = date(2026, 2, 28)
CLASS_LINK = "portfolio_asset_class"
HOLDING_LINK = "portfolio_holding"


@pytest.fixture
def db(tmp_path: Path) -> Iterator[WorkspaceDb]:
    yield from workspace_db(tmp_path)


@pytest.fixture
def team(db: WorkspaceDb) -> WorkspaceDb:
    db.add_member("olga", "owner")
    db.add_member("ed", "editor")
    db.add_member("vic", "viewer")
    return db


@pytest.fixture(autouse=True)
def _portfolio_registry() -> Iterator[None]:
    registry.reset_for_tests()
    register_portfolio_resolvers()
    try:
        yield
    finally:
        registry.reset_for_tests()


def _editor(team: WorkspaceDb) -> Context:
    return team.ctx("ed", "editor")


def _set_auto_link(team: WorkspaceDb, enabled: bool = True) -> None:
    with team.factory.begin() as session:
        session.execute(
            update(Workspace)
            .where(Workspace.id == team.workspace_id)
            .values(auto_link_portfolio=enabled)
        )


def _class(
    team: WorkspaceDb,
    name: str = "Stocks",
    *,
    investable: bool = True,
) -> UUID:
    return create_asset_class(
        team.factory, _editor(team), name=name, color="#2563eb", investable=investable
    ).id


def _instrument(
    team: WorkspaceDb,
    class_id: UUID,
    *,
    name: str = "World ETF",
    type_value: str = "etf",
    currency: str = "EUR",
    allocations: list[tuple[UUID, int]] | None = None,
) -> UUID:
    return create_instrument(
        team.factory,
        _editor(team),
        name=name,
        symbol="",
        type=type_value,
        quote_currency=currency,
        unit_label="share" if type_value != "bond_model" else "bond",
        allocations=allocations or [(class_id, 10_000)],
        locale="en",
    ).id


def _trade(
    team: WorkspaceDb,
    instrument_id: UUID,
    *,
    day: str = "2026-01-10",
    kind: str = "buy",
    quantity: str = "10",
    price: str = "100",
    fx: str = "1",
) -> None:
    create_transaction(
        team.factory,
        _editor(team),
        instrument_id=instrument_id,
        type_value=kind,
        date_value=day,
        quantity_value=quantity,
        unit_price_value=price,
        fee_value="0",
        fx_value=fx,
        note_value="",
        locale="en",
    )


def _undated(
    team: WorkspaceDb,
    instrument_id: UUID,
    *,
    quantity: str = "5",
    price: str = "80",
    currency: str = "EUR",
    fx: str | None = None,
) -> None:
    with team.factory.begin() as session:
        InvestmentTransactionRepository(session, _editor(team)).create(
            instrument_id=instrument_id,
            date=None,
            type="buy",
            quantity=Decimal(quantity),
            unit_price=Decimal(price),
            quote_currency=currency,
            fee_minor=0,
            fx_to_base=Decimal(fx) if fx is not None else None,
        )


def _price(team: WorkspaceDb, instrument_id: UUID, day: str, value: str) -> None:
    save_manual_price(
        team.factory,
        _editor(team),
        instrument_id,
        price_id=None,
        date_value=day,
        price_value=value,
        locale="en",
    )


def _fx(team: WorkspaceDb, day: str, rate: str, quote: str = "USD") -> None:
    with team.factory.begin() as session:
        FxRateRepository(session, _editor(team)).create(
            base_currency="EUR",
            quote_currency=quote,
            date=date.fromisoformat(day),
            rate=Decimal(rate),
            source="manual",
        )


def _resolve(
    team: WorkspaceDb,
    link_type: str,
    link_ids: list[UUID],
    dates: list[date],
) -> dict[tuple[UUID, date], Money | Unavailable]:
    ctx = _editor(team)
    with team.factory() as session:
        resolver = registry.resolver_for(session, ctx, link_type)
        assert resolver is not None
        return dict(resolver.resolve_many(link_ids, dates))


def _resolver(team: WorkspaceDb, link_type: str) -> Any:
    ctx = _editor(team)
    session = team.factory()
    resolver = registry.resolver_for(session, ctx, link_type)
    assert resolver is not None
    return session, resolver


def _category(team: WorkspaceDb, name: str = "Cash") -> UUID:
    return create_category(team.factory, _editor(team), side="asset", name=name, color="#16a34a").id


def _manual_item(
    team: WorkspaceDb,
    category_id: UUID,
    *,
    name: str = "Cash",
    value: str = "100",
) -> UUID:
    return create_item(
        team.factory,
        _editor(team),
        locale="en",
        side="asset",
        category_id=category_id,
        name=name,
        value=value,
    ).id


def _linked_item(
    team: WorkspaceDb,
    category_id: UUID,
    *,
    name: str,
    link_type: str,
    link_id: UUID,
) -> UUID:
    return create_item(
        team.factory,
        _editor(team),
        locale="en",
        side="asset",
        category_id=category_id,
        name=name,
        link_type=link_type,
        link_id=link_id,
    ).id


def _items(team: WorkspaceDb) -> list[NetWorthItem]:
    with team.factory() as session:
        return list(
            session.scalars(
                select(NetWorthItem)
                .where(NetWorthItem.workspace_id == team.workspace_id)
                .order_by(NetWorthItem.created_at, NetWorthItem.id)
            )
        )


def _categories(team: WorkspaceDb) -> list[NetWorthCategory]:
    with team.factory() as session:
        return list(
            session.scalars(
                select(NetWorthCategory)
                .where(NetWorthCategory.workspace_id == team.workspace_id)
                .order_by(NetWorthCategory.sort_order, NetWorthCategory.id)
            )
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


def _snapshot(team: WorkspaceDb, as_of: date) -> NetWorthSnapshot:
    with team.factory() as session:
        return session.scalars(
            select(NetWorthSnapshot).where(
                NetWorthSnapshot.workspace_id == team.workspace_id,
                NetWorthSnapshot.as_of == as_of,
            )
        ).one()


def _class_total(team: WorkspaceDb, class_id: UUID) -> Money:
    page = portfolio_page(team.factory, _editor(team))
    row = next(row for row in page.class_rows if row.asset_class and row.asset_class.id == class_id)
    return row.total.value


def test_today_uses_the_current_portfolio_and_past_dates_use_the_series(
    team: WorkspaceDb,
) -> None:
    stocks = _class(team)
    instrument = _instrument(team, stocks)
    _trade(team, instrument, day="2026-01-10")
    _undated(team, instrument)
    _price(team, instrument, "2026-02-15", "110")
    _price(team, instrument, "2026-03-01", "120")

    current = _resolve(team, CLASS_LINK, [stocks], [TODAY])
    history = _resolve(team, CLASS_LINK, [stocks], [MONTH_END])
    current_holding = _resolve(team, HOLDING_LINK, [instrument], [TODAY])
    history_holding = _resolve(team, HOLDING_LINK, [instrument], [MONTH_END])

    # Today: the F6.6 path, including the undated legacy lot and the latest price.
    assert current[(stocks, TODAY)] == Money(180_000, "EUR")
    assert current[(stocks, TODAY)] == _class_total(team, stocks)
    assert current_holding[(instrument, TODAY)] == Money(180_000, "EUR")
    # A past date: dated lots only, with the 15 February price carried forward.
    assert history[(stocks, MONTH_END)] == Money(110_000, "EUR")
    assert history_holding[(instrument, MONTH_END)] == Money(110_000, "EUR")


def test_a_split_adjusts_the_historical_carried_forward_price(team: WorkspaceDb) -> None:
    stocks = _class(team)
    instrument = _instrument(team, stocks)
    _trade(team, instrument, day="2026-01-10", quantity="10", price="100")
    _price(team, instrument, "2026-01-10", "100")
    _trade(team, instrument, day="2026-02-01", kind="split", quantity="2", price="1")

    history = _resolve(
        team,
        HOLDING_LINK,
        [instrument],
        [date(2026, 1, 31), date(2026, 2, 28)],
    )
    assert history[(instrument, date(2026, 1, 31))] == Money(100_000, "EUR")
    assert history[(instrument, date(2026, 2, 28))] == Money(100_000, "EUR")


def test_class_values_follow_the_same_look_through_as_the_portfolio_page(
    team: WorkspaceDb,
) -> None:
    stocks = _class(team, "Stocks")
    bonds = _class(team, "Bonds")
    instrument = _instrument(
        team,
        stocks,
        allocations=[(stocks, 6000), (bonds, 4000)],
    )
    _trade(team, instrument, day="2026-01-10")
    _price(team, instrument, "2026-03-01", "120")

    current = _resolve(team, CLASS_LINK, [stocks, bonds], [TODAY])
    history = _resolve(team, CLASS_LINK, [stocks, bonds], [MONTH_END])

    assert current[(stocks, TODAY)] == Money(72_000, "EUR")
    assert current[(bonds, TODAY)] == Money(48_000, "EUR")
    assert current[(stocks, TODAY)] == _class_total(team, stocks)
    assert current[(bonds, TODAY)] == _class_total(team, bonds)
    assert history[(stocks, MONTH_END)] == Money(60_000, "EUR")
    assert history[(bonds, MONTH_END)] == Money(40_000, "EUR")


def test_current_and_historical_values_use_the_latest_fx_rate(
    team: WorkspaceDb,
) -> None:
    stocks = _class(team)
    instrument = _instrument(team, stocks, currency="USD")
    _trade(team, instrument, day="2026-01-10", fx="0.8")
    _fx(team, "2026-02-15", "0.9")
    _fx(team, "2026-03-01", "1.1")

    current = _resolve(team, CLASS_LINK, [stocks], [TODAY])
    history = _resolve(team, CLASS_LINK, [stocks], [MONTH_END])

    assert current[(stocks, TODAY)] == Money(110_000, "EUR")
    assert current[(stocks, TODAY)] == _class_total(team, stocks)
    assert history[(stocks, MONTH_END)] == Money(90_000, "EUR")


def test_a_class_is_unavailable_when_one_held_member_has_no_price(
    team: WorkspaceDb,
) -> None:
    stocks = _class(team)
    priced = _instrument(team, stocks, name="Priced")
    unpriced = _instrument(team, stocks, name="Legacy")
    _trade(team, priced, day="2026-01-10")
    _undated(team, unpriced)

    current = _resolve(team, CLASS_LINK, [stocks], [TODAY])
    history = _resolve(team, CLASS_LINK, [stocks], [MONTH_END])
    holdings = _resolve(team, HOLDING_LINK, [priced, unpriced], [TODAY])

    # Never a partial class value: the priced member alone would have been €1,000.
    assert current[(stocks, TODAY)] is UNAVAILABLE
    assert holdings[(priced, TODAY)] == Money(100_000, "EUR")
    assert holdings[(unpriced, TODAY)] is UNAVAILABLE
    # The undated lot is not part of the historical series, so the class is complete.
    assert history[(stocks, MONTH_END)] == Money(100_000, "EUR")
    # The helper does not create an item that would immediately show Unavailable.
    assert create_networth_items_from_asset_classes(team.factory, _editor(team)).created == ()
    # A linked class that is unavailable is not counted, so it cannot double count a holding.
    category = _category(team)
    _linked_item(team, category, name="Stocks", link_type=CLASS_LINK, link_id=stocks)
    _linked_item(team, category, name="Priced", link_type=HOLDING_LINK, link_id=priced)
    page = list_networth(team.factory, _editor(team))
    assert [item for item in page.portfolio_warnings if item.code == "class_holding"] == []


def test_a_class_is_unavailable_when_one_held_member_has_no_fx_rate(
    team: WorkspaceDb,
) -> None:
    stocks = _class(team)
    priced = _instrument(team, stocks, name="Priced")
    foreign = _instrument(team, stocks, name="Legacy USD", currency="USD")
    _trade(team, priced, day="2026-01-10")
    _undated(team, foreign, currency="USD")

    current = _resolve(team, CLASS_LINK, [stocks], [TODAY])
    history = _resolve(team, CLASS_LINK, [stocks], [MONTH_END])
    holdings = _resolve(team, HOLDING_LINK, [foreign], [TODAY])

    assert current[(stocks, TODAY)] is UNAVAILABLE
    assert holdings[(foreign, TODAY)] is UNAVAILABLE
    assert history[(stocks, MONTH_END)] == Money(100_000, "EUR")


def test_choices_and_descriptions_include_archived_and_sold_out_targets(
    team: WorkspaceDb,
) -> None:
    stocks = _class(team, "Stocks")
    bonds = _class(team, "Bonds")
    sold_out = _instrument(team, stocks, name="Sold out")
    archived = _instrument(team, stocks, name="Archived fund")
    active = _instrument(team, stocks, name="Active fund")
    bond_fund = _instrument(team, bonds, name="Bond fund")
    _trade(team, sold_out, day="2026-01-10")
    _trade(team, sold_out, day="2026-01-11", kind="sell")
    _trade(team, archived, day="2026-01-10")
    _trade(team, active, day="2026-01-10")
    _trade(team, bond_fund, day="2026-01-10")
    archive_instrument(team.factory, _editor(team), archived)
    archive_asset_class(team.factory, _editor(team), bonds)

    session, class_resolver = _resolver(team, CLASS_LINK)
    try:
        assert [choice.link_id for choice in class_resolver.choices()] == [stocks]
        target = class_resolver.describe(bonds)
        assert target is not None and target.label == "Bonds"
        assert target.currency == "EUR"
        assert target.href == f"/investments/portfolio?scope={bonds}"
        assert class_resolver.describe(uuid4()) is None
    finally:
        session.close()
    session, holding_resolver = _resolver(team, HOLDING_LINK)
    try:
        assert {choice.link_id for choice in holding_resolver.choices()} == {active, bond_fund}
        target = holding_resolver.describe(archived)
        assert target is not None and target.label == "Archived fund"
        assert target.href == f"/investments/instruments/{archived}"
        assert holding_resolver.describe(sold_out) is not None
    finally:
        session.close()

    current = _resolve(team, HOLDING_LINK, [sold_out, archived, active], [TODAY])
    assert current[(sold_out, TODAY)] == Money(0, "EUR")
    assert current[(archived, TODAY)] == Money(100_000, "EUR")
    assert current[(active, TODAY)] == Money(100_000, "EUR")
    archived_classes = _resolve(team, CLASS_LINK, [stocks, bonds], [TODAY])
    assert archived_classes[(stocks, TODAY)] == Money(200_000, "EUR")
    assert archived_classes[(bonds, TODAY)] == Money(100_000, "EUR")


def test_helper_replaces_an_archived_investments_category(team: WorkspaceDb) -> None:
    archived = _category(team, "Investments")
    archive_category(team.factory, _editor(team), archived)
    stocks = _class(team)
    instrument = _instrument(team, stocks)
    _trade(team, instrument)

    result = create_networth_items_from_asset_classes(team.factory, _editor(team))

    assert result.category_created is True
    active = [row for row in _categories(team) if row.archived_at is None]
    assert [row.name for row in active] == ["Investments"]
    assert active[0].id != archived


def test_helper_creates_investments_items_with_audit_and_reuses_them(
    team: WorkspaceDb,
) -> None:
    _category(team, "Cash")
    stocks = _class(team, "Stocks")
    bonds = _class(team, "Bonds", investable=False)
    _class(team, "Empty")
    archived = _class(team, "Archived")
    stocks_fund = _instrument(team, stocks, name="Share fund")
    bond_fund = _instrument(team, bonds, name="Bond fund")
    archived_fund = _instrument(team, archived, name="Old fund")
    _trade(team, stocks_fund, day="2026-01-10")
    _trade(team, bond_fund, day="2026-01-10", price="50")
    _trade(team, archived_fund, day="2026-01-10")
    archive_asset_class(team.factory, _editor(team), archived)

    result = create_networth_items_from_asset_classes(team.factory, _editor(team))

    assert result.created == ("Stocks", "Bonds")
    assert result.skipped == 1  # the active class without a value
    assert result.category_created is True
    categories = _categories(team)
    assert [row.name for row in categories] == ["Cash", "Investments"]
    investments = categories[-1]
    assert investments.side == "asset"
    items = {item.name: item for item in _items(team)}
    assert set(items) == {"Stocks", "Bonds"}
    for name, link_id, investable in (
        ("Stocks", stocks, True),
        ("Bonds", bonds, False),
    ):
        item = items[name]
        assert item.category_id == investments.id
        assert (item.valuation_mode, item.link_type, item.link_id) == (
            "linked",
            CLASS_LINK,
            link_id,
        )
        assert item.currency == "EUR"
        assert item.investable is investable
        assert item.include_in_net_worth is True
        assert item.estimate_between is True
    created = _events(team, "networth_item.created")
    linked = _events(team, "networth_item.linked")
    assert len(created) == len(linked) == 2
    assert {event.entity_id for event in created} == {str(item.id) for item in items.values()}
    category_events = [
        event
        for event in _events(team, "networth_category.created")
        if event.data.get("name") == "Investments"
    ]
    assert [event.data for event in category_events] == [{"name": "Investments", "side": "asset"}]

    again = create_networth_items_from_asset_classes(team.factory, _editor(team))

    assert again.created == ()
    assert again.skipped == 3
    assert again.category_created is False
    assert len(_items(team)) == 2
    assert len(_categories(team)) == 2
    assert len(_events(team, "networth_item.created")) == 2
    assert (
        len(
            [
                event
                for event in _events(team, "networth_category.created")
                if event.data.get("name") == "Investments"
            ]
        )
        == 1
    )


def test_helper_reuses_an_active_category_named_in_another_locale(
    team: WorkspaceDb,
) -> None:
    _category(team, "Inwestycje")
    stocks = _class(team)
    instrument = _instrument(team, stocks)
    _trade(team, instrument)

    result = create_networth_items_from_asset_classes(team.factory, _editor(team))

    assert result.category_created is False
    assert [row.name for row in _categories(team)] == ["Inwestycje"]


def test_helper_creates_the_category_in_the_workspace_locale(team: WorkspaceDb) -> None:
    stocks = _class(team)
    instrument = _instrument(team, stocks)
    _trade(team, instrument)
    ctx = _editor(team)
    polish = replace(ctx, workspace=replace(ctx.workspace, locale="pl"))

    result = create_networth_items_from_asset_classes(team.factory, polish)

    assert result.category_created is True
    assert _categories(team)[0].name == "Inwestycje"


def test_helper_requires_an_editor_and_is_safe_when_two_clicks_race(
    team: WorkspaceDb,
) -> None:
    stocks = _class(team)
    instrument = _instrument(team, stocks)
    _trade(team, instrument)
    with pytest.raises(PermissionDenied):
        create_networth_items_from_asset_classes(team.factory, team.ctx("vic", "viewer"))

    barrier = threading.Barrier(2)

    def click() -> Any:
        barrier.wait(timeout=10)
        return create_networth_items_from_asset_classes(team.factory, _editor(team))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result(timeout=20) for future in (pool.submit(click), pool.submit(click))]

    assert sorted(len(result.created) for result in results) == [0, 1]
    assert len(_items(team)) == 1
    assert len(_categories(team)) == 1
    assert len(_events(team, "networth_item.created")) == 1
    assert len(_events(team, "networth_item.linked")) == 1
    assert len(_events(team, "networth_category.created")) == 1


def test_warning_when_a_class_and_one_of_its_holdings_are_linked(
    team: WorkspaceDb,
) -> None:
    category = _category(team)
    stocks = _class(team)
    instrument = _instrument(team, stocks, name="World ETF")
    _trade(team, instrument)
    _linked_item(team, category, name="Stocks", link_type=CLASS_LINK, link_id=stocks)
    _linked_item(team, category, name="World ETF", link_type=HOLDING_LINK, link_id=instrument)

    page = list_networth(team.factory, _editor(team))
    warning = next(item for item in page.portfolio_warnings if item.code == "class_holding")
    assert warning.class_name == "Stocks"
    assert warning.holding_names == ("World ETF",)

    _trade(team, instrument, day="2026-01-11", kind="sell")

    page = list_networth(team.factory, _editor(team))
    assert [item for item in page.portfolio_warnings if item.code == "class_holding"] == []


def test_warning_when_a_legacy_property_instrument_and_real_estate_item_both_have_value(
    team: WorkspaceDb,
) -> None:
    property_class = _class(team, "Property")
    instrument = _instrument(
        team,
        property_class,
        name="Legacy flat",
        type_value="real_estate",
        currency="EUR",
    )
    _trade(team, instrument, price="100000")
    real_estate = _category(team, "Real estate")
    _manual_item(team, real_estate, name="Flat", value="1500.00")

    page = list_networth(team.factory, _editor(team))
    warning = next(item for item in page.portfolio_warnings if item.code == "legacy_property")
    assert warning.item_names == ("Flat",)
    assert warning.instrument_names == ("Legacy flat",)

    archive_instrument(team.factory, _editor(team), instrument)
    page = list_networth(team.factory, _editor(team))
    assert [item for item in page.portfolio_warnings if item.code == "legacy_property"] == []


def test_manual_snapshot_uses_current_values_and_auto_snapshot_uses_history(
    team: WorkspaceDb,
) -> None:
    stocks = _class(team)
    instrument = _instrument(team, stocks)
    _trade(team, instrument, day="2026-01-10")
    _undated(team, instrument)
    _price(team, instrument, "2026-02-15", "110")
    _price(team, instrument, "2026-03-01", "120")
    create_networth_items_from_asset_classes(team.factory, _editor(team))

    assert take_manual_snapshot(team.factory, _editor(team))[0] == TODAY
    assert _snapshot(team, TODAY).assets_minor == 180_000
    assert take_auto_snapshot(team.factory, _editor(team), team.clock, "UTC") is True
    assert _snapshot(team, MONTH_END).assets_minor == 110_000


def test_the_scheduler_snapshot_job_resolves_linked_histories(team: WorkspaceDb) -> None:
    stocks = _class(team)
    instrument = _instrument(team, stocks)
    _trade(team, instrument, day="2026-01-10")
    _undated(team, instrument)
    _price(team, instrument, "2026-02-15", "110")
    _price(team, instrument, "2026-03-01", "120")
    create_networth_items_from_asset_classes(team.factory, _editor(team))
    logger = Mock()

    jobs = RecurringJobs(
        team.factory,
        Settings(_env_file=None),
        lambda: team.clock,
        logger,
    )
    jobs.networth_snapshot(team.workspace_id, "UTC")

    assert _snapshot(team, MONTH_END).assets_minor == 110_000
    logger.error.assert_not_called()


def test_resolve_many_loads_history_once_and_is_fast(team: WorkspaceDb) -> None:
    stocks = _class(team)
    instruments = []
    for index in range(30):
        instrument = _instrument(team, stocks, name=f"Fund {index}")
        _trade(team, instrument, day="2020-01-02")
        instruments.append(instrument)
    dates = [date(2020 + index // 12, index % 12 + 1, 28) for index in range(60)]
    import assetboard.services.portfolio_links as portfolio_links

    calls = 0
    real_load = portfolio_links.load_history

    def counting_load(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return real_load(*args, **kwargs)

    portfolio_links.load_history = counting_load
    try:
        started = perf_counter()
        values = _resolve(team, HOLDING_LINK, instruments, dates)
        elapsed = perf_counter() - started
    finally:
        portfolio_links.load_history = real_load

    print(f"portfolio resolve_many: {elapsed * 1000:.1f} ms, {len(values)} values")
    assert calls == 1
    assert len(values) == 60 * 30
    assert all(value == Money(100_000, "EUR") for value in values.values())
    assert elapsed < 2


def test_archived_real_estate_items_do_not_warn(team: WorkspaceDb) -> None:
    property_class = _class(team, "Property")
    instrument = _instrument(
        team,
        property_class,
        name="Legacy flat",
        type_value="real_estate",
        currency="EUR",
    )
    _trade(team, instrument, price="100000")
    real_estate = _category(team, "Real estate")
    item_id = _manual_item(team, real_estate, name="Flat", value="1500.00")
    archive_item(team.factory, _editor(team), item_id)

    page = list_networth(team.factory, _editor(team))
    assert [item for item in page.portfolio_warnings if item.code == "legacy_property"] == []


def test_side_views_keep_the_same_linked_values(team: WorkspaceDb) -> None:
    stocks = _class(team)
    instrument = _instrument(team, stocks)
    _trade(team, instrument)
    category = _category(team)
    _linked_item(team, category, name="Stocks", link_type=CLASS_LINK, link_id=stocks)

    page = list_networth(team.factory, team.ctx("vic", "viewer"))

    asset = page.side_view(Side.ASSET)
    assert [item.current for group in asset.groups for item in group.items] == [
        Money(100_000, "EUR")
    ]
    assert page.side_view(Side.LIABILITY).groups == ()


def test_ensure_creates_links_once_and_leaves_archived_items_alone(
    team: WorkspaceDb,
) -> None:
    stocks = _class(team)
    instrument = _instrument(team, stocks)
    _trade(team, instrument)
    _set_auto_link(team)

    def ensure() -> Any:
        with team.factory.begin() as session:
            return ensure_networth_items_for_asset_classes(session, _editor(team))

    first = ensure()
    assert first.created == ("Stocks",)
    assert first.category_created is True
    assert [(item.name, item.link_type, item.link_id) for item in _items(team)] == [
        ("Stocks", CLASS_LINK, stocks)
    ]

    second = ensure()
    assert second.created == ()
    assert len(_items(team)) == 1

    archive_item(team.factory, _editor(team), _items(team)[0].id)
    assert ensure().created == ()
    assert len(_items(team)) == 1


def test_manual_helper_recreates_a_link_the_user_archived(team: WorkspaceDb) -> None:
    stocks = _class(team)
    instrument = _instrument(team, stocks)
    _trade(team, instrument)

    first = create_networth_items_from_asset_classes(team.factory, _editor(team))
    assert first.created == ("Stocks",)
    archive_item(team.factory, _editor(team), _items(team)[0].id)

    again = create_networth_items_from_asset_classes(team.factory, _editor(team))

    assert again.created == ("Stocks",)
    assert sorted((item.name, item.archived_at is not None) for item in _items(team)) == [
        ("Stocks", False),
        ("Stocks", True),
    ]


def test_ensure_is_a_no_op_while_the_setting_is_off(team: WorkspaceDb) -> None:
    stocks = _class(team)
    instrument = _instrument(team, stocks)
    _trade(team, instrument)

    with team.factory.begin() as session:
        result = ensure_networth_items_for_asset_classes(session, _editor(team))

    assert result.created == ()
    assert _items(team) == []
    assert portfolio_link_candidates(_session(team), _editor(team)) == 1


def _session(team: WorkspaceDb) -> Any:
    return team.factory()


def test_ensure_skips_a_class_with_missing_price_or_fx(team: WorkspaceDb) -> None:
    unpriced = _class(team, "Unpriced")
    unpriced_fund = _instrument(team, unpriced, name="Legacy")
    _undated(team, unpriced_fund)
    foreign = _class(team, "Foreign")
    foreign_fund = _instrument(team, foreign, name="Legacy USD", currency="USD")
    _undated(team, foreign_fund, currency="USD", fx=None)
    _set_auto_link(team)

    with team.factory.begin() as session:
        result = ensure_networth_items_for_asset_classes(session, _editor(team))

    assert result.created == ()
    assert _items(team) == []


def test_a_transaction_write_creates_the_link_when_enabled(team: WorkspaceDb) -> None:
    _set_auto_link(team)
    stocks = _class(team)
    instrument = _instrument(team, stocks)

    _trade(team, instrument)

    assert [(item.name, item.link_type, item.link_id) for item in _items(team)] == [
        ("Stocks", CLASS_LINK, stocks)
    ]


def test_an_allocation_update_creates_links_for_the_new_classes(
    team: WorkspaceDb,
) -> None:
    stocks = _class(team, "Stocks")
    bonds = _class(team, "Bonds")
    instrument = _instrument(team, stocks)
    _trade(team, instrument)
    _set_auto_link(team)

    update_instrument(
        team.factory,
        _editor(team),
        instrument,
        name="World ETF",
        symbol="",
        type="etf",
        quote_currency="EUR",
        unit_label="share",
        allocations=[(stocks, 5000), (bonds, 5000)],
        locale="en",
    )

    assert {item.name for item in _items(team)} == {"Stocks", "Bonds"}


def test_saving_the_risk_policy_creates_links_when_enabled(team: WorkspaceDb) -> None:
    stocks = _class(team)
    instrument = _instrument(team, stocks)
    _trade(team, instrument)
    _set_auto_link(team)

    save_risk_policy(
        team.factory,
        _editor(team),
        mode="fixed",
        safe="60",
        growth="30",
        speculative="10",
        tolerance="5",
        locale="en",
    )

    assert [(item.name, item.link_id) for item in _items(team)] == [("Stocks", stocks)]


def test_link_candidates_are_workspace_scoped(team: WorkspaceDb) -> None:
    stocks = _class(team)
    instrument = _instrument(team, stocks)
    _trade(team, instrument)

    other_id = team.add_workspace("Other")
    team.add_member("olga", "owner", workspace_id=other_id)
    other = team.ctx("olga", "owner", workspace_id=other_id)
    other_class = create_asset_class(
        team.factory, other, name="Bonds", color="#0f766e", investable=True
    ).id
    other_instrument = create_instrument(
        team.factory,
        other,
        name="Bond fund",
        symbol="",
        type="etf",
        quote_currency="EUR",
        unit_label="share",
        allocations=[(other_class, 10_000)],
        locale="en",
    ).id
    create_transaction(
        team.factory,
        other,
        instrument_id=other_instrument,
        type_value="buy",
        date_value="2026-01-10",
        quantity_value="10",
        unit_price_value="100",
        fee_value="0",
        fx_value="1",
        note_value="",
        locale="en",
    )

    first_session = _session(team)
    try:
        assert portfolio_link_candidates(first_session, _editor(team)) == 1
        assert portfolio_link_candidates(first_session, other) == 1
    finally:
        first_session.close()
    create_networth_items_from_asset_classes(team.factory, _editor(team))
    first_session = _session(team)
    try:
        assert portfolio_link_candidates(first_session, _editor(team)) == 0
        assert portfolio_link_candidates(first_session, other) == 1
    finally:
        first_session.close()
