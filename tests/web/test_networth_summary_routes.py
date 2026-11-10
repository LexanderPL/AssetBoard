"""The net-worth summary page (F5.2): figure, bar, combined donut, tables and exclusions."""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID

from sqlalchemy import select

from assetboard.db.models.networth import NetWorthCategory, NetWorthItem
from tests.conftest import OwnerApp


def _category_id(owner: OwnerApp, side: str, name: str) -> UUID:
    with owner.db() as session:
        return session.scalars(
            select(NetWorthCategory.id).where(
                NetWorthCategory.workspace_id == owner.home_id,
                NetWorthCategory.side == side,
                NetWorthCategory.name == name,
            )
        ).one()


def _create_item(
    owner: OwnerApp,
    *,
    name: str,
    value: str,
    side: str = "asset",
    category: str = "Vehicles",
    currency: str = "EUR",
    include_in_net_worth: bool = True,
    as_of: str = "",
) -> UUID:
    response = owner.post(
        "/net-worth/items",
        {
            "side": side,
            "category_id": str(_category_id(owner, side, category)),
            "name": name,
            "value": value,
            "currency": currency,
            "include_in_net_worth": "1" if include_in_net_worth else "",
            "as_of": as_of,
        },
        token_from="/net-worth",
    )
    assert response.status_code == 303, response.text
    with owner.db() as session:
        return session.scalars(
            select(NetWorthItem.id).where(
                NetWorthItem.workspace_id == owner.home_id, NetWorthItem.name == name
            )
        ).one()


def _text(html: str, marker: str) -> str:
    match = re.search(rf"data-{marker}[^>]*>([^<]+)<", html)
    assert match is not None, marker
    return match.group(1)


def _note(html: str, tone: str) -> str:
    pattern = rf'<div class="[^"]*"[^>]*data-summary-note="{tone}".*?</div>'
    match = re.search(pattern, html, re.DOTALL)
    assert match is not None, tone
    return match.group(0)


def _donut_data(html: str, key: str) -> Any:
    match = re.search(rf'id="chart-{key}-data">(.*?)</script>', html, re.DOTALL)
    assert match is not None, key
    return json.loads(match.group(1))


def test_the_sidebar_and_tabs_link_to_the_summary(owner: OwnerApp) -> None:
    home = owner.client.get("/").text
    assert "data-net-worth-link" in home
    assert 'href="/net-worth/summary"' in home
    list_page = owner.client.get("/net-worth").text
    assert "data-summary-tab" in list_page
    page = owner.client.get("/net-worth/summary").text
    assert re.search(r'aria-selected="true"[^>]*data-summary-tab', page) is not None
    assert 'data-side-tab="asset"' in page and 'data-side-tab="liability"' in page


def test_an_empty_summary_shows_the_combined_donut_empty_state(owner: OwnerApp) -> None:
    page = owner.client.get("/net-worth/summary").text
    assert "data-networth-summary" in page
    assert _text(page, "net-worth") == "€0.00"
    assert page.count("data-donut=") == 1
    assert 'data-donut="combined"' in page
    assert "No assets or liabilities yet" in page
    assert "data-chart-source" not in page
    assert "echarts.min.js" not in page
    assert "data-summary-empty" in page
    assert "—" in page


def test_summary_shows_totals_bar_combined_donut_and_tables(owner: OwnerApp) -> None:
    _create_item(owner, name="Car", value="12000")
    _create_item(owner, name="Laptop", value="2000", category="Electronics")
    _create_item(owner, name="Mortgage", value="5000", side="liability", category="Mortgage")
    page = owner.client.get("/net-worth/summary").text
    assert _text(page, "net-worth") == "€9,000.00"
    assert _text(page, "assets-total") == "€14,000.00"
    assert _text(page, "liabilities-total") == "€5,000.00"
    assert 'data-bar-width="1000"' in page
    assert 'data-bar-width="357"' in page
    assert "data-summary-bar-text" in page and "€14,000.00" in page
    assert page.count("data-donut=") == 1
    assert 'data-donut="combined"' in page
    assert 'data-donut="assets"' not in page and 'data-donut="liabilities"' not in page
    assert _text(page, "chart-total") == "€19,000.00"
    data = _donut_data(page, "combined")
    assert data["label"] == "Assets and liabilities"
    assert [slice_["name"] for slice_ in data["slices"]] == ["Assets", "Liabilities"]
    assert [slice_["color"] for slice_ in data["slices"]] == ["#0277bd", "#c62828"]
    assert [slice_["share"] for slice_ in data["slices"]] == ["73.7%", "26.3%"]
    assert page.count("data-chart-legend-item") == 2
    assert page.count("data-chart-row") == 2
    assert ">Side</th>" in page
    assert "data-chart-row" in page and "Assets and liabilities" in page
    assert (
        "The chart shows assets and liabilities as parts of the gross total; "
        "net worth is assets minus liabilities." in page
    )
    assert 'data-summary-table="assets"' in page and 'data-summary-table="liabilities"' in page
    assert "data-summary-category" in page
    assert "85.7%" in page and "14.3%" in page
    assert "echarts.min.js" in page


