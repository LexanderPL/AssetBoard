"""FIRE plan pages (F8.10): one plan, its chooser, setup wizard, read-only view and editor.

``GET /fire`` is a read-only view for every role; ``GET /fire/edit`` is the editor-only page and
``POST /fire/edit`` the only plan write besides the wizard/reset. The edit page recalculates the
results through ``POST /fire/edit/preview`` and never writes. Routes parse and format form values
and map service errors to field messages; validation, roles, the one-time legacy choice, the setup
wizard's step checks, reset and the current-year projection live in ``services.fire_scenarios`` and
``services.fire_projection``. Every state-changing route is covered by the router-level
``require_csrf`` dependency.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from assetboard.db.repositories import NotFound
from assetboard.domain.fire import (
    DEFAULT_EXPENSE_FACTORS_BP,
    DEFAULT_RETURN_DELTAS_BP,
    Rule,
    SensitivityError,
    age_at_start,
    sensitivity,
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
from assetboard.services import fire_linked as fire_linked_service
from assetboard.services import fire_projection as fire_projection_service
from assetboard.services import fire_scenarios as fire_service
from assetboard.services.context import Context, Role, has_role, require_role
from assetboard.web import fire_adjustments
from assetboard.web.context import CurrentContext
from assetboard.web.fire_charts import build_fire_charts
from assetboard.web.fire_csv import fire_scenario_csv as render_fire_csv
from assetboard.web.fire_sensitivity import build_sensitivity_view
from assetboard.web.fire_suggestions import BLOCK_FALLBACK, block_messages, warning_text
from assetboard.web.forms import InvalidPercentOrFixed, signed_percent_to_bp
from assetboard.web.rendering import render_template
from assetboard.web.security import require_csrf

router = APIRouter(dependencies=[Depends(require_csrf)])


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


ERRORS = {
    "birth_year_required": _("Enter your birth year, or add it to your profile first."),
    "invalid_birth_year": _("Enter a birth year between 1900 and the current year."),
    "invalid_horizon": _("Enter a horizon between 1 and 120 years of age."),
    "horizon_not_after_age": _("The horizon age must be older than your current age."),
    "invalid_stop_age": _("Enter the age at which income stops, between 0 and 120."),
    "invalid_rate": _("Enter a percentage between -20% and 50% with at most two decimals."),
    "invalid_swr": _("Enter a withdrawal rate between 0.5% and 20% with at most two decimals."),
    "invalid_rule": _("Choose how the plan decides that you are financially independent."),
    "invalid_amount": _("Enter a valid non-negative amount in the plan currency."),
    "amount_required": _(
        "Enter a yearly amount in the plan currency. Enter 0 if the value is zero."
    ),
    "invalid_year": _("Enter a whole year for every row."),
    "rows_outside_window": _("Every event year must fall inside the plan's window."),
    "too_many_events": _("A plan can have at most 200 events."),
    "invalid_event_kind": _("Choose a valid event type."),
    "invalid_label": _("Give every event a short description."),
    "invalid_event": _("This event is no longer part of the plan. Reload the page and try again."),
    "duplicate_event": _("The same event appears twice. Reload the page and try again."),
    "duplicate_saving_override": _("A year can have only one saving override."),
    "saving_override_current_year": _(
        "A saving override starts in the next year; the current year uses the starting portfolio."
    ),
    "archived_event_read_only": _(
        "This adjustment is in the past or after the plan's horizon. It is kept as history and "
        "can be removed, but not changed. Reload the page and try again."
    ),
    "events_not_allowed_on_create": _("Create the plan first, then add adjustments."),
    "extreme_rate_confirmation_required": _(
        "A rate is outside the usual range. Tick the confirmation box to save it."
    ),
    "invalid_extreme_rates": _("Tick the confirmation box only when you mean the unusual rates."),
    "invalid_projection": _("The saved plan cannot be projected."),
    "plan_exists": _("This workspace already has a FIRE plan."),
    "migration_required": _("Choose one of the old FIRE scenarios before creating a new plan."),
    "choice_required": _("Choose one of the old FIRE scenarios to keep."),
    "legacy_invalid": _(
        "An old FIRE scenario cannot be read. Contact the workspace owner before retrying."
    ),
    "plan_currency_mismatch": _(
        "The workspace base currency no longer matches the plan's currency. The plan cannot be "
        "projected or changed; reset it and create a plan in the current currency."
    ),
    "confirmation_required": _("Confirm that the plan will be deleted."),
    "choice_not_confirmed": _("Confirm that the other old FIRE scenarios will be deleted."),
    "missing_adjustments": _("The adjustment rows are missing. Reload the page and try again."),
}

RESULT_BLOCK_MESSAGES = {
    "plan_currency_mismatch": _(
        "The workspace base currency no longer matches the plan's currency. Reset the plan and "
        "create a new one in the current currency."
    ),
    "horizon_not_after_age": _(
        "The plan's horizon is not after your current age. Raise the horizon age to project it."
    ),
    "invalid_projection": _("The saved plan cannot be projected."),
    "invalid_amount": _("The plan has an invalid amount."),
    "invalid_horizon": _("The plan has an invalid horizon."),
    "invalid_stop_age": _("The plan has an invalid stop-working age."),
    "invalid_rate": _("The plan has an invalid rate."),
    "invalid_swr": _("The plan has an invalid withdrawal rate."),
    "saving_override_current_year": _(
        "A saving override is stored in the current year and is not applied."
    ),
    "duplicate_saving_override": _("A year has more than one saving override."),
}

FALLBACK_ERROR = _("The plan could not be saved. Reload the page and try again.")
SENSITIVITY_ERRORS = {
    "empty_axis": _("The sensitivity axes are empty."),
    "invalid_value": _("The sensitivity axes contain an invalid value."),
    "duplicate_value": _("The sensitivity axes contain a duplicate value."),
    "too_many_values": _("The sensitivity axes have too many values."),
    "too_many_cells": _("The sensitivity grid has too many cells."),
    "missing_base": _("The sensitivity axes must include the saved plan."),
    "return_out_of_range": _(
        "The real-return range would leave the supported -20 to 50 percent bounds. Adjust the "
        "plan's return and try again."
    ),
    "expense_factor_out_of_range": _(
        "The expense range is outside the supported 0 to 1000 percent of the saved plan."
    ),
    "expense_too_large": _(
        "The expense range would exceed the supported amount. Reduce the starting expenses and "
        "try again."
    ),
}
SENSITIVITY_FALLBACK = _("The sensitivity grid could not be calculated.")

NOTICES = {
    "created": _("FIRE plan created."),
    "saved": _("FIRE plan saved."),
    "migrated": _("The old FIRE scenario was converted to the one-plan format. Review its values."),
    "reset": _("FIRE plan reset. Set up a new plan when you are ready."),
    "review_updated": _(
        "The plan values changed. Check the updated review, then press Create plan."
    ),
    "discarded": _("Unsaved changes discarded."),
}

RESET_WORD = "reset"

WIZARD_STEPS = (
    _("About you"),
    _("Income and expenses"),
    _("Portfolio"),
    _("Assumptions and review"),
)
WIZARD_FIELDS = (
    "birth_year",
    "stop_working_age",
    "horizon_age",
    "income_manual",
    "expenses_manual",
    "portfolio_manual",
    "inflation",
    "real_return",
    "income_growth",
    "rule",
    "swr",
    "extreme_rates_confirmed",
)
STEP_FIELDS: dict[int, tuple[str, ...]] = {
    1: ("birth_year", "stop_working_age", "horizon_age"),
    2: ("income_manual", "expenses_manual"),
    3: ("portfolio_manual",),
    4: (
        "inflation",
        "real_return",
        "income_growth",
        "rule",
        "swr",
        "extreme_rates_confirmed",
    ),
}
FIELD_STEPS: dict[str, int] = {
    field: step for step, fields in STEP_FIELDS.items() for field in fields
}

RULE_LABELS = {
    Rule.RETURNS_COVER_EXPENSES.value: _("Returns cover expenses"),
    Rule.SWR.value: _("Withdrawal rate"),
}


def _t(request: Request, message: str) -> str:
    return gettext(message, request.state.locale)


def _error(request: Request, code: str | None) -> str | None:
    if not code:
        return None
    return _t(request, ERRORS.get(code, FALLBACK_ERROR))


def _notice(request: Request, code: str | None) -> str | None:
    if not code:
        return None
    message = NOTICES.get(code)
    return _t(request, message) if message is not None else None


def _preview_error_field(exc: fire_service.FireError) -> str:
    """Name the exact row control for a preview error; ``events`` marks the whole section."""
    if exc.row is not None and exc.column is not None:
        return f"event_{exc.column}_{exc.row}"
    return exc.field


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise NotFound from None


def _basis(value: str | None) -> str:
    return "real" if value == "real" else "nominal"


def _rate_input(rate_bp: int, locale: str) -> str:
    return format_quantity_input(Decimal(rate_bp).scaleb(-2), locale)


def _money_from_text(
    text: str,
    locale: str,
    currency: str,
    *,
    field: str,
) -> Money:
    try:
        amount = parse_amount(text, locale)
        if amount < 0:
            raise ValueError("negative")
        if amount == 0:
            return Money(0, currency)
        return Money(amount_minor(amount, currency), currency)
    except (ValueError, EntryError):
        raise fire_service.FireError(field, "invalid_amount") from None


def _plan_money(
    values: dict[str, str], key: str, locale: str, currency: str, field: str
) -> Money | None:
    text = values.get(key, "").strip()
    if not text:
        return None
    return _money_from_text(text, locale, currency, field=field)


def _percent(values: dict[str, str], key: str, locale: str) -> int:
    text = values.get(key, "").strip()
    try:
        return signed_percent_to_bp(text, locale)
    except (ValueError, InvalidPercentOrFixed):
        raise fire_service.FireError(key, "invalid_rate") from None


def _optional_percent(values: dict[str, str], key: str, locale: str) -> int | None:
    text = values.get(key, "").strip()
    if not text:
        return None
    try:
        return signed_percent_to_bp(text, locale)
    except (ValueError, InvalidPercentOrFixed):
        raise fire_service.FireError(key, "invalid_swr") from None


def _required_int(values: dict[str, str], key: str, code: str) -> int:
    text = values.get(key, "").strip()
    try:
        return int(text)
    except ValueError:
        raise fire_service.FireError(key, code) from None


def _optional_int(values: dict[str, str], key: str, code: str) -> int | None:
    text = values.get(key, "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        raise fire_service.FireError(key, code) from None


def _plan_input(
    values: dict[str, str],
    locale: str,
    currency: str,
    *,
    include_events: bool = True,
) -> fire_service.FirePlanInput:
    parsed = (
        fire_adjustments.parse_adjustments(values, locale, currency)
        if include_events
        else fire_adjustments.ParsedAdjustments(events=(), removed_event_ids=())
    )
    return fire_service.FirePlanInput(
        birth_year=_optional_int(values, "birth_year", "invalid_birth_year"),
        inflation_bp=_percent(values, "inflation", locale),
        real_return_bp=_percent(values, "real_return", locale),
        income_growth_bp=_percent(values, "income_growth", locale),
        stop_working_age=_required_int(values, "stop_working_age", "invalid_stop_age"),
        horizon_age=_required_int(values, "horizon_age", "invalid_horizon"),
        rule=values.get("rule", Rule.RETURNS_COVER_EXPENSES.value),
        swr_bp=_optional_percent(values, "swr", locale),
        income=_plan_money(values, "income_manual", locale, currency, "income_manual"),
        expenses=_plan_money(values, "expenses_manual", locale, currency, "expenses_manual"),
        portfolio=_plan_money(values, "portfolio_manual", locale, currency, "portfolio_manual"),
        extreme_rates_confirmed=values.get("extreme_rates_confirmed") == "1",
        events=parsed.events,
        removed_event_ids=parsed.removed_event_ids,
    )


def _wizard_defaults(defaults: fire_service.FirePlanDefaults, locale: str) -> dict[str, str]:
    return {
        "birth_year": "" if defaults.birth_year is None else str(defaults.birth_year),
        "stop_working_age": "",
        "horizon_age": str(defaults.life_expectancy),
        "income_manual": "",
        "expenses_manual": "",
        "portfolio_manual": "",
        "inflation": _rate_input(defaults.inflation_bp, locale),
        "real_return": _rate_input(defaults.real_return_bp, locale),
        "income_growth": _rate_input(defaults.income_growth_bp, locale),
        "rule": Rule.RETURNS_COVER_EXPENSES.value,
        "swr": _rate_input(defaults.swr_bp, locale),
        "extreme_rates_confirmed": "",
    }


def _wizard_values(
    raw: dict[str, str],
    defaults: fire_service.FirePlanDefaults,
    locale: str,
) -> dict[str, str]:
    """Profile/illustrative defaults, overlaid with every posted wizard field (including empty)."""
    values = _wizard_defaults(defaults, locale)
    for name in WIZARD_FIELDS:
        if name in raw:
            values[name] = raw[name].strip()
    return values


def _wizard_summary(values: dict[str, str], current_year: int) -> dict[str, object]:
    """Display-only ages and warnings; validation owns the real errors."""
    try:
        birth_year = int(values.get("birth_year", ""))
    except ValueError:
        birth_year = None
    try:
        horizon_age = int(values.get("horizon_age", ""))
    except ValueError:
        horizon_age = None
    try:
        stop_working_age = int(values.get("stop_working_age", ""))
    except ValueError:
        stop_working_age = None
    current_age = age_at_start(current_year, birth_year) if birth_year is not None else None
    last_year = (
        birth_year + horizon_age if birth_year is not None and horizon_age is not None else None
    )
    stop_year = (
        birth_year + stop_working_age
        if birth_year is not None and stop_working_age is not None
        else None
    )
    return {
        "current_age": current_age,
        "last_year": last_year,
        "stop_year": stop_year,
        "horizon_warning": (
            current_age is not None and horizon_age is not None and horizon_age <= current_age
        ),
        "income_stopped": (
            current_age is not None
            and stop_working_age is not None
            and stop_working_age <= current_age
        ),
        "stop_after_horizon": (
            horizon_age is not None
            and stop_working_age is not None
            and stop_working_age > horizon_age
        ),
    }


def _wizard_step_for_field(field: str | None) -> int:
    return FIELD_STEPS.get(field or "", 4)


def _wizard_plan_input(
    values: dict[str, str], locale: str, currency: str
) -> fire_service.FirePlanInput:
    """The full plan a wizard step posts; event fields are never read."""
    return _plan_input(values, locale, currency, include_events=False)


def _wizard_validate(
    values: dict[str, str],
    step: int,
    defaults: fire_service.FirePlanDefaults,
    locale: str,
) -> fire_service.ValidatedPlan | None:
    """Validate steps 1..step; step 4 returns the validated plan for the review."""
    if step >= 4:
        return fire_service.validate_plan_input(
            _wizard_plan_input(values, locale, defaults.currency),
            current_year=defaults.current_year,
            currency=defaults.currency,
            default_horizon=defaults.life_expectancy,
            preview=True,
            allow_events=False,
        )
    birth_year = _optional_int(values, "birth_year", "invalid_birth_year")
    stop_working_age = _optional_int(values, "stop_working_age", "invalid_stop_age")
    horizon_age = _required_int(values, "horizon_age", "invalid_horizon")
    fire_service.validate_ages(
        birth_year,
        horizon_age,
        stop_working_age,
        current_year=defaults.current_year,
        default_horizon=defaults.life_expectancy,
    )
    if step >= 2:
        for field in ("income_manual", "expenses_manual"):
            fire_service.validate_amount(
                field,
                _plan_money(values, field, locale, defaults.currency, field),
                defaults.currency,
                required=True,
            )
    if step >= 3:
        fire_service.validate_amount(
            "portfolio_manual",
            _plan_money(values, "portfolio_manual", locale, defaults.currency, "portfolio_manual"),
            defaults.currency,
            required=True,
        )
    return None


def _review_signature(
    validated: fire_service.ValidatedPlan,
    *,
    currency: str,
    current_year: int,
) -> str:
    """A stable digest of every value the review shows and Create plan would save.

    The review is rendered when step 3 advances to step 4, but the step-4 fields stay editable.
    The hidden digest lets Create refuse to write when the fields no longer match the review the
    user saw; the page then re-renders the current values and the user confirms them again.
    """
    parts = (
        str(current_year),
        currency,
        str(validated.birth_year),
        str(validated.stop_working_age),
        str(validated.horizon_age),
        str(validated.income_minor),
        str(validated.expenses_minor),
        str(validated.portfolio_minor),
        str(validated.inflation_bp),
        str(validated.real_return_bp),
        str(validated.income_growth_bp),
        validated.rule.value,
        "" if validated.swr_bp is None else str(validated.swr_bp),
    )
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _wizard_review(
    validated: fire_service.ValidatedPlan,
    currency: str,
    current_year: int,
) -> dict[str, object]:
    stop_year = validated.birth_year + validated.stop_working_age
    return {
        "birth_year": validated.birth_year,
        "current_age": current_year - validated.birth_year,
        "first_year": current_year,
        "last_year": validated.birth_year + validated.horizon_age,
        "currency": currency,
        "stop_working_age": validated.stop_working_age,
        "stop_year": stop_year,
        "income": Money(validated.income_minor, currency),
        "expenses": Money(validated.expenses_minor, currency),
        "portfolio": Money(validated.portfolio_minor, currency),
        "inflation_bp": validated.inflation_bp,
        "real_return_bp": validated.real_return_bp,
        "income_growth_bp": validated.income_growth_bp,
        "rule": validated.rule,
        "swr_bp": validated.swr_bp,
        "income_stopped": validated.stop_working_age <= current_year - validated.birth_year,
        "stop_after_horizon": validated.stop_working_age > validated.horizon_age,
        "needs_extreme_confirmation": validated.needs_extreme_confirmation,
        "signature": _review_signature(validated, currency=currency, current_year=current_year),
    }


def _detail_values(detail: fire_service.FirePlanDetail, locale: str) -> dict[str, str]:
    plan = detail.plan
    return {
        "current_year": str(plan.current_year),
        "currency": plan.currency,
        "birth_year": str(plan.birth_year),
        "horizon_age": str(plan.horizon_age),
        "stop_working_age": str(plan.stop_working_age),
        "inflation": _rate_input(plan.inflation_bp, locale),
        "real_return": _rate_input(plan.real_return_bp, locale),
        "income_growth": _rate_input(plan.income_growth_bp, locale),
        "rule": plan.rule,
        "swr": "" if plan.swr_bp is None else _rate_input(plan.swr_bp, locale),
        "income_manual": format_amount_input(plan.income, locale),
        "expenses_manual": format_amount_input(plan.expenses, locale),
        "portfolio_manual": format_amount_input(plan.portfolio, locale),
        "extreme_rates_confirmed": "",
    }


def _adjustment_error_messages(request: Request) -> dict[str, str]:
    """Translated messages for the adjustment row errors the service and parser can raise."""
    return {code: _t(request, message) for code, message in ERRORS.items()}


def _form_context(
    request: Request,
    ctx: Context,
    *,
    detail: fire_service.FirePlanDetail,
    values: dict[str, str] | None,
    adjustment_rows: tuple[fire_adjustments.AdjustmentRow, ...] | None,
    error: str | None = None,
    error_code: str | None = None,
    error_field: str | None = None,
    adjustment_error: fire_service.FireError | None = None,
) -> dict[str, object]:
    locale = request.state.locale
    posted = values is not None
    if values is None:
        values = _detail_values(detail, locale)
    currency = detail.plan.currency
    if adjustment_rows is None:
        if posted:
            adjustment_rows = fire_adjustments.active_rows_from_values(
                values, locale, currency, error=adjustment_error
            )
        else:
            adjustment_rows = fire_adjustments.active_rows_from_detail(detail, locale)
    removed_archived: tuple[UUID, ...] = ()
    if posted:
        try:
            removed_archived = fire_adjustments.checked_archived_ids(values)
        except fire_service.FireError:
            removed_archived = ()
    archived_adjustment_rows = fire_adjustments.archived_rows(
        detail, locale, removed_ids=removed_archived
    )
    window = fire_adjustments.adjustment_window(detail)
    return {
        "values": values,
        "current_year": detail.plan.current_year,
        "currency": currency,
        "adjustment_rows": adjustment_rows,
        "archived_adjustment_rows": archived_adjustment_rows,
        "adjustment_window": window,
        "next_adjustment_index": fire_adjustments.next_adjustment_index(adjustment_rows),
        "active_adjustment_count": fire_adjustments.saved_active_count(adjustment_rows),
        "archived_open": any(row.remove for row in archived_adjustment_rows),
        "adjustment_kind_labels": fire_adjustments.kind_labels(locale),
        "adjustment_error_messages": _adjustment_error_messages(request),
        "adjustment_error": adjustment_error,
        "adjustments_editable": True,
        "rule_labels": {code: _t(request, label) for code, label in RULE_LABELS.items()},
        "profile_url": request.url_for("account").path,
        "error": error,
        "error_code": error_code,
        "error_field": error_field,
        "can_edit": has_role(ctx, Role.EDITOR),
        "format_money": format_money,
        "format_rate_short": format_rate_short,
    }


def _results_context(
    request: Request,
    ctx: Context,
    results: fire_projection_service.FirePlanResults,
    *,
    basis: str,
    editable_results: bool,
    in_editor: bool = False,
    preview_error: str | None = None,
    preview_error_field: str | None = None,
) -> dict[str, object]:
    projection = results.projection
    return {
        "results": results,
        "detail": results.detail,
        "plan": results.detail.plan,
        "projection": projection,
        "basis": basis,
        "editable_results": editable_results,
        "in_editor": in_editor,
        "charts": (
            build_fire_charts(projection, basis, request.state.locale)
            if projection is not None
            else None
        ),
        "preview_error": preview_error,
        "preview_error_field": preview_error_field,
        "can_edit": has_role(ctx, Role.EDITOR),
        "block_messages": {
            code: _t(request, message) for code, message in RESULT_BLOCK_MESSAGES.items()
        },
        "format_money": format_money,
        "format_rate_short": format_rate_short,
    }


def _suggestion_context(
    request: Request,
    suggestions: fire_linked_service.SetupSuggestions,
    plan: fire_service.FirePlanView,
) -> dict[str, object]:
    """The edit page's suggestion cards: formatted copy values and the saved amounts."""
    locale = request.state.locale
    blocked_fields = {block.field for block in suggestions.blocks}
    inputs: dict[str, str] = {}
    if suggestions.income is not None and "income_manual" not in blocked_fields:
        inputs["income_manual"] = format_amount_input(suggestions.income.annual_total, locale)
    if suggestions.expenses is not None and "expenses_manual" not in blocked_fields:
        inputs["expenses_manual"] = format_amount_input(suggestions.expenses.annual_total, locale)
    if suggestions.portfolio is not None and "portfolio_manual" not in blocked_fields:
        inputs["portfolio_manual"] = format_amount_input(suggestions.portfolio.total, locale)
    return {
        "suggestions": suggestions,
        "suggested_inputs": inputs,
        "saved_amounts": {
            "income_manual": plan.income,
            "expenses_manual": plan.expenses,
            "portfolio_manual": plan.portfolio,
        },
        "suggestion_block_messages": block_messages(locale),
        "suggestion_block_fallback": gettext(BLOCK_FALLBACK, locale),
        "suggestion_warning_text": warning_text,
        "format_day": format_day,
    }


