"""The net-worth summary page (F5.2): figure, one assets-vs-liabilities bar and tables."""

from __future__ import annotations

import re
from uuid import UUID

from sqlalchemy import event, select

from assetboard.clock import FixedClock
from assetboard.db.models.networth import ItemValuation, NetWorthCategory, NetWorthItem
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
    estimate_between: bool = True,
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
            "estimate_between": "1" if estimate_between else "",
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


def _add_value(owner: OwnerApp, item_id: UUID, *, value: str, as_of: str) -> None:
    response = owner.post(
        f"/net-worth/items/{item_id}/valuations",
        {"value": value, "as_of": as_of, "note": ""},
        token_from="/net-worth",
    )
    assert response.status_code == 303, response.text


def _text(html: str, marker: str) -> str:
    match = re.search(rf"data-{marker}[^>]*>([^<]+)<", html)
    assert match is not None, marker
    return match.group(1)


def _attr(html: str, marker: str) -> str:
    match = re.search(rf'data-{marker}="([^"]*)"', html)
    assert match is not None, marker
    return match.group(1)


def _note(html: str, tone: str) -> str:
    pattern = rf'<div class="[^"]*"[^>]*data-summary-note="{tone}".*?</div>'
    match = re.search(pattern, html, re.DOTALL)
    assert match is not None, tone
    return match.group(0)


def test_the_sidebar_and_tabs_link_to_the_summary(owner: OwnerApp) -> None:
    home = owner.client.get("/").text
    assert "data-net-worth-link" in home
    assert 'href="/net-worth/summary"' in home
    list_page = owner.client.get("/net-worth").text
    assert "data-summary-tab" in list_page
    page = owner.client.get("/net-worth/summary").text
    assert re.search(r'data-summary-tab aria-current="page"', page) is not None
    assert re.search(r'<nav class="tab-strip" aria-label="Net worth"', page)
    assert 'data-side-tab="asset"' in page and 'data-side-tab="liability"' in page
    assert 'role="tablist"' not in page and "aria-selected" not in page


def test_an_empty_summary_shows_the_empty_bar_state(owner: OwnerApp) -> None:
    page = owner.client.get("/net-worth/summary").text
    assert "data-networth-summary" in page
    assert _text(page, "net-worth") == "€0.00"
    assert "data-donut=" not in page
    assert "data-chart-source" not in page
    assert "echarts.min.js" not in page
    assert "data-summary-bar" in page
    assert 'data-bar-empty="true"' in page
    assert "data-bar-track" in page
    assert _text(page, "bar-caption") == "No assets or liabilities yet."
    assert "data-summary-empty" in page
    assert "—" in page


def test_summary_shows_totals_one_bar_and_tables(owner: OwnerApp) -> None:
    _create_item(owner, name="Car", value="12000")
    _create_item(owner, name="Laptop", value="2000", category="Electronics")
    _create_item(owner, name="Mortgage", value="5000", side="liability", category="Mortgage")
    page = owner.client.get("/net-worth/summary").text
    assert _text(page, "net-worth") == "€9,000.00"
    assert _text(page, "assets-total") == "€14,000.00"
    assert _text(page, "liabilities-total") == "€5,000.00"
    assert "data-summary-bar" in page
    assert 'data-bar-empty="false"' in page
    assert _attr(page, "bar-assets-width") == "1000"
    assert _attr(page, "bar-liabilities-width") == "357"
    assert _attr(page, "bar-liabilities-start") == "643"
    assert "data-bar-deficit" not in page
    assert _text(page, "bar-caption") == (
        "Liabilities are 35.7% of assets; the green part is net worth."
    )
    assert "data-summary-bar-text" in page and "€14,000.00" in page
    assert "data-donut=" not in page
    assert 'data-chart-source="networth-trend-data"' in page
    assert "echarts.min.js" in page
    assert 'data-summary-table="assets"' in page and 'data-summary-table="liabilities"' in page
    assert "data-summary-category" in page
    assert "85.7%" in page and "14.3%" in page


