"""Loans (F7.1): list, two-path wizard, detail, terms edit and reference rates."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import cast
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from assetboard.db.repositories import NotFound
from assetboard.domain.loans import (
    HIGH_RATE_CONFIRMATION_BP,
    LoanKind,
    LoanValidationError,
    OverpaymentMode,
    OverpaymentStrategy,
    RateMode,
    ScheduleRow,
    clean_loan_name,
    latest_value_on_or_before,
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
from assetboard.services import installment_plans as installment_plan_service
from assetboard.services import loan_links as loan_link_service
from assetboard.services import loan_payments as loan_payment_service
from assetboard.services import loan_scenarios as loan_scenario_service
from assetboard.services import loan_schedule as loan_schedule_service
from assetboard.services import loan_scheduled as loan_scheduled_service
from assetboard.services import loans
from assetboard.services import occurrences as occurrence_service
from assetboard.services.categories import list_categories
from assetboard.services.context import Context, Role, has_role, require_role
from assetboard.web.context import CurrentContext
from assetboard.web.forms import InvalidPercentOrFixed, percent_to_bp, signed_percent_to_bp
from assetboard.web.loan_charts import build_loan_charts, build_scenario_chart
from assetboard.web.loan_page import (
    scenario_presets,
    scenario_summary,
    schedule_csv,
    schedule_page,
)
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
    "invalid_day_count": _("Choose a valid interest calculation."),
    "no_anchor": _("The loan has no starting balance to build a schedule from."),
    "duplicate_rate": _("Two rate changes start on the same date."),
    "negative_amortization": _(
        "The installment is too low to cover the interest. Raise the installment override."
    ),
    "invalid_payment_date": _("The payment dates are invalid. Check the first payment date."),
    "negative_closing": _("The schedule produced a negative balance. Check the loan terms."),
    "invalid_overpayment_mode": _("Choose a valid overpayment type."),
    "invalid_strategy": _("Choose a valid overpayment strategy."),
    "rate_shift_pair_required": _("Enter both the rate shift and the date it starts, or neither."),
    "one_off_pair_required": _("Enter both the one-off amount and its date, or neither."),
    "invalid_rate_shift": _("The rate shift must leave every rate between 0% and 100%."),
    "too_many_scenarios": _("Compare at most three what-if rows at a time."),
    "invalid_payment_kind": _("Choose installment or overpayment."),
    "invalid_payment_total": _("Enter a positive payment total."),
    "no_scheduled_payment": _("No scheduled payment row exists on or after that date."),
    "payment_breakdown_missing": _("Enter all four breakdown amounts."),
    "payment_breakdown_mismatch": _("The breakdown must add up to the payment total."),
    "invalid_payment_breakdown": _("Breakdown amounts cannot be negative."),
    "transaction_not_found": _("The linked transaction no longer exists."),
    "transaction_deleted": _("The linked transaction was deleted."),
    "transaction_not_expense": _("Only an expense transaction can be linked."),
    "transaction_currency": _("The linked transaction uses another currency."),
    "transaction_amount": _("The linked transaction amount must equal the payment total."),
    "checkpoint_future": _("A checkpoint date cannot be in the future."),
    "checkpoint_before_start": _("A checkpoint cannot start before the loan."),
    "checkpoint_exists": _("A checkpoint already exists on that date."),
    "starting_checkpoint_required": _(
        "The starting balance checkpoint is required for a loan entered from its current balance."
    ),
    "schedule_unavailable": _("The loan schedule is unavailable."),
    "property_archived": _("The property item is archived."),
    "property_not_asset": _("Choose an asset item for the property."),
    "property_not_real_estate": _("Choose an item in a real-estate category."),
    "property_currency": _("The property and the loan must use the same currency."),
    "no_property": _("No property is linked to this loan."),
    "property_missing": _("The linked property item no longer exists."),
    "no_value": _("The property has no value on that date."),
    "value_zero": _("The property value is zero, so LTV is unavailable."),
    "balance_unavailable": _("The loan balance is unavailable on that date."),
    "invalid_category": _("Choose an active expense category."),
    "loan_paid_off": _("The loan has no future scheduled payments."),
    "invalid_loan": _("The loan is no longer available."),
    "installment_plan_closed": _("A closed installment plan cannot be restored."),
    "no_due_installment": _("No installment is due yet."),
    "month_closed": _("The month is closed. Reopen it before posting the installment."),
    "not_pending": _("That installment is no longer pending."),
    "failed": _("The loan could not be saved. Check the form and try again."),
}

NOTICES = {
    "loan_created": _("Loan saved. Add rate changes, costs or one-off overpayments below."),
    "loan_created_another": _("Loan saved. Add the next loan below."),
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
    "scenario_saved": _("Saved scenarios updated. Their comparison is below."),
    "scenario_deleted": _("Scenario deleted."),
    "payment_saved": _("Payment saved."),
    "payment_deleted": _("Payment deleted."),
    "checkpoint_saved": _("Balance checkpoint saved."),
    "checkpoint_deleted": _("Balance checkpoint deleted."),
    "networth_item_created": _("Linked net-worth item created."),
    "property_saved": _("Property linked."),
    "property_cleared": _("Property link removed."),
    "auto_record_saved": _("Automatic payment recording saved."),
    "schedule_generated": _("Recurring installments generated or refreshed."),
    "schedule_disabled": _("Recurring installment generation disabled."),
    "installment_plan_created": _("Installment plan created."),
    "installment_plan_updated": _("Installment plan updated."),
    "installment_plan_archived": _("Installment plan archived."),
    "installment_plan_restored": _("Installment plan restored."),
    "installment_plan_posted": _("Next installment posted."),
}

SETTINGS_FORMS = frozenset({"rate", "cost", "overpayment"})
ENTRY_PATHS = ("original", "current")
WIZARD_STEPS = (
    _("The loan"),
    _("Terms"),
    _("Interest"),
    _("Payments"),
)
COST_COMPONENT_ROWS = 3
COST_COMPONENT_FIELDS = tuple(
    f"cost_{field}_{index}"
    for index in range(1, COST_COMPONENT_ROWS + 1)
    for field in (
        "name",
        "mode",
        "rate_percent",
        "fixed_amount",
        "effective_from",
        "effective_to",
    )
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
    *COST_COMPONENT_FIELDS,
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
        *COST_COMPONENT_FIELDS,
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


def _reference_value_error_field(exc: Exception) -> str | None:
    field = getattr(exc, "field", None)
    if field in ("rate_bp", "value_percent"):
        return "value_percent"
    if field == "effective_from":
        return "effective_from"
    return None


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


def _wizard_costs(
    values: dict[str, str], locale: str, currency: str
) -> tuple[loans.CostComponentInput, ...]:
    costs: list[loans.CostComponentInput] = []
    for index in range(1, COST_COMPONENT_ROWS + 1):
        name = values.get(f"cost_name_{index}", "").strip()
        mode = values.get(f"cost_mode_{index}", "percent") or "percent"
        rate_text = values.get(f"cost_rate_percent_{index}", "").strip()
        fixed_text = values.get(f"cost_fixed_amount_{index}", "").strip()
        from_text = values.get(f"cost_effective_from_{index}", "").strip()
        to_text = values.get(f"cost_effective_to_{index}", "").strip()
        if not any((name, rate_text, fixed_text, from_text, to_text)):
            continue
        if not name:
            raise loans.LoanError(f"cost_name_{index}", "invalid_cost")
        costs.append(
            loans.CostComponentInput(
                name=name,
                mode=mode,
                rate_bp=_percent(values, f"cost_rate_percent_{index}", locale),
                fixed=_money(values, f"cost_fixed_amount_{index}", locale, currency),
                effective_from=_optional_date(from_text, f"cost_effective_from_{index}"),
                effective_to=_optional_date(to_text, f"cost_effective_to_{index}"),
            )
        )
    return tuple(costs)


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


def _wizard_review(
    request: Request,
    ctx: Context,
    values: dict[str, str],
    reference_rates: tuple[loans.ReferenceRateView, ...],
) -> dict[str, object]:
    """Validate the draft and calculate its first installment for the final review panel."""
    locale = request.state.locale
    currency = ctx.workspace.base_currency
    entry = values.get("entry_path")
    if entry not in ENTRY_PATHS:
        raise loans.LoanError("entry_path", "invalid_entry_path")
    kind = _loan_kind(values.get("kind", ""))
    principal = _money(values, "principal_original", locale, currency)
    balance = _money(values, "current_balance", locale, currency)
    term = _integer(values.get("term_months", ""), "term_months")
    first = _date(values.get("first_payment_date", ""), "first_payment_date")
    start = _wizard_start(values)
    if start is None:
        raise loans.LoanError("rate_mode", "invalid_date")
    mode = _rate_mode(values.get("rate_mode", ""))
    fixed_rate: int | None = None
    margin: int | None = None
    reference_name: str | None = None
    if mode is RateMode.FIXED:
        fixed_rate = _percent(values, "fixed_rate_percent", locale)
        rate_bp = fixed_rate
    else:
        rate_id = _optional_id(values.get("reference_rate_id", ""))
        if rate_id is None:
            raise loans.LoanError("reference_rate_id", "invalid_reference_rate")
        margin = _percent(values, "margin_percent", locale)
        if margin is None:
            raise loans.LoanError("margin_percent", "invalid_margin")
        reference = next((row for row in reference_rates if row.id == rate_id), None)
        if reference is None:
            raise loans.LoanError("reference_rate_id", "invalid_reference_rate")
        reference_value = latest_value_on_or_before(
            [(row.effective_from, row.value_bp) for row in reference.values], start
        )
        if reference_value is None:
            raise loans.LoanError("reference_rate_id", "missing_reference_value")
        reference_name = reference.name
        rate_bp = reference_value + margin
    override = _money(values, "installment_override", locale, currency)
    installment = loans.preview_first_installment(
        entry_path=cast(loans.EntryPath, entry),
        principal_original=principal,
        current_balance=balance,
        term_months=term,
        kind=kind,
        rate_bp=rate_bp,
        installment_override=override,
    )
    try:
        overpayment_mode = OverpaymentMode(values.get("overpayment_mode", "none"))
        overpayment_strategy = OverpaymentStrategy(
            values.get("overpayment_strategy", "reduce_term")
        )
    except ValueError:
        raise loans.LoanError("overpayment_mode", "invalid_overpayment") from None
    costs = _wizard_costs(values, locale, currency)
    return {
        "name": values.get("name", ""),
        "entry_path": entry,
        "kind": kind,
        "base": principal if entry == "original" else balance,
        "first_payment_date": first,
        "term_months": term,
        "rate_mode": mode,
        "fixed_rate_bp": fixed_rate,
        "reference_rate_name": reference_name,
        "margin_bp": margin,
        "effective_rate_bp": rate_bp,
        "installment_override": override,
        "first_installment": installment,
        "overpayment_mode": overpayment_mode,
        "overpayment_amount": _money(values, "overpayment_amount", locale, currency),
        "overpayment_strategy": overpayment_strategy,
        "costs": tuple(
            {
                "name": cost.name,
                "mode": cost.mode,
                "rate_bp": cost.rate_bp,
                "fixed": cost.fixed,
                "effective_from": cost.effective_from or start,
                "effective_to": cost.effective_to,
            }
            for cost in costs
        ),
    }


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
        "day_count": view.day_count.value,
        "installment_override": amount(view.installment_override),
    }


async def _wizard_context(
    request: Request,
    ctx: Context,
    step: int,
    values: dict[str, str],
    *,
    error: str | None = None,
    error_field: str | None = None,
    high_rate: bool = False,
    notice: str | None = None,
    review: dict[str, object] | None = None,
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
        "cost_rows": COST_COMPONENT_ROWS,
        "error": error,
        "error_field": error_field if error_field in STEP_FIELDS[step] else None,
        "error_is_field": error_field in STEP_FIELDS[step],
        "high_rate": high_rate,
        "notice": notice,
        "review": review,
        "can_edit": has_role(ctx, Role.EDITOR),
        "format_money": format_money,
        "format_day": format_day,
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
    error_field: str | None = None,
    high_rate: bool = False,
    notice: str | None = None,
    review: dict[str, object] | None = None,
    status_code: int = 200,
) -> Response:
    context = await _wizard_context(
        request,
        ctx,
        step,
        values,
        error=error,
        error_field=error_field,
        high_rate=high_rate,
        notice=notice,
        review=review,
    )
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
    return await _render_wizard(
        request,
        ctx,
        1,
        _wizard_values({}),
        notice=_notice(request, request.query_params.get("notice")),
    )


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
        field = getattr(exc, "field", None)
        return await _render_wizard(
            request,
            ctx,
            step,
            values,
            error=_error(request, code),
            error_field=field,
            high_rate=high_rate or code == "high_rate_confirmation_required",
            status_code=400,
        )
    if step < 4:
        next_step = step + 1
        if next_step == 4:
            try:
                review = _wizard_review(request, ctx, values, reference_rates)
            except (loans.LoanError, LoanValidationError) as exc:
                return await _render_wizard(
                    request,
                    ctx,
                    4,
                    values,
                    error=_error(request, getattr(exc, "code", "failed")),
                    error_field=getattr(exc, "field", None),
                    status_code=400,
                )
            return await _render_wizard(request, ctx, 4, values, review=review)
        return await _render_wizard(request, ctx, next_step, values)
    try:
        review = _wizard_review(request, ctx, values, reference_rates)
    except (loans.LoanError, LoanValidationError) as exc:
        return await _render_wizard(
            request,
            ctx,
            4,
            values,
            error=_error(request, getattr(exc, "code", "failed")),
            error_field=getattr(exc, "field", None),
            status_code=400,
        )
    save_add = raw.get("go") == "save_add"
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
            costs=_wizard_costs(values, request.state.locale, ctx.workspace.base_currency),
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
            error_field=getattr(exc, "field", None),
            high_rate=code == "high_rate_confirmation_required",
            review=review,
            status_code=400,
        )
    if save_add:
        return RedirectResponse(
            request.url_for("loan_new").path + "?notice=loan_created_another",
            status_code=303,
        )
    return _redirect_settings(request, loan_id, notice="loan_created")


def _property_as_of(request: Request, today: date) -> date:
    raw_as_of = request.query_params.get("property_as_of", "")
    if not raw_as_of:
        return today
    with contextlib.suppress(ValueError):
        return date.fromisoformat(raw_as_of)
    return today


def _quick_record_url(request: Request, loan_id: UUID, row: ScheduleRow, locale: str) -> str:
    query = urlencode(
        {
            "form": "payment",
            "payment_date": row.date.isoformat(),
            "payment_total": format_amount_input(row.installment_total, locale),
        }
    )
    return (
        request.url_for("loan_payments", loan_id=str(loan_id)).path
        + "?"
        + query
        + "#payment-dialog"
    )


def _detail_context(
    request: Request,
    ctx: Context,
    loan_id: UUID,
    *,
    error: str | None = None,
    notice: str | None = None,
) -> dict[str, object]:
    factory = request.app.state.session_factory
    data = loan_schedule_service.loan_schedule(factory, ctx, loan_id)
    today = ctx.clock.today(ctx.workspace.timezone)
    page = schedule_page(data.schedule, today, request.query_params.get("schedule_page"))
    schedule_view = "planned" if request.query_params.get("view") == "planned" else "actual"
    charts = build_loan_charts(data.schedule, data.baseline, request.state.locale)
    property_as_of = _property_as_of(request, today)
    can_edit = has_role(ctx, Role.EDITOR)
    quick_record_url = (
        _quick_record_url(request, loan_id, page.next_row, request.state.locale)
        if can_edit and not data.view.archived and page.next_row is not None
        else None
    )
    if data.view.archived:
        loan_status = "archived"
    elif data.summary is not None and data.summary.remaining_principal.amount_minor == 0:
        loan_status = "paid_off"
    else:
        loan_status = "active"
    return {
        "loan": data.view,
        "loan_status": loan_status,
        "schedule_data": data,
        "schedule": data.schedule,
        "summary": data.summary,
        "schedule_page": page,
        "schedule_view": schedule_view,
        "charts": charts,
        "schedule_error": _error(request, data.error_code) if data.error_code else None,
        "can_edit": can_edit,
        "error": error,
        "notice": notice,
        "today": today,
        "quick_record_url": quick_record_url,
        "plan_status": loan_payment_service.plan_status(factory, ctx, loan_id),
        "format_money": format_money,
        "format_day": format_day,
        "format_rate_short": format_rate_short,
        "property": loan_link_service.loan_property_view(factory, ctx, loan_id, property_as_of),
        "property_as_of": property_as_of,
        "property_reason": lambda code: _error(request, code),
    }


SETTINGS_FORM_FIELDS: dict[str, frozenset[str]] = {
    "rate": frozenset({"effective_from", "rate_percent"}),
    "cost": frozenset(
        {"name", "mode", "rate_percent", "fixed_amount", "effective_from", "effective_to"}
    ),
    "overpayment": frozenset({"date", "amount", "note"}),
}


def _settings_error_field(exc: Exception, form: str) -> str | None:
    """Map a service error to the input a settings action should highlight."""
    field = getattr(exc, "field", None)
    if field == "rate_bp" and form in ("rate", "cost"):
        return "rate_percent"
    return field if field in SETTINGS_FORM_FIELDS.get(form, frozenset()) else None


def _settings_context(
    request: Request,
    ctx: Context,
    loan_id: UUID,
    *,
    error: str | None = None,
    error_field: str | None = None,
    notice: str | None = None,
    form_values: dict[str, str] | None = None,
    open_form: str | None = None,
    edit_id: str | None = None,
) -> dict[str, object]:
    """Everything the settings cards and dialogs need; the schedule is not built here."""
    factory = request.app.state.session_factory
    view = loans.get_loan(factory, ctx, loan_id)
    today = ctx.clock.today(ctx.workspace.timezone)
    property_as_of = _property_as_of(request, today)
    return {
        "loan": view,
        "can_edit": has_role(ctx, Role.EDITOR),
        "error": error,
        "error_field": error_field,
        "notice": notice,
        "form_values": form_values or {},
        "open_form": open_form if open_form in SETTINGS_FORMS else None,
        "edit_id": edit_id,
        "today": today,
        "format_money": format_money,
        "format_day": format_day,
        "format_amount_input": format_amount_input,
        "format_rate_short": format_rate_short,
        "rate_input": lambda bp: _rate_input(bp, request.state.locale),
        "liability": loan_link_service.loan_liability_view(factory, ctx, loan_id),
        "property_choices": loan_link_service.property_choices(factory, ctx, loan_id),
        "property": loan_link_service.loan_property_view(factory, ctx, loan_id, property_as_of),
        "property_as_of": property_as_of,
        "property_reason": lambda code: _error(request, code),
        "scheduled_item": loan_scheduled_service.loan_schedule_item_view(factory, ctx, loan_id),
        "expense_categories": list_categories(factory, ctx).expense,
    }


def _redirect_settings(
    request: Request,
    loan_id: UUID,
    *,
    notice: str | None = None,
    error: str | None = None,
    error_field: str | None = None,
    anchor: str | None = None,
) -> RedirectResponse:
    query: dict[str, str] = {}
    if notice:
        query["notice"] = notice
    if error:
        query["error"] = error
    if error and error_field:
        query["field"] = error_field
    url = request.url_for("loan_settings", loan_id=str(loan_id)).path
    if query:
        url += "?" + urlencode(query)
    if anchor:
        url += f"#{anchor}"
    return RedirectResponse(url, status_code=303)


async def _render_settings_error(
    request: Request,
    ctx: Context,
    loan_id: UUID,
    exc: Exception,
    *,
    values: dict[str, str],
    form: str,
    edit_id: str | None,
) -> Response:
    context = await run_in_threadpool(
        _settings_context,
        request,
        ctx,
        loan_id,
        error=_error(request, getattr(exc, "code", "failed")),
        error_field=_settings_error_field(exc, form),
        form_values=values,
        open_form=form,
        edit_id=edit_id,
    )
    return render_template(request, "loans/settings.html", status_code=400, context=context)


def _installment_form_values(
    plan: installment_plan_service.InstallmentPlanView | None,
    locale: str,
    submitted: dict[str, str] | None = None,
) -> dict[str, str]:
    if plan is None:
        values = {
            "name": "",
            "installment": "",
            "remaining_count": "12",
            "payment_day": "1",
            "category_id": "",
        }
    else:
        values = {
            "name": plan.name,
            "installment": format_amount_input(plan.installment, locale),
            "remaining_count": str(plan.remaining_count),
            "payment_day": str(plan.loan.payment_day),
            "category_id": str(plan.category_id),
        }
    if submitted:
        values.update(submitted)
    return values


def _installment_plan_form_context(
    request: Request,
    ctx: Context,
    *,
    loan_id: UUID | None = None,
    values: dict[str, str] | None = None,
    error: str | None = None,
) -> dict[str, object]:
    plan = (
        installment_plan_service.get_installment_plan(
            request.app.state.session_factory, ctx, loan_id
        )
        if loan_id is not None
        else None
    )
    categories = list_categories(request.app.state.session_factory, ctx).expense
    return {
        "plan": plan,
        "loan": plan.loan if plan is not None else None,
        "categories": categories,
        "values": _installment_form_values(plan, request.state.locale, values),
        "currency": plan.currency if plan is not None else ctx.workspace.base_currency,
        "error": error,
        "can_edit": True,
        "format_money": format_money,
    }


def _installment_plan_detail_context(
    request: Request,
    ctx: Context,
    loan_id: UUID,
    *,
    error: str | None = None,
    notice: str | None = None,
) -> dict[str, object]:
    plan = installment_plan_service.get_installment_plan(
        request.app.state.session_factory, ctx, loan_id
    )
    factory = request.app.state.session_factory
    return {
        "plan": plan,
        "loan": plan.loan,
        "liability": loan_link_service.loan_liability_view(factory, ctx, loan_id),
        "scheduled_item": loan_scheduled_service.loan_schedule_item_view(factory, ctx, loan_id),
        "expense_categories": list_categories(factory, ctx).expense,
        "can_edit": has_role(ctx, Role.EDITOR),
        "error": error,
        "notice": notice,
        "format_money": format_money,
        "format_day": format_day,
        "format_rate_short": format_rate_short,
    }


def _installment_plans_context(
    request: Request,
    ctx: Context,
    *,
    error: str | None = None,
    notice: str | None = None,
) -> dict[str, object]:
    page = installment_plan_service.list_installment_plans(request.app.state.session_factory, ctx)
    return {
        "page": page,
        "can_edit": has_role(ctx, Role.EDITOR),
        "error": error,
        "notice": notice,
        "format_money": format_money,
        "format_day": format_day,
        "format_rate_short": format_rate_short,
    }


SCENARIO_ROW_COUNT = 3
SCENARIO_FIELDS = (
    "name",
    "overpayment_mode",
    "overpayment_amount",
    "overpayment_strategy",
    "rate_shift",
    "rate_shift_from",
    "one_off_amount",
    "one_off_date",
)
_SCENARIO_DEFAULTS = {"overpayment_mode": "same", "overpayment_strategy": "same"}


def _scenario_form_rows(values: dict[str, str] | None = None) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for number in range(1, SCENARIO_ROW_COUNT + 1):
        row: dict[str, str] = {}
        for field in SCENARIO_FIELDS:
            default = _SCENARIO_DEFAULTS.get(field, "")
            row[field] = (values or {}).get(f"{field}_{number}", default)
        rows.append(row)
    return rows


def _scenario_row_empty(row: dict[str, str]) -> bool:
    for field, value in row.items():
        if not value.strip():
            continue
        if field in _SCENARIO_DEFAULTS and value == _SCENARIO_DEFAULTS[field]:
            continue
        return False
    return True


def _scenario_mode(value: str) -> OverpaymentMode | None:
    if value in ("", "same"):
        return None
    try:
        return OverpaymentMode(value)
    except ValueError:
        raise loans.LoanError("overpayment_mode", "invalid_overpayment_mode") from None


def _scenario_strategy(value: str) -> OverpaymentStrategy | None:
    if value in ("", "same"):
        return None
    try:
        return OverpaymentStrategy(value)
    except ValueError:
        raise loans.LoanError("overpayment_strategy", "invalid_strategy") from None


def _signed_percent(values: dict[str, str], key: str, locale: str) -> int | None:
    text = values.get(key, "").strip()
    if not text:
        return None
    try:
        return signed_percent_to_bp(text, locale)
    except (ValueError, InvalidPercentOrFixed):
        raise loans.LoanError(key, "invalid_rate_shift") from None


def _scenario_parameters(
    row: dict[str, str], locale: str, currency: str
) -> loan_scenario_service.ScenarioParameters:
    return loan_scenario_service.ScenarioParameters(
        name=row["name"].strip() or None,
        overpayment_mode=_scenario_mode(row["overpayment_mode"].strip()),
        overpayment_amount=_money(row, "overpayment_amount", locale, currency),
        strategy=_scenario_strategy(row["overpayment_strategy"].strip()),
        rate_shift_bp=_signed_percent(row, "rate_shift", locale),
        rate_shift_from=_optional_date(row.get("rate_shift_from", ""), "rate_shift_from"),
        one_off_date=_optional_date(row.get("one_off_date", ""), "one_off_date"),
        one_off_amount=_money(row, "one_off_amount", locale, currency),
    )


def _scenario_page(
    request: Request,
    ctx: Context,
    loan_id: UUID,
    *,
    ephemeral: list[loan_scenario_service.ScenarioParameters],
    form_rows: list[dict[str, str]],
    error: str | None = None,
    notice: str | None = None,
) -> dict[str, object]:
    comparison = loan_scenario_service.compare_loan_scenarios(
        request.app.state.session_factory, ctx, loan_id, ephemeral
    )
    saved = loan_scenario_service.list_loan_scenarios(
        request.app.state.session_factory, ctx, loan_id
    )
    locale = request.state.locale
    saved_rows = {row.scenario_id: row for row in comparison.saved}
    return {
        "loan": comparison.view,
        "comparison": comparison,
        "saved": saved,
        "saved_cards": tuple((scenario, saved_rows.get(scenario.id)) for scenario in saved),
        "scenario_chart": build_scenario_chart(comparison, locale),
        "scenario_rows": form_rows,
        "scenario_presets": scenario_presets(comparison.view, locale),
        "scenario_error": _error(request, comparison.error_code) if comparison.error_code else None,
        "scenario_row_error": lambda code: _error(request, code),
        "scenario_summary": lambda parameters: scenario_summary(parameters, locale),
        "can_edit": has_role(ctx, Role.EDITOR),
        "error": error,
        "notice": notice,
        "today": ctx.clock.today(ctx.workspace.timezone),
        "format_money": format_money,
        "format_day": format_day,
        "format_rate_short": format_rate_short,
    }


def _money_or_zero(values: dict[str, str], key: str, locale: str, currency: str) -> Money:
    text = values.get(key, "").strip()
    if not text:
        return Money(0, currency)
    try:
        amount = parse_amount(text, locale)
        if amount == 0:
            return Money(0, currency)
        return Money(amount_minor(amount, currency), currency)
    except (ValueError, EntryError):
        raise loans.LoanError(key, "invalid_amount") from None


def _prefilled_date(text: str | None, fallback: date) -> str:
    if not text:
        return fallback.isoformat()
    with contextlib.suppress(ValueError):
        return date.fromisoformat(text).isoformat()
    return fallback.isoformat()


def _prefilled_total(text: str | None, locale: str, currency: str) -> str:
    if not text:
        return ""
    with contextlib.suppress(ValueError, EntryError):
        return format_amount_input(
            Money(amount_minor(parse_amount(text, locale), currency), currency), locale
        )
    return ""


def _payment_form_values(
    loan: loans.LoanView,
    payment: loan_payment_service.LoanPaymentView | None,
    locale: str,
    today: date,
    submitted: dict[str, str] | None = None,
    *,
    prefill_date: str | None = None,
    prefill_total: str | None = None,
) -> dict[str, str]:
    if payment is None:
        values = {
            "date": _prefilled_date(prefill_date, today),
            "kind": "installment",
            "total": _prefilled_total(prefill_total, locale, loan.currency),
            "principal": "",
            "interest": "",
            "costs": "",
            "overpayment": "",
            "note": "",
            "transaction_id": "",
            "use_breakdown": "0",
        }
    else:
        values = {
            "date": payment.date.isoformat(),
            "kind": payment.kind.value,
            "total": format_amount_input(payment.total, locale),
            "principal": format_amount_input(payment.principal, locale),
            "interest": format_amount_input(payment.interest, locale),
            "costs": format_amount_input(payment.costs, locale),
            "overpayment": format_amount_input(payment.overpayment, locale),
            "note": payment.note,
            "transaction_id": str(payment.transaction_id) if payment.transaction_id else "",
            "use_breakdown": "1",
        }
    if submitted:
        values.update(submitted)
    return values


def _checkpoint_form_values(
    checkpoint: loan_payment_service.CheckpointDetailView | None,
    locale: str,
    today: date,
    submitted: dict[str, str] | None = None,
) -> dict[str, str]:
    if checkpoint is None:
        values = {"as_of": today.isoformat(), "remaining_principal": "", "note": ""}
    else:
        values = {
            "as_of": checkpoint.as_of.isoformat(),
            "remaining_principal": format_amount_input(checkpoint.remaining_principal, locale),
            "note": checkpoint.note,
        }
    if submitted:
        values.update(submitted)
    return values


def _payments_context(
    request: Request,
    ctx: Context,
    loan_id: UUID,
    *,
    error: str | None = None,
    notice: str | None = None,
    form_values: dict[str, str] | None = None,
    open_form: str | None = None,
    edit_id: str | None = None,
    prefill_date: str | None = None,
    prefill_total: str | None = None,
) -> dict[str, object]:
    page = loan_payment_service.payments_page(request.app.state.session_factory, ctx, loan_id)
    payment_edit = next(
        (row for row in page.payments if str(row.id) == edit_id),
        None,
    )
    checkpoint_edit = next(
        (row for row in page.checkpoints if str(row.id) == edit_id),
        None,
    )
    today = ctx.clock.today(ctx.workspace.timezone)
    return {
        "loan": page.loan,
        "payments": page.payments,
        "checkpoints": page.checkpoints,
        "transactions": page.transactions,
        "payment_edit": payment_edit,
        "checkpoint_edit": checkpoint_edit,
        "payment_form": _payment_form_values(
            page.loan,
            payment_edit,
            request.state.locale,
            today,
            form_values,
            prefill_date=prefill_date,
            prefill_total=prefill_total,
        ),
        "checkpoint_form": _checkpoint_form_values(
            checkpoint_edit, request.state.locale, today, form_values
        ),
        "can_edit": has_role(ctx, Role.EDITOR),
        "error": error,
        "notice": notice,
        "open_form": open_form,
        "today": today,
        "format_money": format_money,
        "format_day": format_day,
        "format_amount_input": format_amount_input,
        "row_error": lambda code: _error(request, code),
    }


def _redirect_payments(
    request: Request,
    loan_id: UUID,
    *,
    notice: str | None = None,
    error: str | None = None,
    anchor: str = "payments",
) -> RedirectResponse:
    query: dict[str, str] = {}
    if notice:
        query["notice"] = notice
    if error:
        query["error"] = error
    url = request.url_for("loan_payments", loan_id=str(loan_id)).path
    if query:
        url += "?" + urlencode(query)
    return RedirectResponse(url + f"#{anchor}", status_code=303)


@dataclass(frozen=True, slots=True)
class _PaymentForm:
    date_value: date
    kind: str
    total: Money
    principal: Money | None
    interest: Money | None
    costs: Money | None
    overpayment: Money | None
    note: str
    transaction_id: UUID | None


def _payment_values_from_form(values: dict[str, str], locale: str, currency: str) -> _PaymentForm:
    total = _money(values, "total", locale, currency)
    if total is None:
        raise loans.LoanError("total", "invalid_payment_total")
    breakdown = values.get("use_breakdown") == "1"
    return _PaymentForm(
        date_value=_date(values.get("date", ""), "date"),
        kind=values.get("kind", "installment"),
        total=total,
        principal=_money_or_zero(values, "principal", locale, currency) if breakdown else None,
        interest=_money_or_zero(values, "interest", locale, currency) if breakdown else None,
        costs=_money_or_zero(values, "costs", locale, currency) if breakdown else None,
        overpayment=(
            _money_or_zero(values, "overpayment", locale, currency) if breakdown else None
        ),
        note=values.get("note", ""),
        transaction_id=_optional_id(values.get("transaction_id", "")),
    )


def _checkpoint_values_from_form(
    values: dict[str, str], locale: str, currency: str
) -> dict[str, object]:
    balance = _money(values, "remaining_principal", locale, currency)
    if balance is None:
        raise loans.LoanError("remaining_principal", "invalid_balance")
    return {
        "as_of": _date(values.get("as_of", ""), "as_of"),
        "remaining_principal": balance,
        "note": values.get("note", ""),
    }


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
            "error_field": request.query_params.get("field"),
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
                "error_field": getattr(exc, "field", None),
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
                "error_field": getattr(exc, "field", None),
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
                "error_field": _reference_value_error_field(exc),
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
                "error_field": _reference_value_error_field(exc),
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


@router.get("/loans/{loan_id}/schedule.csv", name="loan_schedule_csv")
async def loan_schedule_csv(request: Request, ctx: CurrentContext, loan_id: UUID) -> Response:
    data = await run_in_threadpool(
        loan_schedule_service.loan_schedule,
        request.app.state.session_factory,
        ctx,
        loan_id,
    )
    if data.schedule is None:
        raise HTTPException(
            status_code=409,
            detail=_error(request, data.error_code),
        )
    content = await run_in_threadpool(schedule_csv, data.schedule)
    filename = f"loan-schedule-{loan_id}.csv"
    return Response(
        content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/loans/{loan_id}/scenarios", name="loan_scenarios")
async def loan_scenarios_page(request: Request, ctx: CurrentContext, loan_id: UUID) -> Response:
    context = await run_in_threadpool(
        _scenario_page,
        request,
        ctx,
        loan_id,
        ephemeral=[],
        form_rows=_scenario_form_rows(),
        error=_error(request, request.query_params.get("error")),
        notice=_notice(request, request.query_params.get("notice")),
    )
    return render_template(request, "loans/scenarios.html", context=context)


@router.post("/loans/{loan_id}/scenarios", name="loan_scenarios_compare")
async def loan_scenarios_compare(request: Request, ctx: CurrentContext, loan_id: UUID) -> Response:
    values = await _form_values(request)
    form_rows = _scenario_form_rows(values)
    try:
        view = await run_in_threadpool(
            loans.get_loan, request.app.state.session_factory, ctx, loan_id
        )
        parameters: list[loan_scenario_service.ScenarioParameters] = []
        for row in form_rows:
            if _scenario_row_empty(row):
                continue
            parameters.append(
                loan_scenario_service.validate_scenario_parameters(
                    _scenario_parameters(row, request.state.locale, view.currency),
                    view.currency,
                )
            )
        ephemeral: list[loan_scenario_service.ScenarioParameters] = []
        saved_any = False
        for parameter in parameters:
            if parameter.name:
                await run_in_threadpool(
                    loan_scenario_service.create_or_update_loan_scenario,
                    request.app.state.session_factory,
                    ctx,
                    loan_id,
                    parameter,
                )
                saved_any = True
            else:
                ephemeral.append(parameter)
        context = await run_in_threadpool(
            _scenario_page,
            request,
            ctx,
            loan_id,
            ephemeral=ephemeral,
            form_rows=form_rows,
            notice=_notice(request, "scenario_saved") if saved_any else None,
        )
        return render_template(request, "loans/scenarios.html", context=context)
    except (loans.LoanError, LoanValidationError) as exc:
        context = await run_in_threadpool(
            _scenario_page,
            request,
            ctx,
            loan_id,
            ephemeral=[],
            form_rows=form_rows,
            error=_error(request, getattr(exc, "code", "failed")),
        )
        return render_template(request, "loans/scenarios.html", status_code=400, context=context)


@router.post(
    "/loans/{loan_id}/scenarios/{scenario_id}/delete",
    name="loan_scenario_delete",
)
async def loan_scenario_delete(
    request: Request, ctx: CurrentContext, loan_id: UUID, scenario_id: UUID
) -> Response:
    await run_in_threadpool(
        loan_scenario_service.delete_loan_scenario,
        request.app.state.session_factory,
        ctx,
        loan_id,
        scenario_id,
    )
    url = request.url_for("loan_scenarios", loan_id=str(loan_id)).path
    return RedirectResponse(url + "?notice=scenario_deleted#scenarios", status_code=303)


@router.get("/loans/{loan_id}/payments", name="loan_payments")
async def loan_payments_page(request: Request, ctx: CurrentContext, loan_id: UUID) -> Response:
    context = await run_in_threadpool(
        _payments_context,
        request,
        ctx,
        loan_id,
        error=_error(request, request.query_params.get("error")),
        notice=_notice(request, request.query_params.get("notice")),
        open_form=request.query_params.get("form"),
        edit_id=request.query_params.get("edit"),
        prefill_date=request.query_params.get("payment_date"),
        prefill_total=request.query_params.get("payment_total"),
    )
    return render_template(request, "loans/payments.html", context=context)


@router.get("/loans/{loan_id}/settings", name="loan_settings")
async def loan_settings_page(request: Request, ctx: CurrentContext, loan_id: UUID) -> Response:
    view = await run_in_threadpool(loans.get_loan, request.app.state.session_factory, ctx, loan_id)
    if view.kind is LoanKind.INSTALLMENT_PLAN:
        return RedirectResponse(
            request.url_for("installment_plan_detail", loan_id=str(loan_id)).path,
            status_code=303,
        )
    requested_form = request.query_params.get("form")
    if requested_form in ("payment", "checkpoint"):
        preserved = ("form", "edit", "payment_date", "payment_total")
        query = urlencode(
            {key: request.query_params[key] for key in preserved if key in request.query_params}
        )
        anchor = "payment-dialog" if requested_form == "payment" else "checkpoint-dialog"
        return RedirectResponse(
            request.url_for("loan_payments", loan_id=str(loan_id)).path
            + "?"
            + query
            + f"#{anchor}",
            status_code=303,
        )
    context = await run_in_threadpool(
        _settings_context,
        request,
        ctx,
        loan_id,
        error=_error(request, request.query_params.get("error")),
        error_field=request.query_params.get("field"),
        notice=_notice(request, request.query_params.get("notice")),
        open_form=requested_form,
        edit_id=request.query_params.get("edit"),
    )
    return render_template(request, "loans/settings.html", context=context)


@router.post("/loans/{loan_id}/payments", name="loan_payment_create")
async def loan_payment_create(request: Request, ctx: CurrentContext, loan_id: UUID) -> Response:
    values = await _form_values(request)
    view = await run_in_threadpool(loans.get_loan, request.app.state.session_factory, ctx, loan_id)
    try:
        form = _payment_values_from_form(values, request.state.locale, view.currency)
        await run_in_threadpool(
            loan_payment_service.create_loan_payment,
            request.app.state.session_factory,
            ctx,
            loan_id,
            date_value=form.date_value,
            kind=form.kind,
            total=form.total,
            principal=form.principal,
            interest=form.interest,
            costs=form.costs,
            overpayment=form.overpayment,
            note=form.note,
            transaction_id=form.transaction_id,
        )
    except (loans.LoanError, LoanValidationError) as exc:
        context = await run_in_threadpool(
            _payments_context,
            request,
            ctx,
            loan_id,
            error=_error(request, getattr(exc, "code", "failed")),
            form_values=values,
            open_form="payment",
        )
        return render_template(request, "loans/payments.html", status_code=400, context=context)
    return _redirect_payments(request, loan_id, notice="payment_saved")


@router.post("/loans/{loan_id}/payments/{payment_id}", name="loan_payment_update")
async def loan_payment_update(
    request: Request, ctx: CurrentContext, loan_id: UUID, payment_id: UUID
) -> Response:
    values = await _form_values(request)
    view = await run_in_threadpool(loans.get_loan, request.app.state.session_factory, ctx, loan_id)
    try:
        form = _payment_values_from_form(values, request.state.locale, view.currency)
        await run_in_threadpool(
            loan_payment_service.update_loan_payment,
            request.app.state.session_factory,
            ctx,
            loan_id,
            payment_id,
            date_value=form.date_value,
            kind=form.kind,
            total=form.total,
            principal=form.principal,
            interest=form.interest,
            costs=form.costs,
            overpayment=form.overpayment,
            note=form.note,
            transaction_id=form.transaction_id,
        )
    except (loans.LoanError, LoanValidationError) as exc:
        context = await run_in_threadpool(
            _payments_context,
            request,
            ctx,
            loan_id,
            error=_error(request, getattr(exc, "code", "failed")),
            form_values=values,
            open_form="payment",
            edit_id=str(payment_id),
        )
        return render_template(request, "loans/payments.html", status_code=400, context=context)
    return _redirect_payments(request, loan_id, notice="payment_saved")


@router.post("/loans/{loan_id}/payments/{payment_id}/delete", name="loan_payment_delete")
async def loan_payment_delete(
    request: Request, ctx: CurrentContext, loan_id: UUID, payment_id: UUID
) -> Response:
    try:
        await run_in_threadpool(
            loan_payment_service.delete_loan_payment,
            request.app.state.session_factory,
            ctx,
            loan_id,
            payment_id,
        )
    except (loans.LoanError, LoanValidationError) as exc:
        context = await run_in_threadpool(
            _payments_context,
            request,
            ctx,
            loan_id,
            error=_error(request, getattr(exc, "code", "failed")),
        )
        return render_template(request, "loans/payments.html", status_code=400, context=context)
    return _redirect_payments(request, loan_id, notice="payment_deleted")


@router.post("/loans/{loan_id}/checkpoints", name="loan_checkpoint_create")
async def loan_checkpoint_create(request: Request, ctx: CurrentContext, loan_id: UUID) -> Response:
    values = await _form_values(request)
    view = await run_in_threadpool(loans.get_loan, request.app.state.session_factory, ctx, loan_id)
    try:
        form = _checkpoint_values_from_form(values, request.state.locale, view.currency)
        await run_in_threadpool(
            loan_payment_service.create_loan_checkpoint,
            request.app.state.session_factory,
            ctx,
            loan_id,
            **form,  # type: ignore[arg-type]
        )
    except (loans.LoanError, LoanValidationError) as exc:
        context = await run_in_threadpool(
            _payments_context,
            request,
            ctx,
            loan_id,
            error=_error(request, getattr(exc, "code", "failed")),
            form_values=values,
            open_form="checkpoint",
        )
        return render_template(request, "loans/payments.html", status_code=400, context=context)
    return _redirect_payments(request, loan_id, notice="checkpoint_saved", anchor="checkpoints")


@router.post("/loans/{loan_id}/checkpoints/{checkpoint_id}", name="loan_checkpoint_update")
async def loan_checkpoint_update(
    request: Request, ctx: CurrentContext, loan_id: UUID, checkpoint_id: UUID
) -> Response:
    values = await _form_values(request)
    view = await run_in_threadpool(loans.get_loan, request.app.state.session_factory, ctx, loan_id)
    try:
        form = _checkpoint_values_from_form(values, request.state.locale, view.currency)
        await run_in_threadpool(
            loan_payment_service.update_loan_checkpoint,
            request.app.state.session_factory,
            ctx,
            loan_id,
            checkpoint_id,
            **form,  # type: ignore[arg-type]
        )
    except (loans.LoanError, LoanValidationError) as exc:
        context = await run_in_threadpool(
            _payments_context,
            request,
            ctx,
            loan_id,
            error=_error(request, getattr(exc, "code", "failed")),
            form_values=values,
            open_form="checkpoint",
            edit_id=str(checkpoint_id),
        )
        return render_template(request, "loans/payments.html", status_code=400, context=context)
    return _redirect_payments(request, loan_id, notice="checkpoint_saved", anchor="checkpoints")


@router.post(
    "/loans/{loan_id}/checkpoints/{checkpoint_id}/delete",
    name="loan_checkpoint_delete",
)
async def loan_checkpoint_delete(
    request: Request, ctx: CurrentContext, loan_id: UUID, checkpoint_id: UUID
) -> Response:
    try:
        await run_in_threadpool(
            loan_payment_service.delete_loan_checkpoint,
            request.app.state.session_factory,
            ctx,
            loan_id,
            checkpoint_id,
        )
    except (loans.LoanError, LoanValidationError) as exc:
        context = await run_in_threadpool(
            _payments_context,
            request,
            ctx,
            loan_id,
            error=_error(request, getattr(exc, "code", "failed")),
        )
        return render_template(request, "loans/payments.html", status_code=400, context=context)
    return _redirect_payments(request, loan_id, notice="checkpoint_deleted", anchor="checkpoints")


@router.post("/loans/{loan_id}/networth-item", name="loan_networth_item_create")
async def loan_networth_item_create(
    request: Request, ctx: CurrentContext, loan_id: UUID
) -> Response:
    try:
        await run_in_threadpool(
            loan_link_service.create_loan_liability_item,
            request.app.state.session_factory,
            ctx,
            loan_id,
        )
    except (loans.LoanError, LoanValidationError) as exc:
        return _redirect_settings(
            request, loan_id, error=getattr(exc, "code", "failed"), anchor="property"
        )
    return _redirect_settings(request, loan_id, notice="networth_item_created", anchor="property")


@router.post("/loans/{loan_id}/property", name="loan_property_set")
async def loan_property_set(request: Request, ctx: CurrentContext, loan_id: UUID) -> Response:
    values = await _form_values(request)
    try:
        await run_in_threadpool(
            loan_link_service.set_loan_property,
            request.app.state.session_factory,
            ctx,
            loan_id,
            _id(values.get("property_item_id", "")),
        )
    except (loans.LoanError, LoanValidationError) as exc:
        field = getattr(exc, "field", None)
        return _redirect_settings(
            request,
            loan_id,
            error=getattr(exc, "code", "failed"),
            error_field=field if field == "property_item_id" else None,
            anchor="property",
        )
    return _redirect_settings(request, loan_id, notice="property_saved", anchor="property")


@router.post("/loans/{loan_id}/property/clear", name="loan_property_clear")
async def loan_property_clear(request: Request, ctx: CurrentContext, loan_id: UUID) -> Response:
    try:
        await run_in_threadpool(
            loan_link_service.clear_loan_property,
            request.app.state.session_factory,
            ctx,
            loan_id,
        )
    except (loans.LoanError, LoanValidationError) as exc:
        return _redirect_settings(
            request, loan_id, error=getattr(exc, "code", "failed"), anchor="property"
        )
    return _redirect_settings(request, loan_id, notice="property_cleared", anchor="property")


@router.post("/loans/{loan_id}/auto-record", name="loan_auto_record_update")
async def loan_auto_record_update(request: Request, ctx: CurrentContext, loan_id: UUID) -> Response:
    values = await _form_values(request)
    try:
        await run_in_threadpool(
            loan_link_service.set_auto_record_payments,
            request.app.state.session_factory,
            ctx,
            loan_id,
            values.get("enabled") == "1",
        )
    except (loans.LoanError, LoanValidationError) as exc:
        return _redirect_settings(
            request, loan_id, error=getattr(exc, "code", "failed"), anchor="upcoming"
        )
    return _redirect_settings(request, loan_id, notice="auto_record_saved", anchor="upcoming")


@router.post("/loans/{loan_id}/scheduled-item", name="loan_scheduled_generate")
async def loan_scheduled_generate(request: Request, ctx: CurrentContext, loan_id: UUID) -> Response:
    values = await _form_values(request)
    try:
        await run_in_threadpool(
            loan_scheduled_service.create_loan_schedule_item,
            request.app.state.session_factory,
            ctx,
            loan_id,
            _id(values.get("category_id", "")),
            request.app.state.settings,
        )
    except (loans.LoanError, LoanValidationError) as exc:
        field = getattr(exc, "field", None)
        return _redirect_settings(
            request,
            loan_id,
            error=getattr(exc, "code", "failed"),
            error_field=field if field == "category_id" else None,
            anchor="upcoming",
        )
    return _redirect_settings(request, loan_id, notice="schedule_generated", anchor="upcoming")


@router.post("/loans/{loan_id}/scheduled-item/disable", name="loan_scheduled_disable")
async def loan_scheduled_disable(request: Request, ctx: CurrentContext, loan_id: UUID) -> Response:
    try:
        await run_in_threadpool(
            loan_scheduled_service.disable_loan_schedule_item,
            request.app.state.session_factory,
            ctx,
            loan_id,
        )
    except (loans.LoanError, LoanValidationError) as exc:
        return _redirect_settings(
            request, loan_id, error=getattr(exc, "code", "failed"), anchor="upcoming"
        )
    return _redirect_settings(request, loan_id, notice="schedule_disabled", anchor="upcoming")


@router.get("/loans/installments", name="installment_plans")
async def installment_plans_page(request: Request, ctx: CurrentContext) -> Response:
    context = await run_in_threadpool(
        _installment_plans_context,
        request,
        ctx,
        error=_error(request, request.query_params.get("error")),
        notice=_notice(request, request.query_params.get("notice")),
    )
    return render_template(request, "loans/installments/list.html", context=context)


@router.get("/loans/installments/new", name="installment_plan_new")
async def installment_plan_new(request: Request, ctx: CurrentContext) -> Response:
    require_role(ctx, Role.EDITOR)
    context = await run_in_threadpool(_installment_plan_form_context, request, ctx)
    return render_template(request, "loans/installments/form.html", context=context)


@router.post("/loans/installments", name="installment_plan_create")
async def installment_plan_create(request: Request, ctx: CurrentContext) -> Response:
    require_role(ctx, Role.EDITOR)
    values = await _form_values(request)
    try:
        await run_in_threadpool(
            installment_plan_service.create_installment_plan,
            request.app.state.session_factory,
            ctx,
            name=values.get("name", ""),
            installment=_money(
                values, "installment", request.state.locale, ctx.workspace.base_currency
            ),
            remaining_count=_integer(values.get("remaining_count", ""), "remaining_count"),
            payment_day=_integer(values.get("payment_day", ""), "payment_day"),
            category_id=_id(values.get("category_id", "")),
            horizon_months=request.app.state.settings.recurrence_horizon_months,
        )
    except (loans.LoanError, LoanValidationError) as exc:
        context = await run_in_threadpool(
            _installment_plan_form_context,
            request,
            ctx,
            values=values,
            error=_error(request, getattr(exc, "code", "failed")),
        )
        return render_template(
            request, "loans/installments/form.html", status_code=400, context=context
        )
    return RedirectResponse(
        request.url_for("installment_plans").path + "?notice=installment_plan_created",
        status_code=303,
    )


@router.get("/loans/installments/{loan_id}", name="installment_plan_detail")
async def installment_plan_detail(request: Request, ctx: CurrentContext, loan_id: UUID) -> Response:
    context = await run_in_threadpool(
        _installment_plan_detail_context,
        request,
        ctx,
        loan_id,
        error=_error(request, request.query_params.get("error")),
        notice=_notice(request, request.query_params.get("notice")),
    )
    return render_template(request, "loans/installments/detail.html", context=context)


@router.post("/loans/installments/{loan_id}/post-next", name="installment_plan_post_next")
async def installment_plan_post_next(
    request: Request, ctx: CurrentContext, loan_id: UUID
) -> Response:
    try:
        await run_in_threadpool(
            installment_plan_service.post_next_installment,
            request.app.state.session_factory,
            ctx,
            loan_id,
        )
    except (loans.LoanError, LoanValidationError, occurrence_service.OccurrenceError) as exc:
        return RedirectResponse(
            request.url_for("installment_plan_detail", loan_id=str(loan_id)).path
            + "?"
            + urlencode({"error": getattr(exc, "code", "failed")}),
            status_code=303,
        )
    return RedirectResponse(
        request.url_for("installment_plan_detail", loan_id=str(loan_id)).path
        + "?notice=installment_plan_posted",
        status_code=303,
    )


@router.get("/loans/installments/{loan_id}/edit", name="installment_plan_edit")
async def installment_plan_edit(request: Request, ctx: CurrentContext, loan_id: UUID) -> Response:
    require_role(ctx, Role.EDITOR)
    context = await run_in_threadpool(_installment_plan_form_context, request, ctx, loan_id=loan_id)
    return render_template(request, "loans/installments/form.html", context=context)


@router.post("/loans/installments/{loan_id}", name="installment_plan_update")
async def installment_plan_update(request: Request, ctx: CurrentContext, loan_id: UUID) -> Response:
    require_role(ctx, Role.EDITOR)
    values = await _form_values(request)
    try:
        await run_in_threadpool(
            installment_plan_service.update_installment_plan,
            request.app.state.session_factory,
            ctx,
            loan_id,
            name=values.get("name", ""),
            installment=_money(
                values, "installment", request.state.locale, ctx.workspace.base_currency
            ),
            remaining_count=_integer(values.get("remaining_count", ""), "remaining_count"),
            payment_day=_integer(values.get("payment_day", ""), "payment_day"),
            category_id=_id(values.get("category_id", "")),
            horizon_months=request.app.state.settings.recurrence_horizon_months,
        )
    except (loans.LoanError, LoanValidationError) as exc:
        context = await run_in_threadpool(
            _installment_plan_form_context,
            request,
            ctx,
            loan_id=loan_id,
            values=values,
            error=_error(request, getattr(exc, "code", "failed")),
        )
        return render_template(
            request, "loans/installments/form.html", status_code=400, context=context
        )
    return RedirectResponse(
        request.url_for("installment_plan_detail", loan_id=str(loan_id)).path
        + "?notice=installment_plan_updated",
        status_code=303,
    )


@router.post("/loans/installments/{loan_id}/archive", name="installment_plan_archive")
async def installment_plan_archive(
    request: Request, ctx: CurrentContext, loan_id: UUID
) -> Response:
    await run_in_threadpool(
        installment_plan_service.archive_installment_plan,
        request.app.state.session_factory,
        ctx,
        loan_id,
    )
    return RedirectResponse(
        request.url_for("installment_plan_detail", loan_id=str(loan_id)).path
        + "?notice=installment_plan_archived",
        status_code=303,
    )


@router.post("/loans/installments/{loan_id}/restore", name="installment_plan_restore")
async def installment_plan_restore(
    request: Request, ctx: CurrentContext, loan_id: UUID
) -> Response:
    try:
        await run_in_threadpool(
            installment_plan_service.restore_installment_plan,
            request.app.state.session_factory,
            ctx,
            loan_id,
        )
    except (loans.LoanError, LoanValidationError) as exc:
        return RedirectResponse(
            request.url_for("installment_plan_detail", loan_id=str(loan_id)).path
            + "?"
            + urlencode({"error": getattr(exc, "code", "failed")}),
            status_code=303,
        )
    return RedirectResponse(
        request.url_for("installment_plan_detail", loan_id=str(loan_id)).path
        + "?notice=installment_plan_restored",
        status_code=303,
    )


@router.get("/loans/{loan_id}", name="loan_detail")
async def loan_detail(request: Request, ctx: CurrentContext, loan_id: UUID) -> Response:
    view = await run_in_threadpool(loans.get_loan, request.app.state.session_factory, ctx, loan_id)
    if view.kind is LoanKind.INSTALLMENT_PLAN:
        return RedirectResponse(
            request.url_for("installment_plan_detail", loan_id=str(loan_id)).path,
            status_code=303,
        )
    context = await run_in_threadpool(
        _detail_context,
        request,
        ctx,
        loan_id,
        error=_error(request, request.query_params.get("error")),
        notice=_notice(request, request.query_params.get("notice")),
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
            "target_error": False,
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
            day_count=values.get("day_count") or None,
        )
    except (loans.LoanError, LoanValidationError) as exc:
        code = getattr(exc, "code", "failed")
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
                "error": _error(request, code),
                "target_error": code == "target_below_installment",
                "can_edit": True,
                "format_money": format_money,
                "format_day": format_day,
                "format_amount_input": format_amount_input,
                "format_rate_short": format_rate_short,
            },
        )
    return _redirect_settings(request, loan_id, notice="terms_saved", anchor="terms")


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
        field = getattr(exc, "field", None)
        return _redirect_settings(
            request,
            loan_id,
            error=getattr(exc, "code", "failed"),
            error_field=(
                field
                if field in ("overpayment_mode", "overpayment_amount", "overpayment_strategy")
                else None
            ),
            anchor="overpayments",
        )
    return _redirect_settings(request, loan_id, notice="overpayment_saved", anchor="overpayments")


@router.post("/loans/{loan_id}/archive", name="loan_archive")
async def loan_archive(request: Request, ctx: CurrentContext, loan_id: UUID) -> Response:
    await run_in_threadpool(loans.archive_loan, request.app.state.session_factory, ctx, loan_id)
    return _redirect_settings(request, loan_id, notice="loan_archived", anchor="archive")


@router.post("/loans/{loan_id}/restore", name="loan_restore")
async def loan_restore(request: Request, ctx: CurrentContext, loan_id: UUID) -> Response:
    await run_in_threadpool(loans.restore_loan, request.app.state.session_factory, ctx, loan_id)
    return _redirect_settings(request, loan_id, notice="loan_restored", anchor="archive")


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
        return await _render_settings_error(
            request,
            ctx,
            loan_id,
            exc,
            values=values,
            form="rate",
            edit_id=values.get("change_id"),
        )
    return _redirect_settings(request, loan_id, notice="rate_saved", anchor="rates")


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
        return await _render_settings_error(
            request,
            ctx,
            loan_id,
            exc,
            values=values,
            form="rate",
            edit_id=str(change_id),
        )
    return _redirect_settings(request, loan_id, notice="rate_saved", anchor="rates")


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
        return _redirect_settings(
            request, loan_id, error=getattr(exc, "code", "failed"), anchor="rates"
        )
    return _redirect_settings(request, loan_id, notice="rate_deleted", anchor="rates")


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
        return await _render_settings_error(
            request,
            ctx,
            loan_id,
            exc,
            values=values,
            form="cost",
            edit_id=values.get("component_id"),
        )
    return _redirect_settings(request, loan_id, notice="cost_saved", anchor="costs")


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
        return await _render_settings_error(
            request,
            ctx,
            loan_id,
            exc,
            values=values,
            form="cost",
            edit_id=str(component_id),
        )
    return _redirect_settings(request, loan_id, notice="cost_saved", anchor="costs")


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
        return _redirect_settings(
            request, loan_id, error=getattr(exc, "code", "failed"), anchor="costs"
        )
    return _redirect_settings(request, loan_id, notice="cost_deleted", anchor="costs")


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
        return await _render_settings_error(
            request,
            ctx,
            loan_id,
            exc,
            values=values,
            form="overpayment",
            edit_id=values.get("overpayment_id"),
        )
    return _redirect_settings(
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
        return await _render_settings_error(
            request,
            ctx,
            loan_id,
            exc,
            values=values,
            form="overpayment",
            edit_id=str(overpayment_id),
        )
    return _redirect_settings(
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
        return _redirect_settings(
            request,
            loan_id,
            error=getattr(exc, "code", "failed"),
            anchor="overpayments",
        )
    return _redirect_settings(
        request, loan_id, notice="overpayment_item_deleted", anchor="overpayments"
    )
