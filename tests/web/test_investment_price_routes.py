"""Manual price entry and the instrument prices panel over HTTP (F6.3)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.db.models.investments import AssetClass, Instrument
from assetboard.db.models.prices import PricePoint
from tests.conftest import OwnerApp

NOW = "2026-03-10T12:00:00+00:00"
INSTRUMENTS = "/investments/instruments"
CLASSES = "/investments/classes"
TX = "/investments/transactions"


@pytest.fixture(autouse=True)
def _fixed_clock(owner: OwnerApp) -> None:
    clock = FixedClock(NOW)
    owner.app.state.clock = clock
    owner.app.state.session_factory.configure(info={"clock": clock})
    owner.clear_asset_classes()


def _class_id(owner: OwnerApp, name: str = "Stocks") -> UUID:
    with owner.db() as session:
        row = session.scalars(select(AssetClass.id).where(AssetClass.name == name)).first()
        if row is not None:
            return row
    response = owner.post(
        CLASSES, {"name": name, "color": "#2563eb", "investable": "1"}, token_from=CLASSES
    )
    assert response.status_code == 303, response.text
    with owner.db() as session:
        return session.scalars(select(AssetClass.id).where(AssetClass.name == name)).one()


def _instrument(
    owner: OwnerApp,
    name: str,
    *,
    type_value: str = "etf",
    quote_currency: str = "EUR",
) -> UUID:
    response = owner.post(
        INSTRUMENTS,
        {
            "name": name,
            "symbol": "TEST",
            "type": type_value,
            "quote_currency": quote_currency,
            "unit_label": quote_currency if type_value == "currency" else "share",
            f"weight_{_class_id(owner)}": "100",
        },
        token_from=INSTRUMENTS,
    )
    assert response.status_code == 303, response.text
    with owner.db() as session:
        return session.scalars(select(Instrument.id).where(Instrument.name == name)).one()


def _price(
    owner: OwnerApp,
    instrument: UUID,
    *,
    date_value: str = "2026-03-01",
    price: str = "100",
    price_id: str = "",
    token_from: str | None = None,
) -> Any:
    return owner.post(
        f"{INSTRUMENTS}/{instrument}/prices",
        {"price_id": price_id, "date": date_value, "price": price},
        token_from=token_from or f"{INSTRUMENTS}/{instrument}",
    )


def _htmx_price(
    owner: OwnerApp,
    instrument: UUID,
    *,
    date_value: str = "2026-03-01",
    price: str = "100",
) -> Any:
    headers = {
        "HX-Request": "true",
        "X-CSRF-Token": owner.token(f"{INSTRUMENTS}/{instrument}"),
    }
    return owner.client.post(
        f"{INSTRUMENTS}/{instrument}/prices",
        data={"price_id": "", "date": date_value, "price": price},
        headers=headers,
        follow_redirects=False,
    )


def _point_id(owner: OwnerApp, instrument: UUID) -> UUID:
    with owner.db() as session:
        return session.scalars(
            select(PricePoint.id).where(
                PricePoint.instrument_id == instrument, PricePoint.source == "manual"
            )
        ).one()


def _stored_provider_point(
    owner: OwnerApp, instrument: UUID, *, day: date = date(2026, 3, 1), price: str = "5"
) -> None:
    with owner.db() as session, session.begin():
        session.add(
            PricePoint(
                workspace_id=owner.home_id,
                instrument_id=instrument,
                date=day,
                price=Decimal(price),
                source="provider",
            )
        )


def test_the_instrument_page_has_an_empty_prices_panel(owner: OwnerApp) -> None:
    instrument = _instrument(owner, "World ETF")
    page = owner.client.get(f"{INSTRUMENTS}/{instrument}").text
    assert "data-price-panel" in page
    assert "No price has been recorded for this instrument yet." in page
    assert "No stored or transaction prices for this instrument yet." in page
    assert "data-price-form" in page


def test_create_a_manual_price_and_show_it_with_a_chart(owner: OwnerApp) -> None:
    instrument = _instrument(owner, "World ETF")
    response = _price(owner, instrument, date_value="2026-03-01", price="120.50")
    assert (response.status_code, response.headers["location"]) == (
        303,
        f"{INSTRUMENTS}/{instrument}?notice=price_saved",
    )
    page = owner.client.get(response.headers["location"]).text
    assert "Price saved." in page
    assert "120.5 EUR" in page
    assert "Mar 1, 2026" in page
    assert 'data-price-source="manual"' in page
    assert "data-price-chart" in page
    assert "<polyline" in page


def test_saving_the_same_date_updates_the_manual_point(owner: OwnerApp) -> None:
    instrument = _instrument(owner, "World ETF")
    _price(owner, instrument, date_value="2026-03-01", price="100")
    first = _point_id(owner, instrument)
    _price(owner, instrument, date_value="2026-03-01", price="110")
    assert _point_id(owner, instrument) == first
    with owner.db() as session:
        rows = list(session.scalars(select(PricePoint)))
    assert len(rows) == 1 and rows[0].price == Decimal("110")
    page = owner.client.get(f"{INSTRUMENTS}/{instrument}").text
    assert "110 EUR" in page
    assert page.count('data-price-source="manual"') == 1


def test_a_recent_price_is_not_stale_but_an_old_one_is(owner: OwnerApp) -> None:
    recent = _instrument(owner, "Recent")
    old = _instrument(owner, "Old")
    _price(owner, recent, date_value="2026-03-08", price="1")
    _price(owner, old, date_value="2026-03-01", price="1")

    recent_page = owner.client.get(f"{INSTRUMENTS}/{recent}").text
    assert "Stale price" not in recent_page
    old_page = owner.client.get(f"{INSTRUMENTS}/{old}").text
    assert "Stale price" in old_page
    assert "older than 5 days" in old_page


def test_transaction_prices_are_shown_as_derived_rows(owner: OwnerApp) -> None:
    instrument = _instrument(owner, "World ETF")
    response = owner.post(
        TX,
        {
            "instrument_id": str(instrument),
            "type": "buy",
            "date": "2026-03-02",
            "quantity": "1",
            "unit_price": "42",
            "fee": "0",
            "fx_to_base": "1",
            "note": "",
        },
        token_from=TX,
    )
    assert response.status_code == 303, response.text

    page = owner.client.get(f"{INSTRUMENTS}/{instrument}").text
    assert "42 EUR" in page
    assert 'data-price-source="transaction"' in page
    assert "Transaction" in page
    assert "edit_price=" not in page


def test_edit_prefills_the_form_and_saves_the_existing_point(owner: OwnerApp) -> None:
    instrument = _instrument(owner, "World ETF")
    _price(owner, instrument, date_value="2026-03-01", price="100")
    point = _point_id(owner, instrument)

    page = owner.client.get(f"{INSTRUMENTS}/{instrument}?edit_price={point}").text
    assert 'value="100"' in page
    assert "Save price" in page
    assert f'name="price_id" value="{point}"' in page

    _price(
        owner,
        instrument,
        date_value="2026-03-02",
        price="105",
        price_id=str(point),
    )
    saved = owner.client.get(f"{INSTRUMENTS}/{instrument}").text
    assert "105 EUR" in saved
    assert "Mar 2, 2026" in saved


def test_delete_a_manual_price(owner: OwnerApp) -> None:
    instrument = _instrument(owner, "World ETF")
    _price(owner, instrument, date_value="2026-03-01", price="100")
    point = _point_id(owner, instrument)
    response = owner.post(
        f"{INSTRUMENTS}/{instrument}/prices/{point}/delete",
        token_from=f"{INSTRUMENTS}/{instrument}",
    )
    assert (response.status_code, response.headers["location"]) == (
        303,
        f"{INSTRUMENTS}/{instrument}?notice=price_deleted",
    )
    page = owner.client.get(response.headers["location"]).text
    assert "Price deleted." in page
    assert "No stored or transaction prices for this instrument yet." in page
    with owner.db() as session:
        assert session.scalars(select(PricePoint)).all() == []


def test_a_future_price_is_refused(owner: OwnerApp) -> None:
    instrument = _instrument(owner, "World ETF")
    response = _price(owner, instrument, date_value="2026-03-11", price="1")
    assert response.status_code == 400
    assert "Manual prices cannot be dated in the future." in response.text
    assert 'value="2026-03-11"' in response.text


def test_an_invalid_price_is_refused(owner: OwnerApp) -> None:
    instrument = _instrument(owner, "World ETF")
    response = _price(owner, instrument, price="-1")
    assert response.status_code == 400
    assert "Enter a price of zero or more, with at most 18 decimal places." in response.text


def test_an_archived_instrument_hides_the_form_and_refuses_writes(owner: OwnerApp) -> None:
    instrument = _instrument(owner, "World ETF")
    _price(owner, instrument, date_value="2026-03-01", price="100")
    point = _point_id(owner, instrument)
    archived = owner.post(
        f"{INSTRUMENTS}/{instrument}/archive", token_from=f"{INSTRUMENTS}/{instrument}"
    )
    assert archived.status_code == 303, archived.text

    page = owner.client.get(f"{INSTRUMENTS}/{instrument}").text
    assert "This instrument is archived. Restore it before changing its prices." in page
    assert "data-price-form" not in page
    assert "edit_price=" not in page

    refused = _price(owner, instrument, date_value="2026-03-02", price="2")
    assert refused.status_code == 400
    assert "This instrument is archived; restore it before changing its prices." in refused.text

    refused_delete = owner.post(
        f"{INSTRUMENTS}/{instrument}/prices/{point}/delete",
        token_from=f"{INSTRUMENTS}/{instrument}",
    )
    assert refused_delete.status_code == 409
    assert (
        "This instrument is archived; restore it before changing its prices." in refused_delete.text
    )


def test_a_viewer_cannot_see_the_form_or_post(owner: OwnerApp) -> None:
    instrument = _instrument(owner, "World ETF")
    owner.set_admin_role("viewer")
    page = owner.client.get(f"{INSTRUMENTS}/{instrument}").text
    assert "data-price-panel" in page
    assert "data-price-form" not in page

    response = _price(owner, instrument, date_value="2026-03-01", price="1")
    assert response.status_code == 403


def test_a_currency_instrument_has_no_manual_price_form(owner: OwnerApp) -> None:
    instrument = _instrument(owner, "US dollars", type_value="currency", quote_currency="USD")
    page = owner.client.get(f"{INSTRUMENTS}/{instrument}").text
    assert "A currency instrument is always priced at 1 in its own currency." in page
    assert "data-price-form" not in page
    assert "data-price-empty" not in page


def test_provider_points_are_labelled_and_not_editable(owner: OwnerApp) -> None:
    instrument = _instrument(owner, "World ETF")
    _stored_provider_point(owner, instrument, price="5")
    page = owner.client.get(f"{INSTRUMENTS}/{instrument}").text
    assert "Provider" in page
    assert "5 EUR" in page
    assert "edit_price=" not in page


def test_an_htmx_save_returns_the_instrument_fragment(owner: OwnerApp) -> None:
    instrument = _instrument(owner, "World ETF")
    response = _htmx_price(owner, instrument, date_value="2026-03-01", price="77")
    assert response.status_code == 200
    assert "<html" not in response.text
    assert "data-price-panel" in response.text
    assert "77 EUR" in response.text
