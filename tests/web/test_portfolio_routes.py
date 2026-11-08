"""Portfolio page over HTTP (F6.6): tab, tables, warnings, chart scope, roles and i18n."""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.db.models.investments import AssetClass, Instrument, InvestmentTransaction
from assetboard.db.models.prices import PricePoint
from tests.conftest import OwnerApp

NOW = "2026-03-10T12:00:00+00:00"
PORTFOLIO = "/investments/portfolio"
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
    type_value: str = "etf",
) -> UUID:
    data = {
        "name": name,
        "symbol": "",
        "type": type_value,
        "quote_currency": currency,
        "unit_label": "share",
        "provider_key": "",
        "provider_symbol": "",
    } | {f"weight_{class_id}": weight for class_id, weight in weights.items()}
    response = owner.post(INSTRUMENTS, data, token_from=INSTRUMENTS)
    assert response.status_code == 303, response.text
    with owner.db() as session:
        return session.scalars(select(Instrument.id).where(Instrument.name == name)).one()


def _buy(owner: OwnerApp, instrument: UUID, quantity: str, price: str, day: str = "2026-03-09"):
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


def _chart_data(page: str) -> dict[str, object]:
    match = re.search(
        r'<script type="application/json" id="chart-portfolio-data">(.*?)</script>', page, re.S
    )
    assert match is not None
    return json.loads(match.group(1))


def _fund(owner: OwnerApp) -> tuple[UUID, dict[str, UUID]]:
    classes = _classes(owner)
    fund = _instrument(owner, "Balanced fund", {classes["Stocks"]: "80", classes["Bonds"]: "20"})
    _buy(owner, fund, "10", "100")
    return fund, classes


def test_the_tab_and_the_empty_state(owner: OwnerApp) -> None:
    page = owner.client.get(PORTFOLIO)
    assert page.status_code == 200
    assert re.search(r'aria-selected="true" data-portfolio-tab', page.text)
    assert "data-portfolio-empty" in page.text
    assert "Go to transactions" in page.text
    assert "echarts.min.js" not in page.text and "data-chart=" not in page.text
    instruments = owner.client.get(INSTRUMENTS).text
    assert re.search(r'aria-selected="false" data-portfolio-tab', instruments)
    assert (
        owner.client.get("/investments", follow_redirects=False)
        .headers["location"]
        .endswith(INSTRUMENTS)
    )


def test_holdings_totals_classes_and_the_default_stocks_chart(owner: OwnerApp) -> None:
    fund, _classes_by_name = _fund(owner)
    with owner.db() as session, session.begin():
        session.add(
            PricePoint(
                workspace_id=owner.home_id,
                instrument_id=fund,
                date=date(2026, 3, 10),
                price=Decimal("110"),
                source="manual",
            )
        )

    page = owner.client.get(PORTFOLIO).text

    assert f'data-portfolio-row="{fund}"' in page
    assert re.search(r"data-portfolio-value>€1,100\.00<", page)
    assert re.search(r"data-portfolio-cost>€1,000\.00<", page)
    assert "€100.00 <span" in page and "(10.0%)" in page
    classes = re.findall(r"data-portfolio-class>.*?</tr>", page, re.S)
    assert len(classes) == 2
    assert "Stocks" in classes[0] and "€880.00" in classes[0] and "€800.00" in classes[0]
    assert "Bonds" in classes[1] and "€220.00" in classes[1] and "€200.00" in classes[1]
    assert "Holdings in Stocks" in page
    data = _chart_data(page)
    assert data["key"] == "portfolio"
    slices = data["slices"]
    assert isinstance(slices, list) and len(slices) == 1
    assert slices[0]["name"] == "Balanced fund" and slices[0]["value"] == 88_000
    assert slices[0]["color"] == "#16a34a"  # the categorical palette, not the class colour
    assert ">Instrument</th>" in page
    assert "echarts.min.js" in page
    assert "100.0%" in page  # the row's share of the portfolio


