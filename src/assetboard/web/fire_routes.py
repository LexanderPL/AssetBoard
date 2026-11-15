"""FIRE plan pages (F8.9): one plan, its chooser, setup wizard, view and reset.

Routes parse and format form values and map service errors to field messages; validation, roles,
the one-time legacy choice, the setup wizard's step checks, reset and the current-year projection
live in ``services.fire_scenarios`` and ``services.fire_projection``. Every state-changing route
is covered by the router-level ``require_csrf`` dependency.
"""

from __future__ import annotations

import hashlib
import re
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
    EventKind,
    Rule,
    SensitivityError,
    age_at_start,
    sensitivity,
    signed_amount_minor,
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
EVENT_KIND_LABELS = {
    EventKind.CASH.value: _("One-off cash flow"),
    EventKind.EXPENSE_CHANGE.value: _("Annual expense change"),
    EventKind.INCOME_CHANGE.value: _("Annual income change"),
    EventKind.SAVING_OVERRIDE.value: _("Saving override"),
}
STARTING_FIELD_LABELS = {
    "income_manual": _("annual income"),
    "expenses_manual": _("annual expenses"),
    "portfolio_manual": _("starting portfolio"),
}

_EVENT_KEY = re.compile(r"^event_(id|year|kind|amount|label)_([0-9]+)$")
_EVENT_FIELDS = ("id", "year", "kind", "amount", "label")


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


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise NotFound from None


def _basis(value: str | None) -> str:
    return "real" if value == "real" else "nominal"


def _rate_input(rate_bp: int, locale: str) -> str:
    return format_quantity_input(Decimal(rate_bp).scaleb(-2), locale)


def _editor_rows(
    values: dict[str, str],
    pattern: re.Pattern[str],
    prefix: str,
    fields: tuple[str, ...],
) -> list[dict[str, str]]:
    indexes = sorted({int(match.group(2)) for key in values if (match := pattern.match(key))})
    return [
        {
            "index": str(index),
            **{field: values.get(f"{prefix}_{field}_{index}", "") for field in fields},
        }
        for index in indexes
    ]


def _row_year(text: str, field: str) -> int:
    value = text.strip()
    try:
        return int(value)
    except ValueError:
        raise fire_service.FireError(field, "invalid_year") from None


def _money_from_text(
    text: str,
    locale: str,
    currency: str,
    *,
    signed: bool,
    field: str,
) -> Money:
    try:
        amount = parse_amount(text, locale)
        if not signed and amount < 0:
            raise ValueError("negative")
        if amount == 0:
            return Money(0, currency)
        minor = signed_amount_minor(amount, currency) if signed else amount_minor(amount, currency)
        return Money(minor, currency)
    except (ValueError, EntryError):
        raise fire_service.FireError(field, "invalid_amount") from None


def _plan_money(
    values: dict[str, str], key: str, locale: str, currency: str, field: str
) -> Money | None:
    text = values.get(key, "").strip()
    if not text:
        return None
    return _money_from_text(text, locale, currency, signed=False, field=field)


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


def _optional_uuid(values: dict[str, str], key: str, field: str) -> UUID | None:
    text = values.get(key, "").strip()
    if not text:
        return None
    try:
        return UUID(text)
    except ValueError:
        raise fire_service.FireError(field, "invalid_event") from None


def _event_inputs(
    values: dict[str, str], locale: str, currency: str
) -> tuple[fire_service.EventInput, ...]:
    rows: list[fire_service.EventInput] = []
    for row in _editor_rows(values, _EVENT_KEY, "event", _EVENT_FIELDS):
        if not any(row[field].strip() for field in ("year", "amount", "label")):
            continue
        year = _row_year(row["year"], "events")
        amount = _money_from_text(
            row["amount"].strip(), locale, currency, signed=True, field="events"
        )
        rows.append(
            fire_service.EventInput(
                year=year,
                kind=row["kind"].strip(),
                amount_minor=amount.amount_minor,
                label=row["label"].strip(),
                id=_optional_uuid(row, "id", "events"),
            )
        )
    return tuple(rows)


def _plan_input(
    values: dict[str, str],
    locale: str,
    currency: str,
    *,
    include_events: bool = True,
) -> fire_service.FirePlanInput:
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
        events=_event_inputs(values, locale, currency) if include_events else (),
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


def _event_rows(detail: fire_service.FirePlanDetail, locale: str) -> list[dict[str, str]]:
    return [
        {
            "index": str(index),
            "id": str(row.id),
            "year": str(row.year),
            "kind": row.kind,
            "amount": format_amount_input(row.amount, locale),
            "label": row.label,
            "archived": "1" if row.archived else "",
        }
        for index, row in enumerate(detail.events, start=1)
    ]


def _blank_row(index: int, fields: tuple[str, ...]) -> dict[str, str]:
    return {"index": str(index), **dict.fromkeys(fields, "")}