def test_only_liabilities_has_negative_net_and_one_liability_slice(owner: OwnerApp) -> None:
    _create_item(owner, name="Mortgage", value="5000", side="liability", category="Mortgage")
    page = owner.client.get("/net-worth/summary").text
    assert _text(page, "net-worth") == "-€5,000.00"
    assert _text(page, "assets-total") == "€0.00"
    assert page.count("data-donut=") == 1
    assert 'data-donut="combined"' in page
    data = _donut_data(page, "combined")
    assert [slice_["name"] for slice_ in data["slices"]] == ["Liabilities"]
    assert [slice_["share"] for slice_ in data["slices"]] == ["100.0%"]
    assert 'data-summary-table="assets"' in page
    assert "data-empty-share" in page


def test_a_zero_value_asset_shows_a_dash_and_an_empty_combined_donut(owner: OwnerApp) -> None:
    _create_item(owner, name="Worthless", value="0")
    page = owner.client.get("/net-worth/summary").text
    assert _text(page, "assets-total") == "€0.00"
    assert page.count("data-donut=") == 1
    assert 'data-donut="combined"' in page
    assert "No assets or liabilities yet" in page
    assert "data-summary-category" not in page
    assert "data-summary-empty" in page
    assert "—" in page


def test_non_base_items_are_shown_as_a_warning_and_not_counted(owner: OwnerApp) -> None:
    _create_item(owner, name="Car", value="100")
    _create_item(owner, name="Boat", value="500", currency="USD")
    page = owner.client.get("/net-worth/summary").text
    assert _text(page, "assets-total") == "€100.00"
    note = _note(page, "non_base")
    assert "Boat" in note and "$500.00" in note
    assert "data-summary-category" in page


def test_excluded_items_use_a_neutral_note(owner: OwnerApp) -> None:
    _create_item(owner, name="Car", value="100")
    _create_item(owner, name="Private", value="900", include_in_net_worth=False)
    page = owner.client.get("/net-worth/summary").text
    note = _note(page, "excluded")
    assert "Private" in note
    assert "bg-warning-soft" not in note
    assert _text(page, "assets-total") == "€100.00"


def test_items_without_a_value_use_a_warning(owner: OwnerApp) -> None:
    _create_item(owner, name="Future", value="900", as_of="2199-01-01")
    page = owner.client.get("/net-worth/summary").text
    note = _note(page, "no_value")
    assert "Future" in note
    assert "bg-warning-soft" in note
    assert _text(page, "assets-total") == "€0.00"


def test_summary_markup_is_csp_safe_and_escaped(owner: OwnerApp) -> None:
    _create_item(owner, name="<b>Car</b> & boat", value="100", currency="USD")
    page = owner.client.get("/net-worth/summary").text
    assert "<b>Car</b>" not in page
    assert "&lt;b&gt;Car&lt;/b&gt; &amp; boat" in page
    assert " style=" not in page
    assert re.search(r"<script(?![^>]*\ssrc=)(?![^>]*type=\"application/json\")", page) is None
    assert "x-data" not in page and "@click" not in page


def test_the_summary_is_translated(owner: OwnerApp) -> None:
    _create_item(owner, name="Car", value="100")
    owner.client.headers["Accept-Language"] = "pl"
    page = owner.client.get("/net-worth/summary").text
    assert "Majątek netto" in page
    assert "Podsumowanie" in page
    assert "Aktywa" in page and "Zobowiązania" in page
    assert "Udział" in page
    assert "Aktywa według kategorii" in page
    assert "Aktywa i zobowiązania" in page


def test_viewers_can_read_the_summary(owner: OwnerApp) -> None:
    _create_item(owner, name="Car", value="100")
    owner.set_admin_role("viewer")
    response = owner.client.get("/net-worth/summary")
    assert response.status_code == 200
    assert "Vehicles" in response.text
