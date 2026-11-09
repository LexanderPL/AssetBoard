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
from assetboard.db.models.identity import User
from assetboard.db.models.investments import (
    AssetClass,
    Instrument,
    InvestmentTransaction,
    RiskTargetPolicy,
)
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


def _class_chart_data(page: str) -> dict[str, object]:
    match = re.search(
        r'<script type="application/json" id="chart-portfolio-classes-data">(.*?)</script>',
        page,
        re.S,
    )
    assert match is not None
    return json.loads(match.group(1))


def _set_tier(owner: OwnerApp, class_id: UUID, tier: str | None) -> None:
    with owner.db() as session, session.begin():
        row = session.get(AssetClass, class_id)
        assert row is not None
        row.risk_tier = tier


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
        .endswith(PORTFOLIO)
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
    totals = re.search(r"data-portfolio-totals.*?</section>", page, re.S)
    assert totals is not None
    assert 'data-donut="portfolio-classes"' in totals.group(0)
    class_data = _class_chart_data(page)
    class_slices = class_data["slices"]
    assert isinstance(class_slices, list) and len(class_slices) == 2
    assert [(s["name"], s["value"], s["share"], s["color"]) for s in class_slices] == [
        ("Stocks", 88_000, "80.0%", "#2563eb"),
        ("Bonds", 22_000, "20.0%", "#16a34a"),
    ]
    assert class_data["total"] == "€1,100.00"
    assert "Asset classes" in page
    assert "Holdings by instrument in Stocks" in page
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
    assert "Holdings by instrument" in whole
    assert _chart_data(whole)["slices"][0]["value"] == 100_000  # type: ignore[index]
    assert re.search(r'<option value="all" selected>Whole portfolio</option>', whole)
    bonds = owner.client.get(PORTFOLIO, params={"scope": str(classes["Bonds"])}).text
    assert "Holdings by instrument in Bonds" in bonds
    assert _chart_data(bonds)["slices"][0]["value"] == 20_000  # type: ignore[index]
    for unknown in ("00000000-0000-0000-0000-000000000000", "nonsense"):
        fallback = owner.client.get(PORTFOLIO, params={"scope": unknown})
        assert fallback.status_code == 200
        assert "Holdings by instrument in Stocks" in fallback.text


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


def test_a_non_investable_class_is_badged_and_still_counted(owner: OwnerApp) -> None:
    response = owner.post(
        "/investments/classes",
        {"name": "Emergency fund", "color": "#0ea5e9"},  # investable unchecked
        token_from="/investments/classes",
    )
    assert response.status_code == 303, response.text
    classes = _classes(owner)
    cash = _instrument(owner, "Cash fund", {classes["Emergency fund"]: "100"})
    _buy(owner, cash, "1", "500")
    _fund(owner)

    page = owner.client.get(PORTFOLIO, params={"scope": "all"}).text

    rows = re.findall(r"data-portfolio-class>.*?</tr>", page, re.S)
    by_name = {("Emergency fund" if "Emergency fund" in row else "other"): row for row in rows}
    assert "data-not-in-fire" in by_name["Emergency fund"]
    assert "not counted in FIRE" in by_name["Emergency fund"]
    assert all("data-not-in-fire" not in row for row in rows if "Emergency fund" not in row)
    # Every class counts: 1 000.00 (fund) + 500.00 (cash) in the totals and the chart.
    assert re.search(r"data-portfolio-value>€1,500\.00<", page)
    assert "€500.00" in by_name["Emergency fund"]
    assert {s["name"] for s in _chart_data(page)["slices"]} == {"Balanced fund", "Cash fund"}  # type: ignore[index]


def test_the_class_donut_includes_the_unallocated_bucket(owner: OwnerApp) -> None:
    _fund(owner)
    with owner.db() as session, session.begin():
        legacy = Instrument(
            workspace_id=owner.home_id,
            name="Legacy fund",
            name_key="legacy fund",
            type="etf",
            quote_currency="EUR",
            unit_label="share",
            valuation_source="manual",
        )
        session.add(legacy)
        session.flush()
        session.add(
            InvestmentTransaction(
                workspace_id=owner.home_id,
                instrument_id=legacy.id,
                date=date(2026, 3, 9),
                type="buy",
                quantity=Decimal("5"),
                unit_price=Decimal("50"),
                quote_currency="EUR",
                fee_minor=0,
                fx_to_base=None,
                note="",
            )
        )

    page = owner.client.get(PORTFOLIO).text

    slices = _class_chart_data(page)["slices"]
    by_name = {slice["name"]: slice for slice in slices}  # type: ignore[index,union-attr]
    assert by_name["Unallocated"]["value"] == 25_000
    assert by_name["Unallocated"]["color"] is None
    assert by_name["Stocks"]["value"] == 80_000
    assert _class_chart_data(page)["total"] == "€1,250.00"  # type: ignore[index]
    assert "Unallocated" in page


