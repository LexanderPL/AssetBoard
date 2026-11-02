"""The linked-item UI (F5.4): picker, chip, read-only display, unlink and unavailable path."""

from __future__ import annotations

import re
from datetime import date
from uuid import UUID, uuid4

from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.db.models.networth import NetWorthCategory, NetWorthItem
from assetboard.domain.networth import Side
from tests.conftest import OwnerApp
from tests.fake_links import FakeLinks, FakeTarget

LINK_TYPE = "portfolio_asset_class"
TODAY = date(2026, 11, 2)


def _category_id(owner: OwnerApp, side: str = "asset", name: str = "Vehicles") -> UUID:
    with owner.db() as session:
        return session.scalars(
            select(NetWorthCategory.id).where(
                NetWorthCategory.workspace_id == owner.home_id,
                NetWorthCategory.side == side,
                NetWorthCategory.name == name,
            )
        ).one()


def _item(owner: OwnerApp, name: str = "Holding") -> NetWorthItem:
    with owner.db() as session:
        return session.scalars(
            select(NetWorthItem).where(
                NetWorthItem.workspace_id == owner.home_id,
                NetWorthItem.name == name,
            )
        ).one()


def _target(
    owner: OwnerApp,
    *,
    label: str = "Holding",
    values: dict[date, int] | None = None,
) -> FakeTarget:
    return FakeTarget(
        workspace_id=owner.home_id,
        link_id=uuid4(),
        label=label,
        currency="EUR",
        href="/portfolio/holdings/1",
        values=values or {},
    )


def _post_linked(
    owner: OwnerApp,
    target: FakeTarget,
    *,
    name: str = "Holding",
    side: str = "asset",
    category: str = "Vehicles",
):
    return owner.post(
        "/net-worth/items",
        {
            "side": side,
            "category_id": str(_category_id(owner, side, category)),
            "name": name,
            "value": "",
            "currency": "EUR",
            "notes": "",
            "acquired_on": "",
            "include_in_net_worth": "1",
            "investable": "",
            "as_of": "",
            "valuation_note": "",
            "link": f"{LINK_TYPE}:{target.link_id}",
        },
        token_from="/net-worth",
    )


def test_no_link_picker_without_a_registered_resolver(owner: OwnerApp) -> None:
    page = owner.client.get("/net-worth").text
    assert "Link to…" not in page
    assert "data-linked-readonly" not in page


def test_link_picker_is_only_on_the_resolvers_side(owner: OwnerApp, fake_links: FakeLinks) -> None:
    target = _target(owner)
    fake_links.register(side=Side.ASSET, targets=[target])
    asset = owner.client.get("/net-worth?side=asset").text
    liability = owner.client.get("/net-worth?side=liability").text
    assert "Link to…" in asset
    assert "Fake source" in asset
    assert f'value="{LINK_TYPE}:{target.link_id}"' in asset
    assert "Link to…" not in liability


def test_create_linked_item_shows_chip_read_only_value_and_link(
    owner: OwnerApp, fake_links: FakeLinks
) -> None:
    owner.app.state.clock = FixedClock("2026-11-02T12:00:00+00:00")
    target = _target(owner, values={TODAY: 125000})
    fake_links.register(targets=[target])
    response = _post_linked(owner, target)
    assert response.status_code == 303, response.text
    item = _item(owner)
    assert (item.valuation_mode, item.link_type, item.link_id) == (
        "linked",
        LINK_TYPE,
        target.link_id,
    )
    page = owner.client.get("/net-worth").text
    assert "data-linked" in page
    assert "Linked to Holding" in page
    assert 'href="/portfolio/holdings/1"' in page
    assert "€1,250.00" in page
    assert "data-add-valuation" not in page
    assert "data-linked-readonly" in page
    assert "data-unlink" in page