def _edit_context(
    request: Request,
    ctx: Context,
    *,
    results: fire_projection_service.FirePlanResults,
    suggestions: fire_linked_service.SetupSuggestions,
    basis: str,
    values: dict[str, str] | None = None,
    adjustment_rows: tuple[fire_adjustments.AdjustmentRow, ...] | None = None,
    error: str | None = None,
    error_code: str | None = None,
    error_field: str | None = None,
    adjustment_error: fire_service.FireError | None = None,
    notice: str | None = None,
    dirty: bool = False,
) -> dict[str, object]:
    context = _form_context(
        request,
        ctx,
        detail=results.detail,
        values=values,
        adjustment_rows=adjustment_rows,
        error=error,
        error_code=error_code,
        error_field=error_field,
        adjustment_error=adjustment_error,
    )
    context.update(
        _results_context(
            request,
            ctx,
            results,
            basis=basis,
            editable_results=True,
            in_editor=True,
        )
    )
    context.update(_suggestion_context(request, suggestions, results.detail.plan))
    context["notice"] = notice
    context["dirty"] = dirty
    return context


def _view_context(
    request: Request,
    ctx: Context,
    results: fire_projection_service.FirePlanResults,
    *,
    basis: str,
    sensitivity_loaded: bool,
    notice: str | None = None,
    error: str | None = None,
    error_code: str | None = None,
) -> dict[str, object]:
    context = _results_context(
        request,
        ctx,
        results,
        basis=basis,
        editable_results=False,
        in_editor=False,
    )
    context.update(
        {
            "notice": notice,
            "error": error,
            "error_code": error_code,
            "profile_url": request.url_for("account").path,
            "adjustment_rows": fire_adjustments.active_rows_from_detail(
                results.detail, request.state.locale, include_blank=False
            ),
            "archived_adjustment_rows": fire_adjustments.archived_rows(
                results.detail, request.state.locale
            ),
            "adjustment_kind_labels": fire_adjustments.kind_labels(request.state.locale),
            "active_adjustment_count": len(results.detail.active_events),
            "adjustments_editable": False,
            "rule_labels": {code: _t(request, label) for code, label in RULE_LABELS.items()},
            "format_day": format_day,
            "sensitivity_loaded": sensitivity_loaded,
            "sensitivity_page_url": (
                request.url_for("fire_scenarios").path + "?sensitivity=1#sensitivity"
            ),
            "sensitivity_fragment_url": request.url_for("fire_sensitivity").path,
            "sensitivity_inputs_url": request.url_for("fire_edit").path + "#amounts",
        }
    )
    if sensitivity_loaded:
        context.update(_sensitivity_context(request, ctx, results))
    return context


