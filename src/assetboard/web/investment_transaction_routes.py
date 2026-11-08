"""Investment transactions and instrument prices (F6.2/F6.3).

Global transaction history and per-instrument pages. The instrument page also shows the price
panel and edits manual price points; transaction prices are derived by ``services.prices``.
The pages are plain forms first: filters are a GET form, writes redirect (303) after success.
HTMX gets the page body or one row back; the delete toast uses the same strict 10-second undo as
the ledger. Authorization and validation live in the service layer.
"""

from __future__ import annotations

import math
from datetime import date
from datetime import date as date_type
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, RedirectResponse, Response

from assetboard.db.repositories import NotFound
from assetboard.domain.money import Money
from assetboard.domain.networth import sparkline_polyline
from assetboard.domain.providers import provider_definition
from assetboard.domain.settlement import base_values_from_quote
from assetboard.i18n import gettext
from assetboard.i18n.dates import format_day
from assetboard.i18n.numbers import (
    format_amount_input,
    format_money,
    format_quantity_input,
)
from assetboard.services.bonds import (
    BondTermsError,
    BondTermsInput,
    BondTermsView,
    bond_panel,
    save_bond_terms,
)
from assetboard.services.context import Context, PermissionDenied, Role, has_role
from assetboard.services.investment_transactions import (
    InvalidInvestmentTransaction,
    InvestmentTransactionError,
    InvestmentTransactionView,
    InvestmentUndoExpired,
    OversellRejected,
    TransactionList,
    create_transaction,
    delete_transaction,
    fx_prefill,
    get_transaction,
    instrument_history,
    list_transactions,
    undo_delete,
    update_transaction,
)
from assetboard.services.investments import InstrumentView, investment_page
from assetboard.services.price_refresh import provider_status
from assetboard.services.price_sources import provider_enabled
from assetboard.services.prices import (
    DEFAULT_STALE_AFTER_DAYS,
    FxAt,
    PriceError,
    delete_manual_price,
    instrument_prices,
    save_manual_price,
)
from assetboard.web.context import CurrentContext
from assetboard.web.errors import is_htmx_fragment_request
from assetboard.web.rendering import render_template
from assetboard.web.security import require_csrf

router = APIRouter(dependencies=[Depends(require_csrf)])

FormStr = Annotated[str, Form()]
LIST_PATH = "/investments/transactions"
EMPTY = TransactionList(rows=(), page=1, pages=1, total=0, has_previous=False, has_next=False)


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


ERRORS = {
    "invalid_type": _("Choose buy or sell."),
    "invalid_date": _("Enter a valid date."),
    "future_date": _("Investment transactions cannot be dated in the future."),
    "invalid_quantity": _("Enter a quantity greater than zero."),
    "invalid_unit_price": _("Enter a unit price of zero or more."),
    "invalid_fee": _("Enter a fee of zero or more."),
    "too_many_decimals": _("The fee has too many decimal places for its currency."),
    "amount_too_large": _("The fee is too large."),
    "invalid_fx": _("Enter a positive FX rate to the base currency."),
    "invalid_fx_to_base": _("Enter a positive FX rate to the base currency."),
    "fx_needed": _("FX rate needed: no stored rate exists for this date, so enter one."),
    "invalid_amounts": _("Choose the currency of the amounts."),
    "settlement_not_allowed": _(
        "Amounts in the base currency are only for instruments quoted in another currency."
    ),
    "invalid_note": _("Enter a note of at most 200 characters."),
    "invalid_description_too_long": _("Enter a note of at most 200 characters."),
    "invalid_instrument": _("Choose an instrument."),
    "instrument_archived": _(
        "This instrument is archived; restore it before changing its transactions."
    ),
    "deleted": _("This transaction is deleted."),
    "undo_expired": _("The 10-second undo window has expired."),
    "undo_future": _("A future-dated transaction cannot be restored."),
    "oversell": _("The sale on %(date)s would exceed the quantity held."),
}
FALLBACK = _("The change could not be made. Reload the page and try again.")
BOND_ERRORS = {
    "not_bond_model": _("Bond terms are only available for a bond-model instrument."),
    "instrument_archived": _("This instrument is archived; restore it before changing its terms."),
    "invalid_series": _("Enter a series code of at most 64 characters."),
    "invalid_face_value": _("Enter a face value greater than zero."),
    "invalid_maturity": _("Enter a maturity date after the first schedule date."),
    "invalid_interest_mode": _("Choose an interest mode."),
    "empty_schedule": _("Add at least one interest period."),
    "invalid_schedule": _("Each period must start on the first date's anniversary."),
    "invalid_rate": _("Enter a rate between 0 and the allowed maximum."),
    "invalid_tax": _("Enter a tax rate between 0 and 100%."),
    "invalid_fee": _("Enter a valid early-redemption fee."),
}
PRICE_ERRORS = {
    "invalid_date": _("Enter a valid date between 1900 and 2199."),
    "bond_model_priced": _("This bond is valued from its terms; manual prices are not used."),
    "future_date": _("Manual prices cannot be dated in the future."),
    "invalid_price": _("Enter a price of zero or more, with at most 18 decimal places."),
    "duplicate_date": _("Another manual price already exists on this date."),
    "instrument_archived": _("This instrument is archived; restore it before changing its prices."),
    "not_manual": _("Only manual prices can be deleted."),
}
NOTICES = {
    "created": _("Transaction added."),
    "saved": _("Transaction saved."),
    "deleted": _("Transaction deleted."),
    "restored": _("Transaction restored."),
    "price_saved": _("Price saved."),
    "price_deleted": _("Price deleted."),
    "bond_terms_saved": _("Bond terms saved."),
}


