"""Net-worth portfolio links over HTTP (F6.8): helper, warnings and placement."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

from sqlalchemy import select, update

from assetboard.clock import FixedClock
from assetboard.db.models.identity import Workspace
from assetboard.db.models.investments import AssetClass
from assetboard.db.models.networth import NetWorthCategory, NetWorthItem
from assetboard.services.context import Role
from assetboard.services.investment_transactions import create_transaction
from assetboard.services.investments import create_asset_class, create_instrument
from assetboard.services.networth import create_item
from tests.conftest import OwnerApp
from tests.tenant.harness import context_for

NOW = "2026-03-10T12:00:00+00:00"
ASSETS = "/net-worth?side=asset"
LIABILITIES = "/net-worth?side=liability"


def _ctx(owner: OwnerApp, role: Role = Role.OWNER):
    ctx = context_for(owner.home_id, owner.app.state.clock, role)
    return replace(ctx, user=replace(ctx.user, id=owner.admin_id))


def _class_id(owner: OwnerApp, name: str = "Stocks") -> UUID:
    with owner.db() as session:
        return session.scalars(
            select(AssetClass.id).where(
                AssetClass.workspace_id == owner.home_id,
                AssetClass.name == name,
            )
        ).one()


def _instrument(
    owner: OwnerApp,
    name: str,
    class_id: UUID,
    *,
    type_value: str = "etf",
    currency: str = "EUR",
) -> UUID:
    return create_instrument(
        owner.app.state.session_factory,
        _ctx(owner),
        name=name,
        symbol="",
        type=type_value,
        quote_currency=currency,
        unit_label="share" if type_value != "real_estate" else "property",
        allocations=[(class_id, 10_000)],
        locale="en",
    ).id


def _buy(
    owner: OwnerApp,
    instrument_id: UUID,
    *,
    day: str = "2026-03-09",
    quantity: str = "10",
    price: str = "100",
) -> None:
    create_transaction(
        owner.app.state.session_factory,
        _ctx(owner),
        instrument_id=instrument_id,
        type_value="buy",
        date_value=day,
        quantity_value=quantity,
        unit_price_value=price,
        fee_value="0",
        fx_value="1",
        note_value="",
        locale="en",
    )


def _category_id(owner: OwnerApp, name: str) -> UUID:
    with owner.db() as session:
        return session.scalars(
            select(NetWorthCategory.id).where(
                NetWorthCategory.workspace_id == owner.home_id,
                NetWorthCategory.name == name,
            )
        ).one()


def _linked_item(
    owner: OwnerApp,
    category_id: UUID,
    *,
    name: str,
    link_type: str,
    link_id: UUID,
) -> UUID:
    return create_item(
        owner.app.state.session_factory,
        _ctx(owner),
        locale="en",
        side="asset",
        category_id=category_id,
        name=name,
        link_type=link_type,
        link_id=link_id,
    ).id


def _manual_item(owner: OwnerApp, category_id: UUID, *, name: str, value: str) -> UUID:
    return create_item(
        owner.app.state.session_factory,
        _ctx(owner),
        locale="en",
        side="asset",
        category_id=category_id,
        name=name,
        value=value,
    ).id


def _create_links(owner: OwnerApp, token_from: str = ASSETS):
    return owner.post("/net-worth/link-asset-classes", {}, token_from=token_from)


def _set_auto_link(owner: OwnerApp, enabled: bool) -> None:
    with owner.db() as session, session.begin():
        session.execute(
            update(Workspace)
            .where(Workspace.id == owner.home_id)
            .values(auto_link_portfolio=enabled)
        )


def test_helper_is_assets_only_and_creates_linked_items(owner: OwnerApp) -> None:
    _set_auto_link(owner, False)
    clock = FixedClock(NOW)
    owner.app.state.clock = clock
    owner.app.state.session_factory.configure(info={"clock": clock})
    stocks = _class_id(owner)
    instrument = _instrument(owner, "World ETF", stocks)
    _buy(owner, instrument)

    assert "data-create-portfolio-links" in owner.client.get(ASSETS).text
    assert "data-create-portfolio-links" not in owner.client.get(LIABILITIES).text

    response = _create_links(owner)

    assert response.status_code == 303
    assert "notice=portfolio_links_created" in response.headers["location"]
    page = owner.client.get(response.headers["location"]).text
    assert "Net-worth items created from asset classes." in page
    assert "Investments" in page
    assert "data-linked" in page
    assert "€1,000.00" in page
    with owner.db() as session:
        items = list(
            session.scalars(select(NetWorthItem).where(NetWorthItem.workspace_id == owner.home_id))
        )
        categories = list(
            session.scalars(
                select(NetWorthCategory).where(
                    NetWorthCategory.workspace_id == owner.home_id,
                    NetWorthCategory.name == "Investments",
                )
            )
        )
    assert [(item.name, item.link_type, item.link_id) for item in items] == [
        ("Stocks", "portfolio_asset_class", stocks)
    ]
    assert items[0].category_id == categories[0].id
    assert categories[0].side == "asset"

    again = _create_links(owner)
    assert again.status_code == 303
    assert "notice=portfolio_links_none" in again.headers["location"]
    with owner.db() as session:
        assert len(list(session.scalars(select(NetWorthItem)))) == 1


def test_viewers_do_not_see_or_cannot_use_the_helper(owner: OwnerApp) -> None:
    owner.set_admin_role("viewer")

    assert "data-create-portfolio-links" not in owner.client.get(ASSETS).text
    response = _create_links(owner)

    assert response.status_code == 403


def test_warnings_are_rendered_on_assets_only(owner: OwnerApp) -> None:
    _set_auto_link(owner, False)
    clock = FixedClock(NOW)
    owner.app.state.clock = clock
    owner.app.state.session_factory.configure(info={"clock": clock})
    stocks = _class_id(owner)
    fund = _instrument(owner, "World ETF", stocks)
    _buy(owner, fund)
    property_class = create_asset_class(
        owner.app.state.session_factory,
        _ctx(owner),
        name="Property",
        color="#a16207",
        investable=False,
    ).id
    legacy = _instrument(owner, "Legacy flat", property_class, type_value="real_estate")
    _buy(owner, legacy, day="2026-03-09", quantity="1", price="100000")

    assert _create_links(owner).status_code == 303

    investments = _category_id(owner, "Investments")
    _linked_item(
        owner,
        investments,
        name="World ETF holding",
        link_type="portfolio_holding",
        link_id=fund,
    )
    real_estate = _category_id(owner, "Real estate")
    _manual_item(owner, real_estate, name="Flat", value="1500.00")

    asset = owner.client.get(ASSETS).text
    assert "Possible double counting" in asset
    assert 'data-portfolio-warning="class_holding"' in asset
    assert 'data-portfolio-warning="legacy_property"' in asset
    liability = owner.client.get(LIABILITIES).text
    assert "Possible double counting" not in liability
    assert "data-portfolio-warning=" not in liability


def test_the_helper_and_warnings_are_translated(owner: OwnerApp) -> None:
    _set_auto_link(owner, False)
    clock = FixedClock(NOW)
    owner.app.state.clock = clock
    owner.app.state.session_factory.configure(info={"clock": clock})
    stocks = _class_id(owner)
    instrument = _instrument(owner, "World ETF", stocks)
    _buy(owner, instrument)
    assert _create_links(owner).status_code == 303
    bonds = _class_id(owner, "Bonds")
    _buy(owner, _instrument(owner, "Bond fund", bonds))
    owner.client.headers["Accept-Language"] = "pl"

    page = owner.client.get(ASSETS).text

    assert "Utwórz pozycje majątku z klas aktywów" in page
    assert "Możliwe podwójne liczenie" not in page  # no warning without a holding link


def test_the_linked_item_links_back_to_the_portfolio(owner: OwnerApp) -> None:
    _set_auto_link(owner, False)
    clock = FixedClock(NOW)
    owner.app.state.clock = clock
    owner.app.state.session_factory.configure(info={"clock": clock})
    stocks = _class_id(owner)
    instrument = _instrument(owner, "World ETF", stocks)
    _buy(owner, instrument)
    _create_links(owner)

    page = owner.client.get(ASSETS).text

    assert f"/investments/portfolio?scope={stocks}" in page
    assert "Linked to Stocks" in page
    assert "Asset class" in page


def test_the_helper_category_is_created_in_the_workspace_locale(
    owner: OwnerApp,
) -> None:
    _set_auto_link(owner, False)
    clock = FixedClock(NOW)
    owner.app.state.clock = clock
    owner.app.state.session_factory.configure(info={"clock": clock})
    with owner.db() as session, session.begin():
        workspace = session.get(Workspace, owner.home_id)
        assert workspace is not None
        workspace.locale = "pl"
    stocks = _class_id(owner)
    instrument = _instrument(owner, "World ETF", stocks)
    _buy(owner, instrument)

    response = _create_links(owner)

    assert response.status_code == 303
    page = owner.client.get(response.headers["location"]).text
    assert "Inwestycje" in page
    whose = _category_id(owner, "Inwestycje")
    with owner.db() as session:
        item = session.scalars(
            select(NetWorthItem).where(NetWorthItem.workspace_id == owner.home_id)
        ).one()
    assert item.category_id == whose


def test_automatic_linking_hides_the_box_and_keeps_items_current(owner: OwnerApp) -> None:
    stocks = _class_id(owner)
    instrument = _instrument(owner, "World ETF", stocks)
    _buy(owner, instrument)

    with owner.db() as session:
        workspace = session.get(Workspace, owner.home_id)
        assert workspace is not None and workspace.auto_link_portfolio is True
        items = list(
            session.scalars(select(NetWorthItem).where(NetWorthItem.workspace_id == owner.home_id))
        )
    assert [(item.name, item.link_type, item.link_id) for item in items] == [
        ("Stocks", "portfolio_asset_class", stocks)
    ]
    page = owner.client.get(ASSETS).text
    assert "data-create-portfolio-links" not in page
    assert "Linked to Stocks" in page


def test_the_manual_box_needs_the_setting_off_and_an_eligible_class(owner: OwnerApp) -> None:
    _set_auto_link(owner, False)
    assert "data-create-portfolio-links" not in owner.client.get(ASSETS).text

    stocks = _class_id(owner)
    instrument = _instrument(owner, "World ETF", stocks)
    _buy(owner, instrument)

    assert "data-create-portfolio-links" in owner.client.get(ASSETS).text
    assert _create_links(owner).status_code == 303
    assert "data-create-portfolio-links" not in owner.client.get(ASSETS).text