async def _form_data(request: Request) -> dict[str, str]:
    form = await request.form()
    return {key: value for key, value in form.items() if isinstance(value, str)}


def _redirect(
    request: Request,
    route: str,
    *,
    notice: str | None = None,
    error: str | None = None,
    basis: str | None = None,
    **params: str,
) -> RedirectResponse:
    url = request.url_for(route, **params).path
    query: list[str] = []
    if notice is not None:
        query.append(f"notice={notice}")
    if error is not None:
        query.append(f"error={error}")
    if basis == "real":
        query.append("basis=real")
    if query:
        url = f"{url}?{'&'.join(query)}"
    return RedirectResponse(url, status_code=303)


def _sensitivity_context(
    request: Request,
    ctx: Context,
    results: fire_projection_service.FirePlanResults,
) -> dict[str, object]:
    fragment_url = request.url_for("fire_sensitivity").path
    page_url = request.url_for("fire_scenarios").path + "?sensitivity=1#sensitivity"
    plan = results.detail.plan
    if plan.currency_mismatch:
        inputs_url = request.url_for("fire_reset").path
        inputs_label = _t(request, _("Reset plan"))
    else:
        inputs_url = request.url_for("fire_edit").path + "#amounts"
        inputs_label = _t(request, _("Edit the plan"))
    context: dict[str, object] = {
        "sensitivity": None,
        "sensitivity_error": None,
        "sensitivity_error_code": None,
        "sensitivity_blocks": results.blocks,
        "block_messages": {
            code: _t(request, message) for code, message in RESULT_BLOCK_MESSAGES.items()
        },
        "sensitivity_fragment_url": fragment_url,
        "sensitivity_page_url": page_url,
        "sensitivity_inputs_url": inputs_url,
        "sensitivity_inputs_label": inputs_label,
        "can_edit": has_role(ctx, Role.EDITOR),
    }
    if results.projection is None:
        return context
    try:
        grid = sensitivity(
            results.projection.inputs,
            DEFAULT_RETURN_DELTAS_BP,
            DEFAULT_EXPENSE_FACTORS_BP,
        )
    except SensitivityError as exc:
        context["sensitivity_error"] = _t(
            request, SENSITIVITY_ERRORS.get(exc.code, SENSITIVITY_FALLBACK)
        )
        context["sensitivity_error_code"] = exc.code
    else:
        context["sensitivity"] = build_sensitivity_view(
            grid,
            horizon_age=results.projection.inputs.horizon_age,
            locale=request.state.locale,
        )
    return context