def _t(request: Request, message: str) -> str:
    return gettext(message, request.state.locale)


def _parse_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise NotFound from None


def _int(value: str, default: int = 1) -> int:
    return int(value) if value.isascii() and value.isdigit() else default


def _error_text(request: Request, exc: InvestmentTransactionError) -> str:
    if isinstance(exc, OversellRejected):
        when = (
            format_day(exc.on, request.state.locale)
            if exc.on is not None
            else _t(request, _("an undated transaction"))
        )
        return _t(request, ERRORS["oversell"]) % {"date": when}
    message = ERRORS.get(exc.code, FALLBACK)
    return _t(request, message)


def _price_error_message(exc: PriceError) -> str:
    return PRICE_ERRORS.get(exc.code, FALLBACK)


def _price_error_text(request: Request, exc: PriceError) -> str:
    return _t(request, _price_error_message(exc))


def _bond_error_message(exc: BondTermsError) -> str:
    return BOND_ERRORS.get(exc.code, FALLBACK)


def _bond_input_values(values: BondTermsInput) -> dict[str, str]:
    return {
        "series_code": values.series_code,
        "face_value": values.face_value,
        "maturity_date": values.maturity_date,
        "interest_mode": values.interest_mode,
        "tax_rate": values.tax_rate,
        "fee_mode": values.fee_mode,
        "fee_value": values.fee_value,
    }


def _bond_form_values(
    terms: BondTermsView | None, today: date, locale: str
) -> tuple[dict[str, str], list[dict[str, str]]]:
    if terms is None:
        return (
            {
                "series_code": "",
                "face_value": "",
                "maturity_date": "",
                "interest_mode": "capitalized_annually",
                "tax_rate": "",
                "fee_mode": "",
                "fee_value": "",
            },
            [{"from_date": today.isoformat(), "rate": ""}],
        )
    fee = terms.early_redemption_fee
    if fee is None:
        fee_mode, fee_value = "", ""
    elif fee.mode == "percent" and fee.rate_bp is not None:
        fee_mode = "percent"
        fee_value = format_quantity_input(Decimal(fee.rate_bp).scaleb(-2), locale)
    else:
        fee_mode = "fixed"
        fee_value = format_amount_input(fee.fixed, locale) if fee.fixed is not None else ""
    return (
        {
            "series_code": terms.series_code or "",
            "face_value": format_amount_input(terms.face_value, locale),
            "maturity_date": terms.maturity_date.isoformat(),
            "interest_mode": terms.interest_mode.value,
            "tax_rate": (
                ""
                if terms.tax_rate_bp == 0
                else format_quantity_input(Decimal(terms.tax_rate_bp).scaleb(-2), locale)
            ),
            "fee_mode": fee_mode,
            "fee_value": fee_value,
        },
        [
            {
                "from_date": period.from_date.isoformat(),
                "rate": format_quantity_input(Decimal(period.annual_rate_bp).scaleb(-2), locale),
            }
            for period in terms.rate_schedule
        ],
    )


def _default_values(instrument: InstrumentView | None, today: date) -> dict[str, str]:
    return {
        "date": today.isoformat(),
        "type": "buy",
        "instrument_id": str(instrument.id) if instrument is not None else "",
        "quantity": "",
        "unit_price": "",
        "fee": "0",
        "fx_to_base": "",
        "note": "",
    }


def _fx_hint(request: Request, stored: FxAt | None) -> str:
    """The hint under the FX field: the stored rate's date and source, or that none exists."""
    if stored is None:
        return ""
    if stored.rate is None or stored.as_of is None:
        return _t(request, _("No stored FX rate for this date; enter the rate."))
    source = stored.source or ""
    definition = provider_definition(source)
    if definition is not None:
        label = definition.label
    elif source == "manual":
        label = _t(request, _("manual"))
    else:
        label = source
    return _t(request, _("Stored rate of %(date)s (%(source)s).")) % {
        "date": format_day(stored.as_of, request.state.locale),
        "source": label,
    }


def _stored_fx(request: Request, ctx: Context, instrument_id: str, on: date) -> FxAt | None:
    """The stored FX to pre-fill for a form's instrument and date (F6.2a), if a rate is asked."""
    try:
        item = UUID(instrument_id)
    except ValueError:
        return None
    try:
        return fx_prefill(request.app.state.session_factory, ctx, item, on)
    except NotFound:
        return None


