"""FIRE plan pages (F8.8): one plan, its chooser and the current results/inputs page.

Routes parse and format form values and map service errors to field messages; validation, roles,
the one-time legacy choice and the current-year projection live in ``services.fire_scenarios`` and
``services.fire_projection``. Every state-changing route is covered by the router-level
``require_csrf`` dependency.
"""

from __future__ import annotations

import re
from decimal import Decimal
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
from assetboard.services import fire_projection as fire_projection_service
from assetboard.services import fire_scenarios as fire_service
from assetboard.services.context import Context, Role, has_role, require_role
from assetboard.web.context import CurrentContext
from assetboard.web.fire_charts import build_fire_charts
from assetboard.web.fire_csv import fire_scenario_csv as render_fire_csv
from assetboard.web.fire_sensitivity import build_sensitivity_view
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
        events=_event_inputs(values, locale, currency),
    )


def _base_values(
    *,
    current_year: int,
    currency: str,
    birth_year: int | None,
    horizon_age: int,
    inflation_bp: int,
    real_return_bp: int,
    income_growth_bp: int,
    swr_bp: int | None,
    stop_working_age: int | str = "",
    rule: str = Rule.RETURNS_COVER_EXPENSES.value,
    income_manual: str = "",
    expenses_manual: str = "",
    portfolio_manual: str = "",
) -> dict[str, str]:
    return {
        "current_year": str(current_year),
        "currency": currency,
        "birth_year": "" if birth_year is None else str(birth_year),
        "horizon_age": str(horizon_age),
        "stop_working_age": str(stop_working_age),
        "inflation": _rate_input(inflation_bp, "en"),
        "real_return": _rate_input(real_return_bp, "en"),
        "income_growth": _rate_input(income_growth_bp, "en"),
        "rule": rule,
        "swr": "" if swr_bp is None else _rate_input(swr_bp, "en"),
        "income_manual": income_manual,
        "expenses_manual": expenses_manual,
        "portfolio_manual": portfolio_manual,
        "extreme_rates_confirmed": "",
    }