def _int_or(text: str | None, default: int) -> int:
    try:
        return int(text or "")
    except ValueError:
        return default


def _wizard_context(
    request: Request,
    ctx: Context,
    step: int,
    values: dict[str, str],
    defaults: fire_service.FirePlanDefaults,
    *,
    suggestions: fire_linked_service.SetupSuggestions | None = None,
    review: dict[str, object] | None = None,
    error: str | None = None,
    error_field: str | None = None,
    notice: str | None = None,
) -> dict[str, object]:
    locale = request.state.locale
    summary = _wizard_summary(values, defaults.current_year)
    return {
        "step": step,
        "last_step": 4,
        "steps": [_t(request, label) for label in WIZARD_STEPS],
        "values": values,
        "hidden_fields": [name for name in WIZARD_FIELDS if name not in STEP_FIELDS[step]],
        "currency": defaults.currency,
        "current_year": defaults.current_year,
        "profile_url": request.url_for("account").path,
        "cancel_url": request.url_for("fire_scenarios").path + "?cancelled=1",
        "suggestions": suggestions,
        "suggestion_block_messages": block_messages(locale),
        "suggestion_block_fallback": gettext(BLOCK_FALLBACK, locale),
        "suggestion_warning_text": warning_text,
        "review": review,
        "error": error,
        "error_field": error_field,
        "error_is_field": error_field is not None and error_field in STEP_FIELDS[step],
        "notice": notice,
        "can_edit": True,
        "rule_labels": {code: _t(request, label) for code, label in RULE_LABELS.items()},
        "format_money": format_money,
        "format_rate_short": format_rate_short,
        "format_day": format_day,
        **summary,
    }