def _prefill_fx(request: Request, ctx: Context, values: dict[str, str], today: date) -> None:
    """Server-side pre-fill of a new form's FX field and hint (works without JavaScript)."""
    if not has_role(ctx, Role.EDITOR):
        return
    stored = _stored_fx(request, ctx, values.get("instrument_id", ""), today)
    if stored is not None and stored.rate is not None and not values.get("fx_to_base"):
        values["fx_to_base"] = format_quantity_input(stored.rate, request.state.locale)
    values["fx_hint"] = _fx_hint(request, stored)


def _edit_values(
    view: InvestmentTransactionView, locale: str, base_currency: str
) -> dict[str, str]:
    """A saved row's form values; both amount modes are pre-filled so switching keeps them."""
    base_unit_price = ""
    base_fee = ""
    if view.settlement_unit_price is not None and view.settlement_fee is not None:
        base_unit_price = format_quantity_input(view.settlement_unit_price, locale)
        base_fee = format_amount_input(view.settlement_fee, locale)
    elif view.quote_currency != base_currency and view.instrument_type != "currency":
        # quote -> base: base price = unit_price × fx, base fee = fee × fx (rounded once).
        price, fee_minor = base_values_from_quote(
            view.unit_price,
            view.fee.amount_minor,
            view.fx_to_base,
            base_currency=base_currency,
            quote_currency=view.quote_currency,
        )
        base_unit_price = format_quantity_input(price, locale)
        base_fee = format_amount_input(Money(fee_minor, base_currency), locale)
    return {
        "date": view.date.isoformat() if view.date is not None else "",
        "type": view.type,
        "instrument_id": str(view.instrument_id),
        "quantity": format_quantity_input(view.quantity, locale),
        "unit_price": format_quantity_input(view.unit_price, locale),
        "fee": format_quantity_input(view.fee.to_decimal(), locale),
        "fx_to_base": (
            ""
            if view.quote_currency == base_currency
            else format_quantity_input(view.fx_to_base, locale)
        ),
        "note": view.note,
        "amounts": view.amounts,
        "base_unit_price": base_unit_price,
        "base_fee": base_fee,
    }


def _form_errors(request: Request, exc: InvestmentTransactionError) -> dict[str, str]:
    return {exc.field or "form": _error_text(request, exc)}


def _instruments(
    factory: Any, ctx: Context, locale: str
) -> tuple[tuple[InstrumentView, ...], tuple[InstrumentView, ...]]:
    page = investment_page(factory, ctx, locale=locale)
    return page.instruments, (*page.instruments, *page.archived_instruments)


def _list_values(
    request: Request, ctx: Context, instruments: tuple[InstrumentView, ...], today: date
) -> dict[str, str]:
    values = _default_values(None, today)
    if instruments:
        values["instrument_id"] = str(instruments[0].id)
    return values


def _filter_values(request: Request) -> dict[str, str]:
    return {
        "instrument": request.query_params.get("instrument", ""),
        "type": request.query_params.get("type", ""),
        "from": request.query_params.get("from", ""),
        "to": request.query_params.get("to", ""),
        "fx": "check" if request.query_params.get("fx", "") == "check" else "",
    }


def _filter_instrument(request: Request) -> UUID | None:
    raw = request.query_params.get("instrument", "")
    if not raw:
        return None
    try:
        return UUID(raw)
    except ValueError:
        raise InvalidInvestmentTransaction("instrument", "invalid_instrument") from None