def test_the_unassigned_help_and_one_click_suggested_tiers(owner: OwnerApp) -> None:
    _fund_id, classes = _fund(owner)
    assert "data-risk-unassigned-help" not in owner.client.get(PORTFOLIO).text

    _set_tier(owner, classes["Bonds"], None)
    page = owner.client.get(PORTFOLIO).text
    assert "data-risk-unassigned-help" in page
    assert "Unassigned: €200.00 (20% of the portfolio)" in page
    assert "Open Asset classes" in page
    assert "data-apply-risk-tiers" in page

    response = owner.post(f"{PORTFOLIO}/apply-risk-tiers", token_from=PORTFOLIO)

    assert response.status_code == 303
    assert response.headers["location"].endswith("?notice=risk_tiers_suggested")
    page = owner.client.get(response.headers["location"]).text
    assert "Suggested risk tiers applied." in page
    assert "data-risk-unassigned-help" not in page
    with owner.db() as session:
        assert session.get(AssetClass, classes["Bonds"]).risk_tier == "safe"  # type: ignore[union-attr]

    response = owner.post(f"{PORTFOLIO}/apply-risk-tiers", token_from=PORTFOLIO)
    assert response.headers["location"].endswith("?notice=risk_tiers_complete")
    assert (
        "Every matching asset class already has a risk tier."
        in owner.client.get(response.headers["location"]).text
    )


def test_the_unassigned_help_has_no_button_without_suggestions(owner: OwnerApp) -> None:
    classes = _classes(owner)
    catch_all = _instrument(owner, "Catch-all fund", {classes["Other"]: "100"})
    _buy(owner, catch_all, "1", "100")

    page = owner.client.get(PORTFOLIO).text

    assert "data-risk-unassigned-help" in page
    assert "Unassigned: €100.00 (100% of the portfolio)" in page
    assert "data-apply-risk-tiers" not in page


def test_only_editors_can_apply_suggested_risk_tiers_from_the_portfolio(owner: OwnerApp) -> None:
    _fund_id, classes = _fund(owner)
    _set_tier(owner, classes["Bonds"], None)
    owner.set_admin_role("viewer")

    page = owner.client.get(PORTFOLIO).text

    assert "data-risk-unassigned-help" in page
    assert "data-apply-risk-tiers" not in page
    response = owner.post(f"{PORTFOLIO}/apply-risk-tiers", token_from=PORTFOLIO)
    assert response.status_code == 403


def test_the_class_donut_includes_archived_classes(owner: OwnerApp) -> None:
    _fund(owner)
    classes = _classes(owner)
    with owner.db() as session, session.begin():
        stocks = session.get(AssetClass, classes["Stocks"])
        assert stocks is not None
        stocks.archived_at = stocks.created_at

    page = owner.client.get(PORTFOLIO).text

    slices = _class_chart_data(page)["slices"]
    by_name = {slice["name"]: slice for slice in slices}  # type: ignore[index,union-attr]
    assert by_name["Stocks"]["value"] == 80_000
    assert by_name["Stocks"]["color"] == "#2563eb"
    assert "Archived" in page


def test_risk_balance_defaults_flags_and_hint(owner: OwnerApp) -> None:
    fund, classes = _fund(owner)
    _set_tier(owner, classes["Stocks"], "growth")
    _set_tier(owner, classes["Bonds"], "safe")
    _set_tier(owner, classes["Crypto"], "speculative")

    page = owner.client.get(PORTFOLIO).text

    assert "data-risk-balance" in page
    assert "Risk balance" in page
    assert 'data-risk-tier="growth"' in page
    assert "Outside tolerance" in page
    assert "Move about €300.00 from Growth to Safe." in page
    assert "data-risk-unassigned" in page
    assert "not investment advice" in page.lower()