def _load_wizard_suggestions(
    factory: object,
    ctx: Context,
    step: int,
    currency: str,
) -> fire_linked_service.SetupSuggestions | None:
    """Only the cards the rendered step shows (a bounded set of bulk queries)."""
    if step == 2:
        return fire_linked_service.setup_suggestions(
            factory,  # type: ignore[arg-type]
            ctx,
            currency=currency,
            include_income=True,
            include_expenses=True,
            include_portfolio=False,
        )
    if step == 3:
        return fire_linked_service.setup_suggestions(
            factory,  # type: ignore[arg-type]
            ctx,
            currency=currency,
            include_income=False,
            include_expenses=False,
            include_portfolio=True,
        )
    return None


def _wizard_suggestion_amount(
    suggestions: fire_linked_service.SetupSuggestions | None,
    field: str,
) -> Money | None:
    if suggestions is None:
        return None
    if any(block.field == field for block in suggestions.blocks):
        return None
    if field == "income_manual" and suggestions.income is not None:
        return suggestions.income.annual_total
    if field == "expenses_manual" and suggestions.expenses is not None:
        return suggestions.expenses.annual_total
    if field == "portfolio_manual" and suggestions.portfolio is not None:
        return suggestions.portfolio.total
    return None


async def _render_wizard(
    request: Request,
    ctx: Context,
    step: int,
    values: dict[str, str],
    defaults: fire_service.FirePlanDefaults,
    *,
    suggestions: fire_linked_service.SetupSuggestions | None = None,
    review: dict[str, object] | None = None,
    error: str | None = None,
    error_field: str | None = None,
    notice: str | None = None,
    status_code: int = 200,
) -> Response:
    if suggestions is None:
        suggestions = await run_in_threadpool(
            _load_wizard_suggestions,
            request.app.state.session_factory,
            ctx,
            step,
            defaults.currency,
        )
    context = _wizard_context(
        request,
        ctx,
        step,
        values,
        defaults,
        suggestions=suggestions,
        review=review,
        error=error,
        error_field=error_field,
        notice=notice,
    )
    return render_template(request, "fire/wizard.html", context=context, status_code=status_code)


