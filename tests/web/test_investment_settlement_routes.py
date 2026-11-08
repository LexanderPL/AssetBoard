"""Amounts in the base currency over HTTP (F6.2a): form modes, FX pre-fill, history display."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.db.models.identity import Workspace
from assetboard.db.models.investments import AssetClass, Instrument, InvestmentTransaction
from assetboard.db.models.prices import FxRate
from tests.conftest import OwnerApp

NOW = "2026-03-10T12:00:00+00:00"
TX = "/investments/transactions"
INSTRUMENTS = "/investments/instruments"
CLASSES = "/investments/classes"
FX = "/investments/fx-rate"


@pytest.fixture(autouse=True)
def _empty_investments(owner: OwnerApp) -> None:
    clock = FixedClock(NOW)
    owner.app.state.clock = clock
    owner.app.state.session_factory.configure(info={"clock": clock})
    owner.clear_asset_classes()


def _base(owner: OwnerApp) -> str:
    with owner.db() as session:
        return session.scalars(
            select(Workspace.base_currency).where(Workspace.id == owner.home_id)
        ).one()


def _instrument(owner: OwnerApp, name: str, quote_currency: str, type: str = "etf") -> UUID:
    with owner.db() as session:
        stocks = session.scalars(select(AssetClass.id).where(AssetClass.name == "Stocks")).first()
    if stocks is None:
        response = owner.post(
            CLASSES, {"name": "Stocks", "color": "#2563eb", "investable": "1"}, token_from=CLASSES
        )
        assert response.status_code == 303, response.text
        with owner.db() as session:
            stocks = session.scalars(select(AssetClass.id).where(AssetClass.name == "Stocks")).one()
    response = owner.post(
        INSTRUMENTS,
        {
            "name": name,
            "symbol": name[:4].upper(),
            "type": type,
            "quote_currency": quote_currency,
            "unit_label": "share",
            f"weight_{stocks}": "100",
        },
        token_from=INSTRUMENTS,
    )
    assert response.status_code == 303, response.text
    with owner.db() as session:
        return session.scalars(select(Instrument.id).where(Instrument.name == name)).one()


def _stored_rate(owner: OwnerApp, quote: str, day: date, rate: str, source: str = "ecb") -> None:
    with owner.db() as session, session.begin():
        session.add(
            FxRate(
                workspace_id=owner.home_id,
                base_currency=_base(owner),
                quote_currency=quote,
                date=day,
                rate=Decimal(rate),
                source=source,
            )
        )


def _post(owner: OwnerApp, instrument: UUID, **fields: str) -> Any:
    data = {
        "instrument_id": str(instrument),
        "type": "buy",
        "date": "2026-03-01",
        "quantity": "4",
        "unit_price": "",
        "fee": "0",
        "fx_to_base": "4.0123",
        "note": "",
        "amounts": "base",
        "base_unit_price": "1600.00",
        "base_fee": "5.00",
    } | fields
    return owner.post(TX, data, token_from=TX)


def _rows(owner: OwnerApp) -> list[InvestmentTransaction]:
    with owner.db() as session:
        return list(session.scalars(select(InvestmentTransaction)))


def test_the_form_offers_amounts_in_quote_or_base_currency(owner: OwnerApp) -> None:
    usd = _instrument(owner, "US ETF", "USD")
    base = _base(owner)
    page = owner.client.get(TX).text
    assert "data-amounts-choice" in page
    assert 'name="base_unit_price"' in page and 'name="base_fee"' in page
    assert f"Unit price in {base}" in page
    instrument_page = owner.client.get(f"{INSTRUMENTS}/{usd}").text
    assert 'name="amounts" value="base"' in instrument_page
    assert "<span data-amounts-quote-label>USD</span>" in instrument_page
    local = _instrument(owner, "Local ETF", base)
    local_page = owner.client.get(f"{INSTRUMENTS}/{local}").text
    assert "data-amounts-choice" not in local_page
    assert '<input type="hidden" name="amounts" value="quote">' in local_page


def test_a_base_mode_buy_is_saved_and_shown_with_its_derived_quote_price(owner: OwnerApp) -> None:
    usd = _instrument(owner, "US ETF", "USD")
    base = _base(owner)
    response = _post(owner, usd)
    assert response.status_code == 303, response.text
    [row] = _rows(owner)
    assert (row.settlement_currency, row.settlement_fee_minor) == (base, 500)
    assert row.settlement_unit_price == Decimal("1600.00")
    assert str(row.unit_price) == "398.773770655235151908"
    history = owner.client.get(f"{INSTRUMENTS}/{usd}").text
    assert "data-settled" in history
    assert f"1600 {base}" in history
    assert "≈ 398.773771 USD @ 4.0123" in history
    assert "data-fx-check" not in history


def test_a_bad_base_amount_or_a_refused_mode_returns_an_inline_error(owner: OwnerApp) -> None:
    usd = _instrument(owner, "US ETF", "USD")
    response = _post(owner, usd, base_unit_price="abc")
    assert response.status_code == 400
    assert "Enter a unit price of zero or more." in response.text
    local = _instrument(owner, "Local ETF", _base(owner))
    response = _post(owner, local)
    assert response.status_code == 400
    assert "only for instruments quoted in another currency" in response.text
    assert _rows(owner) == []


def test_the_server_pre_fills_stored_fx_and_reports_a_missing_rate(owner: OwnerApp) -> None:
    usd = _instrument(owner, "US ETF", "USD")
    page = owner.client.get(f"{INSTRUMENTS}/{usd}").text
    assert "No stored FX rate for this date; enter the rate." in page
    response = _post(owner, usd, fx_to_base="")
    assert response.status_code == 400
    assert "FX rate needed" in response.text
    _stored_rate(owner, "USD", date(2026, 3, 9), "3.95")
    page = owner.client.get(f"{INSTRUMENTS}/{usd}").text
    assert re.search(r'name="fx_to_base"[^>]*value="3\.95"', page)
    assert "Stored rate of Mar 9, 2026 (ECB)." in page
    response = _post(owner, usd, fx_to_base="", date="2026-03-09")
    assert response.status_code == 303
    assert _rows(owner)[0].fx_to_base == Decimal("3.95")


def test_the_fx_endpoint_re_fills_for_a_date_and_is_workspace_scoped(owner: OwnerApp) -> None:
    usd = _instrument(owner, "US ETF", "USD")
    local = _instrument(owner, "Local ETF", _base(owner))
    _stored_rate(owner, "USD", date(2026, 1, 5), "4.10", source="manual")
    _stored_rate(owner, "USD", date(2026, 2, 5), "4.00")
    early = owner.client.get(FX, params={"instrument_id": str(usd), "date": "2026-01-20"}).json()
    assert early["rate"] == "4.1" and early["as_of"] == "2026-01-05"
    assert early["source"] == "manual" and "manual" in early["hint"]
    late = owner.client.get(FX, params={"instrument_id": str(usd), "date": "2026-02-20"}).json()
    assert (late["rate"], late["source"]) == ("4", "ecb")
    none = owner.client.get(FX, params={"instrument_id": str(usd), "date": "2025-12-01"}).json()
    assert none["rate"] == "" and none["asked"] is True
    same = owner.client.get(FX, params={"instrument_id": str(local), "date": "2026-02-20"}).json()
    assert same == {"asked": False, "rate": "", "as_of": "", "source": "", "hint": ""}
    other = owner.add_workspace("Other")
    owner.join(owner.admin_id, other, "owner")
    owner.post("/workspace/switch", {"workspace_id": str(other)})
    response = owner.client.get(FX, params={"instrument_id": str(usd), "date": "2026-02-20"})
    assert response.status_code == 404


def test_editing_opens_in_the_saved_mode_and_pre_fills_the_other(owner: OwnerApp) -> None:
    usd = _instrument(owner, "US ETF", "USD")
    _post(owner, usd)
    _post(
        owner,
        usd,
        amounts="quote",
        unit_price="400",
        fee="1.50",
        fx_to_base="4",
        base_unit_price="",
        base_fee="0",
    )
    based, quoted = sorted(_rows(owner), key=lambda row: row.settlement_currency is None)
    edit = owner.client.get(f"{TX}/{based.id}/edit", headers={"HX-Request": "true"}).text
    assert re.search(r'name="amounts" value="base" checked', edit)
    assert 'name="base_unit_price" type="text" inputmode="decimal" value="1600"' in edit
    assert re.search(r'name="unit_price"[^>]*value="398\.773770655235151908"', edit)
    edit = owner.client.get(f"{TX}/{quoted.id}/edit", headers={"HX-Request": "true"}).text
    assert re.search(r'name="amounts" value="quote" checked', edit)
    # quote -> base pre-fill: 400 × 4 and 1.50 × 4.
    assert 'name="base_unit_price" type="text" inputmode="decimal" value="1600"' in edit
    assert 'name="base_fee" type="text" inputmode="decimal" value="6.00"' in edit


def test_the_check_fx_rate_badge_and_filter(owner: OwnerApp) -> None:
    usd = _instrument(owner, "US ETF", "USD")
    _post(owner, usd, amounts="quote", unit_price="1600", fx_to_base="1", base_unit_price="")
    _post(owner, usd, amounts="quote", unit_price="400", fx_to_base="4", date="2026-03-02")
    page = owner.client.get(TX).text
    assert page.count("data-fx-check>") == 1
    assert "check FX rate" in page
    assert owner.client.get(f"{INSTRUMENTS}/{usd}").text.count("data-fx-check>") == 1
    filtered = owner.client.get(TX, params={"fx": "check"}).text
    assert "Showing 1 transaction." in filtered
    assert 'name="fx" type="checkbox" value="check" checked' in filtered