def test_liabilities_above_assets_add_a_hatched_deficit_and_marker(owner: OwnerApp) -> None:
    _create_item(owner, name="Car", value="5000")
    _create_item(owner, name="Loan", value="8000", side="liability", category="Mortgage")
    page = owner.client.get("/net-worth/summary").text
    assert _text(page, "net-worth") == "-€3,000.00"
    assert _attr(page, "bar-assets-width") == "625"
    assert _attr(page, "bar-liabilities-width") == "1000"
    assert _attr(page, "bar-deficit-width") == "375"
    assert re.search(r'x1="625"[^>]*data-bar-assets-end', page) is not None
    assert _text(page, "bar-caption") == (
        "Liabilities are 160.0% of assets; they exceed assets by €3,000.00."
    )


def test_equal_assets_and_liabilities_show_a_full_red_bar(owner: OwnerApp) -> None:
    _create_item(owner, name="Car", value="5000")
    _create_item(owner, name="Loan", value="5000", side="liability", category="Mortgage")
    page = owner.client.get("/net-worth/summary").text
    assert _text(page, "net-worth") == "€0.00"
    assert _attr(page, "bar-assets-width") == "1000"
    assert _attr(page, "bar-liabilities-width") == "1000"
    assert "data-bar-deficit" not in page
    assert _text(page, "bar-caption") == "Liabilities equal assets; net worth is zero."


def test_only_liabilities_show_a_fully_hatched_bar(owner: OwnerApp) -> None:
    _create_item(owner, name="Mortgage", value="5000", side="liability", category="Mortgage")
    page = owner.client.get("/net-worth/summary").text
    assert _text(page, "net-worth") == "-€5,000.00"
    assert _text(page, "assets-total") == "€0.00"
    assert _attr(page, "bar-assets-width") == "0"
    assert _attr(page, "bar-liabilities-width") == "1000"
    assert _attr(page, "bar-deficit-width") == "1000"
    assert _text(page, "bar-caption") == "Liabilities exceed assets by €5,000.00."
    assert 'data-summary-table="assets"' in page
    assert "data-empty-share" in page


def test_a_zero_value_asset_shows_a_dash_and_the_empty_bar(owner: OwnerApp) -> None:
    _create_item(owner, name="Worthless", value="0")
    page = owner.client.get("/net-worth/summary").text
    assert _text(page, "assets-total") == "€0.00"
    assert 'data-bar-empty="true"' in page
    assert _text(page, "bar-caption") == "No assets or liabilities yet."
    assert "data-summary-category" not in page
    assert "data-summary-empty" in page
    assert "—" in page


def test_no_liabilities_show_a_full_green_bar(owner: OwnerApp) -> None:
    _create_item(owner, name="Car", value="700")
    page = owner.client.get("/net-worth/summary").text
    assert _attr(page, "bar-assets-width") == "1000"
    assert "data-bar-liabilities" not in page
    assert "data-bar-deficit" not in page
    assert _text(page, "bar-caption") == "No liabilities; the whole bar is net worth."


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
    assert 'data-chart="line"' in page and 'data-chart-source="networth-trend-data"' in page
    assert page.count('type="application/json"') == 1
    assert "data-summary-bar" in page
    assert "x-data" not in page and "@click" not in page


def test_the_summary_is_translated(owner: OwnerApp) -> None:
    _create_item(owner, name="Car", value="12000")
    _create_item(owner, name="Loan", value="5000", side="liability", category="Mortgage")
    owner.client.headers["Accept-Language"] = "pl"
    page = owner.client.get("/net-worth/summary").text
    assert "Majątek netto" in page
    assert "Podsumowanie" in page
    assert "Aktywa" in page and "Zobowiązania" in page
    assert "Udział" in page
    assert "Aktywa według kategorii" in page
    assert "Zobowiązania według kategorii" in page
    assert "Zobowiązania stanowią 41,7% aktywów; zielona część to majątek netto." in page


def test_viewers_can_read_the_summary(owner: OwnerApp) -> None:
    _create_item(owner, name="Car", value="100")
    owner.set_admin_role("viewer")
    response = owner.client.get("/net-worth/summary")
    assert response.status_code == 200
    assert "Vehicles" in response.text