def _empty_context(
    request: Request,
    ctx: Context,
    *,
    cancelled: bool,
    notice: str | None,
) -> dict[str, object]:
    return {
        "can_edit": has_role(ctx, Role.EDITOR),
        "cancelled": cancelled,
        "notice": notice,
        "setup_url": request.url_for("fire_scenarios").path,
    }


@router.get("/fire", name="fire_scenarios")
async def fire_scenarios_page(request: Request, ctx: CurrentContext) -> Response:
    entry = await run_in_threadpool(fire_service.plan_entry, request.app.state.session_factory, ctx)
    notice = _notice(request, request.query_params.get("notice"))
    error_code = request.query_params.get("error")
    error = _error(request, error_code)
    if entry.migration is not None:
        return _redirect(request, "fire_scenarios", notice="migrated")
    if entry.detail is not None:
        if request.query_params.get("edit") == "1":
            return _redirect(request, "fire_edit", basis=_basis(request.query_params.get("basis")))
        results = fire_projection_service.results_from_detail(entry.detail)
        context = await run_in_threadpool(
            _view_context,
            request,
            ctx,
            results,
            basis=_basis(request.query_params.get("basis")),
            sensitivity_loaded=request.query_params.get("sensitivity") == "1",
            notice=notice,
            error=error,
            error_code=error_code,
        )
        return render_template(request, "fire/view.html", context=context)
    if entry.legacy:
        context = {
            "legacy": entry.legacy,
            "can_edit": has_role(ctx, Role.EDITOR),
            "error": error,
            "notice": notice,
            "format_money": format_money,
        }
        return render_template(request, "fire/choose.html", context=context)
    cancelled = request.query_params.get("cancelled") == "1"
    if cancelled or not has_role(ctx, Role.EDITOR):
        return render_template(
            request,
            "fire/empty.html",
            context=_empty_context(request, ctx, cancelled=cancelled, notice=notice),
        )
    defaults = await run_in_threadpool(
        fire_service.plan_defaults, request.app.state.session_factory, ctx
    )
    values = _wizard_values({}, defaults, request.state.locale)
    return await _render_wizard(request, ctx, 1, values, defaults, notice=notice)


@router.get("/fire/edit", name="fire_edit")
async def fire_edit_page(request: Request, ctx: CurrentContext) -> Response:
    require_role(ctx, Role.EDITOR)
    detail = await run_in_threadpool(
        fire_service.get_fire_plan_if_any, request.app.state.session_factory, ctx
    )
    if detail is None or detail.plan.currency_mismatch:
        return RedirectResponse(request.url_for("fire_scenarios").path, status_code=303)
    return await _render_edit(
        request,
        ctx,
        detail,
        basis=_basis(request.query_params.get("basis")),
        notice=_notice(
            request,
            "discarded"
            if request.query_params.get("discarded") == "1"
            else request.query_params.get("notice"),
        ),
    )


async def _render_edit(
    request: Request,
    ctx: Context,
    detail: fire_service.FirePlanDetail,
    *,
    basis: str,
    values: dict[str, str] | None = None,
    adjustment_rows: tuple[fire_adjustments.AdjustmentRow, ...] | None = None,
    error: str | None = None,
    error_code: str | None = None,
    error_field: str | None = None,
    adjustment_error: fire_service.FireError | None = None,
    notice: str | None = None,
    dirty: bool = False,
    status_code: int = 200,
) -> Response:
    suggestions = await run_in_threadpool(
        fire_linked_service.setup_suggestions,
        request.app.state.session_factory,
        ctx,
        currency=detail.plan.currency,
    )
    results = fire_projection_service.results_from_detail(detail)
    context = await run_in_threadpool(
        _edit_context,
        request,
        ctx,
        results=results,
        suggestions=suggestions,
        basis=basis,
        values=values,
        adjustment_rows=adjustment_rows,
        error=error,
        error_code=error_code,
        error_field=error_field,
        adjustment_error=adjustment_error,
        notice=notice,
        dirty=dirty,
    )
    return render_template(request, "fire/edit.html", context=context, status_code=status_code)


@router.post("/fire/edit/preview", name="fire_edit_preview")
async def fire_edit_preview(request: Request, ctx: CurrentContext) -> Response:
    require_role(ctx, Role.EDITOR)
    values = await _form_data(request)
    detail = await run_in_threadpool(
        fire_service.get_fire_plan_if_any, request.app.state.session_factory, ctx
    )
    if detail is None:
        raise NotFound
    basis = _basis(values.get("basis"))
    try:
        plan = _plan_input(values, request.state.locale, detail.plan.currency)
        results = await run_in_threadpool(
            fire_projection_service.preview_plan_results,
            ctx,
            detail,
            plan,
            factory=request.app.state.session_factory,
        )
    except fire_service.FireError as exc:
        saved = fire_projection_service.results_from_detail(detail)
        context = await run_in_threadpool(
            _results_context,
            request,
            ctx,
            saved,
            basis=basis,
            editable_results=True,
            in_editor=True,
            preview_error=_error(request, exc.code),
            preview_error_field=_preview_error_field(exc),
        )
        return render_template(request, "fire/_results.html", context=context)
    context = await run_in_threadpool(
        _results_context,
        request,
        ctx,
        results,
        basis=basis,
        editable_results=True,
        in_editor=True,
    )
    return render_template(request, "fire/_results.html", context=context)


