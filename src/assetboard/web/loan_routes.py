"""Loans (F7.1): list, two-path wizard, detail, terms edit and reference rates."""

from __future__ import annotations

import contextlib
from datetime import date
from decimal import Decimal
from typing import cast
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from assetboard.db.repositories import NotFound
from assetboard.domain.loans import (
    HIGH_RATE_CONFIRMATION_BP,
    LoanKind,
    LoanValidationError,
    RateMode,
    clean_loan_name,
    payment_date_matches_day,
    validate_payment_day,
    validate_term_months,
)
from assetboard.domain.money import Money
from assetboard.domain.transactions import EntryError, amount_minor
from assetboard.i18n import gettext
from assetboard.i18n.dates import format_day
from assetboard.i18n.numbers import (
    format_amount_input,
    format_money,
    format_quantity_input,
    format_rate_short,
    parse_amount,
)
from assetboard.services import loans
from assetboard.services.context import Context, Role, has_role, require_role
from assetboard.web.context import CurrentContext
from assetboard.web.forms import InvalidPercentOrFixed, percent_to_bp
from assetboard.web.rendering import render_template
from assetboard.web.security import require_csrf

router = APIRouter(dependencies=[Depends(require_csrf)])


def _(message: str) -> str:
    return message


ERRORS = {
    "invalid_name": _("Enter a name."),
    "invalid_note": _("Enter a shorter note."),
    "invalid_date": _("Enter a valid date between 1900 and 2199."),
    "invalid_principal": _("Enter a positive original principal."),
    "invalid_balance": _("Enter a positive current balance."),
    "invalid_amount": _("Enter a valid amount in the workspace currency."),
    "invalid_term": _("Enter a term between 1 and 600 months."),
    "invalid_payment_day": _("Enter a payment day between 1 and 31."),
    "invalid_first_payment": _(
        "The first payment date must fall on the payment day, or on the last day of a "
        "shorter month."
    ),
    "invalid_kind": _("Choose the loan type."),
    "invalid_rate_mode": _("Choose a fixed or variable rate."),
    "invalid_rate": _("Enter a rate between 0% and 100% with at most two decimals."),
    "invalid_margin": _("Enter a non-negative margin between 0% and 100%."),
    "high_rate_confirmation_required": _(
        "This rate is above 25%. Tick the confirmation box to save it."
    ),
    "invalid_reference_rate": _("Choose a reference rate."),
    "missing_reference_value": _(
        "This reference rate has no value on or before the loan's start date. Add a value first."
    ),
    "rate_mode_locked": _("The rate type cannot be changed after the loan is created."),
    "rate_change_missing": _("A fixed loan needs a rate on or before its first payment date."),
    "rate_change_before_start": _("A rate change cannot start before the loan starts."),
    "rate_change_exists": _("Another rate change already starts on that date."),
    "invalid_cost": _("Enter a cost name, a percentage or a positive fixed amount."),
    "invalid_effective_range": _("The end date must be on or after the start date."),
    "invalid_overpayment": _("Enter a positive overpayment amount and choose a strategy."),
    "target_below_installment": _("The target total cannot be below the monthly installment."),
    "invalid_override": _("Enter a positive installment override, or leave it empty."),
    "override_annuity_only": _("An installment override applies only to an annuity loan."),
    "missing_rate": _("The loan has no rate on its first payment date."),
    "reference_rate_exists": _("Another reference rate already has that name."),
    "reference_rate_in_use": _("This reference rate is used by a loan and cannot be deleted."),
    "value_exists": _("A value already exists for that date."),
    "value_not_future": _("Only a future value can be changed or deleted."),
    "archived": _("Archived loans cannot be changed. Restore the loan first."),
    "invalid_entry_path": _("Choose how you want to enter the loan."),
    "invalid_currency": _("Enter the amount in the workspace currency."),
    "failed": _("The loan could not be saved. Check the form and try again."),
}

NOTICES = {
    "loan_created": _("Loan saved. Add rate changes, costs or one-off overpayments below."),
    "terms_saved": _("Loan terms saved."),
    "overpayment_saved": _("Regular overpayment settings saved."),
    "rate_saved": _("Rate change saved."),
    "rate_deleted": _("Rate change deleted."),
    "cost_saved": _("Cost saved."),
    "cost_deleted": _("Cost deleted."),
    "overpayment_item_saved": _("One-off overpayment saved."),
    "overpayment_item_deleted": _("One-off overpayment deleted."),
    "loan_archived": _("Loan archived."),
    "loan_restored": _("Loan restored."),
    "reference_rate_saved": _("Reference rate saved."),
    "reference_rate_deleted": _("Reference rate deleted."),
    "reference_value_saved": _("Reference rate value saved."),
    "reference_value_deleted": _("Reference rate value deleted."),
}

ENTRY_PATHS = ("original", "current")
WIZARD_STEPS = (
    _("The loan"),
    _("Terms"),
    _("Interest"),
    _("Payments"),
)
WIZARD_FIELDS = (
    "entry_path",
    "name",
    "kind",
    "principal_original",
    "origination_date",
    "current_balance",
    "balance_date",
    "first_payment_date",
    "term_months",
    "payment_day",
    "rate_mode",
    "fixed_rate_percent",
    "reference_rate_id",
    "margin_percent",
    "high_rate_confirmed",
    "installment_override",
    "overpayment_mode",
    "overpayment_amount",
    "overpayment_strategy",
)
STEP_FIELDS: dict[int, tuple[str, ...]] = {
    1: ("entry_path", "name", "kind"),
    2: (
        "principal_original",
        "origination_date",
        "current_balance",
        "balance_date",
        "first_payment_date",
        "term_months",
        "payment_day",
    ),
    3: (
        "rate_mode",
        "fixed_rate_percent",
        "reference_rate_id",
        "margin_percent",
        "high_rate_confirmed",
    ),
    4: (
        "installment_override",
        "overpayment_mode",
        "overpayment_amount",
        "overpayment_strategy",
    ),
}