def _chart(page: str) -> dict:
    import json

    match = re.search(
        r'<script type="application/json" id="networth-trend-data">(.*?)</script>',
        page,
        re.DOTALL,
    )
    assert match is not None
    return json.loads(match.group(1))


def _queries(owner: OwnerApp, path: str) -> int:
    seen = 0

    def before(*_args: object) -> None:
        nonlocal seen
        seen += 1

    engine = owner.app.state.database_engine
    event.listen(engine, "before_cursor_execute", before)
    try:
        response = owner.client.get(path)
        assert response.status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", before)
    return seen


def test_summary_shows_the_derived_trend_without_any_snapshots(owner: OwnerApp) -> None:
    owner.app.state.clock = FixedClock("2026-11-16T12:00:00+00:00")
    _create_item(owner, name="Car", value="120", as_of="2025-01-01")

    page = owner.client.get("/net-worth/summary").text
    assert "data-networth-trend" in page
    assert 'data-chart="line"' in page and "echarts.min.js" in page
    assert "data-trend-row" in page
    # The trend sits directly after the figure/bar panel and before the warnings and tables.
    assert page.index("data-summary-figure") < page.index('id="net-worth-trend"')
    assert page.index('id="net-worth-trend"') < page.index('data-summary-table="assets"')
    chart = _chart(page)
    assert chart["series"][0]["values"] == [12000] * 13
    assert chart["labels"][0] == "November 2025"
    assert chart["labels"][-1] == "Nov 16, 2026"


def test_trend_has_one_point_per_month_end_and_today(owner: OwnerApp) -> None:
    owner.app.state.clock = FixedClock("2026-11-16T12:00:00+00:00")
    _create_item(owner, name="Car", value="100", as_of="2024-01-15")
    page = owner.client.get("/net-worth/summary").text
    assert page.count("data-trend-row") == 13
    assert page.count("data-trend-today") == 2  # the row marker and its label
    assert "November 2025" in page
    assert "Nov 16, 2026" in page


def test_range_selector_changes_the_points(owner: OwnerApp) -> None:
    owner.app.state.clock = FixedClock("2026-11-16T12:00:00+00:00")
    _create_item(owner, name="Car", value="100", as_of="2020-01-01")

    one_year = owner.client.get("/net-worth/summary").text
    three_years = owner.client.get("/net-worth/summary?range=3y").text
    everything = owner.client.get("/net-worth/summary?range=all").text
    assert one_year.count("data-trend-row") == 13
    assert three_years.count("data-trend-row") == 37
    assert everything.count("data-trend-row") == 83
    assert re.search(r'<span[^>]*aria-current="true"[^>]*data-range="3y"', three_years) is not None
    assert 'href="/net-worth/summary?range=5y#net-worth-trend"' in one_year
    assert "All time" in one_year


def test_unknown_range_falls_back_to_one_year(owner: OwnerApp) -> None:
    owner.app.state.clock = FixedClock("2026-11-16T12:00:00+00:00")
    _create_item(owner, name="Car", value="100", as_of="2024-01-15")
    page = owner.client.get("/net-worth/summary?range=10y").text
    assert page.count("data-trend-row") == 13
    assert re.search(r'<span[^>]*aria-current="true"[^>]*data-range="1y"', page) is not None


def test_fixed_range_clips_leading_months(owner: OwnerApp) -> None:
    owner.app.state.clock = FixedClock("2026-11-16T12:00:00+00:00")
    _create_item(owner, name="Car", value="100", as_of="2026-09-15")
    page = owner.client.get("/net-worth/summary?range=1y").text
    assert page.count("data-trend-row") == 3
    assert "September 2026" in page and "October 2026" in page
    assert "December 2025" not in page
    assert _chart(page)["labels"] == ["September 2026", "October 2026", "Nov 16, 2026"]