@router.post("/fire/edit", name="fire_edit_save")
async def fire_edit_save(request: Request, ctx: CurrentContext) -> Response:
    """Validate and save the plan; preview, navigation and discard never write."""
    require_role(ctx, Role.EDITOR)
    values = await _form_data(request)
    basis = _basis(values.get("basis"))
    detail = await run_in_threadpool(
        fire_service.get_fire_plan_if_any, request.app.state.session_factory, ctx
    )
    if detail is None or detail.plan.currency_mismatch:
        return RedirectResponse(request.url_for("fire_scenarios").path, status_code=303)
    # A malformed/truncated save must not be read as "delete every adjustment". The editor always
    # posts a marker with the adjustment section, including when the user removed every row.
    if values.get("adjustments_present") != "1":
        return await _render_edit(
            request,
            ctx,
            detail,
            basis=basis,
            values=values,
            error=_error(request, "missing_adjustments"),
            error_code="missing_adjustments",
            error_field="events",
            dirty=True,
            status_code=400,
        )
    try:
        plan = _plan_input(values, request.state.locale, detail.plan.currency)
        await run_in_threadpool(
            fire_service.update_fire_plan,
            request.app.state.session_factory,
            ctx,
            plan,
        )
    except NotFound:
        return RedirectResponse(request.url_for("fire_scenarios").path, status_code=303)
    except fire_service.FireError as exc:
        if exc.code == "plan_currency_mismatch":
            return RedirectResponse(request.url_for("fire_scenarios").path, status_code=303)
        return await _render_edit(
            request,
            ctx,
            detail,
            basis=basis,
            values=values,
            error=_error(request, exc.code),
            error_code=exc.code,
            error_field=exc.field,
            adjustment_error=exc,
            dirty=True,
            status_code=400,
        )
    return _redirect(request, "fire_scenarios", notice="saved", basis=basis)


@router.get("/fire/sensitivity", name="fire_sensitivity")
async def fire_sensitivity(request: Request, ctx: CurrentContext) -> Response:
    require_role(ctx, Role.VIEWER)
    results = await run_in_threadpool(
        fire_projection_service.scenario_results,
        request.app.state.session_factory,
        ctx,
    )
    context = await run_in_threadpool(_sensitivity_context, request, ctx, results)
    return render_template(request, "fire/_sensitivity.html", context=context)