def test_scope_selector_whole_portfolio_class_and_unknown(owner: OwnerApp) -> None:
    _fund_id, classes = _fund(owner)
    whole = owner.client.get(PORTFOLIO, params={"scope": "all"}).text
    assert "Holdings in the whole portfolio" in whole
    assert _chart_data(whole)["slices"][0]["value"] == 100_000  # type: ignore[index]
    assert re.search(r'<option value="all" selected>Whole portfolio</option>', whole)
    bonds = owner.client.get(PORTFOLIO, params={"scope": str(classes["Bonds"])}).text
    assert "Holdings in Bonds" in bonds
    assert _chart_data(bonds)["slices"][0]["value"] == 20_000  # type: ignore[index]
    for unknown in ("00000000-0000-0000-0000-000000000000", "nonsense"):
        fallback = owner.client.get(PORTFOLIO, params={"scope": unknown})
        assert fallback.status_code == 200
        assert "Holdings in Stocks" in fallback.text


def test_missing_price_is_warned_and_excluded_from_the_totals(owner: OwnerApp) -> None:
    fund, classes = _fund(owner)
    legacy = _instrument(owner, "Legacy fund", {classes["Stocks"]: "100"})
    with owner.db() as session, session.begin():
        session.add(
            InvestmentTransaction(
                workspace_id=owner.home_id,
                instrument_id=legacy,
                date=None,
                type="buy",
                quantity=Decimal("5"),
                unit_price=Decimal("20"),
                quote_currency="EUR",
                fee_minor=0,
                fx_to_base=None,
                note="",
            )
        )

    page = owner.client.get(PORTFOLIO).text

    assert f'data-portfolio-row="{legacy}" data-excluded="price"' in page
    assert "Price missing: not included in the totals." in page
    assert "1 holding is excluded from the totals (missing price or FX rate)." in page
    assert re.search(r"data-portfolio-cost>€1,000\.00<", page)  # the legacy €100.00 left out
    assert [s["name"] for s in _chart_data(page)["slices"]] == ["Balanced fund"]  # type: ignore[index]
    assert f'data-portfolio-row="{fund}"' in page


def test_stale_price_badge_and_archived_instrument(owner: OwnerApp) -> None:
    classes = _classes(owner)
    old = _instrument(owner, "Old fund", {classes["Stocks"]: "100"})
    _buy(owner, old, "1", "50", day="2026-02-01")
    with owner.db() as session, session.begin():
        row = session.get(Instrument, old)
        assert row is not None
        row.archived_at = row.created_at

    page = owner.client.get(PORTFOLIO).text

    row_html = re.search(rf'data-portfolio-row="{old}".*?</tr>', page, re.S)
    assert row_html is not None
    assert "data-price-stale" in row_html.group(0)
    assert "data-archived-badge" in row_html.group(0)
    assert "€50.00" in row_html.group(0)


def test_viewer_reads_the_page_and_htmx_gets_the_fragment(owner: OwnerApp) -> None:
    _fund(owner)
    owner.set_admin_role("viewer")
    page = owner.client.get(PORTFOLIO)
    assert page.status_code == 200
    assert "data-portfolio-row" in page.text
    assert "data-providers-tab" not in page.text
    fragment = owner.client.get(PORTFOLIO, headers={"HX-Request": "true"})
    assert fragment.text.lstrip().startswith('<div id="portfolio-body"')
    assert "<html" not in fragment.text
    assert "HX-Request" in fragment.headers["vary"]


def test_polish_page(owner: OwnerApp) -> None:
    _fund(owner)
    owner.client.headers["Accept-Language"] = "pl"
    page = owner.client.get(PORTFOLIO).text
    assert "Portfel" in page
    assert "Zysk niezrealizowany" in page
    assert "Cały portfel" in page
    assert "data-portfolio-value>1\u00a0000,00\u00a0€<" in page  # EUR in the Polish locale
    assert "100,0%" in page  # the share, with the Polish decimal comma