def _row_index(rows: list[dict[str, str]]) -> int:
    return max((int(row["index"]) for row in rows), default=0) + 1


def _form_context(
    request: Request,
    ctx: Context,
    *,
    detail: fire_service.FirePlanDetail,
    values: dict[str, str] | None,
    event_rows: list[dict[str, str]] | None,
    error: str | None = None,
    error_code: str | None = None,
    error_field: str | None = None,
) -> dict[str, object]:
    locale = request.state.locale
    if values is None:
        values = _detail_values(detail, locale)
    if event_rows is None:
        event_rows = _event_rows(detail, locale)
    current_year = detail.plan.current_year
    window_end: int | None = None
    try:
        birth_year = int(values.get("birth_year", ""))
        horizon_age = int(values.get("horizon_age", ""))
        window_end = birth_year + horizon_age
    except ValueError:
        window_end = None
    currency = detail.plan.currency
    return {
        "detail": detail,
        "values": values,
        "title": _t(request, "FIRE plan"),
        "action": request.url_for("fire_scenario_update", scenario_id=str(detail.plan.id)).path,
        "cancel_url": request.url_for("fire_scenarios").path,
        "current_year": current_year,
        "currency": currency,
        "window_end": window_end,
        "event_rows": event_rows,
        "empty_event": _blank_row(_row_index(event_rows), _EVENT_FIELDS),
        "rule_labels": {code: _t(request, label) for code, label in RULE_LABELS.items()},
        "event_kind_labels": {
            code: _t(request, label) for code, label in EVENT_KIND_LABELS.items()
        },
        "profile_url": request.url_for("account").path,
        "error": error,
        "error_code": error_code,
        "error_field": error_field,
        "notice": None,
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
    inputs_open: bool,
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
        "inputs_open": inputs_open,
        "charts": (
            build_fire_charts(projection, basis, request.state.locale)
            if projection is not None
            else None
        ),
        "preview_error": preview_error,
        "preview_error_field": preview_error_field,
        "can_preview": has_role(ctx, Role.EDITOR),
        "can_edit": has_role(ctx, Role.EDITOR),
        "field_labels": {
            field: _t(request, label) for field, label in STARTING_FIELD_LABELS.items()
        },
        "block_messages": {
            code: _t(request, message) for code, message in RESULT_BLOCK_MESSAGES.items()
        },
        "format_money": format_money,
        "format_rate_short": format_rate_short,
        "format_day": lambda day: format_day(day, request.state.locale),
        "rule_labels": {code: _t(request, label) for code, label in RULE_LABELS.items()},
        "event_kind_labels": {
            code: _t(request, label) for code, label in EVENT_KIND_LABELS.items()
        },
        "sensitivity_loaded": False,
        "sensitivity_fragment_url": request.url_for(
            "fire_scenario_sensitivity", scenario_id=str(results.detail.plan.id)
        ).path,
        "sensitivity_page_url": (
            request.url_for("fire_scenarios").path + "?sensitivity=1#sensitivity"
        ),
    }


def _combined_context(
    request: Request,
    ctx: Context,
    results: fire_projection_service.FirePlanResults,
    *,
    basis: str,
    inputs_open: bool,
    notice: str | None = None,
    error: str | None = None,
    error_code: str | None = None,
    error_field: str | None = None,
    values: dict[str, str] | None = None,
    event_rows: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    detail = results.detail
    context = _form_context(
        request,
        ctx,
        detail=detail,
        values=values,
        event_rows=event_rows,
        error=error,
        error_code=error_code,
        error_field=error_field,
    )
    context.update(
        _results_context(
            request,
            ctx,
            results,
            basis=basis,
            inputs_open=inputs_open,
        )
    )
    context["notice"] = notice
    results_url = request.url_for("fire_scenarios").path
    if basis == "real":
        results_url = f"{results_url}?basis=real"
    context["cancel_url"] = results_url
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
    request: Request, results: fire_projection_service.FirePlanResults
) -> dict[str, object]:
    fragment_url = request.url_for(
        "fire_scenario_sensitivity", scenario_id=str(results.detail.plan.id)
    ).path
    page_url = request.url_for("fire_scenarios").path + "?sensitivity=1#sensitivity"
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
        "sensitivity_inputs_url": request.url_for("fire_scenarios").path + "?edit=1#inputs",
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
        results = fire_projection_service.results_from_detail(entry.detail)
        inputs_open = request.query_params.get("edit") == "1" or error is not None
        context = await run_in_threadpool(
            _combined_context,
            request,
            ctx,
            results,
            basis=_basis(request.query_params.get("basis")),
            inputs_open=inputs_open,
            notice=notice,
            error=error,
            error_code=error_code,
        )
        if request.query_params.get("sensitivity") == "1":
            context.update(await run_in_threadpool(_sensitivity_context, request, results))
            context["sensitivity_loaded"] = True
        return render_template(request, "fire/results.html", context=context)
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
    detail = await run_in_threadpool(
        fire_service.get_fire_plan, request.app.state.session_factory, ctx
    )
    if str(detail.plan.id) != scenario_id:
        raise NotFound
    params = {
        key: value
        for key in ("basis", "sensitivity", "edit", "notice", "error")
        if (value := request.query_params.get(key))
    }
    anchor = "#sensitivity" if params.get("sensitivity") == "1" else ""
    if params.get("edit") == "1":
        anchor = "#inputs"
    url = request.url_for("fire_scenarios").path
    if params:
        url = f"{url}?{urlencode(params)}"
    return RedirectResponse(f"{url}{anchor}", status_code=303)