def test_risk_policy_editor_saves_and_recomputes(owner: OwnerApp) -> None:
    _fund(owner)

    response = owner.post(
        f"{PORTFOLIO}/risk-targets",
        {
            "mode": "fixed",
            "safe": "20",
            "growth": "70",
            "fixed_speculative": "10",
            "age_scaled_speculative": "10",
            "age_in_bonds_speculative": "0",
            "tolerance": "5",
        },
        token_from=PORTFOLIO,
    )

    assert response.status_code == 303
    assert "notice=risk_policy_saved" in response.headers["location"]
    page = owner.client.get(response.headers["location"]).text
    assert "Risk targets saved." in page
    assert "20%" in page and "70%" in page
    with owner.db() as session:
        row = session.scalars(select(RiskTargetPolicy)).one()
    assert (row.mode, row.safe_bp, row.growth_bp, row.speculative_bp) == (
        "fixed",
        2000,
        7000,
        1000,
    )


def test_invalid_risk_policy_is_refused_with_a_message(owner: OwnerApp) -> None:
    response = owner.post(
        f"{PORTFOLIO}/risk-targets",
        {
            "mode": "fixed",
            "safe": "50",
            "growth": "40",
            "fixed_speculative": "20",
            "age_scaled_speculative": "10",
            "age_in_bonds_speculative": "0",
            "tolerance": "5",
        },
        token_from=PORTFOLIO,
    )

    assert response.status_code == 400
    assert "The fixed targets must total exactly 100%." in response.text


def test_the_risk_editor_shows_only_the_fields_of_the_selected_mode(owner: OwnerApp) -> None:
    _fund(owner)
    page = owner.client.get(PORTFOLIO).text
    assert "data-risk-mode" in page
    assert re.search(r'data-risk-fields="fixed">', page)
    assert re.search(r'data-risk-fields="age_scaled" hidden>', page)
    assert re.search(r'data-risk-fields="age_in_bonds" hidden>', page)
    assert 'id="risk-tolerance"' in page

    with owner.db() as session, session.begin():
        user = session.get(User, owner.admin_id)
        assert user is not None
        user.birth_year = 1986
        user.life_expectancy = 80
    response = owner.post(
        f"{PORTFOLIO}/risk-targets",
        {
            "mode": "age_scaled",
            "safe": "20",
            "growth": "70",
            "fixed_speculative": "10",
            "age_scaled_speculative": "10",
            "age_in_bonds_speculative": "0",
            "tolerance": "5",
        },
        token_from=PORTFOLIO,
    )
    assert response.status_code == 303
    page = owner.client.get(response.headers["location"]).text
    assert re.search(r'data-risk-fields="fixed" hidden>', page)
    assert re.search(r'data-risk-fields="age_scaled">', page)
    assert re.search(r'data-risk-fields="age_in_bonds" hidden>', page)
    assert 'id="risk-tolerance"' in page


def test_age_mode_requires_a_birth_year(owner: OwnerApp) -> None:
    response = owner.post(
        f"{PORTFOLIO}/risk-targets",
        {
            "mode": "age_scaled",
            "safe": "50",
            "growth": "40",
            "fixed_speculative": "10",
            "age_scaled_speculative": "10",
            "age_in_bonds_speculative": "0",
            "tolerance": "5",
        },
        token_from=PORTFOLIO,
    )
    assert response.status_code == 400
    assert "Set your birth year in Profile" in response.text

    with owner.db() as session, session.begin():
        user = session.get(User, owner.admin_id)
        assert user is not None
        user.birth_year = 1986
        user.life_expectancy = 80
    response = owner.post(
        f"{PORTFOLIO}/risk-targets",
        {
            "mode": "age_scaled",
            "safe": "50",
            "growth": "40",
            "fixed_speculative": "10",
            "age_scaled_speculative": "10",
            "age_in_bonds_speculative": "0",
            "tolerance": "5",
        },
        token_from=PORTFOLIO,
    )
    assert response.status_code == 303
    page = owner.client.get(response.headers["location"]).text
    assert "based on your age" in page


def test_the_risk_editor_is_only_for_editors(owner: OwnerApp) -> None:
    owner.set_admin_role("viewer")

    page = owner.client.get(PORTFOLIO).text

    assert "data-risk-policy" not in page
    assert "Risk balance" in page