def _list_page(
    request: Request,
    ctx: Context,
    *,
    notice: str | None = None,
    error: str | None = None,
    create_values: dict[str, str] | None = None,
    create_errors: dict[str, str] | None = None,
    editing: UUID | None = None,
    edit_values: dict[str, str] | None = None,
    edit_errors: dict[str, str] | None = None,
    toast_undo: str | None = None,
    toast_seconds: int = 0,
    status_code: int = 200,
) -> Response:
    factory = request.app.state.session_factory
    locale = request.state.locale
    today = ctx.clock.today(ctx.workspace.timezone)
    active_instruments, instruments = _instruments(factory, ctx, locale)
    filters = _filter_values(request)
    page_number = _int(request.query_params.get("page", "1"))
    try:
        instrument_id = _filter_instrument(request)
        result = list_transactions(
            factory,
            ctx,
            instrument_id=instrument_id,
            type_value=filters["type"],
            date_from=filters["from"],
            date_to=filters["to"],
            page=page_number,
            fx_check=filters["fx"] == "check",
        )
    except InvestmentTransactionError as exc:
        result = EMPTY
        error = error or _error_text(request, exc)
    if create_values is None:
        create_values = _list_values(request, ctx, active_instruments, today)
        _prefill_fx(request, ctx, create_values, today)
    editing_view: InvestmentTransactionView | None = None
    if editing is not None:
        try:
            editing_view = get_transaction(factory, ctx, editing, locale=locale)
        except (NotFound, InvestmentTransactionError):
            editing = None
            edit_values = None
    context: dict[str, Any] = {
        "result": result,
        "instruments": instruments,
        "active_instruments": active_instruments,
        "filters": filters,
        "create_values": create_values,
        "create_errors": {
            field: _t(request, message) for field, message in (create_errors or {}).items()
        },
        "editing": editing,
        "editing_view": editing_view,
        "edit_values": edit_values,
        "edit_errors": {
            field: _t(request, message) for field, message in (edit_errors or {}).items()
        },
        "notice": _t(request, NOTICES[notice]) if notice in NOTICES else None,
        "error": error,
        "base_currency": ctx.workspace.base_currency,
        "can_edit": has_role(ctx, Role.EDITOR),
        "can_manage_providers": has_role(ctx, Role.OWNER),
        "coingecko_enabled": provider_enabled(request.app.state.session_factory, ctx, "coingecko"),
        "today": today,
        "toast_undo": toast_undo,
        "toast_seconds": toast_seconds,
        "toast_message": _t(request, NOTICES["deleted"]) if toast_undo else None,
        "format_day": format_day,
        "format_money": format_money,
        "format_quantity_input": format_quantity_input,
        "format_amount_input": format_amount_input,
        "locale": locale,
    }
    template = (
        "investments/_transactions_body.html"
        if is_htmx_fragment_request(request)
        else "investments/transactions.html"
    )
    response = render_template(request, template, context=context, status_code=status_code)
    response.headers["Vary"] = "HX-Request"
    return response


def _history_page(
    request: Request,
    ctx: Context,
    history: Any,
    *,
    notice: str | None = None,
    error: str | None = None,
    create_values: dict[str, str] | None = None,
    create_errors: dict[str, str] | None = None,
    editing: UUID | None = None,
    edit_values: dict[str, str] | None = None,
    edit_errors: dict[str, str] | None = None,
    editing_price: UUID | None = None,
    price_values: dict[str, str] | None = None,
    price_errors: dict[str, str] | None = None,
    bond_values: dict[str, str] | None = None,
    bond_schedule: list[dict[str, str]] | None = None,
    bond_errors: dict[str, str] | None = None,
    toast_undo: str | None = None,
    toast_seconds: int = 0,
    status_code: int = 200,
) -> Response:
    locale = request.state.locale
    today = ctx.clock.today(ctx.workspace.timezone)
    prices = instrument_prices(
        request.app.state.session_factory,
        ctx,
        history.instrument.id,
        on=today,
    )
    bond = (
        bond_panel(
            request.app.state.session_factory,
            ctx,
            history.instrument.id,
            on=None,
        )
        if history.instrument.type == "bond_model"
        else None
    )
    bond_default_values, bond_default_schedule = _bond_form_values(
        bond.terms if bond is not None else None, today, locale
    )
    provider_state = (
        provider_status(request.app.state.session_factory, ctx, history.instrument.id)
        if history.instrument.provider_key
        else None
    )
    provider_error_days = (
        max(0, (today - provider_state.last_price_date).days)
        if provider_state is not None and provider_state.last_price_date is not None
        else 0
    )
    if price_values is None and editing_price is not None:
        point = next(
            (row for row in prices.points if row.id == editing_price and row.editable),
            None,
        )
        if point is None:
            editing_price = None
        else:
            price_values = {
                "price_id": str(point.id),
                "date": point.date.isoformat(),
                "price": format_quantity_input(point.price, locale),
            }
    context: dict[str, Any] = {
        "history": history,
        "instrument": history.instrument,
        "prices": prices,
        "bond": bond,
        "bond_values": bond_values or bond_default_values,
        "bond_schedule": bond_schedule or bond_default_schedule,
        "bond_errors": {
            field: _t(request, message) for field, message in (bond_errors or {}).items()
        },
        "provider_status": provider_state,
        "provider_error_days": provider_error_days,
        "price_values": price_values or {"price_id": "", "date": today.isoformat(), "price": ""},
        "price_errors": {
            field: _t(request, message) for field, message in (price_errors or {}).items()
        },
        "editing_price": editing_price,
        "price_sparkline": sparkline_polyline([point.price for point in prices.effective]),
        "default_stale_after_days": DEFAULT_STALE_AFTER_DAYS,
        "create_values": create_values or _history_defaults(request, ctx, history, today),
        "create_errors": {
            field: _t(request, message) for field, message in (create_errors or {}).items()
        },
        "editing": editing,
        "edit_values": edit_values,
        "edit_errors": {
            field: _t(request, message) for field, message in (edit_errors or {}).items()
        },
        "notice": _t(request, NOTICES[notice]) if notice in NOTICES else None,
        "error": error,
        "base_currency": ctx.workspace.base_currency,
        "can_edit": has_role(ctx, Role.EDITOR),
        "can_manage_providers": has_role(ctx, Role.OWNER),
        "coingecko_enabled": provider_enabled(request.app.state.session_factory, ctx, "coingecko"),
        "today": today,
        "toast_undo": toast_undo,
        "toast_seconds": toast_seconds,
        "toast_message": _t(request, NOTICES["deleted"]) if toast_undo else None,
        "format_day": format_day,
        "format_money": format_money,
        "format_quantity_input": format_quantity_input,
        "format_amount_input": format_amount_input,
        "locale": locale,
    }
    template = (
        "investments/_instrument_history_body.html"
        if is_htmx_fragment_request(request)
        else "investments/instrument.html"
    )
    response = render_template(request, template, context=context, status_code=status_code)
    response.headers["Vary"] = "HX-Request"
    return response