def test_unlink_through_the_ui_keeps_the_value(owner: OwnerApp, fake_links: FakeLinks) -> None:
    owner.app.state.clock = FixedClock("2026-11-02T12:00:00+00:00")
    target = _target(owner, values={TODAY: 125000})
    fake_links.register(targets=[target])
    _post_linked(owner, target)
    response = owner.post(
        f"/net-worth/items/{_item(owner).id}/unlink",
        {"side": "asset"},
        token_from="/net-worth",
    )
    assert response.status_code == 303
    assert "notice=item_unlinked" in response.headers["location"]
    item = _item(owner)
    assert item.valuation_mode == "manual"
    assert item.link_type is None and item.link_id is None
    page = owner.client.get("/net-worth").text
    assert "data-linked" not in page
    assert "€1,250.00" in page
    assert "data-add-valuation" in page


def test_unavailable_link_is_visible_in_the_list_summary_and_history(
    owner: OwnerApp, fake_links: FakeLinks
) -> None:
    owner.app.state.clock = FixedClock("2026-11-02T12:00:00+00:00")
    target = _target(owner, values={})
    fake_links.register(targets=[target])
    _post_linked(owner, target)
    page = owner.client.get("/net-worth").text
    assert "data-unavailable" in page
    assert "Value unavailable" in page
    summary = owner.client.get("/net-worth/summary").text
    assert 'data-summary-note="unavailable"' in summary
    assert "linked item is unavailable" in summary
    owner.post("/net-worth/history/snapshot", token_from="/net-worth/history")
    history = owner.client.get("/net-worth/history").text
    assert "linked item was unavailable and was not counted" in history


def test_unknown_link_type_on_read_is_unavailable_and_unlinkable(
    owner: OwnerApp, fake_links: FakeLinks
) -> None:
    owner.app.state.clock = FixedClock("2026-11-02T12:00:00+00:00")
    target = _target(owner, values={TODAY: 100})
    fake_links.register(targets=[target])
    _post_linked(owner, target)
    fake_links.reset()
    page = owner.client.get("/net-worth").text
    assert "Linked to an unavailable source" in page
    assert "data-unavailable" in page
    assert 'href="/portfolio/holdings/1"' not in page
    response = owner.post(
        f"/net-worth/items/{_item(owner).id}/unlink",
        {"side": "asset"},
        token_from="/net-worth",
    )
    assert response.status_code == 303
    assert "notice=item_unlinked_no_value" in response.headers["location"]
    assert _item(owner).valuation_mode == "manual"


def test_viewers_see_no_link_controls_and_cannot_unlink(
    owner: OwnerApp, fake_links: FakeLinks
) -> None:
    owner.app.state.clock = FixedClock("2026-11-02T12:00:00+00:00")
    target = _target(owner, values={TODAY: 100})
    fake_links.register(targets=[target])
    _post_linked(owner, target)
    item_id = _item(owner).id
    owner.set_admin_role("viewer")
    page = owner.client.get("/net-worth").text
    assert "Link to…" not in page
    assert "data-unlink" not in page
    response = owner.post(
        f"/net-worth/items/{item_id}/unlink",
        {"side": "asset"},
        token_from="/net-worth",
    )
    assert response.status_code == 403


def test_linked_ui_is_translated(owner: OwnerApp, fake_links: FakeLinks) -> None:
    owner.app.state.clock = FixedClock("2026-11-02T12:00:00+00:00")
    target = _target(owner, values={TODAY: 100})
    fake_links.register(targets=[target])
    _post_linked(owner, target)
    owner.client.headers["Accept-Language"] = "pl"
    page = owner.client.get("/net-worth").text
    assert "Powiąż z…" in page
    assert "Wartość ręczna" in page
    assert "Powiązana z Holding" in page
    assert "Odłącz" in page


def test_linked_markup_is_csp_safe(owner: OwnerApp, fake_links: FakeLinks) -> None:
    owner.app.state.clock = FixedClock("2026-11-02T12:00:00+00:00")
    target = _target(owner, values={TODAY: 100})
    fake_links.register(targets=[target])
    _post_linked(owner, target)
    page = owner.client.get("/net-worth").text
    assert " style=" not in page
    assert re.search(r"<script(?![^>]*\ssrc=)", page) is None
