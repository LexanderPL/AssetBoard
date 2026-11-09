"""Performance page over HTTP (F6.7): tab, chart, periods, ranges, classes and empty state."""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.db.models.investments import AssetClass, Instrument, InvestmentTransaction
from assetboard.db.models.prices import PricePoint
from tests.conftest import OwnerApp

NOW = "2026-03-10T12:00:00+00:00"
PERFORMANCE = "/investments/performance"
INSTRUMENTS = "/investments/instruments"
TRANSACTIONS = "/investments/transactions"


@pytest.fixture(autouse=True)
def _fixed_clock(owner: OwnerApp) -> None:
    clock = FixedClock(NOW)
    owner.app.state.clock = clock
    owner.app.state.session_factory.configure(info={"clock": clock})


def _classes(owner: OwnerApp) -> dict[str, UUID]:
    with owner.db() as session:
        rows = session.scalars(
            select(AssetClass).where(AssetClass.workspace_id == owner.home_id)
        ).all()
        return {row.name: row.id for row in rows}


def _instrument(
    owner: OwnerApp,
    name: str,
    weights: dict[UUID, str],
    *,
    currency: str = "EUR",
) -> UUID:
    data = {
        "name": name,
        "symbol": "",
        "type": "etf",
        "quote_currency": currency,
        "unit_label": "share",
        "provider_key": "",
        "provider_symbol": "",
    } | {f"weight_{class_id}": weight for class_id, weight in weights.items()}
    response = owner.post(INSTRUMENTS, data, token_from=INSTRUMENTS)
    assert response.status_code == 303, response.text
    with owner.db() as session:
        return session.scalars(select(Instrument.id).where(Instrument.name == name)).one()


def _buy(owner: OwnerApp, instrument: UUID, quantity: str, price: str, day: str = "2024-01-01"):
    response = owner.post(
        TRANSACTIONS,
        {
            "instrument_id": str(instrument),
            "type": "buy",
            "date": day,
            "quantity": quantity,
            "unit_price": price,
            "fee": "0",
            "fx_to_base": "1",
            "note": "",
        },
        token_from=TRANSACTIONS,
    )
    assert response.status_code == 303, response.text


def _price(owner: OwnerApp, instrument: UUID, day: date, value: str) -> None:
    with owner.db() as session, session.begin():
        session.add(
            PricePoint(
                workspace_id=owner.home_id,
                instrument_id=instrument,
                date=day,
                price=Decimal(value),
                source="manual",
            )
        )


def _chart_data(page: str) -> dict[str, Any]:
    match = re.search(
        r'<script type="application/json" id="performance-data">(.*?)</script>',
        page,
        re.S,
    )
    assert match is not None
    return json.loads(match.group(1))


def _fund(owner: OwnerApp) -> tuple[UUID, dict[str, UUID]]:
    classes = _classes(owner)
    fund = _instrument(owner, "Balanced fund", {classes["Stocks"]: "80", classes["Bonds"]: "20"})
    _buy(owner, fund, "10", "100")
    _price(owner, fund, date(2024, 1, 1), "100")
    _price(owner, fund, date(2026, 3, 10), "110")
    return fund, classes


def test_the_tab_and_empty_state(owner: OwnerApp) -> None:
    page = owner.client.get(PERFORMANCE)
    assert page.status_code == 200
    assert re.search(r'aria-selected="true" data-performance-tab', page.text)
    assert "data-performance-empty" in page.text
    assert "echarts.min.js" not in page.text and "data-chart=" not in page.text
    instruments = owner.client.get(INSTRUMENTS).text
    assert re.search(r'aria-selected="false" data-performance-tab', instruments)


def test_chart_periods_and_default_one_year_range(owner: OwnerApp) -> None:
    _fund(owner)
    page = owner.client.get(PERFORMANCE).text
    assert "data-performance-chart" in page
    assert 'data-chart="performance"' in page
    assert "echarts.min.js" in page
    assert 'data-performance-range-option="1y"' in page
    assert re.search(r'aria-current="true"[^>]*data-performance-range-option="1y"', page)
    assert page.count("data-performance-period=") == 5
    assert "Annualised XIRR" in page and "Period return" in page
    assert "Δ value − net flows" in page
    assert "data-performance-points" in page
    data = _chart_data(page)
    assert data["labels"] and data["value"] and data["invested"]
    assert data["value"][-1] == 110_000
    assert data["invested"][-1] == 100_000
    assert data["legend"]["value"] == "Value"
    assert all(len(tooltip) == 3 for tooltip in data["tooltips"])


def test_range_and_class_filters(owner: OwnerApp) -> None:
    _fund(owner)
    classes = _classes(owner)
    ytd = owner.client.get(f"{PERFORMANCE}?range=ytd").text
    assert re.search(r'aria-current="true"[^>]*data-performance-range-option="ytd"', ytd)
    stocks = owner.client.get(f"{PERFORMANCE}?class_id={classes['Stocks']}").text
    assert re.search(r'<option value="' + str(classes["Stocks"]) + r'" selected', stocks)
    data = _chart_data(stocks)
    assert data["value"][-1] == 88_000


def test_undated_rows_are_reported(owner: OwnerApp) -> None:
    fund, _classes_by_name = _fund(owner)
    with owner.db() as session, session.begin():
        session.add(
            InvestmentTransaction(
                workspace_id=owner.home_id,
                instrument_id=fund,
                date=None,
                type="buy",
                quantity=Decimal("1"),
                unit_price=Decimal("100"),
                quote_currency="EUR",
                fee_minor=0,
                fx_to_base=Decimal("1"),
                note="",
            )
        )
    page = owner.client.get(PERFORMANCE).text
    assert "undated legacy transaction" in page
    assert "data-performance-undated" in page


def _incomplete_page() -> Any:
    from assetboard.domain.money import Money
    from assetboard.domain.performance import SeriesPoint
    from assetboard.services.performance import PerformancePage

    return PerformancePage(
        currency="EUR",
        today=date(2026, 3, 10),
        range_code="1y",
        range_start=date(2025, 3, 10),
        class_scope=None,
        class_options=(),
        chart=(SeriesPoint(date(2026, 1, 10), Money(0, "EUR"), Money(0, "EUR"), incomplete=True),),
        periods=(),
        undated_count=0,
        incomplete_points=1,
        first_date=date(2026, 1, 10),
    )


def test_chart_json_uses_nulls_and_the_table_uses_a_dash_for_incomplete_points(
    owner: OwnerApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    import assetboard.web.performance_routes as performance_routes

    monkeypatch.setattr(
        performance_routes, "performance_page", lambda *args, **kwargs: _incomplete_page()
    )

    page = owner.client.get(PERFORMANCE).text
    data = _chart_data(page)

    assert data["value"] == [None]
    assert data["invested"] == [None]
    assert data["gain"] == [None]
    assert data["tooltips"] == [["Incomplete: an instrument has no price or FX rate."]]
    assert "data-performance-incomplete" in page
    assert '<td class="whitespace-nowrap py-1.5 pr-3 text-right tabular-nums">—</td>' in page
    assert "Incomplete data" in page


def test_the_chart_does_not_start_with_a_zero_prefix(owner: OwnerApp) -> None:
    _fund(owner)

    page = owner.client.get(f"{PERFORMANCE}?range=all").text
    data = _chart_data(page)

    assert data["labels"][0] == "Jan 7, 2024"
    assert data["value"][0] and data["invested"][0]
    assert all(value is not None for value in data["value"])