def _values_for_locale(values: dict[str, str], locale: str) -> dict[str, str]:
    result = dict(values)
    for key in ("inflation", "real_return", "income_growth", "swr"):
        text = result.get(key, "").strip()
        if text:
            result[key] = format_quantity_input(parse_amount(text, "en"), locale)
    return result


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
    detail: fire_service.FirePlanDetail | None,
    defaults: fire_service.FirePlanDefaults | None,
    values: dict[str, str] | None,
    event_rows: list[dict[str, str]] | None,
    error: str | None = None,
    error_code: str | None = None,
    error_field: str | None = None,
) -> dict[str, object]:
    locale = request.state.locale
    if values is None:
        if detail is not None:
            values = _detail_values(detail, locale)
        else:
            assert defaults is not None
            values = _values_for_locale(
                _base_values(
                    current_year=defaults.current_year,
                    currency=defaults.currency,
                    birth_year=defaults.birth_year,
                    horizon_age=defaults.life_expectancy,
                    inflation_bp=defaults.inflation_bp,
                    real_return_bp=defaults.real_return_bp,
                    income_growth_bp=defaults.income_growth_bp,
                    swr_bp=defaults.swr_bp,
                ),
                locale,
            )
    if event_rows is None:
        event_rows = _event_rows(detail, locale) if detail is not None else []
    current_year = int(values.get("current_year", "0") or 0)
    window_end: int | None = None
    try:
        birth_year = int(values.get("birth_year", ""))
        horizon_age = int(values.get("horizon_age", ""))
        window_end = birth_year + horizon_age
    except ValueError:
        window_end = None
    if detail is not None:
        currency = detail.plan.currency
        title = _t(request, "FIRE plan")
        action = request.url_for("fire_scenario_update", scenario_id=str(detail.plan.id)).path
        cancel = request.url_for("fire_scenario", scenario_id=str(detail.plan.id)).path
    else:
        assert defaults is not None
        current_year = defaults.current_year
        currency = defaults.currency
        title = _t(request, "Create your FIRE plan")
        action = request.url_for("fire_scenario_create").path
        cancel = request.url_for("fire_scenarios").path
    return {
        "detail": detail,
        "values": values,
        "title": title,
        "action": action,
        "cancel_url": cancel,
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
            request.url_for("fire_scenario", scenario_id=str(results.detail.plan.id)).path
            + "?sensitivity=1#sensitivity"
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
        defaults=None,
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
    results_url = request.url_for("fire_scenario", scenario_id=str(detail.plan.id)).path
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
    plan = results.detail.plan
    fragment_url = request.url_for("fire_scenario_sensitivity", scenario_id=str(plan.id)).path
    page_url = (
        request.url_for("fire_scenario", scenario_id=str(plan.id)).path
        + "?sensitivity=1#sensitivity"
    )
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
        "sensitivity_inputs_url": (
            request.url_for("fire_scenario", scenario_id=str(plan.id)).path + "?edit=1#inputs"
        ),
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


@router.get("/fire", name="fire_scenarios")
async def fire_scenarios_page(request: Request, ctx: CurrentContext) -> Response:
    entry = await run_in_threadpool(fire_service.plan_entry, request.app.state.session_factory, ctx)
    if entry.migration is not None:
        return _redirect(
            request,
            "fire_scenario",
            notice="migrated",
            scenario_id=str(entry.migration.plan_id),
        )
    if entry.plan is not None:
        return RedirectResponse(
            request.url_for("fire_scenario", scenario_id=str(entry.plan.id)).path,
            status_code=303,
        )
    if entry.legacy:
        context = {
            "legacy": entry.legacy,
            "can_edit": has_role(ctx, Role.EDITOR),
            "error": _error(request, request.query_params.get("error")),
            "notice": _notice(request, request.query_params.get("notice")),
            "format_money": format_money,
        }
        return render_template(request, "fire/choose.html", context=context)
    defaults = await run_in_threadpool(
        fire_service.plan_defaults, request.app.state.session_factory, ctx
    )
    context = await run_in_threadpool(
        _form_context,
        request,
        ctx,
        detail=None,
        defaults=defaults,
        values=None,
        event_rows=None,
    )
    return render_template(request, "fire/form.html", context=context)


@router.get("/fire/new", name="fire_scenario_new")
async def fire_scenario_new(request: Request, ctx: CurrentContext) -> Response:
    require_role(ctx, Role.EDITOR)
    return RedirectResponse(request.url_for("fire_scenarios").path, status_code=303)


@router.get("/fire/compare", name="fire_compare")
async def fire_compare_page(request: Request, ctx: CurrentContext) -> Response:
    """The old comparison URL; comparison is gone in favour of the one-plan model."""
    require_role(ctx, Role.VIEWER)
    return RedirectResponse(request.url_for("fire_scenarios").path, status_code=303)


@router.post("/fire", name="fire_scenario_create")
async def fire_scenario_create(request: Request, ctx: CurrentContext) -> Response:
    require_role(ctx, Role.EDITOR)
    values = await _form_data(request)
    event_rows = _editor_rows(values, _EVENT_KEY, "event", _EVENT_FIELDS)
    defaults = await run_in_threadpool(
        fire_service.plan_defaults, request.app.state.session_factory, ctx
    )
    try:
        plan = _plan_input(values, request.state.locale, defaults.currency)
        if plan.events:
            raise fire_service.FireError("events", "events_not_allowed_on_create")
        plan_id = await run_in_threadpool(
            fire_service.create_fire_plan,
            request.app.state.session_factory,
            ctx,
            plan,
        )
    except fire_service.FireError as exc:
        context = await run_in_threadpool(
            _form_context,
            request,
            ctx,
            detail=None,
            defaults=defaults,
            values=values,
            event_rows=event_rows,
            error=_error(request, exc.code),
            error_code=exc.code,
            error_field=exc.field,
        )
        return render_template(request, "fire/form.html", status_code=400, context=context)
    return _redirect(request, "fire_scenario", notice="created", scenario_id=str(plan_id))


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
        result = await run_in_threadpool(
            fire_service.migrate_legacy_plan,
            request.app.state.session_factory,
            ctx,
            scenario_id,
        )
    except fire_service.FireError as exc:
        return _redirect(request, "fire_scenarios", error=exc.code)
    return _redirect(request, "fire_scenario", notice="migrated", scenario_id=str(result.plan_id))


@router.get("/fire/{scenario_id}", name="fire_scenario")
async def fire_scenario_page(request: Request, ctx: CurrentContext, scenario_id: str) -> Response:
    results = await run_in_threadpool(
        fire_projection_service.scenario_results,
        request.app.state.session_factory,
        ctx,
        _uuid(scenario_id),
    )
    error_code = request.query_params.get("error")
    error = _error(request, error_code)
    inputs_open = request.query_params.get("edit") == "1" or error is not None
    context = await run_in_threadpool(
        _combined_context,
        request,
        ctx,
        results,
        basis=_basis(request.query_params.get("basis")),
        inputs_open=inputs_open,
        notice=_notice(request, request.query_params.get("notice")),
        error=error,
        error_code=error_code,
    )
    if request.query_params.get("sensitivity") == "1":
        context.update(await run_in_threadpool(_sensitivity_context, request, results))
        context["sensitivity_loaded"] = True
    return render_template(request, "fire/results.html", context=context)


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
    url = request.url_for("fire_scenario", scenario_id=str(detail.plan.id)).path
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