@router.get("/fire/{scenario_id}/sensitivity", name="fire_scenario_sensitivity")
async def fire_scenario_sensitivity(
    request: Request, ctx: CurrentContext, scenario_id: str
) -> Response:
    require_role(ctx, Role.VIEWER)
    results = await run_in_threadpool(
        fire_projection_service.scenario_results,
        request.app.state.session_factory,
        ctx,
        _uuid(scenario_id),
    )
    context = await run_in_threadpool(_sensitivity_context, request, results)
    return render_template(request, "fire/_sensitivity.html", context=context)


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
    url = request.url_for("fire_scenarios").path
    return RedirectResponse(f"{url}?edit=1#inputs", status_code=303)


@router.post("/fire/{scenario_id}/preview", name="fire_scenario_preview")
async def fire_scenario_preview(
    request: Request, ctx: CurrentContext, scenario_id: str
) -> Response:
    require_role(ctx, Role.EDITOR)
    values = await _form_data(request)
    detail = await run_in_threadpool(
        fire_service.get_fire_plan, request.app.state.session_factory, ctx
    )
    if str(detail.plan.id) != scenario_id:
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
        saved = await run_in_threadpool(
            fire_projection_service.scenario_results,
            request.app.state.session_factory,
            ctx,
            detail.plan.id,
        )
        context = await run_in_threadpool(
            _results_context,
            request,
            ctx,
            saved,
            basis=basis,
            inputs_open=True,
            preview_error=_error(request, exc.code),
            preview_error_field=exc.field,
        )
        return render_template(request, "fire/_results.html", context=context)
    context = await run_in_threadpool(
        _results_context,
        request,
        ctx,
        results,
        basis=basis,
        inputs_open=True,
    )
    return render_template(request, "fire/_results.html", context=context)


@router.get("/fire/{scenario_id}/results.csv", name="fire_scenario_csv")
async def fire_scenario_csv(request: Request, ctx: CurrentContext, scenario_id: str) -> Response:
    results = await run_in_threadpool(
        fire_projection_service.scenario_results,
        request.app.state.session_factory,
        ctx,
        _uuid(scenario_id),
    )
    if results.projection is None:
        code = results.blocked.code if results.blocked is not None else "invalid_projection"
        raise HTTPException(status_code=409, detail=_error(request, code))
    content = await run_in_threadpool(render_fire_csv, results.projection)
    filename = f"fire-plan-{results.detail.plan.id}.csv"
    return Response(
        content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/fire/{scenario_id}", name="fire_scenario_update")
async def fire_scenario_update(request: Request, ctx: CurrentContext, scenario_id: str) -> Response:
    require_role(ctx, Role.EDITOR)
    values = await _form_data(request)
    event_rows = _editor_rows(values, _EVENT_KEY, "event", _EVENT_FIELDS)
    detail = await run_in_threadpool(
        fire_service.get_fire_plan, request.app.state.session_factory, ctx
    )
    if str(detail.plan.id) != scenario_id:
        raise NotFound
    try:
        plan = _plan_input(values, request.state.locale, detail.plan.currency)
        saved_id = await run_in_threadpool(
            fire_service.update_fire_plan,
            request.app.state.session_factory,
            ctx,
            plan,
        )
    except fire_service.FireError as exc:
        saved = await run_in_threadpool(
            fire_projection_service.scenario_results,
            request.app.state.session_factory,
            ctx,
            detail.plan.id,
        )
        context = await run_in_threadpool(
            _combined_context,
            request,
            ctx,
            saved,
            basis=_basis(values.get("basis")),
            inputs_open=True,
            error=_error(request, exc.code),
            error_code=exc.code,
            error_field=exc.field,
            values=values,
            event_rows=event_rows,
        )
        return render_template(request, "fire/results.html", status_code=400, context=context)
    return _redirect(
        request,
        "fire_scenario",
        notice="saved",
        basis=_basis(values.get("basis")),
        scenario_id=str(saved_id),
    )


__all__ = ["router"]
