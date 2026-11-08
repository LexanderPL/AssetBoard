"""Investments page over HTTP (F6.1): classes, instruments, allocations and roles."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select

from assetboard.db.models.investments import AssetClass, Instrument
from assetboard.domain.instruments import ASSET_CLASS_SEED
from assetboard.i18n import gettext
from tests.conftest import OwnerApp

INSTRUMENTS = "/investments/instruments"
CLASSES = "/investments/classes"


@pytest.fixture(autouse=True)
def _empty_investments(owner: OwnerApp) -> None:
    owner.clear_asset_classes()


def _htmx(owner: OwnerApp, path: str, data: dict[str, str] | None = None, target: str = "") -> Any:
    headers = {
        "HX-Request": "true",
        "X-CSRF-Token": owner.token(INSTRUMENTS),
    }
    if target:
        headers["HX-Target"] = target
    return owner.client.post(path, data=data or {}, headers=headers, follow_redirects=False)


def _class_id(owner: OwnerApp, name: str) -> UUID:
    with owner.db() as session:
        return session.scalars(select(AssetClass.id).where(AssetClass.name == name)).one()


def _instrument_id(owner: OwnerApp, name: str) -> UUID:
    with owner.db() as session:
        return session.scalars(select(Instrument.id).where(Instrument.name == name)).one()


def _class(
    owner: OwnerApp,
    name: str = "Stocks",
    *,
    color: str = "#2563eb",
    investable: str = "1",
) -> UUID:
    response = owner.post(
        CLASSES,
        {"name": name, "color": color, "investable": investable},
        token_from=CLASSES,
    )
    assert response.status_code == 303, response.text
    return _class_id(owner, name)


def _instrument(
    owner: OwnerApp,
    name: str,
    class_id: UUID,
    *,
    weight: str = "100",
    type: str = "etf",
    quote_currency: str = "EUR",
    unit_label: str = "",
    symbol: str = "VWCE",
) -> UUID:
    response = owner.post(
        INSTRUMENTS,
        {
            "name": name,
            "symbol": symbol,
            "type": type,
            "quote_currency": quote_currency,
            "unit_label": unit_label,
            f"weight_{class_id}": weight,
        },
        token_from=INSTRUMENTS,
    )
    assert response.status_code == 303, response.text
    return _instrument_id(owner, name)


def _names(html: str, attribute: str) -> list[str]:
    return re.findall(rf'{attribute}="([^"]+)"', html)


def test_investment_pages_do_not_render_the_field_error_macro(owner: OwnerApp) -> None:
    stocks = _class(owner, "Stocks")
    _instrument(owner, "World ETF", stocks)
    for path in (INSTRUMENTS, CLASSES):
        page = owner.client.get(path).text
        assert "<Macro 'error'>" not in page
        assert "data-error>" not in page


def test_the_nav_links_to_the_investments_page(owner: OwnerApp) -> None:
    home = owner.client.get("/").text
    assert "data-investments-link" in home and f'href="{INSTRUMENTS}"' in home
    page = owner.client.get(INSTRUMENTS).text
    assert 'data-investments-link aria-current="page"' in page
    classes = owner.client.get(CLASSES).text
    assert 'data-investments-link aria-current="page"' in classes


def test_an_empty_workspace_offers_the_seed_set(owner: OwnerApp) -> None:
    page = owner.client.get(CLASSES).text
    assert "data-seed-offer" in page and "data-seed" in page
    response = owner.post("/investments/classes/seed", token_from=CLASSES)
    assert (response.status_code, response.headers["location"]) == (
        303,
        f"{CLASSES}?notice=classes_seeded",
    )
    page = owner.client.get(f"{CLASSES}?notice=classes_seeded").text
    assert "Suggested asset classes added." in page
    assert "data-seed-offer" not in page
    assert _names(page, "data-class-name")[:3] == ["Stocks", "Bonds", "Metals"]


def test_seeding_without_javascript_from_the_instruments_tab_returns_there(
    owner: OwnerApp,
) -> None:
    response = owner.post(
        "/investments/classes/seed",
        {"return_to": "instruments"},
        token_from=INSTRUMENTS,
    )
    assert (response.status_code, response.headers["location"]) == (
        303,
        f"{INSTRUMENTS}?notice=classes_seeded",
    )


def test_the_seed_set_is_named_in_the_users_language(owner: OwnerApp) -> None:
    owner.client.headers["Accept-Language"] = "pl"
    owner.post("/investments/classes/seed", token_from=CLASSES)
    page = owner.client.get(CLASSES).text
    assert _names(page, "data-class-name")[0] == gettext("Stocks", "pl")
    assert "Akcje" in page


def test_create_a_class_and_an_instrument(owner: OwnerApp) -> None:
    stocks = _class(owner, "Stocks")
    _instrument(owner, "World ETF", stocks, weight="100")
    page = owner.client.get(INSTRUMENTS).text
    assert "World ETF" in page and "ETF" in page
    assert "EUR · share" in page
    assert "Stocks" in page and "100%" in page
    assert 'data-instrument-name="World ETF"' in page
    with owner.db() as session:
        instrument = session.scalars(select(Instrument).where(Instrument.name == "World ETF")).one()
        assert (instrument.quote_currency, instrument.unit_label) == ("EUR", "share")


def test_a_currency_instrument_uses_its_currency_code_as_the_unit(owner: OwnerApp) -> None:
    currencies = _class(owner, "Currencies")
    _instrument(
        owner,
        "US dollar",
        currencies,
        type="currency",
        quote_currency="USD",
        unit_label="dollar",
        symbol="",
    )
    page = owner.client.get(INSTRUMENTS).text
    assert "USD · USD" in page


def test_a_currency_instrument_is_created_with_the_currency_code(
    owner: OwnerApp,
) -> None:
    currencies = _class(owner, "Currencies")
    _instrument(
        owner,
        "US dollar",
        currencies,
        type="currency",
        quote_currency="USD",
        unit_label="dollar",
        symbol="",
    )
    with owner.db() as session:
        instrument = session.scalars(select(Instrument).where(Instrument.name == "US dollar")).one()
        assert instrument.unit_label == "USD"


def test_allocation_must_total_100_percent(owner: OwnerApp) -> None:
    stocks = _class(owner, "Stocks")
    response = owner.post(
        INSTRUMENTS,
        {
            "name": "World ETF",
            "symbol": "VWCE",
            "type": "etf",
            "quote_currency": "EUR",
            "unit_label": "",
            f"weight_{stocks}": "90",
        },
        token_from=INSTRUMENTS,
    )
    assert response.status_code == 400
    assert "Allocation weights must total exactly 100%." in response.text
    assert 'value="World ETF"' in response.text


def test_allocation_rejects_a_bad_percentage(owner: OwnerApp) -> None:
    stocks = _class(owner, "Stocks")
    response = owner.post(
        INSTRUMENTS,
        {
            "name": "World ETF",
            "symbol": "VWCE",
            "type": "etf",
            "quote_currency": "EUR",
            "unit_label": "",
            f"weight_{stocks}": "12,345",
        },
        token_from=INSTRUMENTS,
    )
    assert response.status_code == 400
    assert "Enter a weight between 0 and 100% with at most two decimals." in response.text


def test_edit_an_instrument_and_change_the_allocation(owner: OwnerApp) -> None:
    stocks = _class(owner, "Stocks")
    bonds = _class(owner, "Bonds")
    instrument_id = _instrument(owner, "World ETF", stocks, weight="100")
    response = owner.post(
        f"{INSTRUMENTS}/{instrument_id}",
        {
            "name": "World ETF",
            "symbol": "VWCE",
            "type": "etf",
            "quote_currency": "EUR",
            "unit_label": "share",
            f"weight_{stocks}": "80",
            f"weight_{bonds}": "20",
        },
        token_from=f"{INSTRUMENTS}?edit={instrument_id}",
    )
    assert response.status_code == 303
    page = owner.client.get(INSTRUMENTS).text
    assert "Stocks" in page and "80%" in page
    assert "Bonds" in page and "20%" in page


def test_bond_model_instruments_show_the_terms_needed_state(owner: OwnerApp) -> None:
    bonds = _class(owner, "Bonds")
    _instrument(owner, "Treasury bonds", bonds, type="bond_model", quote_currency="PLN")
    page = owner.client.get(INSTRUMENTS).text
    assert "Terms needed" in page
    assert "Bond terms are needed before this instrument can be valued" in page


def test_archive_and_restore_an_instrument(owner: OwnerApp) -> None:
    stocks = _class(owner, "Stocks")
    instrument_id = _instrument(owner, "World ETF", stocks)
    response = owner.post(f"{INSTRUMENTS}/{instrument_id}/archive", token_from=INSTRUMENTS)
    assert response.status_code == 303
    page = owner.client.get(INSTRUMENTS).text
    assert "Archived instruments" in page and "World ETF" in page
    response = owner.post(f"{INSTRUMENTS}/{instrument_id}/restore", token_from=INSTRUMENTS)
    assert response.status_code == 303
    page = owner.client.get(INSTRUMENTS).text
    assert "Archived instruments" not in page and "World ETF" in page


def test_archive_restore_and_reorder_asset_classes(owner: OwnerApp) -> None:
    a = _class(owner, "A")
    b = _class(owner, "B")
    response = _htmx(owner, f"{CLASSES}/{b}/move", {"direction": "up"}, "investments-classes-body")
    assert response.status_code == 200
    assert _names(response.text, "data-class-name")[:2] == ["B", "A"]
    owner.post(f"{CLASSES}/{a}/archive", token_from=CLASSES)
    page = owner.client.get(CLASSES).text
    assert "Archived asset classes" in page
    owner.post(f"{CLASSES}/{a}/restore", token_from=CLASSES)
    page = owner.client.get(CLASSES).text
    assert _names(page, "data-class-name")[:2] == ["B", "A"]


def test_duplicate_names_are_refused(owner: OwnerApp) -> None:
    _class(owner, "Stocks")
    response = owner.post(
        CLASSES,
        {"name": "STOCKS", "color": "#2563eb", "investable": "1"},
        token_from=CLASSES,
    )
    assert response.status_code == 400
    assert "Another active asset class already has this name." in response.text


def test_htmx_create_returns_the_body_fragment(owner: OwnerApp) -> None:
    response = _htmx(
        owner,
        CLASSES,
        {"name": "Stocks", "color": "#2563eb", "investable": "1"},
        "investments-classes-body",
    )
    assert response.status_code == 200
    assert response.text.lstrip().startswith('<div id="investments-classes-body"')
    assert "<html" not in response.text
    assert "Stocks" in response.text


def test_a_viewer_cannot_see_the_forms(owner: OwnerApp) -> None:
    _class(owner, "Stocks")
    owner.set_admin_role("viewer")
    page = owner.client.get(INSTRUMENTS).text
    assert "Add an instrument" not in page and "data-instrument-form" not in page
    assert "World ETF" not in page
    classes = owner.client.get(CLASSES).text
    assert "Add an asset class" not in classes and "data-class-form" not in classes


def test_the_allocation_total_uses_the_locale_decimal_separator(owner: OwnerApp) -> None:
    _class(owner, "Stocks")
    owner.client.headers["Accept-Language"] = "pl"
    page = owner.client.get(INSTRUMENTS).text
    assert 'data-decimal-separator=","' in page


def test_a_viewer_post_is_refused_by_the_service(owner: OwnerApp) -> None:
    stocks = _class(owner, "Stocks")
    owner.set_admin_role("viewer")
    created = owner.post(
        CLASSES,
        {"name": "Bonds", "color": "#16a34a", "investable": "1"},
        token_from=CLASSES,
    )
    assert created.status_code == 403
    archived = owner.post(
        f"{CLASSES}/{stocks}/archive",
        token_from=CLASSES,
    )
    assert archived.status_code == 403


def test_seed_from_the_instruments_page_returns_the_instruments_body(
    owner: OwnerApp,
) -> None:
    response = _htmx(owner, "/investments/classes/seed", {}, "investments-body")
    assert response.status_code == 200
    assert response.text.lstrip().startswith('<div id="investments-body"')
    assert "Suggested asset classes added." in response.text
    assert "Stocks" in response.text


def test_a_bond_model_instrument_can_be_created_before_its_terms_exist(
    owner: OwnerApp,
) -> None:
    bonds = _class(owner, "Bonds")
    _instrument(owner, "Treasury bonds", bonds, type="bond_model", quote_currency="PLN")
    with owner.db() as session:
        instrument = session.scalars(
            select(Instrument).where(Instrument.name == "Treasury bonds")
        ).one()
        assert (instrument.type, instrument.valuation_source) == ("bond_model", "bond_model")


def test_the_seeded_class_set_has_the_investable_flags(owner: OwnerApp) -> None:
    owner.post("/investments/classes/seed", token_from=CLASSES)
    page = owner.client.get(CLASSES).text
    assert 'data-investable="true"' in page
    assert 'data-investable="false"' in page
    assert len(_names(page, "data-class-name")) == len(ASSET_CLASS_SEED)


def test_bond_terms_form_saves_and_values_the_instrument(owner: OwnerApp) -> None:
    bonds = _class(owner, "Bonds")
    instrument_id = _instrument(
        owner, "Treasury bonds", bonds, type="bond_model", quote_currency="PLN"
    )
    page = owner.client.get(f"{INSTRUMENTS}/{instrument_id}").text
    assert "data-bond-panel" in page
    assert "Terms needed" in page
    assert "data-price-form" not in page

    response = owner.post(
        f"{INSTRUMENTS}/{instrument_id}/bond-terms",
        {
            "series_code": "OTS",
            "face_value": "100",
            "maturity_date": "2028-01-01",
            "interest_mode": "capitalized_annually",
            "tax_rate": "19",
            "fee_mode": "",
            "fee_value": "",
            "period_from": "2026-01-01",
            "period_rate": "5",
        },
        token_from=f"{INSTRUMENTS}/{instrument_id}",
    )
    assert response.status_code == 303
    page = owner.client.get(response.headers["location"]).text
    assert "Bond terms saved." in page
    assert "Valued from terms" in page
    assert "Model value (net of tax)" in page
    assert "Terms needed" not in page

    response = owner.post(
        "/investments/transactions",
        {
            "instrument_id": str(instrument_id),
            "type": "buy",
            "date": "2026-01-01",
            "quantity": "1",
            "unit_price": "100",
            "fee": "0",
            "fx_to_base": "1",
            "note": "",
        },
        token_from="/investments/transactions",
    )
    assert response.status_code == 303
    page = owner.client.get(f"{INSTRUMENTS}/{instrument_id}").text
    assert "100.00" in page
    assert "Projected net at maturity" in page
    assert "Values are estimates from user-entered terms." in page


def test_bond_terms_validation_is_inline(owner: OwnerApp) -> None:
    bonds = _class(owner, "Bonds")
    instrument_id = _instrument(
        owner, "Treasury bonds", bonds, type="bond_model", quote_currency="PLN"
    )
    response = owner.post(
        f"{INSTRUMENTS}/{instrument_id}/bond-terms",
        {
            "series_code": "OTS",
            "face_value": "100",
            "maturity_date": "2028-01-01",
            "interest_mode": "capitalized_annually",
            "tax_rate": "19",
            "fee_mode": "",
            "fee_value": "",
        },
        token_from=f"{INSTRUMENTS}/{instrument_id}",
    )
    assert response.status_code == 400
    assert "Add at least one interest period." in response.text
    assert "data-bond-panel" in response.text


def test_bond_terms_need_a_bond_model_instrument(owner: OwnerApp) -> None:
    stocks = _class(owner, "Stocks")
    instrument_id = _instrument(owner, "World ETF", stocks, type="etf")
    response = owner.post(
        f"{INSTRUMENTS}/{instrument_id}/bond-terms",
        {
            "series_code": "OTS",
            "face_value": "100",
            "maturity_date": "2028-01-01",
            "interest_mode": "capitalized_annually",
            "period_from": "2026-01-01",
            "period_rate": "5",
        },
        token_from=f"{INSTRUMENTS}/{instrument_id}",
    )
    assert response.status_code == 400
    assert "Bond terms are only available for a bond-model instrument." in response.text
