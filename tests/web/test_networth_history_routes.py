"""The net-worth history page (F5.3): snapshots, chart, table, ranges, roles and currencies."""

from __future__ import annotations

import re
from datetime import date
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.db.models.networth import NetWorthCategory, NetWorthItem, NetWorthSnapshot
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
    category: str = "Vehicles",
    side: str = "asset",
    currency: str = "EUR",
    as_of: str = "2026-01-01",
) -> UUID:
    response = owner.post(
        "/net-worth/items",
        {
            "side": side,
            "category_id": str(_category_id(owner, side, category)),
            "name": name,
            "value": value,
            "currency": currency,
            "include_in_net_worth": "1",
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


def _breakdown(*rows: tuple[UUID, str, int], estimated: int = 0) -> dict[str, Any]:
    return {
        "v": 1,
        "categories": [
            {"category_id": str(category_id), "side": side, "total_minor": total}
            for category_id, side, total in rows
        ],
        "not_counted": {"non_base": 0, "no_value": 0},
        "estimated": estimated,
    }


def _snapshot(
    owner: OwnerApp,
    *,
    as_of: date,
    source: str = "manual",
    currency: str = "EUR",
    assets_minor: int = 10000,
    liabilities_minor: int = 0,
    breakdown: dict[str, Any] | None = None,
) -> UUID:
    with owner.db() as session, session.begin():
        row = NetWorthSnapshot(
            workspace_id=owner.home_id,
            as_of=as_of,
            assets_minor=assets_minor,
            liabilities_minor=liabilities_minor,
            net_minor=assets_minor - liabilities_minor,
            currency=currency,
            breakdown=breakdown or _breakdown(),
            source=source,
        )
        session.add(row)
        session.flush()
        return row.id


def _rows(owner: OwnerApp, source: str | None = None) -> list[NetWorthSnapshot]:
    with owner.db() as session:
        statement = select(NetWorthSnapshot).where(NetWorthSnapshot.workspace_id == owner.home_id)
        if source is not None:
            statement = statement.where(NetWorthSnapshot.source == source)
        return list(session.scalars(statement.order_by(NetWorthSnapshot.as_of)))


def test_the_history_tab_is_on_every_net_worth_page(owner: OwnerApp) -> None:
    assert "data-history-tab" in owner.client.get("/net-worth").text
    assert "data-history-tab" in owner.client.get("/net-worth/summary").text
    page = owner.client.get("/net-worth/history").text
    assert re.search(r'aria-selected="true"[^>]*data-history-tab', page) is not None


def test_an_empty_history_page_has_editor_actions_and_no_chart_script(
    owner: OwnerApp,
) -> None:
    page = owner.client.get("/net-worth/history").text
    assert "data-history-empty" in page
    assert "data-take-snapshot" in page and "data-backfill" in page
    assert 'data-chart="line"' not in page
    assert "echarts.min.js" not in page
    assert "No snapshots yet" in page


def test_take_snapshot_now_stores_today_in_the_workspace_timezone(
    owner: OwnerApp,
) -> None:
    owner.app.state.clock = FixedClock("2026-03-15T12:00:00+00:00")
    _create_item(owner, name="Car", value="120", as_of="2026-01-01")
    response = owner.post("/net-worth/history/snapshot", token_from="/net-worth/history")
    assert response.status_code == 303
    assert "notice=snapshot_taken" in response.headers["location"]
    rows = _rows(owner)
    assert len(rows) == 1
    assert rows[0].as_of == date(2026, 3, 15)
    assert (rows[0].assets_minor, rows[0].source) == (12000, "manual")


def test_taking_a_second_snapshot_replaces_the_manual_row(owner: OwnerApp) -> None:
    owner.app.state.clock = FixedClock("2026-03-15T12:00:00+00:00")
    _create_item(owner, name="Car", value="100", as_of="2026-01-01")
    owner.post("/net-worth/history/snapshot", token_from="/net-worth/history")
    _create_item(owner, name="Laptop", value="50", as_of="2026-01-01")
    response = owner.post("/net-worth/history/snapshot", token_from="/net-worth/history")
    assert "notice=snapshot_replaced" in response.headers["location"]
    rows = _rows(owner)
    assert len(rows) == 1 and rows[0].assets_minor == 15000


def test_backfill_creates_the_expected_month_ends(owner: OwnerApp) -> None:
    owner.app.state.clock = FixedClock("2026-03-15T12:00:00+00:00")
    _create_item(owner, name="Car", value="100", as_of="2026-01-01")
    response = owner.post("/net-worth/history/backfill", token_from="/net-worth/history")
    assert "notice=backfill_done" in response.headers["location"]
    rows = _rows(owner)
    assert [(row.as_of, row.source) for row in rows] == [
        (date(2026, 1, 31), "backfill"),
        (date(2026, 2, 28), "backfill"),
    ]


def test_backfill_without_values_reports_an_empty_result(owner: OwnerApp) -> None:
    owner.app.state.clock = FixedClock("2026-03-15T12:00:00+00:00")
    response = owner.post("/net-worth/history/backfill", token_from="/net-worth/history")
    assert "notice=backfill_empty" in response.headers["location"]
    assert _rows(owner) == []


def test_viewers_see_no_actions_and_cannot_post(owner: OwnerApp) -> None:
    _snapshot(owner, as_of=date(2026, 10, 31))
    owner.set_admin_role("viewer")
    page = owner.client.get("/net-worth/history").text
    assert "data-take-snapshot" not in page and "data-backfill" not in page
    response = owner.post("/net-worth/history/snapshot", token_from="/net-worth/history")
    assert response.status_code == 403


def test_csrf_is_required_for_snapshot_actions(owner: OwnerApp) -> None:
    response = owner.client.post("/net-worth/history/snapshot", data={})
    assert response.status_code == 403


def test_history_shows_the_chart_table_sources_and_breakdown(owner: OwnerApp) -> None:
    owner.app.state.clock = FixedClock("2026-11-02T12:00:00+00:00")
    vehicles = _category_id(owner, "asset", "Vehicles")
    mortgage = _category_id(owner, "liability", "Mortgage")
    _snapshot(
        owner,
        as_of=date(2026, 9, 30),
        source="manual",
        assets_minor=1200000,
        liabilities_minor=500000,
        breakdown=_breakdown((vehicles, "asset", 1200000), (mortgage, "liability", 500000)),
    )
    _snapshot(
        owner,
        as_of=date(2026, 10, 31),
        source="backfill",
        assets_minor=1300000,
        liabilities_minor=400000,
        breakdown=_breakdown((vehicles, "asset", 1300000), (mortgage, "liability", 400000)),
    )
    page = owner.client.get("/net-worth/history").text
    assert 'data-chart="line"' in page and "echarts.min.js" in page
    assert '"y_min"' in page and '"values": [700000, 900000]' in page
    assert "data-history-table" in page
    assert page.count("data-history-row") == 2
    assert 'data-source-badge="manual"' in page
    assert 'data-source-badge="backfill"' in page
    assert "From history" in page
    assert "Vehicles" in page and "Mortgage" in page
    assert "€12,000.00" in page and "€5,000.00" in page
    assert "data-history-breakdown" in page


def test_the_range_selector_filters_by_date(owner: OwnerApp) -> None:
    owner.app.state.clock = FixedClock("2026-11-02T12:00:00+00:00")
    _snapshot(owner, as_of=date(2024, 1, 31), assets_minor=100)
    _snapshot(owner, as_of=date(2026, 10, 31), assets_minor=200)
    default = owner.client.get("/net-worth/history").text
    assert default.count("data-history-row") == 1
    assert "October 2026" in default and "January 2024" not in default
    everything = owner.client.get("/net-worth/history?range=all").text
    assert everything.count("data-history-row") == 2
    assert "January 2024" in everything


def test_two_snapshots_in_one_month_have_distinct_labels(owner: OwnerApp) -> None:
    owner.app.state.clock = FixedClock("2026-11-02T12:00:00+00:00")
    _snapshot(owner, as_of=date(2026, 10, 15), assets_minor=100)
    _snapshot(owner, as_of=date(2026, 10, 31), assets_minor=200)
    page = owner.client.get("/net-worth/history").text
    assert "Oct 15, 2026" in page
    assert "October 2026" in page
    chart = re.search(
        r'<script type="application/json" id="networth-history-data">(.*?)</script>',
        page,
        re.DOTALL,
    )
    assert chart is not None
    assert '"labels": ["Oct 15, 2026", "October 2026"]' in chart.group(1)


def test_other_currency_snapshots_are_in_the_table_but_not_the_chart(
    owner: OwnerApp,
) -> None:
    owner.app.state.clock = FixedClock("2026-11-02T12:00:00+00:00")
    _snapshot(owner, as_of=date(2026, 9, 30), currency="EUR", assets_minor=100)
    _snapshot(owner, as_of=date(2026, 10, 31), currency="USD", assets_minor=200)
    page = owner.client.get("/net-worth/history").text
    assert "data-history-note" in page
    assert "data-snapshot-currency" in page and "USD" in page
    assert page.count("data-history-row") == 2
    chart = re.search(
        r'<script type="application/json" id="networth-history-data">(.*?)</script>',
        page,
        re.DOTALL,
    )
    assert chart is not None
    assert "USD" not in chart.group(1)
    assert '"values": [100]' in chart.group(1)


def test_only_non_base_currency_points_load_no_chart(owner: OwnerApp) -> None:
    owner.app.state.clock = FixedClock("2026-11-02T12:00:00+00:00")
    _snapshot(owner, as_of=date(2026, 10, 31), currency="USD")
    page = owner.client.get("/net-worth/history").text
    assert "data-history-note" in page
    assert 'data-chart="line"' not in page
    assert "echarts.min.js" not in page


def test_deleted_categories_use_a_neutral_fallback(owner: OwnerApp) -> None:
    owner.app.state.clock = FixedClock("2026-11-02T12:00:00+00:00")
    _snapshot(
        owner,
        as_of=date(2026, 10, 31),
        breakdown=_breakdown((uuid4(), "asset", 10000)),
    )
    page = owner.client.get("/net-worth/history").text
    assert "Deleted category" in page


def test_history_markup_is_csp_safe_and_escaped(owner: OwnerApp) -> None:
    owner.app.state.clock = FixedClock("2026-11-02T12:00:00+00:00")
    _snapshot(
        owner,
        as_of=date(2026, 10, 31),
        breakdown=_breakdown((_category_id(owner, "asset", "Vehicles"), "asset", 100)),
    )
    page = owner.client.get("/net-worth/history").text
    assert " style=" not in page
    assert re.search(r"<script(?![^>]*\ssrc=)(?![^>]*type=\"application/json\")", page) is None
    assert "x-data" not in page and "@click" not in page


def test_history_is_translated(owner: OwnerApp) -> None:
    owner.app.state.clock = FixedClock("2026-11-02T12:00:00+00:00")
    _snapshot(owner, as_of=date(2026, 10, 31))
    owner.client.headers["Accept-Language"] = "pl"
    page = owner.client.get("/net-worth/history").text
    assert "Historia" in page
    assert "Zrób migawkę teraz" in page
    assert "Ręczna" in page
    assert "Zakres" in page and "Cały okres" in page
    assert "Data" in page and "Źródło" in page


def test_backfilled_estimate_count_is_marked_in_the_table_and_tooltip(
    owner: OwnerApp,
) -> None:
    owner.app.state.clock = FixedClock("2026-11-02T12:00:00+00:00")
    _snapshot(
        owner,
        as_of=date(2026, 10, 31),
        source="backfill",
        breakdown=_breakdown(estimated=3),
    )
    page = owner.client.get("/net-worth/history").text
    assert "includes 3 estimated values" in page
    assert "data-estimated" in page
    chart = re.search(
        r'<script type="application/json" id="networth-history-data">(.*?)</script>',
        page,
        re.DOTALL,
    )
    assert chart is not None
    assert "includes 3 estimated values" in chart.group(1)