def _history_defaults(request: Request, ctx: Context, history: Any, today: date) -> dict[str, str]:
    values = _default_values(history.instrument, today)
    _prefill_fx(request, ctx, values, today)
    return values


def _render_history(request: Request, ctx: Context, instrument_id: UUID, **kwargs: Any) -> Response:
    history = instrument_history(
        request.app.state.session_factory,
        ctx,
        instrument_id,
        locale=request.state.locale,
    )
    return _history_page(request, ctx, history, **kwargs)


def _done(
    request: Request,
    ctx: Context,
    notice: str,
    *,
    instrument_id: UUID | None,
) -> Response:
    if is_htmx_fragment_request(request):
        if instrument_id is not None:
            return _render_history(request, ctx, instrument_id, notice=notice)
        return _list_page(request, ctx, notice=notice)
    path = (
        request.url_for("investments_instrument", instrument_id=str(instrument_id)).path
        if instrument_id is not None
        else request.url_for("investments_transactions").path
    )
    return RedirectResponse(f"{path}?notice={notice}", status_code=303)


def _instrument_from_form(form: Any) -> UUID:
    value = form.get("instrument_id", "")
    if not isinstance(value, str) or not value:
        raise InvalidInvestmentTransaction("instrument", "invalid_instrument")
    try:
        return UUID(value)
    except ValueError:
        raise InvalidInvestmentTransaction("instrument", "invalid_instrument") from None


@router.get("/investments/transactions", name="investments_transactions")
async def transactions_page(request: Request, ctx: CurrentContext, notice: str = "") -> Response:
    editing: UUID | None = None
    values: dict[str, str] | None = None
    raw_edit = request.query_params.get("edit", "")
    if raw_edit and has_role(ctx, Role.EDITOR):
        try:
            editing = UUID(raw_edit)
            view = await run_in_threadpool(
                get_transaction,
                request.app.state.session_factory,
                ctx,
                editing,
                locale=request.state.locale,
            )
            values = _edit_values(view, request.state.locale, ctx.workspace.base_currency)
        except (ValueError, NotFound, InvestmentTransactionError):
            editing = None
            values = None
    return await run_in_threadpool(
        lambda: _list_page(
            request,
            ctx,
            notice=notice or None,
            editing=editing,
            edit_values=values,
        )
    )


@router.post("/investments/transactions", name="investment_transaction_create")
async def transaction_create(
    request: Request,
    ctx: CurrentContext,
    instrument_id: FormStr = "",
    type: FormStr = "",
    date: FormStr = "",
    quantity: FormStr = "",
    unit_price: FormStr = "",
    fee: FormStr = "0",
    fx_to_base: FormStr = "",
    note: FormStr = "",
    return_to: FormStr = "",
    amounts: FormStr = "quote",
    base_unit_price: FormStr = "",
    base_fee: FormStr = "0",
) -> Response:
    values = {
        "date": date,
        "type": type,
        "instrument_id": instrument_id,
        "quantity": quantity,
        "unit_price": unit_price,
        "fee": fee,
        "fx_to_base": fx_to_base,
        "note": note,
        "amounts": amounts,
        "base_unit_price": base_unit_price,
        "base_fee": base_fee,
    }
    form = await request.form()
    try:
        item = _instrument_from_form(form)
    except InvestmentTransactionError as error:
        return await run_in_threadpool(
            lambda failure=error: _list_page(
                request,
                ctx,
                status_code=400,
                create_values=values,
                create_errors=_form_errors(request, failure),
            )
        )
    try:
        await run_in_threadpool(
            lambda: create_transaction(
                request.app.state.session_factory,
                ctx,
                instrument_id=item,
                type_value=type,
                date_value=date,
                quantity_value=quantity,
                unit_price_value=unit_price,
                fee_value=fee,
                fx_value=fx_to_base,
                note_value=note,
                locale=request.state.locale,
                amounts_value=amounts,
                base_unit_price_value=base_unit_price,
                base_fee_value=base_fee,
            )
        )
    except InvestmentTransactionError as exc:
        target_instrument = item if return_to == "instrument" else None
        if target_instrument is not None:
            return await run_in_threadpool(
                lambda error=exc: _render_history(
                    request,
                    ctx,
                    target_instrument,
                    status_code=400,
                    create_values=values,
                    create_errors=_form_errors(request, error),
                )
            )
        return await run_in_threadpool(
            lambda error=exc: _list_page(
                request,
                ctx,
                status_code=400,
                create_values=values,
                create_errors=_form_errors(request, error),
            )
        )
    return await run_in_threadpool(
        _done,
        request,
        ctx,
        "created",
        instrument_id=item if return_to == "instrument" else None,
    )