async def _form_values(request: Request) -> dict[str, str]:
    form = await request.form()
    return {key: value for key, value in form.items() if isinstance(value, str)}


def _id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise NotFound from None


def _optional_id(value: str) -> UUID | None:
    try:
        return UUID(value) if value else None
    except ValueError:
        raise NotFound from None


def _rate_input(rate_bp: int, locale: str) -> str:
    return format_quantity_input(Decimal(rate_bp).scaleb(-2), locale)


def _error(request: Request, code: str | None) -> str | None:
    if not code:
        return None
    return gettext(ERRORS.get(code, ERRORS["failed"]), request.state.locale)


def _notice(request: Request, code: str | None) -> str | None:
    if not code:
        return None
    message = NOTICES.get(code)
    if message is None:
        return None
    return gettext(message, request.state.locale)


def _date(value: str, field: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise loans.LoanError(field, "invalid_date") from None
    if not 1900 <= parsed.year <= 2199:
        raise loans.LoanError(field, "invalid_date")
    return parsed


def _optional_date(value: str, field: str) -> date | None:
    return _date(value, field) if value.strip() else None


def _integer(value: str, field: str) -> int:
    try:
        return int(value)
    except ValueError:
        code = "invalid_payment_day" if field == "payment_day" else "invalid_term"
        raise loans.LoanError(field, code) from None


def _money(values: dict[str, str], key: str, locale: str, currency: str) -> Money | None:
    text = values.get(key, "").strip()
    if not text:
        return None
    try:
        amount = parse_amount(text, locale)
        return Money(amount_minor(amount, currency), currency)
    except (ValueError, EntryError):
        raise loans.LoanError(key, "invalid_amount") from None


def _percent(values: dict[str, str], key: str, locale: str) -> int | None:
    text = values.get(key, "").strip()
    if not text:
        return None
    try:
        return percent_to_bp(text, locale)
    except (ValueError, InvalidPercentOrFixed):
        raise loans.LoanError(key, "invalid_rate") from None


def _rate_mode(value: str) -> RateMode:
    try:
        return RateMode(value)
    except ValueError:
        raise loans.LoanError("rate_mode", "invalid_rate_mode") from None


def _loan_kind(value: str) -> LoanKind:
    try:
        return LoanKind(value)
    except ValueError:
        raise loans.LoanError("kind", "invalid_kind") from None


def _wizard_values(raw: dict[str, str]) -> dict[str, str]:
    defaults = {
        "entry_path": "original",
        "kind": "annuity",
        "rate_mode": "fixed",
        "overpayment_mode": "none",
        "overpayment_strategy": "reduce_term",
    }
    values = {key: raw.get(key, defaults.get(key, "")) for key in WIZARD_FIELDS}
    # The payment day is usually the first payment date's day. Leaving the field empty takes
    # that day; entering 31 keeps month-end semantics for the following months.
    if not values["payment_day"] and values["first_payment_date"]:
        with contextlib.suppress(ValueError):
            values["payment_day"] = str(date.fromisoformat(values["first_payment_date"]).day)
    return values


def _wizard_start(values: dict[str, str]) -> date | None:
    if values.get("entry_path") == "current":
        text = values.get("balance_date", "")
    else:
        text = values.get("origination_date", "")
    if not text:
        return None
    field = "balance_date" if values.get("entry_path") == "current" else "origination_date"
    return _date(text, field)


def _check_step(
    request: Request,
    step: int,
    values: dict[str, str],
    ctx: Context,
    reference_rates: tuple[loans.ReferenceRateView, ...] = (),
) -> bool:
    """Validate one wizard step; returns whether a high rate needs confirmation."""
    if step == 1:
        if values.get("entry_path") not in ENTRY_PATHS:
            raise loans.LoanError("entry_path", "invalid_entry_path")
        clean_loan_name(values.get("name", ""))
        _loan_kind(values.get("kind", ""))
        return False
    if step == 2:
        entry = values.get("entry_path")
        balance_date: date | None = None
        if entry == "original":
            principal = _money(
                values,
                "principal_original",
                request.state.locale,
                ctx.workspace.base_currency,
            )
            if principal is None or principal.amount_minor <= 0:
                raise loans.LoanError("principal_original", "invalid_principal")
            origination = _date(values.get("origination_date", ""), "origination_date")
        else:
            balance = _money(
                values,
                "current_balance",
                request.state.locale,
                ctx.workspace.base_currency,
            )
            if balance is None or balance.amount_minor <= 0:
                raise loans.LoanError("current_balance", "invalid_balance")
            balance_date = _date(values.get("balance_date", ""), "balance_date")
            principal = _money(
                values, "principal_original", request.state.locale, ctx.workspace.base_currency
            )
            if principal is not None and principal.amount_minor < balance.amount_minor:
                raise loans.LoanError("principal_original", "invalid_principal")
            origination = None
        first = _date(values.get("first_payment_date", ""), "first_payment_date")
        term = _integer(values.get("term_months", ""), "term_months")
        validate_term_months(term)
        day = _integer(values.get("payment_day", ""), "payment_day")
        validate_payment_day(day)
        if not payment_date_matches_day(first, day):
            raise loans.LoanError("first_payment_date", "invalid_first_payment")
        if origination is not None and first <= origination:
            raise loans.LoanError("first_payment_date", "invalid_first_payment")
        if entry == "current" and balance_date is not None and first <= balance_date:
            raise loans.LoanError("first_payment_date", "invalid_first_payment")
        return False
    if step == 3:
        mode = _rate_mode(values.get("rate_mode", ""))
        start = _wizard_start(values)
        if start is None:
            raise loans.LoanError("rate_mode", "invalid_date")
        total: int | None = None
        if mode is RateMode.FIXED:
            total = _percent(values, "fixed_rate_percent", request.state.locale)
            if total is None:
                raise loans.LoanError("fixed_rate_percent", "invalid_rate")
        else:
            rate_id = _optional_id(values.get("reference_rate_id", ""))
            if rate_id is None:
                raise loans.LoanError("reference_rate_id", "invalid_reference_rate")
            margin = _percent(values, "margin_percent", request.state.locale)
            if margin is None:
                raise loans.LoanError("margin_percent", "invalid_margin")
            rate = next((row for row in reference_rates if row.id == rate_id), None)
            if rate is None:
                raise loans.LoanError("reference_rate_id", "invalid_reference_rate")
            reference = None
            for value in rate.values:
                if value.effective_from <= start:
                    reference = value.value_bp
            if reference is None:
                raise loans.LoanError("reference_rate_id", "missing_reference_value")
            total = reference + margin
        high = total is not None and total > HIGH_RATE_CONFIRMATION_BP
        if high and values.get("high_rate_confirmed") != "1":
            raise loans.LoanError("rate_bp", "high_rate_confirmation_required")
        return high
    return False


def _loan_values_from_view(view: loans.LoanView, locale: str) -> dict[str, str]:
    def amount(value: Money | None) -> str:
        return format_amount_input(value, locale) if value is not None else ""

    return {
        "name": view.name,
        "kind": view.kind.value,
        "principal_original": amount(view.principal_original),
        "origination_date": view.origination_date.isoformat() if view.origination_date else "",
        "current_balance": amount(view.current_balance),
        "balance_date": view.balance_date.isoformat() if view.balance_date else "",
        "first_payment_date": view.first_payment_date.isoformat(),
        "term_months": str(view.term_months),
        "payment_day": str(view.payment_day),
        "installment_override": amount(view.installment_override),
    }


async def _wizard_context(
    request: Request,
    ctx: Context,
    step: int,
    values: dict[str, str],
    *,
    error: str | None = None,
    high_rate: bool = False,
) -> dict[str, object]:
    page = await run_in_threadpool(
        loans.list_reference_rates, request.app.state.session_factory, ctx
    )
    return {
        "step": step,
        "last_step": 4,
        "steps": [gettext(label, request.state.locale) for label in WIZARD_STEPS],
        "values": values,
        "hidden_fields": [name for name in WIZARD_FIELDS if name not in STEP_FIELDS[step]],
        "reference_rates": page.rates,
        "error": error,
        "high_rate": high_rate,
        "can_edit": has_role(ctx, Role.EDITOR),
        "format_money": format_money,
        "format_amount_input": format_amount_input,
        "format_quantity_input": format_quantity_input,
        "format_rate_short": format_rate_short,
    }


async def _render_wizard(
    request: Request,
    ctx: Context,
    step: int,
    values: dict[str, str],
    *,
    error: str | None = None,
    high_rate: bool = False,
    status_code: int = 200,
) -> Response:
    context = await _wizard_context(request, ctx, step, values, error=error, high_rate=high_rate)
    return render_template(request, "loans/new.html", context=context, status_code=status_code)


@router.get("/loans", name="loans")
async def loans_page(request: Request, ctx: CurrentContext) -> Response:
    page = await run_in_threadpool(loans.list_loans, request.app.state.session_factory, ctx)
    return render_template(
        request,
        "loans/list.html",
        context={
            "page": page,
            "can_edit": has_role(ctx, Role.EDITOR),
            "notice": _notice(request, request.query_params.get("notice")),
            "error": _error(request, request.query_params.get("error")),
            "format_money": format_money,
            "format_day": format_day,
            "format_rate_short": format_rate_short,
        },
    )


@router.get("/loans/new", name="loan_new")
async def loan_new(request: Request, ctx: CurrentContext) -> Response:
    require_role(ctx, Role.EDITOR)
    return await _render_wizard(request, ctx, 1, _wizard_values({}))


@router.post("/loans/new", name="loan_new_step")
async def loan_new_step(request: Request, ctx: CurrentContext) -> Response:
    require_role(ctx, Role.EDITOR)
    raw = await _form_values(request)
    values = _wizard_values(raw)
    try:
        requested_step = int(raw.get("step", "1") or "1")
    except ValueError:
        requested_step = 1
    step = max(1, min(4, requested_step))
    if raw.get("go") == "back":
        return await _render_wizard(request, ctx, max(1, step - 1), values)
    reference_rates: tuple[loans.ReferenceRateView, ...] = ()
    if step >= 3:
        page = await run_in_threadpool(
            loans.list_reference_rates, request.app.state.session_factory, ctx
        )
        reference_rates = page.rates
    high_rate = False
    try:
        for checked in range(1, step + 1):
            high_rate = _check_step(request, checked, values, ctx, reference_rates) or high_rate
    except (loans.LoanError, LoanValidationError) as exc:
        code = getattr(exc, "code", "failed")
        return await _render_wizard(
            request,
            ctx,
            step,
            values,
            error=_error(request, code),
            high_rate=high_rate or code == "high_rate_confirmation_required",
            status_code=400,
        )
    if step < 4:
        return await _render_wizard(request, ctx, step + 1, values)
    try:
        loan_id = await run_in_threadpool(
            loans.create_loan,
            request.app.state.session_factory,
            ctx,
            entry_path=cast(loans.EntryPath, values["entry_path"]),
            name=values["name"],
            kind=values["kind"],
            principal_original=_money(
                values, "principal_original", request.state.locale, ctx.workspace.base_currency
            ),
            current_balance=_money(
                values, "current_balance", request.state.locale, ctx.workspace.base_currency
            ),
            balance_date=_optional_date(values["balance_date"], "balance_date"),
            origination_date=_optional_date(values["origination_date"], "origination_date"),
            first_payment_date=_date(values["first_payment_date"], "first_payment_date"),
            term_months=_integer(values["term_months"], "term_months"),
            payment_day=_integer(values["payment_day"], "payment_day"),
            rate_mode=values["rate_mode"],
            fixed_rate_bp=_percent(values, "fixed_rate_percent", request.state.locale),
            reference_rate_id=_optional_id(values["reference_rate_id"]),
            margin_bp=_percent(values, "margin_percent", request.state.locale),
            high_rate_confirmed=values["high_rate_confirmed"] == "1",
            installment_override=_money(
                values, "installment_override", request.state.locale, ctx.workspace.base_currency
            ),
            overpayment_mode=values["overpayment_mode"],
            overpayment_amount=_money(
                values, "overpayment_amount", request.state.locale, ctx.workspace.base_currency
            ),
            overpayment_strategy=values["overpayment_strategy"],
        )
    except (loans.LoanError, LoanValidationError) as exc:
        code = getattr(exc, "code", "failed")
        target_step = 3 if code == "high_rate_confirmation_required" else 4
        return await _render_wizard(
            request,
            ctx,
            target_step,
            values,
            error=_error(request, code),
            high_rate=code == "high_rate_confirmation_required",
            status_code=400,
        )
    return RedirectResponse(
        request.url_for("loan_detail", loan_id=str(loan_id)).path
        + "?"
        + urlencode({"notice": "loan_created"}),
        status_code=303,
    )


def _detail_context(
    request: Request,
    ctx: Context,
    loan_id: UUID,
    *,
    error: str | None = None,
    notice: str | None = None,
    form_values: dict[str, str] | None = None,
    open_form: str | None = None,
    edit_id: str | None = None,
) -> dict[str, object]:
    view = loans.get_loan(request.app.state.session_factory, ctx, loan_id)
    rates = loans.list_reference_rates(request.app.state.session_factory, ctx).rates
    return {
        "loan": view,
        "reference_rates": rates,
        "can_edit": has_role(ctx, Role.EDITOR),
        "error": error,
        "notice": notice,
        "form_values": form_values or {},
        "open_form": open_form,
        "edit_id": edit_id,
        "today": ctx.clock.today(ctx.workspace.timezone),
        "format_money": format_money,
        "format_day": format_day,
        "format_amount_input": format_amount_input,
        "format_quantity_input": format_quantity_input,
        "format_rate_short": format_rate_short,
        "rate_input": lambda bp: _rate_input(bp, request.state.locale),
    }


def _redirect_detail(
    request: Request,
    loan_id: UUID,
    *,
    notice: str | None = None,
    error: str | None = None,
    anchor: str | None = None,
    form: str | None = None,
    edit: str | None = None,
) -> RedirectResponse:
    query: dict[str, str] = {}
    if notice:
        query["notice"] = notice
    if error:
        query["error"] = error
    if form:
        query["form"] = form
    if edit:
        query["edit"] = edit
    url = request.url_for("loan_detail", loan_id=str(loan_id)).path
    if query:
        url += "?" + urlencode(query)
    if anchor:
        url += f"#{anchor}"
    return RedirectResponse(url, status_code=303)


@router.get("/loans/reference-rates", name="reference_rates")
async def reference_rates_page(request: Request, ctx: CurrentContext) -> Response:
    page = await run_in_threadpool(
        loans.list_reference_rates, request.app.state.session_factory, ctx
    )
    return render_template(
        request,
        "loans/reference_rates.html",
        context={
            "page": page,
            "can_edit": has_role(ctx, Role.EDITOR),
            "notice": _notice(request, request.query_params.get("notice")),
            "error": _error(request, request.query_params.get("error")),
            "form_values": {},
            "open_form": request.query_params.get("form"),
            "edit_id": request.query_params.get("edit"),
            "value_edit_id": request.query_params.get("value"),
            "today": ctx.clock.today(ctx.workspace.timezone),
            "format_day": format_day,
            "format_rate_short": format_rate_short,
            "format_amount_input": format_amount_input,
            "rate_input": lambda bp: _rate_input(bp, request.state.locale),
        },
    )


@router.post("/loans/reference-rates", name="reference_rate_create")
async def reference_rate_create(request: Request, ctx: CurrentContext) -> Response:
    values = await _form_values(request)
    try:
        await run_in_threadpool(
            loans.create_reference_rate,
            request.app.state.session_factory,
            ctx,
            name=values.get("name", ""),
            notes=values.get("notes", ""),
        )
    except (loans.LoanError, LoanValidationError) as exc:
        page = await run_in_threadpool(
            loans.list_reference_rates, request.app.state.session_factory, ctx
        )
        return render_template(
            request,
            "loans/reference_rates.html",
            status_code=400,
            context={
                "page": page,
                "can_edit": True,
                "error": _error(request, getattr(exc, "code", "failed")),
                "notice": None,
                "form_values": values,
                "open_form": "rate",
                "edit_id": None,
                "value_edit_id": None,
                "today": ctx.clock.today(ctx.workspace.timezone),
                "format_day": format_day,
                "format_rate_short": format_rate_short,
                "format_amount_input": format_amount_input,
                "rate_input": lambda bp: _rate_input(bp, request.state.locale),
            },
        )
    return RedirectResponse(
        request.url_for("reference_rates").path
        + "?"
        + urlencode({"notice": "reference_rate_saved"}),
        status_code=303,
    )


@router.post("/loans/reference-rates/{rate_id}", name="reference_rate_update")
async def reference_rate_update(request: Request, ctx: CurrentContext, rate_id: UUID) -> Response:
    values = await _form_values(request)
    try:
        await run_in_threadpool(
            loans.update_reference_rate,
            request.app.state.session_factory,
            ctx,
            rate_id,
            name=values.get("name", ""),
            notes=values.get("notes", ""),
        )
    except (loans.LoanError, LoanValidationError) as exc:
        page = await run_in_threadpool(
            loans.list_reference_rates, request.app.state.session_factory, ctx
        )
        return render_template(
            request,
            "loans/reference_rates.html",
            status_code=400,
            context={
                "page": page,
                "can_edit": True,
                "error": _error(request, getattr(exc, "code", "failed")),
                "notice": None,
                "form_values": values,
                "open_form": "rate",
                "edit_id": str(rate_id),
                "value_edit_id": None,
                "today": ctx.clock.today(ctx.workspace.timezone),
                "format_day": format_day,
                "format_rate_short": format_rate_short,
                "format_amount_input": format_amount_input,
                "rate_input": lambda bp: _rate_input(bp, request.state.locale),
            },
        )
    return RedirectResponse(
        request.url_for("reference_rates").path
        + "?"
        + urlencode({"notice": "reference_rate_saved"}),
        status_code=303,
    )


@router.post("/loans/reference-rates/{rate_id}/delete", name="reference_rate_delete")
async def reference_rate_delete(request: Request, ctx: CurrentContext, rate_id: UUID) -> Response:
    try:
        await run_in_threadpool(
            loans.delete_reference_rate,
            request.app.state.session_factory,
            ctx,
            rate_id,
        )
    except (loans.LoanError, LoanValidationError) as exc:
        return RedirectResponse(
            request.url_for("reference_rates").path
            + "?"
            + urlencode({"error": getattr(exc, "code", "failed")}),
            status_code=303,
        )
    return RedirectResponse(
        request.url_for("reference_rates").path
        + "?"
        + urlencode({"notice": "reference_rate_deleted"}),
        status_code=303,
    )


@router.post("/loans/reference-rates/{rate_id}/values", name="reference_rate_value_create")
async def reference_rate_value_create(
    request: Request, ctx: CurrentContext, rate_id: UUID
) -> Response:
    values = await _form_values(request)
    try:
        value_bp = _percent(values, "value_percent", request.state.locale)
        if value_bp is None:
            raise loans.LoanError("value_percent", "invalid_rate")
        await run_in_threadpool(
            loans.add_reference_rate_value,
            request.app.state.session_factory,
            ctx,
            rate_id,
            effective_from=_date(values.get("effective_from", ""), "effective_from"),
            value_bp=value_bp,
            high_rate_confirmed=values.get("high_rate_confirmed") == "1",
        )
    except (loans.LoanError, LoanValidationError) as exc:
        page = await run_in_threadpool(
            loans.list_reference_rates, request.app.state.session_factory, ctx
        )
        return render_template(
            request,
            "loans/reference_rates.html",
            status_code=400,
            context={
                "page": page,
                "can_edit": True,
                "error": _error(request, getattr(exc, "code", "failed")),
                "notice": None,
                "form_values": values,
                "open_form": "value",
                "edit_id": str(rate_id),
                "value_edit_id": None,
                "today": ctx.clock.today(ctx.workspace.timezone),
                "format_day": format_day,
                "format_rate_short": format_rate_short,
                "format_amount_input": format_amount_input,
                "rate_input": lambda bp: _rate_input(bp, request.state.locale),
            },
        )
    return RedirectResponse(
        request.url_for("reference_rates").path
        + "?"
        + urlencode({"notice": "reference_value_saved"}),
        status_code=303,
    )


@router.post(
    "/loans/reference-rates/{rate_id}/values/{value_id}",
    name="reference_rate_value_update",
)
async def reference_rate_value_update(
    request: Request, ctx: CurrentContext, rate_id: UUID, value_id: UUID
) -> Response:
    values = await _form_values(request)
    try:
        value_bp = _percent(values, "value_percent", request.state.locale)
        if value_bp is None:
            raise loans.LoanError("value_percent", "invalid_rate")
        await run_in_threadpool(
            loans.update_reference_rate_value,
            request.app.state.session_factory,
            ctx,
            rate_id,
            value_id,
            effective_from=_date(values.get("effective_from", ""), "effective_from"),
            value_bp=value_bp,
            high_rate_confirmed=values.get("high_rate_confirmed") == "1",
        )
    except (loans.LoanError, LoanValidationError) as exc:
        page = await run_in_threadpool(
            loans.list_reference_rates, request.app.state.session_factory, ctx
        )
        return render_template(
            request,
            "loans/reference_rates.html",
            status_code=400,
            context={
                "page": page,
                "can_edit": True,
                "error": _error(request, getattr(exc, "code", "failed")),
                "notice": None,
                "form_values": values,
                "open_form": "value",
                "edit_id": str(rate_id),
                "value_edit_id": str(value_id),
                "today": ctx.clock.today(ctx.workspace.timezone),
                "format_day": format_day,
                "format_rate_short": format_rate_short,
                "format_amount_input": format_amount_input,
                "rate_input": lambda bp: _rate_input(bp, request.state.locale),
            },
        )
    return RedirectResponse(
        request.url_for("reference_rates").path
        + "?"
        + urlencode({"notice": "reference_value_saved"}),
        status_code=303,
    )


@router.post(
    "/loans/reference-rates/{rate_id}/values/{value_id}/delete",
    name="reference_rate_value_delete",
)
async def reference_rate_value_delete(
    request: Request, ctx: CurrentContext, rate_id: UUID, value_id: UUID
) -> Response:
    try:
        await run_in_threadpool(
            loans.delete_reference_rate_value,
            request.app.state.session_factory,
            ctx,
            rate_id,
            value_id,
        )
    except (loans.LoanError, LoanValidationError) as exc:
        return RedirectResponse(
            request.url_for("reference_rates").path
            + "?"
            + urlencode({"error": getattr(exc, "code", "failed")}),
            status_code=303,
        )
    return RedirectResponse(
        request.url_for("reference_rates").path
        + "?"
        + urlencode({"notice": "reference_value_deleted"}),
        status_code=303,
    )


@router.get("/loans/{loan_id}", name="loan_detail")
async def loan_detail(request: Request, ctx: CurrentContext, loan_id: UUID) -> Response:
    context = await run_in_threadpool(
        _detail_context,
        request,
        ctx,
        loan_id,
        error=_error(request, request.query_params.get("error")),
        notice=_notice(request, request.query_params.get("notice")),
        open_form=request.query_params.get("form"),
        edit_id=request.query_params.get("edit"),
    )
    return render_template(request, "loans/detail.html", context=context)


@router.get("/loans/{loan_id}/edit", name="loan_edit")
async def loan_edit(request: Request, ctx: CurrentContext, loan_id: UUID) -> Response:
    require_role(ctx, Role.EDITOR)
    view = await run_in_threadpool(loans.get_loan, request.app.state.session_factory, ctx, loan_id)
    rates = await run_in_threadpool(
        loans.list_reference_rates, request.app.state.session_factory, ctx
    )
    return render_template(
        request,
        "loans/edit.html",
        context={
            "loan": view,
            "reference_rates": rates.rates,
            "values": _loan_values_from_view(view, request.state.locale),
            "error": None,
            "can_edit": has_role(ctx, Role.EDITOR),
            "format_money": format_money,
            "format_day": format_day,
            "format_amount_input": format_amount_input,
            "format_rate_short": format_rate_short,
        },
    )


@router.post("/loans/{loan_id}/edit", name="loan_update")
async def loan_update(request: Request, ctx: CurrentContext, loan_id: UUID) -> Response:
    values = await _form_values(request)
    view = await run_in_threadpool(loans.get_loan, request.app.state.session_factory, ctx, loan_id)
    try:
        await run_in_threadpool(
            loans.update_loan_terms,
            request.app.state.session_factory,
            ctx,
            loan_id,
            name=values.get("name", ""),
            kind=values.get("kind", ""),
            principal_original=_money(
                values, "principal_original", request.state.locale, view.currency
            ),
            current_balance=_money(values, "current_balance", request.state.locale, view.currency),
            balance_date=_optional_date(values.get("balance_date", ""), "balance_date"),
            origination_date=_optional_date(values.get("origination_date", ""), "origination_date"),
            first_payment_date=_date(values.get("first_payment_date", ""), "first_payment_date"),
            term_months=_integer(values.get("term_months", ""), "term_months"),
            payment_day=_integer(values.get("payment_day", ""), "payment_day"),
            installment_override=_money(
                values, "installment_override", request.state.locale, view.currency
            ),
            reference_rate_id=_optional_id(values.get("reference_rate_id", "")),
            margin_bp=_percent(values, "margin_percent", request.state.locale),
            high_rate_confirmed=values.get("high_rate_confirmed") == "1",
        )
    except (loans.LoanError, LoanValidationError) as exc:
        rates = await run_in_threadpool(
            loans.list_reference_rates, request.app.state.session_factory, ctx
        )
        return render_template(
            request,
            "loans/edit.html",
            status_code=400,
            context={
                "loan": view,
                "reference_rates": rates.rates,
                "values": {**_loan_values_from_view(view, request.state.locale), **values},
                "error": _error(request, getattr(exc, "code", "failed")),
                "can_edit": True,
                "format_money": format_money,
                "format_day": format_day,
                "format_amount_input": format_amount_input,
                "format_rate_short": format_rate_short,
            },
        )
    return RedirectResponse(
        request.url_for("loan_detail", loan_id=str(loan_id)).path
        + "?"
        + urlencode({"notice": "terms_saved"}),
        status_code=303,
    )


@router.post("/loans/{loan_id}/overpayment-settings", name="loan_overpayment_settings")
async def loan_overpayment_settings(
    request: Request, ctx: CurrentContext, loan_id: UUID
) -> Response:
    values = await _form_values(request)
    view = await run_in_threadpool(loans.get_loan, request.app.state.session_factory, ctx, loan_id)
    try:
        await run_in_threadpool(
            loans.set_overpayment_settings,
            request.app.state.session_factory,
            ctx,
            loan_id,
            mode=values.get("overpayment_mode", "none"),
            amount=_money(values, "overpayment_amount", request.state.locale, view.currency),
            strategy=values.get("overpayment_strategy", "reduce_term"),
        )
    except (loans.LoanError, LoanValidationError) as exc:
        return RedirectResponse(
            request.url_for("loan_detail", loan_id=str(loan_id)).path
            + "?"
            + urlencode({"error": getattr(exc, "code", "failed")}),
            status_code=303,
        )
    return RedirectResponse(
        request.url_for("loan_detail", loan_id=str(loan_id)).path
        + "?"
        + urlencode({"notice": "overpayment_saved"}),
        status_code=303,
    )


@router.post("/loans/{loan_id}/archive", name="loan_archive")
async def loan_archive(request: Request, ctx: CurrentContext, loan_id: UUID) -> Response:
    await run_in_threadpool(loans.archive_loan, request.app.state.session_factory, ctx, loan_id)
    return RedirectResponse(
        request.url_for("loan_detail", loan_id=str(loan_id)).path
        + "?"
        + urlencode({"notice": "loan_archived"}),
        status_code=303,
    )


@router.post("/loans/{loan_id}/restore", name="loan_restore")
async def loan_restore(request: Request, ctx: CurrentContext, loan_id: UUID) -> Response:
    await run_in_threadpool(loans.restore_loan, request.app.state.session_factory, ctx, loan_id)
    return RedirectResponse(
        request.url_for("loan_detail", loan_id=str(loan_id)).path
        + "?"
        + urlencode({"notice": "loan_restored"}),
        status_code=303,
    )


@router.post("/loans/{loan_id}/rate-changes", name="loan_rate_change_create")
async def loan_rate_change_create(request: Request, ctx: CurrentContext, loan_id: UUID) -> Response:
    values = await _form_values(request)
    try:
        rate_bp = _percent(values, "rate_percent", request.state.locale)
        if rate_bp is None:
            raise loans.LoanError("rate_percent", "invalid_rate")
        await run_in_threadpool(
            loans.add_rate_change,
            request.app.state.session_factory,
            ctx,
            loan_id,
            effective_from=_date(values.get("effective_from", ""), "effective_from"),
            rate_bp=rate_bp,
            high_rate_confirmed=values.get("high_rate_confirmed") == "1",
        )
    except (loans.LoanError, LoanValidationError) as exc:
        context = await run_in_threadpool(
            _detail_context,
            request,
            ctx,
            loan_id,
            error=_error(request, getattr(exc, "code", "failed")),
            form_values=values,
            open_form="rate",
            edit_id=values.get("change_id"),
        )
        return render_template(request, "loans/detail.html", status_code=400, context=context)
    return RedirectResponse(
        request.url_for("loan_detail", loan_id=str(loan_id)).path
        + "?"
        + urlencode({"notice": "rate_saved"})
        + "#rates",
        status_code=303,
    )


@router.post("/loans/{loan_id}/rate-changes/{change_id}", name="loan_rate_change_update")
async def loan_rate_change_update(
    request: Request, ctx: CurrentContext, loan_id: UUID, change_id: UUID
) -> Response:
    values = await _form_values(request)
    try:
        rate_bp = _percent(values, "rate_percent", request.state.locale)
        if rate_bp is None:
            raise loans.LoanError("rate_percent", "invalid_rate")
        await run_in_threadpool(
            loans.update_rate_change,
            request.app.state.session_factory,
            ctx,
            loan_id,
            change_id,
            effective_from=_date(values.get("effective_from", ""), "effective_from"),
            rate_bp=rate_bp,
            high_rate_confirmed=values.get("high_rate_confirmed") == "1",
        )
    except (loans.LoanError, LoanValidationError) as exc:
        context = await run_in_threadpool(
            _detail_context,
            request,
            ctx,
            loan_id,
            error=_error(request, getattr(exc, "code", "failed")),
            form_values=values,
            open_form="rate",
            edit_id=str(change_id),
        )
        return render_template(request, "loans/detail.html", status_code=400, context=context)
    return RedirectResponse(
        request.url_for("loan_detail", loan_id=str(loan_id)).path
        + "?"
        + urlencode({"notice": "rate_saved"})
        + "#rates",
        status_code=303,
    )


@router.post(
    "/loans/{loan_id}/rate-changes/{change_id}/delete",
    name="loan_rate_change_delete",
)
async def loan_rate_change_delete(
    request: Request, ctx: CurrentContext, loan_id: UUID, change_id: UUID
) -> Response:
    try:
        await run_in_threadpool(
            loans.delete_rate_change,
            request.app.state.session_factory,
            ctx,
            loan_id,
            change_id,
        )
    except (loans.LoanError, LoanValidationError) as exc:
        return _redirect_detail(
            request, loan_id, error=getattr(exc, "code", "failed"), anchor="rates"
        )
    return _redirect_detail(request, loan_id, notice="rate_deleted", anchor="rates")


@router.post("/loans/{loan_id}/costs", name="loan_cost_create")
async def loan_cost_create(request: Request, ctx: CurrentContext, loan_id: UUID) -> Response:
    values = await _form_values(request)
    view = await run_in_threadpool(loans.get_loan, request.app.state.session_factory, ctx, loan_id)
    try:
        fixed = (
            _money(values, "fixed_amount", request.state.locale, view.currency)
            if values.get("mode") == "fixed"
            else None
        )
        await run_in_threadpool(
            loans.add_cost_component,
            request.app.state.session_factory,
            ctx,
            loan_id,
            name=values.get("name", ""),
            mode=values.get("mode", "percent"),
            rate_bp=(
                _percent(values, "rate_percent", request.state.locale)
                if values.get("mode") == "percent"
                else None
            ),
            fixed=fixed,
            effective_from=_date(values.get("effective_from", ""), "effective_from"),
            effective_to=_optional_date(values.get("effective_to", ""), "effective_to"),
        )
    except (loans.LoanError, LoanValidationError) as exc:
        context = await run_in_threadpool(
            _detail_context,
            request,
            ctx,
            loan_id,
            error=_error(request, getattr(exc, "code", "failed")),
            form_values=values,
            open_form="cost",
            edit_id=values.get("component_id"),
        )
        return render_template(request, "loans/detail.html", status_code=400, context=context)
    return _redirect_detail(request, loan_id, notice="cost_saved", anchor="costs")


@router.post("/loans/{loan_id}/costs/{component_id}", name="loan_cost_update")
async def loan_cost_update(
    request: Request, ctx: CurrentContext, loan_id: UUID, component_id: UUID
) -> Response:
    values = await _form_values(request)
    view = await run_in_threadpool(loans.get_loan, request.app.state.session_factory, ctx, loan_id)
    try:
        fixed = (
            _money(values, "fixed_amount", request.state.locale, view.currency)
            if values.get("mode") == "fixed"
            else None
        )
        await run_in_threadpool(
            loans.update_cost_component,
            request.app.state.session_factory,
            ctx,
            loan_id,
            component_id,
            name=values.get("name", ""),
            mode=values.get("mode", "percent"),
            rate_bp=(
                _percent(values, "rate_percent", request.state.locale)
                if values.get("mode") == "percent"
                else None
            ),
            fixed=fixed,
            effective_from=_date(values.get("effective_from", ""), "effective_from"),
            effective_to=_optional_date(values.get("effective_to", ""), "effective_to"),
        )
    except (loans.LoanError, LoanValidationError) as exc:
        context = await run_in_threadpool(
            _detail_context,
            request,
            ctx,
            loan_id,
            error=_error(request, getattr(exc, "code", "failed")),
            form_values=values,
            open_form="cost",
            edit_id=str(component_id),
        )
        return render_template(request, "loans/detail.html", status_code=400, context=context)
    return _redirect_detail(request, loan_id, notice="cost_saved", anchor="costs")


@router.post("/loans/{loan_id}/costs/{component_id}/delete", name="loan_cost_delete")
async def loan_cost_delete(
    request: Request, ctx: CurrentContext, loan_id: UUID, component_id: UUID
) -> Response:
    try:
        await run_in_threadpool(
            loans.delete_cost_component,
            request.app.state.session_factory,
            ctx,
            loan_id,
            component_id,
        )
    except (loans.LoanError, LoanValidationError) as exc:
        return _redirect_detail(
            request, loan_id, error=getattr(exc, "code", "failed"), anchor="costs"
        )
    return _redirect_detail(request, loan_id, notice="cost_deleted", anchor="costs")


@router.post("/loans/{loan_id}/overpayments", name="loan_overpayment_create")
async def loan_overpayment_create(request: Request, ctx: CurrentContext, loan_id: UUID) -> Response:
    values = await _form_values(request)
    view = await run_in_threadpool(loans.get_loan, request.app.state.session_factory, ctx, loan_id)
    try:
        amount = _money(values, "amount", request.state.locale, view.currency)
        if amount is None:
            raise loans.LoanError("amount", "invalid_overpayment")
        await run_in_threadpool(
            loans.add_overpayment,
            request.app.state.session_factory,
            ctx,
            loan_id,
            date_value=_date(values.get("date", ""), "date"),
            amount=amount,
            note=values.get("note", ""),
        )
    except (loans.LoanError, LoanValidationError) as exc:
        context = await run_in_threadpool(
            _detail_context,
            request,
            ctx,
            loan_id,
            error=_error(request, getattr(exc, "code", "failed")),
            form_values=values,
            open_form="overpayment",
            edit_id=values.get("overpayment_id"),
        )
        return render_template(request, "loans/detail.html", status_code=400, context=context)
    return _redirect_detail(
        request, loan_id, notice="overpayment_item_saved", anchor="overpayments"
    )


@router.post(
    "/loans/{loan_id}/overpayments/{overpayment_id}",
    name="loan_overpayment_update",
)
async def loan_overpayment_update(
    request: Request, ctx: CurrentContext, loan_id: UUID, overpayment_id: UUID
) -> Response:
    values = await _form_values(request)
    view = await run_in_threadpool(loans.get_loan, request.app.state.session_factory, ctx, loan_id)
    try:
        amount = _money(values, "amount", request.state.locale, view.currency)
        if amount is None:
            raise loans.LoanError("amount", "invalid_overpayment")
        await run_in_threadpool(
            loans.update_overpayment,
            request.app.state.session_factory,
            ctx,
            loan_id,
            overpayment_id,
            date_value=_date(values.get("date", ""), "date"),
            amount=amount,
            note=values.get("note", ""),
        )
    except (loans.LoanError, LoanValidationError) as exc:
        context = await run_in_threadpool(
            _detail_context,
            request,
            ctx,
            loan_id,
            error=_error(request, getattr(exc, "code", "failed")),
            form_values=values,
            open_form="overpayment",
            edit_id=str(overpayment_id),
        )
        return render_template(request, "loans/detail.html", status_code=400, context=context)
    return _redirect_detail(
        request, loan_id, notice="overpayment_item_saved", anchor="overpayments"
    )


@router.post(
    "/loans/{loan_id}/overpayments/{overpayment_id}/delete",
    name="loan_overpayment_delete",
)
async def loan_overpayment_delete(
    request: Request, ctx: CurrentContext, loan_id: UUID, overpayment_id: UUID
) -> Response:
    try:
        await run_in_threadpool(
            loans.delete_overpayment,
            request.app.state.session_factory,
            ctx,
            loan_id,
            overpayment_id,
        )
    except (loans.LoanError, LoanValidationError) as exc:
        return _redirect_detail(
            request,
            loan_id,
            error=getattr(exc, "code", "failed"),
            anchor="overpayments",
        )
    return _redirect_detail(
        request, loan_id, notice="overpayment_item_deleted", anchor="overpayments"
    )
