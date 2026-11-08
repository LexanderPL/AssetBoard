"""Investments UI fixes over HTTP: the instrument add/edit forms, the transaction form's FX
error and pre-fills, the bond unit-price help and the average-cost display."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.db.models.identity import Workspace
from assetboard.db.models.investments import (
    AssetClass,
    BondTerms,
    Instrument,
    InvestmentTransaction,
)
from assetboard.db.models.prices import FxRate
from tests.conftest import OwnerApp

NOW = "2026-03-10T12:00:00+00:00"
INSTRUMENTS = "/investments/instruments"
TRANSACTIONS = "/investments/transactions"
HTMX = {"HX-Request": "true"}


@pytest.fixture(autouse=True)
def _fixed_clock(owner: OwnerApp) -> None:
    clock = FixedClock(NOW)
    owner.app.state.clock = clock
    owner.app.state.session_factory.configure(info={"clock": clock})


def _stocks(owner: OwnerApp) -> UUID:
    with owner.db() as session:
        return session.scalars(
            select(AssetClass.id).where(
                AssetClass.workspace_id == owner.home_id, AssetClass.name == "Stocks"
            )
        ).one()


def _instrument_form(name: str, stocks: UUID, **changes: str) -> dict[str, str]:
    return {
        "name": name,
        "symbol": "",
        "type": "etf",
        "quote_currency": "EUR",
        "unit_label": "",
        "provider_key": "",
        "provider_symbol": "",
        f"weight_{stocks}": "100",
    } | changes


def _create(owner: OwnerApp, name: str, **changes: str) -> UUID:
    response = owner.post(
        INSTRUMENTS, _instrument_form(name, _stocks(owner), **changes), token_from=INSTRUMENTS
    )
    assert response.status_code == 303, response.text
    with owner.db() as session:
        return session.scalars(select(Instrument.id).where(Instrument.name == name)).one()


def _htmx_post(owner: OwnerApp, path: str, data: dict[str, str], token_from: str):
    form = {"csrf_token": owner.token(token_from)} | data
    return owner.client.post(path, data=form, headers=HTMX)


def _base_to_pln(owner: OwnerApp) -> None:
    with owner.db() as session, session.begin():
        workspace = session.get(Workspace, owner.home_id)
        assert workspace is not None
        workspace.base_currency = "PLN"


def _row(page: str, instrument: UUID) -> str:
    match = re.search(rf'<li id="instrument-{instrument}".*?</li>', page, re.S)
    assert match is not None
    return match.group(0)


# 1, 2, 3: adding an instrument


def test_a_successful_add_shows_only_the_notice_and_scrolls_to_it(owner: OwnerApp) -> None:
    response = _htmx_post(
        owner, INSTRUMENTS, _instrument_form("World ETF", _stocks(owner)), INSTRUMENTS
    )

    assert response.status_code == 200
    body = response.text
    assert body.lstrip().startswith('<div id="investments-body"')
    assert "Instrument added." in body and "data-notice" in body
    assert "data-error" not in body and "data-field-error" not in body
    # The client-side warning starts hidden; the script shows it only once a weight is typed.
    assert re.search(r'class="hidden [^"]*" data-allocation-warning', body)
    # The add form closes again and the swap shows the top of the section, where the notice is.
    assert re.search(r"<details data-add-instrument>", body)
    assert 'hx-swap="outerHTML show:#investments-body:top"' in body
    assert body.index("data-notice") < body.index("data-add-instrument")


def test_the_add_form_sits_behind_a_button_and_reopens_on_errors(owner: OwnerApp) -> None:
    page = owner.client.get(INSTRUMENTS).text
    assert re.search(
        r"<details data-add-instrument>\s*<summary[^>]*>Add instrument</summary>", page
    )
    assert 'id="new-instrument-name"' in page

    refused = owner.post(
        INSTRUMENTS,
        _instrument_form("", _stocks(owner), symbol="KEEP.ME", unit_label="unit"),
        token_from=INSTRUMENTS,
    )

    assert refused.status_code == 400
    assert "<details data-add-instrument open>" in refused.text
    assert 'value="KEEP.ME"' in refused.text and 'value="unit"' in refused.text
    assert "Enter a name of 1 to 200 characters" in refused.text


def test_viewers_get_no_add_button(owner: OwnerApp) -> None:
    owner.set_admin_role("viewer")
    page = owner.client.get(INSTRUMENTS).text
    assert "data-add-instrument" not in page and "data-instrument-form" not in page


# 4: editing in place


def test_edit_opens_in_the_row_and_cancel_restores_it(owner: OwnerApp) -> None:
    first = _create(owner, "First fund")
    second = _create(owner, "Second fund")
    page = owner.client.get(INSTRUMENTS).text
    link = re.search(rf'<a href="[^"]*\?edit={second}#instrument-{second}" hx-get="([^"]+)"', page)
    assert link is not None
    assert f'hx-swap="outerHTML show:#instrument-{second}:top"' in page

    edit = owner.client.get(link.group(1), headers=HTMX)

    assert edit.status_code == 200
    row = _row(edit.text, second)
    assert "data-editing" in row and f'id="edit-{second}-form"' in row
    assert 'value="Second fund"' in row
    assert "data-editing" not in _row(edit.text, first)
    cancel = re.search(r'hx-get="([^"]+/row)"', row)
    assert cancel is not None

    restored = owner.client.get(cancel.group(1), headers=HTMX)

    assert restored.status_code == 200
    assert "data-editing" not in restored.text
    assert 'data-instrument-name="Second fund"' in restored.text


def test_edit_without_javascript_lands_on_the_row(owner: OwnerApp) -> None:
    instrument = _create(owner, "World ETF")
    plain = owner.client.get(f"{INSTRUMENTS}/{instrument}/edit", follow_redirects=False)
    assert plain.status_code == 303
    assert plain.headers["location"] == f"{INSTRUMENTS}?edit={instrument}#instrument-{instrument}"
    page = owner.client.get(f"{INSTRUMENTS}?edit={instrument}").text
    assert "data-editing" in _row(page, instrument)
    cancel = owner.client.get(f"{INSTRUMENTS}/{instrument}/row", follow_redirects=False)
    assert cancel.headers["location"] == f"{INSTRUMENTS}#instrument-{instrument}"


def test_a_refused_edit_stays_in_the_row_with_its_values(owner: OwnerApp) -> None:
    instrument = _create(owner, "World ETF")
    response = _htmx_post(
        owner,
        f"{INSTRUMENTS}/{instrument}",
        _instrument_form("World ETF", _stocks(owner), symbol="x" * 40),
        f"{INSTRUMENTS}?edit={instrument}",
    )
    assert response.status_code == 400
    row = _row(response.text, instrument)
    assert "data-editing" in row
    assert "Enter a symbol of at most 32 characters." in row
    assert 'value="' + "x" * 40 + '"' in row


def test_a_saved_edit_shows_the_updated_row(owner: OwnerApp) -> None:
    instrument = _create(owner, "World ETF")
    response = _htmx_post(
        owner,
        f"{INSTRUMENTS}/{instrument}",
        _instrument_form("World ETF Acc", _stocks(owner)),
        f"{INSTRUMENTS}?edit={instrument}",
    )
    assert response.status_code == 200
    assert "Instrument saved." in response.text
    assert "data-editing" not in response.text
    assert 'data-instrument-name="World ETF Acc"' in response.text


# 5: bond face value as the default unit price


def test_bond_unit_price_defaults_to_the_face_value_with_help(owner: OwnerApp) -> None:
    bond = _create(owner, "Savings bond", type="bond_model", unit_label="bond")
    page = owner.client.get(f"{INSTRUMENTS}/{bond}").text
    assert re.search(r'id="transaction-price"[^>]*value=""', page)
    assert "data-bond-price-help" in page
    with owner.db() as session, session.begin():
        session.add(
            BondTerms(
                workspace_id=owner.home_id,
                instrument_id=bond,
                series_code="OTS",
                face_value_minor=10_000,
                maturity_date=date(2027, 1, 1),
                interest_mode="capitalized_annually",
                rate_schedule=[{"from_date": "2026-01-01", "annual_rate_bp": 500}],
                tax_rate_bp=1_900,
            )
        )

    page = owner.client.get(f"{INSTRUMENTS}/{bond}").text

    assert re.search(r'id="transaction-price"[^>]*value="100.00"', page)
    assert "Face value is the nominal the interest accrues on" in page
    etf = _create(owner, "World ETF")
    assert "data-bond-price-help" not in owner.client.get(f"{INSTRUMENTS}/{etf}").text


# 6: missing FX is an inline error; stored FX is the default


def _buy(instrument: UUID, return_to: str, **changes: str) -> dict[str, str]:
    return {
        "instrument_id": str(instrument),
        "type": "buy",
        "date": "2026-03-09",
        "quantity": "2,5",
        "unit_price": "101,25",
        "fee": "1,50",
        "fx_to_base": "",
        "note": "keep me",
        "return_to": return_to,
    } | changes


@pytest.mark.parametrize("htmx", [True, False])
@pytest.mark.parametrize("return_to", ["list", "instrument"])
def test_missing_fx_shows_an_inline_error_and_keeps_every_value(
    owner: OwnerApp, htmx: bool, return_to: str
) -> None:
    _base_to_pln(owner)
    etf = _create(owner, "VWCE", quote_currency="EUR")
    owner.client.headers["Accept-Language"] = "pl"
    page = TRANSACTIONS if return_to == "list" else f"{INSTRUMENTS}/{etf}"
    form = {"csrf_token": owner.token(page)} | _buy(etf, return_to)

    response = owner.client.post(TRANSACTIONS, data=form, headers=HTMX if htmx else {})

    assert response.status_code == 400
    text = response.text
    fx = re.search(r'<div class="space-y-1.5" data-transaction-fx>.*?</div>', text, re.S)
    assert fx is not None
    assert "Kurs walutowy do PLN" in fx.group(0)
    assert 'data-field-error="fx_to_base"' in fx.group(0)
    assert 'aria-invalid="true"' in fx.group(0) and "data-autofocus" in fx.group(0)
    for kept in ('value="2,5"', 'value="101,25"', 'value="1,50"', 'value="keep me"'):
        assert kept in text
    assert 'value="2026-03-09"' in text
    with owner.db() as session:
        assert session.scalars(select(InvestmentTransaction.id)).all() == []


def test_stored_fx_prefills_the_new_transaction_but_not_transaction_rates(
    owner: OwnerApp,
) -> None:
    _base_to_pln(owner)
    etf = _create(owner, "VWCE", quote_currency="EUR")
    usd = _create(owner, "US fund", quote_currency="USD")
    page = owner.client.get(f"{INSTRUMENTS}/{etf}").text
    assert re.search(r'id="transaction-fx"[^>]*value=""', page)
    with owner.db() as session, session.begin():
        for day, rate, source in (
            (date(2026, 3, 6), "4.2811", "ecb"),
            (date(2026, 3, 9), "4.2950", "nbp"),
            (date(2026, 3, 11), "9.9999", "nbp"),  # after today: ignored
        ):
            session.add(
                FxRate(
                    workspace_id=owner.home_id,
                    base_currency="PLN",
                    quote_currency="EUR",
                    date=day,
                    rate=Decimal(rate),
                    source=source,
                )
            )

    instrument_page = owner.client.get(f"{INSTRUMENTS}/{etf}").text
    assert re.search(r'id="transaction-fx"[^>]*value="4.295"', instrument_page)
    listing = owner.client.get(TRANSACTIONS).text
    assert re.search(rf'<option value="{etf}"[^>]*data-fx="4.295"', listing)
    assert re.search(rf'<option value="{usd}"[^>]*data-fx=""', listing)

    # A USD trade with its own FX is a transaction rate: it never becomes the default.
    created = owner.post(
        TRANSACTIONS,
        _buy(usd, "list", quantity="1", unit_price="10", fee="0", fx_to_base="3.9"),
        token_from=TRANSACTIONS,
    )
    assert created.status_code == 303
    assert re.search(
        r'id="transaction-fx"[^>]*value=""', owner.client.get(f"{INSTRUMENTS}/{usd}").text
    )


def test_a_base_currency_instrument_has_no_fx_field(owner: OwnerApp) -> None:
    etf = _create(owner, "Home fund")
    page = owner.client.get(f"{INSTRUMENTS}/{etf}").text
    assert "data-transaction-fx" not in page
    assert '<input type="hidden" name="fx_to_base" value="1">' in page


# 7: the average cost is money


def test_average_cost_is_money_rounded_once(owner: OwnerApp) -> None:
    etf = _create(owner, "World ETF")
    created = owner.post(
        TRANSACTIONS,
        _buy(etf, "instrument", quantity="3", unit_price="10", fee="0,01", fx_to_base="1"),
        token_from=f"{INSTRUMENTS}/{etf}",
    )
    assert created.status_code == 303, created.text
    page = owner.client.get(f"{INSTRUMENTS}/{etf}").text
    average = re.search(r"data-avg-cost>(.*?)</dd>", page)
    assert average is not None
    assert average.group(1) == "€10.00"  # 30.01 / 3 = 10.00333… → 10.00