@router.get("/fire/results.csv", name="fire_results_csv")
async def fire_results_csv(request: Request, ctx: CurrentContext) -> Response:
    results = await run_in_threadpool(
        fire_projection_service.scenario_results,
        request.app.state.session_factory,
        ctx,
    )
    if results.projection is None:
        code = results.blocked.code if results.blocked is not None else "invalid_projection"
        raise HTTPException(status_code=409, detail=_error(request, code))
    content = await run_in_threadpool(render_fire_csv, results.projection)
    return Response(
        content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="fire-plan.csv"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/fire/setup", name="fire_wizard")
async def fire_wizard_page(request: Request, ctx: CurrentContext) -> Response:
    require_role(ctx, Role.EDITOR)
    entry = await run_in_threadpool(
        fire_service.plan_entry,
        request.app.state.session_factory,
        ctx,
        auto_migrate=False,
    )
    if entry.plan is not None or entry.legacy:
        return RedirectResponse(request.url_for("fire_scenarios").path, status_code=303)
    defaults = await run_in_threadpool(
        fire_service.plan_defaults, request.app.state.session_factory, ctx
    )
    values = _wizard_values({}, defaults, request.state.locale)
    return await _render_wizard(
        request,
        ctx,
        1,
        values,
        defaults,
        notice=_notice(request, request.query_params.get("notice")),
    )


@router.post("/fire/setup", name="fire_wizard_step")
async def fire_wizard_step(request: Request, ctx: CurrentContext) -> Response:
    """One step of the setup wizard; only ``go=create`` on step 4 writes a plan."""
    require_role(ctx, Role.EDITOR)
    raw = await _form_data(request)
    defaults = await run_in_threadpool(
        fire_service.plan_defaults, request.app.state.session_factory, ctx
    )
    values = _wizard_values(raw, defaults, request.state.locale)
    step = min(max(_int_or(raw.get("step"), 1), 1), 4)
    entry = await run_in_threadpool(
        fire_service.plan_entry,
        request.app.state.session_factory,
        ctx,
        auto_migrate=False,
    )
    if entry.plan is not None:
        return _redirect(request, "fire_scenarios", error="plan_exists")
    if entry.legacy:
        return _redirect(request, "fire_scenarios", error="migration_required")
    if raw.get("go") == "back":
        return await _render_wizard(request, ctx, max(step - 1, 1), values, defaults)
    use = raw.get("use", "")
    if use:
        suggestions = await run_in_threadpool(
            _load_wizard_suggestions,
            request.app.state.session_factory,
            ctx,
            step,
            defaults.currency,
        )
        amount = _wizard_suggestion_amount(suggestions, use) if use in STEP_FIELDS[step] else None
        if amount is not None:
            values[use] = format_amount_input(amount, request.state.locale)
        return await _render_wizard(
            request,
            ctx,
            step,
            values,
            defaults,
            suggestions=suggestions,
        )
    try:
        validated = await run_in_threadpool(
            _wizard_validate, values, step, defaults, request.state.locale
        )
    except fire_service.FireError as exc:
        error_step = _wizard_step_for_field(exc.field)
        return await _render_wizard(
            request,
            ctx,
            error_step,
            values,
            defaults,
            error=_error(request, exc.code),
            error_field=exc.field,
            status_code=400,
        )
    if step < 4:
        next_step = step + 1
        if next_step < 4:
            return await _render_wizard(request, ctx, next_step, values, defaults)
        try:
            validated = await run_in_threadpool(
                _wizard_validate, values, 4, defaults, request.state.locale
            )
        except fire_service.FireError as exc:
            error_step = _wizard_step_for_field(exc.field)
            return await _render_wizard(
                request,
                ctx,
                error_step,
                values,
                defaults,
                error=_error(request, exc.code),
                error_field=exc.field,
                status_code=400,
            )
        assert validated is not None
        review = _wizard_review(validated, defaults.currency, defaults.current_year)
        return await _render_wizard(request, ctx, 4, values, defaults, review=review)
    assert validated is not None
    review = _wizard_review(validated, defaults.currency, defaults.current_year)
    if raw.get("go") != "create":
        return await _render_wizard(request, ctx, 4, values, defaults, review=review)
    # The hidden digest is rendered with the review. If a step-4 field changed after that render
    # (or the workspace currency/year changed), show the review of the submitted values first;
    # Create plan must never save values the user has not seen summarised.
    if raw.get("review_signature", "") != review["signature"]:
        return await _render_wizard(
            request,
            ctx,
            4,
            values,
            defaults,
            review=review,
            notice=_notice(request, "review_updated"),
        )
    try:
        plan = _wizard_plan_input(values, request.state.locale, defaults.currency)
        await run_in_threadpool(
            fire_service.create_fire_plan,
            request.app.state.session_factory,
            ctx,
            plan,
        )
    except fire_service.FireError as exc:
        if exc.code in ("plan_exists", "migration_required"):
            return _redirect(request, "fire_scenarios", error=exc.code)
        error_step = _wizard_step_for_field(exc.field)
        return await _render_wizard(
            request,
            ctx,
            error_step,
            values,
            defaults,
            error=_error(request, exc.code),
            error_field=exc.field,
            status_code=400,
        )
    return _redirect(request, "fire_scenarios", notice="created")


@router.get("/fire/reset", name="fire_reset")
async def fire_reset_page(request: Request, ctx: CurrentContext) -> Response:
    require_role(ctx, Role.EDITOR)
    detail = await run_in_threadpool(
        fire_service.get_fire_plan, request.app.state.session_factory, ctx
    )
    return render_template(
        request,
        "fire/reset.html",
        context=_reset_context(request, ctx, detail),
    )


def _reset_context(
    request: Request,
    ctx: Context,
    detail: fire_service.FirePlanDetail,
    *,
    error: str | None = None,
) -> dict[str, object]:
    return {
        "plan": detail.plan,
        "reset_word": RESET_WORD,
        "archive_url": request.url_for("workspace_data").path,
        "workspace_currency": ctx.workspace.base_currency,
        "error": error,
        "format_money": format_money,
    }


@router.post("/fire/reset", name="fire_reset_confirm")
async def fire_reset_confirm(request: Request, ctx: CurrentContext) -> Response:
    require_role(ctx, Role.EDITOR)
    form = await request.form()
    confirm = form.get("confirm")
    confirmed = isinstance(confirm, str) and confirm.strip().casefold() == RESET_WORD
    detail = await run_in_threadpool(
        fire_service.get_fire_plan, request.app.state.session_factory, ctx
    )
    try:
        await run_in_threadpool(
            fire_service.delete_fire_plan,
            request.app.state.session_factory,
            ctx,
            confirmed=confirmed,
        )
    except fire_service.FireError as exc:
        return render_template(
            request,
            "fire/reset.html",
            context=_reset_context(request, ctx, detail, error=_error(request, exc.code)),
            status_code=400,
        )
    return _redirect(request, "fire_scenarios", notice="reset")


@router.get("/fire/new", name="fire_scenario_new")
async def fire_scenario_new(request: Request, ctx: CurrentContext) -> Response:
    require_role(ctx, Role.EDITOR)
    return RedirectResponse(request.url_for("fire_scenarios").path, status_code=303)


@router.get("/fire/compare", name="fire_compare")
async def fire_compare_page(request: Request, ctx: CurrentContext) -> Response:
    """The old comparison URL; comparison is gone in favour of the one-plan model."""
    require_role(ctx, Role.VIEWER)
    return RedirectResponse(request.url_for("fire_scenarios").path, status_code=303)


@router.post("/fire/choose", name="fire_scenario_choose")
async def fire_scenario_choose(request: Request, ctx: CurrentContext) -> Response:
    require_role(ctx, Role.EDITOR)
    form = await request.form()
    scenario_value = form.get("scenario_id")
    confirmed = form.get("confirm") == "1"
    try:
        if not confirmed:
            raise fire_service.FireError("plan", "choice_not_confirmed")
        scenario_id = _uuid(scenario_value if isinstance(scenario_value, str) else "")
        await run_in_threadpool(
            fire_service.migrate_legacy_plan,
            request.app.state.session_factory,
            ctx,
            scenario_id,
        )
    except fire_service.FireError as exc:
        return _redirect(request, "fire_scenarios", error=exc.code)
    return _redirect(request, "fire_scenarios", notice="migrated")


@router.get("/fire/{scenario_id}", name="fire_scenario")
async def fire_scenario_page(request: Request, ctx: CurrentContext, scenario_id: str) -> Response:
    """Keep old bookmarks working; the live plan no longer has an id in its URLs."""
    detail = await run_in_threadpool(
        fire_service.get_fire_plan, request.app.state.session_factory, ctx
    )
    if str(detail.plan.id) != scenario_id:
        raise NotFound
    if request.query_params.get("edit") == "1":
        return _redirect(request, "fire_edit", basis=_basis(request.query_params.get("basis")))
    params = {
        key: value
        for key in ("basis", "sensitivity", "notice", "error")
        if (value := request.query_params.get(key))
    }
    anchor = "#sensitivity" if params.get("sensitivity") == "1" else ""
    url = request.url_for("fire_scenarios").path
    if params:
        url = f"{url}?{urlencode(params)}"
    return RedirectResponse(f"{url}{anchor}", status_code=303)


@router.get("/fire/{scenario_id}/sensitivity", name="fire_scenario_sensitivity")
async def fire_scenario_sensitivity(
    request: Request, ctx: CurrentContext, scenario_id: str
) -> Response:
    require_role(ctx, Role.VIEWER)
    detail = await run_in_threadpool(
        fire_service.get_fire_plan, request.app.state.session_factory, ctx
    )
    if str(detail.plan.id) != scenario_id:
        raise NotFound
    return RedirectResponse(request.url_for("fire_sensitivity").path, status_code=303)


@router.get("/fire/{scenario_id}/edit", name="fire_scenario_edit")
async def fire_scenario_edit(request: Request, ctx: CurrentContext, scenario_id: str) -> Response:
    require_role(ctx, Role.EDITOR)
    detail = await run_in_threadpool(
        fire_service.get_fire_plan,
        request.app.state.session_factory,
        ctx,
    )
    if str(detail.plan.id) != scenario_id:
        raise NotFound
    return _redirect(request, "fire_edit", basis=_basis(request.query_params.get("basis")))


@router.get("/fire/{scenario_id}/results.csv", name="fire_scenario_csv")
async def fire_scenario_csv(request: Request, ctx: CurrentContext, scenario_id: str) -> Response:
    detail = await run_in_threadpool(
        fire_service.get_fire_plan, request.app.state.session_factory, ctx
    )
    if str(detail.plan.id) != scenario_id:
        raise NotFound
    return RedirectResponse(request.url_for("fire_results_csv").path, status_code=303)


__all__ = ["router"]
