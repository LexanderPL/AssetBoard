"""Investment transaction history over HTTP (F6.2): create, edit, delete, undo and filters."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select

from assetboard.clock import FixedClock
from assetboard.db.models.investments import AssetClass, Instrument
from assetboard.db.models.ledger import Category, Transaction
from tests.conftest import OwnerApp

NOW = "2026-03-10T12:00:00+00:00"
TX = "/investments/transactions"
INSTRUMENTS = "/investments/instruments"
CLASSES = "/investments/classes"


@pytest.fixture(autouse=True)
def _empty_investments(owner: OwnerApp) -> None:
    clock = FixedClock(NOW)
    owner.app.state.clock = clock
    owner.app.state.session_factory.configure(info={"clock": clock})
    owner.clear_asset_classes()


def _class_id(owner: OwnerApp, name: str) -> UUID:
    with owner.db() as session:
        return session.scalars(select(AssetClass.id).where(AssetClass.name == name)).one()


def _instrument_id(owner: OwnerApp, name: str) -> UUID:
    with owner.db() as session:
        return session.scalars(select(Instrument.id).where(Instrument.name == name)).one()


def _class(owner: OwnerApp, name: str = "Stocks") -> UUID:
    response = owner.post(
        CLASSES, {"name": name, "color": "#2563eb", "investable": "1"}, token_from=CLASSES
    )
    assert response.status_code == 303, response.text
    return _class_id(owner, name)


def _instrument(
    owner: OwnerApp,
    name: str,
    type: str = "etf",
    quote_currency: str = "EUR",
    unit_label: str = "share",
) -> UUID:
    stocks = (
        _class(owner, "Stocks") if not _has_class(owner, "Stocks") else _class_id(owner, "Stocks")
    )
    response = owner.post(
        INSTRUMENTS,
        {
            "name": name,
            "symbol": "TEST",
            "type": type,
            "quote_currency": quote_currency,
            "unit_label": unit_label,
            f"weight_{stocks}": "100",
        },
        token_from=INSTRUMENTS,
    )
    assert response.status_code == 303, response.text
    return _instrument_id(owner, name)


def _has_class(owner: OwnerApp, name: str) -> bool:
    with owner.db() as session:
        return (
            session.scalars(select(AssetClass.id).where(AssetClass.name == name)).first()
            is not None
        )


def _tx(
    owner: OwnerApp,
    instrument_id: UUID,
    *,
    type: str = "buy",
    date: str = "2026-03-01",
    quantity: str = "10",
    unit_price: str = "100",
    fee: str = "0",
    fx: str = "",
    note: str = "",
    return_to: str = "",
) -> Any:
    return owner.post(
        TX,
        {
            "instrument_id": str(instrument_id),
            "type": type,
            "date": date,
            "quantity": quantity,
            "unit_price": unit_price,
            "fee": fee,
            "fx_to_base": fx,
            "note": note,
            "return_to": return_to,
        },
        token_from=TX,
    )


def _interest_id(owner: OwnerApp) -> UUID:
    with owner.db() as session:
        return session.scalars(
            select(Category.id).where(Category.name == "Interest", Category.kind == "income")
        ).one()


def _dividend(
    owner: OwnerApp,
    instrument_id: UUID,
    *,
    gross: str = "10",
    withholding: str = "1",
    record_income: bool = True,
    category_id: UUID | None = None,
    return_to: str = "",
    fx: str = "",
) -> Any:
    return owner.post(
        TX,
        {
            "instrument_id": str(instrument_id),
            "type": "dividend",
            "date": "2026-03-01",
            "quantity": "",
            "unit_price": "",
            "fee": "0",
            "fx_to_base": fx,
            "note": "",
            "return_to": return_to,
            "dividend_gross": gross,
            "dividend_withholding": withholding,
            "record_income": "true" if record_income else "",
            "income_category_id": str(category_id) if category_id is not None else "",
            "split_new": "",
            "split_old": "",
        },
        token_from=TX,
    )


def _dialog(page: str) -> str:
    match = re.search(r'<dialog id="transaction-dialog".*?</dialog>', page, re.S)
    assert match is not None, "the instrument transaction dialog is missing"
    return match.group(0)


def _split(
    owner: OwnerApp,
    instrument_id: UUID,
    *,
    new: str,
    old: str,
    return_to: str = "",
) -> Any:
    return owner.post(
        TX,
        {
            "instrument_id": str(instrument_id),
            "type": "split",
            "date": "2026-03-01",
            "quantity": "",
            "unit_price": "",
            "fee": "0",
            "fx_to_base": "",
            "note": "",
            "return_to": return_to,
            "dividend_gross": "",
            "dividend_withholding": "0",
            "record_income": "",
            "income_category_id": "",
            "split_new": new,
            "split_old": old,
        },
        token_from=TX,
    )


def _control(html: str, element_id: str) -> str:
    match = re.search(rf'<(?:input|select|textarea)[^>]*\bid="{element_id}"[^>]*>', html)
    assert match is not None, f"the control #{element_id} is missing"
    return match.group(0)


def _control_by_name(html: str, name: str) -> str:
    match = re.search(rf'<input[^>]*\bname="{name}"[^>]*>', html)
    assert match is not None, f"the control named {name} is missing"
    return match.group(0)


def _disabled(html: str, element_id: str) -> bool:
    return " disabled" in _control(html, element_id)


def _disabled_by_name(html: str, name: str) -> bool:
    return " disabled" in _control_by_name(html, name)


def _htmx(owner: OwnerApp, path: str, data: dict[str, str] | None = None) -> Any:
    headers = {"HX-Request": "true", "X-CSRF-Token": owner.token(TX)}
    return owner.client.post(path, data=data or {}, headers=headers, follow_redirects=False)


def test_the_transactions_tab_and_empty_page(owner: OwnerApp) -> None:
    page = owner.client.get(TX).text
    assert "Transaction history" in page
    assert "data-investment-transactions-tab" in page
    assert re.search(r'<nav class="tab-strip" aria-label="Investments"', page)
    assert 'role="tablist"' not in page and 'role="tab"' not in page
    assert "No investment transactions match these filters." in page
    tab = owner.client.get(INSTRUMENTS).text
    assert 'href="/investments/transactions"' in tab


def test_the_filters_are_a_disclosure_above_the_history(owner: OwnerApp) -> None:
    page = owner.client.get(f"{TX}?type=buy&from=2026-03-01").text
    assert '<details class="panel" data-filter-disclosure open>' in page
    summary = page.split("data-filter-disclosure", 1)[1].split("</summary>", 1)[0]
    assert "Filters" in summary and "2 filters" in summary
    assert "data-transaction-filters" in page
    # The form is a sibling of the history panel, so a collapsed disclosure cannot hide the
    # heading, and the table is directly below the always-visible summary.
    assert page.index("data-transaction-filters") < page.index("data-transaction-history")


def test_the_add_and_bulk_forms_are_dialogs(owner: OwnerApp) -> None:
    instrument = _instrument(owner, "World ETF")
    page = owner.client.get(TX).text
    assert page.index("data-tab-strip") < page.index("data-page-actions")
    assert re.search(
        r'<a href="[^"]+" data-dialog-open="transaction-dialog"[^>]*data-add-transaction>'
        r"Add a transaction</a>",
        page,
    )
    dialog = re.search(r'<dialog id="transaction-dialog".*?</dialog>', page, re.S)
    assert dialog is not None and "data-transaction-form" in dialog.group(0)
    opening = re.search(r'<dialog id="transaction-dialog"[^>]*>', page)
    assert opening is not None and " open" not in opening.group(0)
    bulk = re.search(r'<dialog id="investment-bulk-panel".*?</dialog>', page, re.S)
    assert bulk is not None and "data-bulk-text" in bulk.group(0)
    actions = page.split("data-page-actions", 1)[1].split("</div>", 1)[0]
    assert "data-bulk-open" in actions and "data-add-transaction" in actions
    assert "data-transaction-actions" not in page
    assert "show:#investment-bulk-panel:top" not in page

    refused = _tx(owner, instrument, quantity="0")
    assert refused.status_code == 400
    reopened = re.search(r'<dialog id="transaction-dialog"[^>]*>', refused.text)
    assert reopened is not None and " open" in reopened.group(0)


def test_the_instrument_transaction_form_is_a_dialog_opened_from_the_summary(
    owner: OwnerApp,
) -> None:
    instrument = _instrument(owner, "World ETF")
    clean = f"{INSTRUMENTS}/{instrument}"
    detail = owner.client.get(clean).text
    summary = detail.split("data-holding-summary", 1)[0]
    assert (
        f'<a href="{clean}?form=transaction#transaction-dialog" '
        'data-dialog-open="transaction-dialog"' in summary
    )
    assert ">Add a transaction</a>" in summary
    opening = re.search(r'<dialog id="transaction-dialog"[^>]*>', detail)
    assert opening is not None and " open" not in opening.group(0)
    dialog = _dialog(detail)
    assert "data-transaction-form" in dialog
    assert 'data-instrument-type="etf"' in dialog
    assert 'data-instrument-currency="EUR"' in dialog

    opened = owner.client.get(f"{clean}?form=transaction")
    assert opened.status_code == 200
    opening = re.search(r'<dialog id="transaction-dialog"[^>]*>', opened.text)
    assert opening is not None and " open" in opening.group(0)
    assert f'href="{clean}" data-dialog-close' in opened.text
    assert f'<a href="{clean}" data-dialog-close class="rounded-xl' in opened.text
    assert ">Cancel</a>" in opened.text

    refused = _tx(owner, instrument, quantity="0", return_to="instrument")
    assert refused.status_code == 400
    opening = re.search(r'<dialog id="transaction-dialog"[^>]*>', refused.text)
    assert opening is not None and " open" in opening.group(0)
    assert "Enter a quantity greater than zero." in refused.text


def test_a_buy_form_disables_dividend_and_split_fields_without_hiding_them(
    owner: OwnerApp,
) -> None:
    instrument = _instrument(owner, "US fund", quote_currency="USD")
    form = _dialog(owner.client.get(f"{INSTRUMENTS}/{instrument}").text)
    for element_id in (
        "transaction-quantity",
        "transaction-price",
        "transaction-fee",
        "transaction-fx",
    ):
        assert not _disabled(form, element_id), element_id
    for element_id in (
        "transaction-dividend-gross",
        "transaction-dividend-withholding",
        "transaction-income-category",
        "transaction-split-new",
        "transaction-split-old",
    ):
        assert _disabled(form, element_id), element_id
    assert _disabled_by_name(form, "record_income")
    # The groups stay in the document; only their controls are disabled.
    assert "data-dividend-field" in form and "data-split-field" in form


def test_a_refused_buy_shows_one_error_on_the_active_control(owner: OwnerApp) -> None:
    instrument = _instrument(owner, "US fund", quote_currency="USD")
    refused = _tx(owner, instrument, quantity="0", fx="1.08", return_to="instrument")
    assert refused.status_code == 400
    form = _dialog(refused.text)
    assert form.count("data-field-error=") == 1
    assert form.count('aria-invalid="true"') == 1
    quantity = _control(form, "transaction-quantity")
    assert 'aria-invalid="true"' in quantity
    assert 'aria-describedby="transaction-quantity-error"' in quantity
    assert "data-autofocus" in quantity
    assert "aria-invalid" not in _control(form, "transaction-split-new")
    assert "aria-invalid" not in _control(form, "transaction-price")
    assert "aria-invalid" not in _control(form, "transaction-dividend-gross")


def test_a_refused_dividend_form_enables_dividend_fields_and_disables_the_rest(
    owner: OwnerApp,
) -> None:
    instrument = _instrument(owner, "US fund", quote_currency="USD")
    refused = _dividend(
        owner,
        instrument,
        gross="10",
        withholding="1",
        record_income=True,
        category_id=None,
        return_to="instrument",
        fx="1.08",
    )
    assert refused.status_code == 400
    form = _dialog(refused.text)
    assert not _disabled(form, "transaction-dividend-gross")
    assert not _disabled(form, "transaction-dividend-withholding")
    assert not _disabled_by_name(form, "record_income")
    assert not _disabled(form, "transaction-income-category")
    assert not _disabled(form, "transaction-fx")
    for element_id in (
        "transaction-quantity",
        "transaction-price",
        "transaction-fee",
        "transaction-split-new",
        "transaction-split-old",
    ):
        assert _disabled(form, element_id), element_id
    assert form.count("data-field-error=") == 1
    assert form.count('aria-invalid="true"') == 1
    category = _control(form, "transaction-income-category")
    assert 'aria-invalid="true"' in category
    assert 'aria-describedby="transaction-income-category-error"' in category
    assert "data-autofocus" in category
    assert "aria-invalid" not in _control(form, "transaction-quantity")
    assert "aria-invalid" not in _control(form, "transaction-dividend-gross")

    unrecorded = _dividend(
        owner,
        instrument,
        gross="0",
        withholding="0",
        record_income=False,
        return_to="instrument",
    )
    assert unrecorded.status_code == 400
    form = _dialog(unrecorded.text)
    assert _disabled(form, "transaction-income-category")
    assert not _disabled(form, "transaction-dividend-gross")
    assert not _disabled_by_name(form, "record_income")
    assert form.count("data-field-error=") == 1
    assert 'aria-invalid="true"' in _control(form, "transaction-dividend-gross")
    assert "aria-invalid" not in _control(form, "transaction-income-category")


def test_a_refused_split_form_disables_trade_dividend_and_fx_fields(
    owner: OwnerApp,
) -> None:
    instrument = _instrument(owner, "US fund", quote_currency="USD")
    refused = _split(owner, instrument, new="", old="1", return_to="instrument")
    assert refused.status_code == 400
    form = _dialog(refused.text)
    assert not _disabled(form, "transaction-split-new")
    assert not _disabled(form, "transaction-split-old")
    for element_id in (
        "transaction-quantity",
        "transaction-price",
        "transaction-fee",
        "transaction-fx",
        "transaction-dividend-gross",
        "transaction-dividend-withholding",
        "transaction-income-category",
    ):
        assert _disabled(form, element_id), element_id
    assert _disabled_by_name(form, "record_income")
    assert form.count("data-field-error=") == 1
    assert form.count('aria-invalid="true"') == 1
    split_new = _control(form, "transaction-split-new")
    assert 'aria-invalid="true"' in split_new
    assert 'aria-describedby="transaction-split-new-error"' in split_new
    assert "data-autofocus" in split_new
    assert "aria-invalid" not in _control(form, "transaction-quantity")
    assert "aria-invalid" not in _control(form, "transaction-price")

    different = _split(owner, instrument, new="1", old="1", return_to="instrument")
    assert different.status_code == 400
    form = _dialog(different.text)
    assert form.count("data-field-error=") == 1
    assert form.count('aria-invalid="true"') == 1
    assert 'aria-invalid="true"' in _control(form, "transaction-split-old")
    assert "aria-invalid" not in _control(form, "transaction-price")


def test_the_transactions_tab_form_also_disables_inapplicable_fields(
    owner: OwnerApp,
) -> None:
    _instrument(owner, "US fund", quote_currency="USD")
    form = _dialog(owner.client.get(TX).text)
    assert not _disabled(form, "transaction-quantity")
    assert _disabled(form, "transaction-dividend-gross")
    assert _disabled(form, "transaction-split-new")


def test_the_list_form_renders_a_refused_dividend_and_split_with_their_groups(
    owner: OwnerApp,
) -> None:
    instrument = _instrument(owner, "US fund", quote_currency="USD")
    refused = _dividend(
        owner,
        instrument,
        gross="10",
        withholding="1",
        record_income=True,
        category_id=None,
        fx="1.08",
    )
    assert refused.status_code == 400
    form = _dialog(refused.text)
    assert not _disabled(form, "transaction-dividend-gross")
    assert not _disabled(form, "transaction-dividend-withholding")
    assert not _disabled_by_name(form, "record_income")
    assert not _disabled(form, "transaction-income-category")
    assert not _disabled(form, "transaction-fx")
    assert _disabled(form, "transaction-quantity")
    assert _disabled(form, "transaction-price")
    assert _disabled(form, "transaction-split-new")
    assert form.count("data-field-error=") == 1
    assert 'aria-invalid="true"' in _control(form, "transaction-income-category")

    refused = _split(owner, instrument, new="", old="1")
    assert refused.status_code == 400
    form = _dialog(refused.text)
    assert not _disabled(form, "transaction-split-new")
    assert not _disabled(form, "transaction-split-old")
    assert _disabled(form, "transaction-quantity")
    assert _disabled(form, "transaction-price")
    assert _disabled(form, "transaction-fx")
    assert _disabled(form, "transaction-dividend-gross")
    assert form.count("data-field-error=") == 1
    assert 'aria-invalid="true"' in _control(form, "transaction-split-new")


def test_a_base_currency_instrument_disables_the_list_form_fx_field(
    owner: OwnerApp,
) -> None:
    _instrument(owner, "Home ETF", quote_currency="EUR")
    form = _dialog(owner.client.get(TX).text)
    assert _disabled(form, "transaction-fx")
    assert "data-transaction-fx" in form  # disabled, not hidden


def test_inline_edit_rows_render_the_disabled_groups(owner: OwnerApp) -> None:
    instrument = _instrument(owner, "US fund", quote_currency="USD")
    _tx(owner, instrument, fx="1.08")
    with owner.db() as session:
        from assetboard.db.models.investments import InvestmentTransaction

        buy_id = session.scalars(select(InvestmentTransaction.id)).one()
    buy_edit = owner.client.get(
        f"{TX}/{buy_id}/edit?show_instrument=1&target=investment-transactions-body",
        headers={"HX-Request": "true"},
    ).text
    assert "data-transaction-form" in buy_edit
    assert not _disabled(buy_edit, "transaction-quantity")
    assert not _disabled(buy_edit, "transaction-fx")
    assert _disabled(buy_edit, "transaction-dividend-gross")
    assert _disabled(buy_edit, "transaction-split-new")
    assert _disabled_by_name(buy_edit, "record_income")

    assert (
        _dividend(
            owner,
            instrument,
            gross="10",
            withholding="1",
            category_id=_interest_id(owner),
            fx="1.08",
        ).status_code
        == 303
    )
    with owner.db() as session:
        from assetboard.db.models.investments import InvestmentTransaction

        dividend_id = session.scalars(
            select(InvestmentTransaction.id).where(InvestmentTransaction.type == "dividend")
        ).one()
    dividend_edit = owner.client.get(
        f"{TX}/{dividend_id}/edit?show_instrument=1&target=investment-transactions-body",
        headers={"HX-Request": "true"},
    ).text
    assert not _disabled(dividend_edit, "transaction-dividend-gross")
    assert not _disabled_by_name(dividend_edit, "record_income")
    assert not _disabled(dividend_edit, "transaction-income-category")
    assert _disabled(dividend_edit, "transaction-quantity")
    assert _disabled(dividend_edit, "transaction-split-new")


def test_create_a_buy_and_see_it_in_both_histories(owner: OwnerApp) -> None:
    instrument = _instrument(owner, "World ETF")
    response = _tx(owner, instrument, quantity="10", unit_price="100", fee="2.50")
    assert (response.status_code, response.headers["location"]) == (
        303,
        f"{TX}?notice=created",
    )
    page = owner.client.get(TX).text
    assert "World ETF" in page and "Buy" in page
    assert "10" in page and "€100.25" not in page  # cost is shown on the instrument page
    detail = owner.client.get(f"{INSTRUMENTS}/{instrument}").text
    assert "data-holding-summary" in detail
    assert "€1,002.50" in detail
    assert "World ETF" in detail and "Buy" in detail


def test_edit_a_transaction_and_keep_the_instrument_page(owner: OwnerApp) -> None:
    instrument = _instrument(owner, "World ETF")
    _tx(owner, instrument, quantity="10")
    with owner.db() as session:
        from assetboard.db.models.investments import InvestmentTransaction

        tx_id = session.scalars(select(InvestmentTransaction.id)).one()
    edit = owner.client.get(
        f"{TX}/{tx_id}/edit?show_instrument=1&target=investment-transactions-body",
        headers={"HX-Request": "true"},
    )
    assert (
        edit.status_code == 200
        and "data-transaction-form" in edit.text
        and "<html" not in edit.text
    )
    response = owner.post(
        f"{TX}/{tx_id}",
        {
            "type": "buy",
            "date": "2026-03-02",
            "quantity": "12",
            "unit_price": "100",
            "fee": "0",
            "fx_to_base": "",
            "note": "more",
            "return_to": "list",
        },
        token_from=TX,
    )
    assert (response.status_code, response.headers["location"]) == (
        303,
        f"{TX}?notice=saved",
    )
    page = owner.client.get(TX).text
    assert "more" in page and "12" in page


def test_a_sell_that_would_oversell_is_refused_with_the_date(owner: OwnerApp) -> None:
    instrument = _instrument(owner, "World ETF")
    _tx(owner, instrument, quantity="1", date="2026-03-01")
    response = _tx(owner, instrument, type="sell", date="2026-03-02", quantity="2")
    assert response.status_code == 400
    assert "Mar 2, 2026" in response.text
    assert "would exceed the quantity held" in response.text


def test_future_dates_are_refused_with_a_message(owner: OwnerApp) -> None:
    instrument = _instrument(owner, "World ETF")
    response = _tx(owner, instrument, date="2026-03-11")
    assert response.status_code == 400
    assert "Investment transactions cannot be dated in the future." in response.text


def test_delete_and_undo(owner: OwnerApp) -> None:
    instrument = _instrument(owner, "World ETF")
    _tx(owner, instrument)
    with owner.db() as session:
        from assetboard.db.models.investments import InvestmentTransaction

        tx_id = session.scalars(select(InvestmentTransaction.id)).one()
    assert 'data-confirm-tone="danger"' in owner.client.get(TX).text
    deleted = _htmx(owner, f"{TX}/{tx_id}/delete", {"return_to": "list"})
    assert deleted.status_code == 200
    assert "Transaction deleted." in deleted.text
    assert f"{TX}/{tx_id}/undo" in deleted.text
    restored = _htmx(owner, f"{TX}/{tx_id}/undo")
    assert restored.status_code == 200
    assert "Transaction restored." in restored.text
    assert "World ETF" in owner.client.get(TX).text


def test_filters_by_instrument_type_and_date(owner: OwnerApp) -> None:
    first = _instrument(owner, "First")
    second = _instrument(owner, "Second")
    _tx(owner, first, date="2026-03-01")
    _tx(owner, second, date="2026-03-02", type="buy")
    page = owner.client.get(f"{TX}?instrument={first}&type=buy&from=2026-03-01").text
    assert len(re.findall(r'data-transaction="[0-9a-f-]+"', page)) == 1
    assert "No investment transactions match these filters." not in page


def test_currency_instrument_price_is_one_in_the_form_and_stored(owner: OwnerApp) -> None:
    cash = _instrument(
        owner, "US dollar", type="currency", quote_currency="USD", unit_label="dollar"
    )
    response = _tx(owner, cash, quantity="100", unit_price="2.5", fx="0.9")
    assert response.status_code == 303
    detail = owner.client.get(f"{INSTRUMENTS}/{cash}").text
    assert "1 USD" in detail
    with owner.db() as session:
        from decimal import Decimal

        from assetboard.db.models.investments import InvestmentTransaction

        row = session.scalars(select(InvestmentTransaction)).one()
        assert row.unit_price == Decimal("1")


def test_a_viewer_gets_no_forms_and_no_actions(owner: OwnerApp) -> None:
    instrument = _instrument(owner, "World ETF")
    _tx(owner, instrument)
    owner.set_admin_role("viewer")
    page = owner.client.get(TX).text
    assert "Add a transaction" not in page and "data-transaction-form" not in page
    assert "Delete" not in page and "Edit" not in page
    detail = owner.client.get(f"{INSTRUMENTS}/{instrument}").text
    assert "Add a transaction" not in detail and "Delete" not in detail
    with owner.db() as session:
        from assetboard.db.models.investments import InvestmentTransaction

        tx_id = session.scalars(select(InvestmentTransaction.id)).one()
    assert owner.client.get(f"{TX}/{tx_id}/edit").status_code == 403


def test_an_archived_instrument_has_no_form_or_row_actions(owner: OwnerApp) -> None:
    instrument = _instrument(owner, "World ETF")
    _tx(owner, instrument)
    owner.post(f"{INSTRUMENTS}/{instrument}/archive", token_from=INSTRUMENTS)
    detail = owner.client.get(f"{INSTRUMENTS}/{instrument}").text
    assert "This instrument is archived." in detail
    assert "Add a transaction" not in detail
    assert "data-add-transaction" not in detail
    assert 'id="transaction-dialog"' not in detail
    assert "data-transaction-form" not in detail
    assert "Delete" not in detail
    global_page = owner.client.get(TX).text
    assert f"{TX}/" not in global_page.split("data-transaction=", 1)[0]  # no row actions
    assert "/delete" not in global_page


def test_instrument_quote_and_type_controls_are_locked(owner: OwnerApp) -> None:
    instrument = _instrument(owner, "World ETF")
    _tx(owner, instrument)
    page = owner.client.get(f"{INSTRUMENTS}?edit={instrument}").text
    assert "Locked: transactions exist." in page
    assert 'name="quote_currency" value="EUR"' in page
    assert 'name="type" value="etf"' in page


def test_workspace_cost_basis_setting_round_trips(owner: OwnerApp) -> None:
    page = owner.client.get("/workspace/settings").text
    assert 'name="investment_cost_basis"' in page
    response = owner.post(
        "/workspace/settings",
        {
            "name": "Home",
            "base_currency": "EUR",
            "country_code": "",
            "timezone": "UTC",
            "locale": "en",
            "auto_copy_plan": "",
            "require_two_factor": "",
            "upcoming_days": "",
            "investment_cost_basis": "fifo",
        },
        token_from="/workspace/settings",
    )
    assert response.status_code == 303
    page = owner.client.get("/workspace/settings").text
    assert 'value="fifo" selected' in page


def test_the_form_offers_dividends_splits_and_income(owner: OwnerApp) -> None:
    _instrument(owner, "World ETF")
    page = owner.client.get(TX).text
    assert "Dividend / interest" in page
    assert "Record as income" in page
    assert "Income category" in page
    assert "New units" in page
    assert "Old units" in page


def test_a_dividend_records_the_net_income_and_the_income_tab_shows_it(
    owner: OwnerApp,
) -> None:
    instrument = _instrument(owner, "World ETF")
    interest = _interest_id(owner)
    response = _dividend(owner, instrument, gross="10", withholding="1", category_id=interest)
    assert (response.status_code, response.headers["location"]) == (
        303,
        f"{TX}?notice=created",
    )

    page = owner.client.get(TX).text
    assert "Dividend / interest" in page
    assert "10 EUR" in page and "€1.00" in page

    ledger = owner.client.get("/transactions/2026-03").text
    assert "Investment income: World ETF" in ledger
    assert "from investment income" in ledger

    income = owner.client.get("/investments/income").text
    assert "Income from investments" in income
    assert re.search(r'<table class="sheet[^"]*"', income)
    assert "Income from investments by year" in income and 'scope="col"' in income
    assert "2026" in income
    assert "€10.00" in income and "€1.00" in income and "€9.00" in income
    assert "data-income-tab" in income

    owner.set_admin_role("viewer")
    assert owner.client.get("/investments/income").status_code == 200


def test_recording_income_without_a_category_is_refused(owner: OwnerApp) -> None:
    instrument = _instrument(owner, "World ETF")
    response = _dividend(owner, instrument, gross="10", withholding="0", category_id=None)
    assert response.status_code == 400
    assert "Choose an income category when recording the dividend as income." in response.text


def test_the_ledger_refuses_deleting_a_linked_income_row(owner: OwnerApp) -> None:
    instrument = _instrument(owner, "World ETF")
    assert (
        _dividend(
            owner, instrument, gross="10", withholding="1", category_id=_interest_id(owner)
        ).status_code
        == 303
    )
    with owner.db() as session:
        row = session.scalars(select(Transaction).where(Transaction.source == "investment")).one()
        income_id = row.id
        assert row.direction == "income"
    response = owner.post(
        f"/m/2026-03/transactions/{income_id}/delete", {}, token_from="/m/2026-03"
    )
    assert response.status_code == 303
    assert "error=investment_income_linked" in response.headers["location"]
    page = owner.client.get(response.headers["location"]).text
    assert "managed by its investment dividend" in page


def test_a_split_can_be_created_from_the_form(owner: OwnerApp) -> None:
    instrument = _instrument(owner, "World ETF")
    assert _tx(owner, instrument, quantity="10", unit_price="100").status_code == 303
    response = owner.post(
        TX,
        {
            "instrument_id": str(instrument),
            "type": "split",
            "date": "2026-03-01",
            "quantity": "",
            "unit_price": "",
            "fee": "0",
            "fx_to_base": "",
            "note": "",
            "dividend_gross": "",
            "dividend_withholding": "0",
            "record_income": "",
            "income_category_id": "",
            "split_new": "2",
            "split_old": "1",
        },
        token_from=TX,
    )
    assert response.status_code == 303
    page = owner.client.get(TX).text
    assert "2 : 1" in page
    detail = owner.client.get(f"{INSTRUMENTS}/{instrument}").text
    assert "data-holding-summary" in detail and "20" in detail


def test_a_closed_month_refuses_only_the_linked_dividend(owner: OwnerApp) -> None:
    instrument = _instrument(owner, "World ETF")
    assert owner.post("/m/2026-03/close", {}, token_from="/m/2026-03").status_code == 303
    interest = _interest_id(owner)
    response = _dividend(owner, instrument, gross="10", withholding="0", category_id=interest)
    assert response.status_code == 409
    assert "This month is closed" in response.text
    unlinked = _dividend(owner, instrument, gross="10", withholding="0", record_income=False)
    assert unlinked.status_code == 303