@router.get("/investments/fx-rate", name="investment_fx_rate")
async def fx_rate_lookup(
    request: Request, ctx: CurrentContext, instrument_id: str = "", date: str = ""
) -> Response:
    """Read-only stored FX for an instrument's quote currency on a date (F6.2a form re-fill).

    Workspace-scoped through the service; another workspace's instrument is 404. A rate that is
    not asked (base-quoted instrument) or a bad date gives an empty rate.
    """
    item = _parse_uuid(instrument_id)
    today = ctx.clock.today(ctx.workspace.timezone)
    try:
        day = date_type.fromisoformat(date) if date else today
    except ValueError:
        day = today
    day = min(day, today)
    stored = await run_in_threadpool(fx_prefill, request.app.state.session_factory, ctx, item, day)
    rate = (
        format_quantity_input(stored.rate, request.state.locale)
        if stored is not None and stored.rate is not None
        else ""
    )
    response = JSONResponse(
        {
            "asked": stored is not None,
            "rate": rate,
            "as_of": stored.as_of.isoformat() if stored and stored.as_of else "",
            "source": (stored.source or "") if stored is not None else "",
            "hint": _fx_hint(request, stored),
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get(
    "/investments/transactions/{transaction_id}/edit",
    name="investment_transaction_edit",
)
async def transaction_edit(
    request: Request,
    ctx: CurrentContext,
    transaction_id: str,
    show_instrument: str = "",
    target: str = "",
) -> Response:
    if not has_role(ctx, Role.EDITOR):
        raise PermissionDenied(Role.EDITOR, ctx.role)
    view = await run_in_threadpool(
        get_transaction,
        request.app.state.session_factory,
        ctx,
        _parse_uuid(transaction_id),
        locale=request.state.locale,
    )
    values = _edit_values(view, request.state.locale, ctx.workspace.base_currency)
    context: dict[str, Any] = {
        "transaction": view,
        "values": values,
        "errors": {},
        "show_instrument": bool(show_instrument),
        "target": target
        or ("investment-transactions-body" if show_instrument else "instrument-history-body"),
        "can_edit": has_role(ctx, Role.EDITOR),
        "base_currency": ctx.workspace.base_currency,
        "format_day": format_day,
        "format_money": format_money,
        "format_quantity_input": format_quantity_input,
        "locale": request.state.locale,
    }
    return render_template(request, "investments/_transaction_edit_row.html", context=context)


@router.get(
    "/investments/transactions/{transaction_id}/row",
    name="investment_transaction_row",
)
async def transaction_row(
    request: Request, ctx: CurrentContext, transaction_id: str, show_instrument: str = ""
) -> Response:
    view = await run_in_threadpool(
        get_transaction,
        request.app.state.session_factory,
        ctx,
        _parse_uuid(transaction_id),
        locale=request.state.locale,
    )
    return render_template(
        request,
        "investments/_transaction_row_only.html",
        context={
            "transaction": view,
            "show_instrument": bool(show_instrument),
            "can_edit": has_role(ctx, Role.EDITOR),
            "edit_href": request.url_for(
                "investments_transactions" if show_instrument else "investments_instrument",
                **({} if show_instrument else {"instrument_id": str(view.instrument_id)}),
            ).path,
            "target": "investment-transactions-body"
            if show_instrument
            else "instrument-history-body",
            "return_to": "list" if show_instrument else "instrument",
            "instrument_id": "" if show_instrument else str(view.instrument_id),
            "format_day": format_day,
            "format_money": format_money,
            "format_quantity_input": format_quantity_input,
            "locale": request.state.locale,
        },
    )


@router.post("/investments/transactions/{transaction_id}", name="investment_transaction_update")
async def transaction_update(
    request: Request,
    ctx: CurrentContext,
    transaction_id: str,
    type: FormStr = "",
    date: FormStr = "",
    quantity: FormStr = "",
    unit_price: FormStr = "",
    fee: FormStr = "0",
    fx_to_base: FormStr = "",
    note: FormStr = "",
    return_to: FormStr = "",
    instrument_id: FormStr = "",
    amounts: FormStr = "quote",
    base_unit_price: FormStr = "",
    base_fee: FormStr = "0",
) -> Response:
    item_id = _parse_uuid(transaction_id)
    values = {
        "date": date,
        "type": type,
        "instrument_id": instrument_id,
        "quantity": quantity,
        "unit_price": unit_price,
        "fee": fee,
        "fx_to_base": fx_to_base,
        "note": note,
        "amounts": amounts,
        "base_unit_price": base_unit_price,
        "base_fee": base_fee,
    }
    try:
        view = await run_in_threadpool(
            lambda: update_transaction(
                request.app.state.session_factory,
                ctx,
                item_id,
                type_value=type,
                date_value=date,
                quantity_value=quantity,
                unit_price_value=unit_price,
                fee_value=fee,
                fx_value=fx_to_base,
                note_value=note,
                locale=request.state.locale,
                amounts_value=amounts,
                base_unit_price_value=base_unit_price,
                base_fee_value=base_fee,
            )
        )
    except InvestmentTransactionError as exc:
        target = _parse_uuid(instrument_id) if return_to == "instrument" and instrument_id else None
        if target is not None:
            return await run_in_threadpool(
                lambda error=exc: _render_history(
                    request,
                    ctx,
                    target,
                    status_code=400,
                    editing=item_id,
                    edit_values=values,
                    edit_errors=_form_errors(request, error),
                )
            )
        return await run_in_threadpool(
            lambda error=exc: _list_page(
                request,
                ctx,
                status_code=400,
                editing=item_id,
                edit_values=values,
                edit_errors=_form_errors(request, error),
            )
        )
    target = view.instrument_id if return_to == "instrument" else None
    return await run_in_threadpool(_done, request, ctx, "saved", instrument_id=target)


@router.post(
    "/investments/transactions/{transaction_id}/delete",
    name="investment_transaction_delete",
)
async def transaction_delete(
    request: Request,
    ctx: CurrentContext,
    transaction_id: str,
    instrument_id: FormStr = "",
    return_to: FormStr = "",
) -> Response:
    item_id = _parse_uuid(transaction_id)
    target = _parse_uuid(instrument_id) if instrument_id else None
    try:
        deleted = await run_in_threadpool(
            delete_transaction, request.app.state.session_factory, ctx, item_id
        )
    except InvestmentTransactionError as exc:
        if target is not None:
            return await run_in_threadpool(
                lambda error=exc: _render_history(
                    request,
                    ctx,
                    target,
                    status_code=409,
                    error=_error_text(request, error),
                )
            )
        return await run_in_threadpool(
            lambda error=exc: _list_page(
                request,
                ctx,
                status_code=409,
                error=_error_text(request, error),
            )
        )
    seconds = max(0, math.ceil((deleted.undo_until - ctx.clock.now()).total_seconds()))
    undo_path = request.url_for("investment_transaction_undo", transaction_id=str(item_id)).path
    if target is not None:
        undo_path += f"?return_to=instrument&instrument_id={target}"
    if is_htmx_fragment_request(request):
        if target is not None:
            return await run_in_threadpool(
                lambda: _render_history(
                    request,
                    ctx,
                    target,
                    toast_undo=undo_path,
                    toast_seconds=seconds,
                )
            )
        return await run_in_threadpool(
            lambda: _list_page(request, ctx, toast_undo=undo_path, toast_seconds=seconds)
        )
    path = (
        request.url_for("investments_instrument", instrument_id=str(target)).path
        if target is not None
        else request.url_for("investments_transactions").path
    )
    return RedirectResponse(f"{path}?notice=deleted", status_code=303)


@router.post(
    "/investments/transactions/{transaction_id}/undo",
    name="investment_transaction_undo",
)
async def transaction_undo(request: Request, ctx: CurrentContext, transaction_id: str) -> Response:
    item_id = _parse_uuid(transaction_id)
    raw_instrument = request.query_params.get("instrument_id", "")
    return_to = request.query_params.get("return_to", "")
    target = _parse_uuid(raw_instrument) if raw_instrument else None
    try:
        view = await run_in_threadpool(undo_delete, request.app.state.session_factory, ctx, item_id)
    except InvestmentTransactionError as exc:
        code = 409 if isinstance(exc, InvestmentUndoExpired) else 400
        if target is not None:
            return await run_in_threadpool(
                lambda error=exc: _render_history(
                    request,
                    ctx,
                    target,
                    status_code=code,
                    error=_error_text(request, error),
                )
            )
        return await run_in_threadpool(
            lambda error=exc: _list_page(
                request,
                ctx,
                status_code=code,
                error=_error_text(request, error),
            )
        )
    target = target or (view.instrument_id if return_to == "instrument" else None)
    return await run_in_threadpool(_done, request, ctx, "restored", instrument_id=target)


@router.post(
    "/investments/instruments/{instrument_id}/bond-terms",
    name="investment_bond_terms_save",
)
async def bond_terms_save(
    request: Request,
    ctx: CurrentContext,
    instrument_id: str,
    series_code: FormStr = "",
    face_value: FormStr = "",
    maturity_date: FormStr = "",
    interest_mode: FormStr = "",
    tax_rate: FormStr = "",
    fee_mode: FormStr = "",
    fee_value: FormStr = "",
) -> Response:
    item_id = _parse_uuid(instrument_id)
    form = await request.form()
    periods = tuple(
        (str(day), str(rate))
        for day, rate in zip(form.getlist("period_from"), form.getlist("period_rate"), strict=False)
    )
    values = BondTermsInput(
        series_code=series_code,
        face_value=face_value,
        maturity_date=maturity_date,
        interest_mode=interest_mode,
        rate_periods=periods,
        tax_rate=tax_rate,
        fee_mode=fee_mode,
        fee_value=fee_value,
    )
    try:
        await run_in_threadpool(
            lambda: save_bond_terms(
                request.app.state.session_factory,
                ctx,
                item_id,
                values=values,
                locale=request.state.locale,
            )
        )
    except BondTermsError as exc:
        if exc.code in {"not_bond_model", "instrument_archived"}:
            return await run_in_threadpool(
                lambda error=exc: _render_history(
                    request,
                    ctx,
                    item_id,
                    status_code=400,
                    error=_t(request, _bond_error_message(error)),
                )
            )
        return await run_in_threadpool(
            lambda error=exc: _render_history(
                request,
                ctx,
                item_id,
                status_code=400,
                bond_values=_bond_input_values(values),
                bond_schedule=[{"from_date": day, "rate": rate} for day, rate in periods],
                bond_errors={error.field or "schedule": _bond_error_message(error)},
            )
        )
    return await run_in_threadpool(_done, request, ctx, "bond_terms_saved", instrument_id=item_id)


@router.post(
    "/investments/instruments/{instrument_id}/prices",
    name="investment_price_save",
)
async def price_save(
    request: Request,
    ctx: CurrentContext,
    instrument_id: str,
    price_id: FormStr = "",
    date: FormStr = "",
    price: FormStr = "",
) -> Response:
    item_id = _parse_uuid(instrument_id)
    values = {"price_id": price_id, "date": date, "price": price}
    editing_price: UUID | None = None
    if price_id:
        try:
            editing_price = UUID(price_id)
        except ValueError:
            return await run_in_threadpool(
                lambda: _render_history(
                    request,
                    ctx,
                    item_id,
                    status_code=400,
                    price_values=values,
                    price_errors={"price": PRICE_ERRORS["invalid_price"]},
                )
            )
    try:
        await run_in_threadpool(
            lambda: save_manual_price(
                request.app.state.session_factory,
                ctx,
                item_id,
                price_id=editing_price,
                date_value=date,
                price_value=price,
                locale=request.state.locale,
            )
        )
    except PriceError as exc:
        return await run_in_threadpool(
            lambda error=exc: _render_history(
                request,
                ctx,
                item_id,
                status_code=400,
                editing_price=editing_price,
                price_values=values,
                price_errors={error.field or "price": _price_error_message(error)},
            )
        )
    return await run_in_threadpool(_done, request, ctx, "price_saved", instrument_id=item_id)


@router.post(
    "/investments/instruments/{instrument_id}/prices/{price_id}/delete",
    name="investment_price_delete",
)
async def price_delete(
    request: Request,
    ctx: CurrentContext,
    instrument_id: str,
    price_id: str,
) -> Response:
    item_id = _parse_uuid(instrument_id)
    price_item = _parse_uuid(price_id)
    try:
        await run_in_threadpool(
            delete_manual_price,
            request.app.state.session_factory,
            ctx,
            item_id,
            price_item,
        )
    except PriceError as exc:
        return await run_in_threadpool(
            lambda error=exc: _render_history(
                request,
                ctx,
                item_id,
                status_code=409,
                error=_price_error_text(request, error),
            )
        )
    return await run_in_threadpool(_done, request, ctx, "price_deleted", instrument_id=item_id)


@router.get(
    "/investments/instruments/{instrument_id}",
    name="investments_instrument",
)
async def instrument_page(
    request: Request, ctx: CurrentContext, instrument_id: str, notice: str = ""
) -> Response:
    item_id = _parse_uuid(instrument_id)
    editing: UUID | None = None
    values: dict[str, str] | None = None
    editing_price: UUID | None = None
    raw_edit = request.query_params.get("edit", "")
    if raw_edit and has_role(ctx, Role.EDITOR):
        try:
            editing = UUID(raw_edit)
            view = await run_in_threadpool(
                get_transaction,
                request.app.state.session_factory,
                ctx,
                editing,
                locale=request.state.locale,
            )
            values = _edit_values(view, request.state.locale, ctx.workspace.base_currency)
        except (ValueError, NotFound, InvestmentTransactionError):
            editing = None
            values = None
    raw_price = request.query_params.get("edit_price", "")
    if raw_price and has_role(ctx, Role.EDITOR):
        try:
            editing_price = UUID(raw_price)
        except ValueError:
            editing_price = None
    return await run_in_threadpool(
        lambda: _render_history(
            request,
            ctx,
            item_id,
            notice=notice or None,
            editing=editing,
            edit_values=values,
            editing_price=editing_price,
        )
    )