def test_trend_tooltip_has_the_category_breakdown_and_estimated_note(owner: OwnerApp) -> None:
    owner.app.state.clock = FixedClock("2026-11-16T12:00:00+00:00")
    item_id = _create_item(owner, name="Car", value="100", as_of="2024-01-01")
    _add_value(owner, item_id, value="200", as_of="2026-11-15")

    page = owner.client.get("/net-worth/summary?range=1y").text
    chart = _chart(page)
    assert any("Vehicles" in line and "€" in line for line in chart["tooltips"][0])
    assert any("includes 1 estimated value" in line for line in chart["tooltips"][0])
    assert not any("estimated" in line for line in chart["tooltips"][-1])
    assert "includes 1 estimated value" in page
    assert "data-estimated" in page


def test_page_reflects_an_edited_value(owner: OwnerApp) -> None:
    owner.app.state.clock = FixedClock("2026-11-16T12:00:00+00:00")
    item_id = _create_item(owner, name="Car", value="100", as_of="2026-09-15")
    with owner.db() as session:
        valuation_id = session.scalars(
            select(ItemValuation.id).where(ItemValuation.item_id == item_id)
        ).one()
    before = owner.client.get("/net-worth/summary").text
    assert _chart(before)["series"][0]["values"] == [10000] * 3

    response = owner.post(
        f"/net-worth/items/{item_id}/valuations/{valuation_id}",
        {"value": "250", "as_of": "2026-09-15", "note": "corrected"},
        token_from="/net-worth",
    )
    assert response.status_code == 303
    after = owner.client.get("/net-worth/summary").text
    assert _chart(after)["series"][0]["values"] == [25000] * 3
    assert "€250.00" in after


def test_today_is_not_estimated_and_is_marked(owner: OwnerApp) -> None:
    owner.app.state.clock = FixedClock("2026-11-16T12:00:00+00:00")
    item_id = _create_item(owner, name="Car", value="100", as_of="2024-01-01")
    _add_value(owner, item_id, value="200", as_of="2026-11-15")
    page = owner.client.get("/net-worth/summary?range=1y").text
    today = re.search(r"<tr[^>]*data-trend-today[^>]*>.*?</tr>", page, re.DOTALL)
    assert today is not None
    assert "data-trend-today-label" in today.group(0)
    assert ">Today<" in today.group(0)
    assert "data-estimated" not in today.group(0)
    assert _chart(page)["tooltips"][-1][-1] != "includes 1 estimated value"


def test_monthly_values_table_is_collapsed(owner: OwnerApp) -> None:
    owner.app.state.clock = FixedClock("2026-11-16T12:00:00+00:00")
    _create_item(owner, name="Car", value="100", as_of="2026-09-15")
    page = owner.client.get("/net-worth/summary").text
    table = re.search(r"<details[^>]*data-history-table[^>]*>", page)
    assert table is not None and "open" not in table.group(0)
    assert "<summary" in page and "Monthly values" in page
    assert "data-trend-row" in page
    assert re.search(r'<table[^>]*class="sheet w-full text-sm"', page) is not None
    assert '<caption class="sr-only">Monthly values</caption>' in page
    assert 'scope="col"' in page and 'scope="row"' in page


def test_no_snapshot_ui_or_history_tab_remains(owner: OwnerApp) -> None:
    owner.app.state.clock = FixedClock("2026-11-16T12:00:00+00:00")
    _create_item(owner, name="Car", value="100", as_of="2025-01-01")
    for path in ("/net-worth/summary", "/net-worth"):
        page = owner.client.get(path).text
        for label in (
            "Take snapshot now",
            "Backfill from history",
            "Automatic snapshots",
            "Net-worth snapshots",
            "Snapshot saved",
            "data-source-badge",
            "data-snapshot-source",
            "data-snapshot-currency",
            "data-take-snapshot",
            "data-backfill",
        ):
            assert label not in page, (path, label)
        assert re.search(r"data-history-tab(?![a-z-])", page) is None


def test_empty_workspace_shows_the_empty_state_instead_of_a_chart(owner: OwnerApp) -> None:
    page = owner.client.get("/net-worth/summary").text
    assert "data-trend-empty" in page
    assert "No values yet. Add an item and its first value to see the trend." in page
    assert "data-chart-source" not in page
    assert "echarts.min.js" not in page
    assert "data-history-table" not in page
    assert page.count('type="application/json"') == 0


def test_no_value_workspace_shows_the_empty_state_and_warning(owner: OwnerApp) -> None:
    _create_item(owner, name="Future", value="900", as_of="2199-01-01")
    page = owner.client.get("/net-worth/summary").text
    assert "data-trend-empty" in page
    assert "data-trend-row" not in page
    note = _note(page, "no_value")
    assert "Future" in note


def test_zero_recorded_value_shows_a_zero_chart(owner: OwnerApp) -> None:
    owner.app.state.clock = FixedClock("2026-11-16T12:00:00+00:00")
    _create_item(owner, name="Worthless", value="0", as_of="2025-01-01")
    page = owner.client.get("/net-worth/summary").text
    assert "data-trend-empty" not in page
    assert "data-trend-row" in page
    assert _chart(page)["series"][0]["values"] == [0] * 13


def test_non_base_items_remain_excluded_with_the_summary_warning(owner: OwnerApp) -> None:
    owner.app.state.clock = FixedClock("2026-11-16T12:00:00+00:00")
    _create_item(owner, name="Car", value="100", as_of="2025-01-01")
    _create_item(owner, name="Boat", value="500", currency="USD", as_of="2025-01-01")
    page = owner.client.get("/net-worth/summary").text
    assert "Boat" in _note(page, "non_base")
    assert _chart(page)["series"][0]["values"] == [10000] * 13
    assert _chart(page)["tooltips"][0][1].startswith("1 item in another currency")


def test_summary_trend_markup_is_csp_safe(owner: OwnerApp) -> None:
    owner.app.state.clock = FixedClock("2026-11-16T12:00:00+00:00")
    _create_item(owner, name="Car", value="100", as_of="2025-01-01")
    page = owner.client.get("/net-worth/summary").text
    assert " style=" not in page
    assert re.search(r"<script(?![^>]*\ssrc=)(?![^>]*type=\"application/json\")", page) is None
    assert page.count('type="application/json"') == 1
    assert "x-data" not in page and "@click" not in page


def test_summary_trend_is_mobile_friendly(owner: OwnerApp) -> None:
    owner.app.state.clock = FixedClock("2026-11-16T12:00:00+00:00")
    _create_item(owner, name="Car", value="100", as_of="2025-01-01")
    page = owner.client.get("/net-worth/summary").text
    assert "data-trend-range" in page
    assert 'class="mt-4 flex flex-wrap items-center gap-1"' in page
    assert "overflow-x-auto" in page
    assert "h-72 w-full sm:h-80" in page
    assert "min-w-0" in page


def test_summary_trend_is_translated(owner: OwnerApp) -> None:
    owner.app.state.clock = FixedClock("2026-11-16T12:00:00+00:00")
    item_id = _create_item(owner, name="Car", value="100", as_of="2024-01-01")
    _add_value(owner, item_id, value="200", as_of="2026-11-15")
    owner.client.headers["Accept-Language"] = "pl"
    page = owner.client.get("/net-worth/summary").text
    assert "Wartość netto w czasie" in page
    assert "Zakres" in page
    assert "Cały okres" in page
    assert "Wartości miesięczne" in page
    assert "Dzisiaj" in page
    assert "zawiera 1 szacowaną wartość" in page


def test_viewers_can_read_the_summary_and_trend(owner: OwnerApp) -> None:
    owner.app.state.clock = FixedClock("2026-11-16T12:00:00+00:00")
    _create_item(owner, name="Car", value="100", as_of="2025-01-01")
    owner.set_admin_role("viewer")
    response = owner.client.get("/net-worth/summary")
    assert response.status_code == 200
    assert "data-networth-trend" in response.text
    assert "data-trend-row" in response.text


def test_trend_query_count_is_bounded(owner: OwnerApp) -> None:
    owner.app.state.clock = FixedClock("2026-11-16T12:00:00+00:00")
    _create_item(owner, name="Car", value="100", as_of="2014-01-01")
    one_year = _queries(owner, "/net-worth/summary?range=1y")
    all_time = _queries(owner, "/net-worth/summary?range=all")
    assert all_time == one_year
    assert all_time <= 20
