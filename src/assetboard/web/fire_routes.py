"""FIRE planner settings (F8.1): scenario list, overview and the create/edit form.

Routes parse and format form values and map service errors to field messages; validation,
roles, uniqueness, defaults and archiving live in ``services.fire_scenarios``. Every
state-changing route is covered by the router-level ``require_csrf`` dependency.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
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
    ExpensesMode,
    IncomeMode,
    PortfolioMode,
    Rule,
    SensitivityError,
    sensitivity,
    signed_amount_minor,
)
from assetboard.domain.money import Money
from assetboard.domain.transactions import EntryError, amount_minor
from assetboard.i18n import gettext
from assetboard.i18n.dates import format_day, format_month
from assetboard.i18n.numbers import (
    format_amount_input,
    format_money,
    format_quantity_input,
    format_rate_short,
    parse_amount,
)
from assetboard.services import fire_comparison as fire_comparison_service
from assetboard.services import fire_linked as fire_linked_service
from assetboard.services import fire_projection as fire_projection_service
from assetboard.services import fire_scenarios as fire_service
from assetboard.services.context import Context, Role, has_role, require_role
from assetboard.web.context import CurrentContext
from assetboard.web.fire_charts import build_fire_charts, build_fire_comparison_chart
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
    "invalid_name": _("Enter a name of 1 to 200 characters."),
    "birth_year_required": _("Enter your birth year, or add it to your profile first."),
    "invalid_birth_year": _("Enter a birth year between 1900 and the plan's start year."),
    "invalid_horizon": _("Enter a horizon between 1 and 120 years of age."),
    "horizon_not_after_age": _(
        "The horizon age must be older than your age at the start of the plan."
    ),
    "invalid_stop_age": _("Enter the age at which income stops, between 0 and 120."),
    "stop_before_age": _("Income cannot stop before your age at the start of the plan."),
    "invalid_rate": _("Enter a percentage between -20% and 50% with at most two decimals."),
    "invalid_swr": _("Enter a withdrawal rate between 0.5% and 20% with at most two decimals."),
    "invalid_rule": _("Choose how the plan decides that you are financially independent."),
    "invalid_income_mode": _("Choose how starting income is entered."),
    "invalid_expenses_mode": _("Choose how starting expenses are entered."),
    "invalid_portfolio_mode": _("Choose how the starting portfolio is entered."),
    "linked_mode_unavailable": _(
        "This input can only be resolved while saving; preview the plan from its page."
    ),
    "invalid_expense_exclusion": _(
        "An excluded category is no longer a regular expense category of this workspace. "
        "Reload the page and try again."
    ),
    "invalid_amount": _("Enter a valid non-negative amount in the plan currency."),
    "duplicate_name": _("Another active scenario already has this name."),
    "rows_outside_window": _(
        "Every contribution and event year must fall inside the plan's window."
    ),
    "duplicate_contribution_year": _("Each contribution year can appear only once."),
    "too_many_contributions": _("A scenario can have at most 120 contribution rows."),
    "too_many_events": _("A scenario can have at most 200 events."),
    "invalid_event_kind": _("Choose a valid event type."),
    "invalid_label": _("Give every event a short description."),
    "invalid_event": _(
        "This event is no longer part of the scenario. Reload the page and try again."
    ),
    "duplicate_event": _("The same event appears twice. Reload the page and try again."),
    "invalid_year": _("Enter a whole year for every row."),
    "archived": _("Archived scenarios are read-only. Restore the scenario first."),
    "extreme_rate_confirmation_required": _(
        "A rate is outside the usual range. Tick the confirmation box to save it."
    ),
    "invalid_extreme_rates": _("Tick the confirmation box only when you mean the unusual rates."),
    "missing_starting_value": _("The plan is missing a starting value."),
    "invalid_projection": _("The saved plan cannot be projected."),
    "loan_payoff_unavailable": _("This loan payoff can no longer be added."),
    "loan_event_exists": _("This loan payoff is already in the plan."),
}

RESULT_BLOCK_MESSAGES = {
    "missing_starting_value": _("The plan is missing a resolved starting value."),
    "invalid_projection": _("The saved plan cannot be projected."),
    "no_income_data": _(
        "No regular income transactions fall in the last 12 full months, so linked income is "
        "unavailable. Add income to the ledger or switch this input to Manual."
    ),
    "no_expense_data": _(
        "No regular expense transactions fall in the last 12 full months, so linked expenses are "
        "unavailable. Add expenses to the ledger or switch this input to Manual."
    ),
    "expenses_all_excluded": _(
        "Every regular expense category in the window is excluded, so linked expenses are "
        "unavailable. Clear an exclusion or switch this input to Manual."
    ),
    "ledger_currency_mismatch": _(
        "Some ledger rows in the window use another currency. Linked values need only the "
        "scenario currency; switch this input to Manual or review those rows."
    ),
    "scenario_currency_mismatch": _(
        "The workspace base currency no longer matches the scenario's currency, so linked "
        "values are unavailable. Create a new scenario or switch these inputs to Manual."
    ),
    "no_linked_portfolio_data": _(
        "No investable portfolio classes or investable manual net-worth items were found. Add "
        "data or switch the starting portfolio to Manual."
    ),
    "portfolio_price_missing": _(
        "An investable holding has no current price. Add a price or switch the starting "
        "portfolio to Manual."
    ),
    "portfolio_fx_missing": _(
        "An investable holding has no FX rate to the scenario currency. Add a rate or switch "
        "the starting portfolio to Manual."
    ),
    "item_value_missing": _(
        "An investable net-worth item has no value effective today. Add a valuation or switch "
        "the starting portfolio to Manual."
    ),
    "item_fx_missing": _(
        "An investable net-worth item has no FX rate to the scenario currency. Add a rate or "
        "switch the starting portfolio to Manual."
    ),
}

FALLBACK_ERROR = _("The scenario could not be saved. Reload the page and try again.")

COMPARISON_ERRORS = {
    "too_many_scenarios": _("Compare at most three scenarios at a time."),
    "currency_mismatch": _(
        "The selected scenarios use different currencies. Choose scenarios that share one "
        "currency; there is no currency conversion in the comparison."
    ),
}

SENSITIVITY_ERRORS = {
    "empty_axis": _("The sensitivity axes are empty."),
    "invalid_value": _("The sensitivity axes contain an invalid value."),
    "duplicate_value": _("The sensitivity axes contain a duplicate value."),
    "too_many_values": _("The sensitivity axes have too many values."),
    "too_many_cells": _("The sensitivity grid has too many cells."),
    "missing_base": _("The sensitivity axes must include the saved plan."),
    "return_out_of_range": _(
        "The real-return range would leave the supported -20 to 50 percent bounds. Adjust the "
        "scenario's return and try again."
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
    "created": _("Scenario created."),
    "saved": _("Scenario saved."),
    "duplicated": _("Scenario duplicated."),
    "archived": _("Scenario archived."),
    "restored": _("Scenario restored."),
    "default_changed": _("Default scenario changed."),
    "loan_event_added": _("Loan payoff added to the plan."),
}

RULE_LABELS = {
    Rule.RETURNS_COVER_EXPENSES.value: _("Returns cover expenses"),
    Rule.SWR.value: _("Withdrawal rate"),
}
EVENT_KIND_LABELS = {
    EventKind.CASH.value: _("One-off cash flow"),
    EventKind.EXPENSE_CHANGE.value: _("Annual expense change"),
    EventKind.INCOME_CHANGE.value: _("Annual income change"),
}
STARTING_FIELD_LABELS = {
    "income_manual": _("annual income"),
    "expenses_manual": _("annual expenses"),
    "portfolio_manual": _("starting portfolio"),
}

_CONTRIBUTION_KEY = re.compile(r"^contribution_(year|amount)_([0-9]+)$")
_EVENT_KEY = re.compile(r"^event_(id|year|kind|amount|label)_([0-9]+)$")
_CONTRIBUTION_FIELDS = ("year", "amount")
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


def _compare_url(request: Request, scenario_ids: Sequence[UUID], basis: str) -> str:
    query = [("scenario", str(scenario_id)) for scenario_id in scenario_ids]
    if basis == "real":
        query.append(("basis", "real"))
    path = request.url_for("fire_compare").path
    return f"{path}?{urlencode(query)}" if query else path


def _preselected_ids(scenarios: fire_service.FireScenarioList) -> tuple[UUID, ...]:
    """The default active scenario, else the first active one, else the first archived one."""
    if scenarios.active:
        default = next((row for row in scenarios.active if row.is_default), scenarios.active[0])
        return (default.id,)
    if scenarios.archived:
        return (scenarios.archived[0].id,)
    return ()


def _compare_context(
    request: Request,
    comparison: fire_comparison_service.FireComparison,
    scenarios: fire_service.FireScenarioList,
    *,
    basis: str,
) -> dict[str, object]:
    error_code = comparison.error_code
    chart = (
        build_fire_comparison_chart(comparison, basis, request.state.locale)
        if error_code is None
        else None
    )
    return {
        "comparison": comparison,
        "scenarios": scenarios,
        "selected_ids": set(comparison.selected_ids),
        "selected_order": comparison.selected_ids,
        "comparison_error": _t(request, COMPARISON_ERRORS[error_code]) if error_code else None,
        "comparison_error_code": error_code,
        "chart": chart,
        "basis": basis,
        "nominal_url": _compare_url(request, comparison.selected_ids, "nominal"),
        "real_url": _compare_url(request, comparison.selected_ids, "real"),
        "block_messages": {
            code: _t(request, message) for code, message in RESULT_BLOCK_MESSAGES.items()
        },
        "format_money": format_money,
    }


def _sensitivity_context(
    request: Request, results: fire_projection_service.FireScenarioResults
) -> dict[str, object]:
    scenario = results.detail.scenario
    fragment_url = request.url_for("fire_scenario_sensitivity", scenario_id=str(scenario.id)).path
    page_url = (
        request.url_for("fire_scenario", scenario_id=str(scenario.id)).path
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
            request.url_for("fire_scenario", scenario_id=str(scenario.id)).path + "?edit=1#inputs"
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


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise NotFound from None


def _basis(value: str | None) -> str:
    """The page's money basis; anything but ``real`` is the default nominal view."""
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


def _row_blank(row: dict[str, str], fields: tuple[str, ...]) -> bool:
    return all(not row.get(field, "").strip() for field in fields)


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
        code = "invalid_amount"
        raise fire_service.FireError(field, code) from None


def _scenario_money(
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


def _contribution_inputs(
    values: dict[str, str], locale: str, currency: str
) -> tuple[fire_service.ContributionInput, ...]:
    rows: list[fire_service.ContributionInput] = []
    for row in _editor_rows(values, _CONTRIBUTION_KEY, "contribution", _CONTRIBUTION_FIELDS):
        if _row_blank(row, _CONTRIBUTION_FIELDS):
            continue
        year = _row_year(row["year"], "contributions")
        amount = _money_from_text(
            row["amount"].strip(), locale, currency, signed=True, field="contributions"
        )
        rows.append(fire_service.ContributionInput(year=year, amount_minor=amount.amount_minor))
    return tuple(rows)


def _event_inputs(
    values: dict[str, str], locale: str, currency: str
) -> tuple[fire_service.EventInput, ...]:
    rows: list[fire_service.EventInput] = []
    for row in _editor_rows(values, _EVENT_KEY, "event", _EVENT_FIELDS):
        # The kind select always carries a value, so a row is blank when its text fields are.
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


def _scenario_input(
    values: dict[str, str],
    locale: str,
    currency: str,
    exclusion_ids: Sequence[str] = (),
) -> fire_service.FireScenarioInput:
    exclusions: list[UUID] = []
    for value in exclusion_ids:
        try:
            exclusions.append(UUID(value))
        except ValueError:
            raise fire_service.FireError(
                "expense_exclusions", "invalid_expense_exclusion"
            ) from None
    return fire_service.FireScenarioInput(
        name=values.get("name", ""),
        birth_year=_optional_int(values, "birth_year", "invalid_birth_year"),
        inflation_bp=_percent(values, "inflation", locale),
        real_return_bp=_percent(values, "real_return", locale),
        income_growth_bp=_percent(values, "income_growth", locale),
        stop_working_age=_required_int(values, "stop_working_age", "invalid_stop_age"),
        horizon_age=_required_int(values, "horizon_age", "invalid_horizon"),
        rule=values.get("rule", Rule.RETURNS_COVER_EXPENSES.value),
        swr_bp=_optional_percent(values, "swr", locale),
        income_mode=values.get("income_mode", IncomeMode.MANUAL.value),
        income_manual=_scenario_money(values, "income_manual", locale, currency, "income_manual"),
        expenses_mode=values.get("expenses_mode", ExpensesMode.MANUAL.value),
        expenses_manual=_scenario_money(
            values, "expenses_manual", locale, currency, "expenses_manual"
        ),
        portfolio_mode=values.get("portfolio_mode", PortfolioMode.MANUAL.value),
        portfolio_manual=_scenario_money(
            values, "portfolio_manual", locale, currency, "portfolio_manual"
        ),
        expense_exclusion_ids=tuple(exclusions),
        extreme_rates_confirmed=values.get("extreme_rates_confirmed") == "1",
        contributions=_contribution_inputs(values, locale, currency),
        events=_event_inputs(values, locale, currency),
    )


def _base_values(
    *,
    start_year: int,
    currency: str,
    birth_year: int | None,
    horizon_age: int,
    inflation_bp: int,
    real_return_bp: int,
    income_growth_bp: int,
    swr_bp: int | None,
    stop_working_age: int | str = "",
    rule: str = Rule.RETURNS_COVER_EXPENSES.value,
    income_mode: str = IncomeMode.MANUAL.value,
    expenses_mode: str = ExpensesMode.MANUAL.value,
    portfolio_mode: str = PortfolioMode.MANUAL.value,
    name: str = "",
    income_manual: str = "",
    expenses_manual: str = "",
    portfolio_manual: str = "",
) -> dict[str, str]:
    return {
        "name": name,
        "start_year": str(start_year),
        "currency": currency,
        "birth_year": "" if birth_year is None else str(birth_year),
        "horizon_age": str(horizon_age),
        "stop_working_age": str(stop_working_age),
        "inflation": _rate_input(inflation_bp, "en"),
        "real_return": _rate_input(real_return_bp, "en"),
        "income_growth": _rate_input(income_growth_bp, "en"),
        "rule": rule,
        "swr": "" if swr_bp is None else _rate_input(swr_bp, "en"),
        "income_mode": income_mode,
        "income_manual": income_manual,
        "expenses_mode": expenses_mode,
        "expenses_manual": expenses_manual,
        "portfolio_mode": portfolio_mode,
        "portfolio_manual": portfolio_manual,
        "extreme_rates_confirmed": "",
    }


def _values_for_locale(values: dict[str, str], locale: str) -> dict[str, str]:
    """Re-render the illustrative default rates with the interface's decimal sign."""
    result = dict(values)
    for key in ("inflation", "real_return", "income_growth", "swr"):
        text = result.get(key, "").strip()
        if text:
            result[key] = format_quantity_input(parse_amount(text, "en"), locale)
    return result


def _detail_values(detail: fire_service.FireScenarioDetail, locale: str) -> dict[str, str]:
    scenario = detail.scenario
    return {
        "name": scenario.name,
        "start_year": str(scenario.start_year),
        "currency": scenario.currency,
        "birth_year": str(scenario.birth_year),
        "horizon_age": str(scenario.horizon_age),
        "stop_working_age": str(scenario.stop_working_age),
        "inflation": _rate_input(scenario.inflation_bp, locale),
        "real_return": _rate_input(scenario.real_return_bp, locale),
        "income_growth": _rate_input(scenario.income_growth_bp, locale),
        "rule": scenario.rule,
        "swr": "" if scenario.swr_bp is None else _rate_input(scenario.swr_bp, locale),
        "income_mode": scenario.income_mode,
        "income_manual": ""
        if scenario.income_manual is None
        else format_amount_input(scenario.income_manual, locale),
        "expenses_mode": scenario.expenses_mode,
        "expenses_manual": ""
        if scenario.expenses_manual is None
        else format_amount_input(scenario.expenses_manual, locale),
        "portfolio_mode": scenario.portfolio_mode,
        "portfolio_manual": ""
        if scenario.portfolio_manual is None
        else format_amount_input(scenario.portfolio_manual, locale),
        "extreme_rates_confirmed": "",
    }


def _contribution_rows(
    detail: fire_service.FireScenarioDetail, locale: str
) -> list[dict[str, str]]:
    return [
        {
            "index": str(index),
            "year": str(row.year),
            "amount": format_amount_input(row.amount, locale),
        }
        for index, row in enumerate(detail.contributions, start=1)
    ]


def _event_rows(detail: fire_service.FireScenarioDetail, locale: str) -> list[dict[str, str]]:
    return [
        {
            "index": str(index),
            "id": str(row.id),
            "year": str(row.year),
            "kind": row.kind,
            "amount": format_amount_input(row.amount, locale),
            "label": row.label,
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
    detail: fire_service.FireScenarioDetail | None,
    defaults: fire_service.FireScenarioDefaults | None,
    values: dict[str, str] | None,
    contribution_rows: list[dict[str, str]] | None,
    event_rows: list[dict[str, str]] | None,
    exclusion_ids: Sequence[str] | None = None,
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
                    start_year=defaults.start_year,
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
    if contribution_rows is None:
        contribution_rows = _contribution_rows(detail, locale) if detail is not None else []
    if event_rows is None:
        event_rows = _event_rows(detail, locale) if detail is not None else []
    if exclusion_ids is None:
        exclusion_ids = (
            tuple(str(category_id) for category_id in detail.expense_exclusion_ids)
            if detail is not None
            else ()
        )
    selected_exclusions: set[UUID] = set()
    for value in exclusion_ids:
        try:
            selected_exclusions.add(UUID(value))
        except ValueError:
            continue
    exclusion_options = fire_linked_service.expense_exclusion_options(
        request.app.state.session_factory,
        ctx,
        selected_ids=selected_exclusions,
    )
    empty_contribution = _blank_row(_row_index(contribution_rows), _CONTRIBUTION_FIELDS)
    empty_event = _blank_row(_row_index(event_rows), _EVENT_FIELDS)
    window_end: int | None = None
    try:
        birth_year = int(values.get("birth_year", ""))
        horizon_age = int(values.get("horizon_age", ""))
        window_end = birth_year + horizon_age
    except ValueError:
        window_end = None
    if detail is not None:
        start_year = detail.scenario.start_year
        currency = detail.scenario.currency
        title = _t(request, "Edit scenario")
        action = request.url_for("fire_scenario_update", scenario_id=str(detail.scenario.id)).path
        cancel = request.url_for("fire_scenario", scenario_id=str(detail.scenario.id)).path
    else:
        assert defaults is not None
        start_year = defaults.start_year
        currency = defaults.currency
        title = _t(request, "New scenario")
        action = request.url_for("fire_scenario_create").path
        cancel = request.url_for("fire_scenarios").path
    return {
        "detail": detail,
        "values": values,
        "title": title,
        "action": action,
        "cancel_url": cancel,
        "start_year": start_year,
        "currency": currency,
        "window_end": window_end,
        "contribution_rows": contribution_rows,
        "empty_contribution": empty_contribution,
        "event_rows": event_rows,
        "empty_event": empty_event,
        "expense_exclusion_options": exclusion_options,
        "selected_exclusion_ids": selected_exclusions,
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


def _list_context(
    request: Request,
    ctx: Context,
    *,
    notice: str | None = None,
    error: str | None = None,
) -> dict[str, object]:
    scenarios = fire_service.list_fire_scenarios(request.app.state.session_factory, ctx)
    return {
        "scenarios": scenarios,
        "notice": notice,
        "error": error,
        "can_edit": has_role(ctx, Role.EDITOR),
        "format_money": format_money,
        "format_rate_short": format_rate_short,
        "format_day": format_day,
        "rule_labels": {code: _t(request, label) for code, label in RULE_LABELS.items()},
    }


def _results_context(
    request: Request,
    ctx: Context,
    results: fire_projection_service.FireScenarioResults,
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
        "can_preview": has_role(ctx, Role.EDITOR) and not results.detail.scenario.archived,
        "field_labels": {
            field: _t(request, label) for field, label in STARTING_FIELD_LABELS.items()
        },
        "block_messages": {
            code: _t(request, message) for code, message in RESULT_BLOCK_MESSAGES.items()
        },
        "linked": results.linked,
        "format_money": format_money,
        "format_rate_short": format_rate_short,
        "format_day": lambda day: format_day(day, request.state.locale),
        "format_month": lambda month: format_month(month, request.state.locale),
        "rule_labels": {code: _t(request, label) for code, label in RULE_LABELS.items()},
        "event_kind_labels": {
            code: _t(request, label) for code, label in EVENT_KIND_LABELS.items()
        },
        "sensitivity_loaded": False,
        "sensitivity_fragment_url": request.url_for(
            "fire_scenario_sensitivity", scenario_id=str(results.detail.scenario.id)
        ).path,
        "sensitivity_page_url": (
            request.url_for("fire_scenario", scenario_id=str(results.detail.scenario.id)).path
            + "?sensitivity=1#sensitivity"
        ),
    }


def _combined_context(
    request: Request,
    ctx: Context,
    results: fire_projection_service.FireScenarioResults,
    *,
    basis: str,
    inputs_open: bool,
    notice: str | None = None,
    error: str | None = None,
    error_code: str | None = None,
    error_field: str | None = None,
    values: dict[str, str] | None = None,
    contribution_rows: list[dict[str, str]] | None = None,
    event_rows: list[dict[str, str]] | None = None,
    exclusion_ids: Sequence[str] | None = None,
) -> dict[str, object]:
    detail = results.detail
    context = _form_context(
        request,
        ctx,
        detail=detail,
        defaults=None,
        values=values,
        contribution_rows=contribution_rows,
        event_rows=event_rows,
        exclusion_ids=exclusion_ids,
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
    results_url = request.url_for("fire_scenario", scenario_id=str(detail.scenario.id)).path
    if basis == "real":
        results_url = f"{results_url}?basis=real"
    context["cancel_url"] = results_url
    return context


async def _form_data(request: Request) -> tuple[dict[str, str], list[str]]:
    """Text fields plus the repeated ``expense_exclusion`` checkbox values."""
    form = await request.form()
    values = {key: value for key, value in form.items() if isinstance(value, str)}
    exclusions = [
        value
        for value in form.getlist("expense_exclusion")
        if isinstance(value, str) and value.strip()
    ]
    return values, exclusions


def _redirect(
    request: Request,
    route: str,
    *,
    notice: str | None = None,
    error: str | None = None,
    basis: str | None = None,
    anchor: str | None = None,
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
    if anchor is not None:
        url = f"{url}#{anchor}"
    return RedirectResponse(url, status_code=303)


@router.get("/fire", name="fire_scenarios")
async def fire_scenarios_page(request: Request, ctx: CurrentContext) -> Response:
    context = await run_in_threadpool(
        _list_context,
        request,
        ctx,
        notice=_notice(request, request.query_params.get("notice")),
        error=_error(request, request.query_params.get("error")),
    )
    return render_template(request, "fire/list.html", context=context)


@router.get("/fire/new", name="fire_scenario_new")
async def fire_scenario_new(request: Request, ctx: CurrentContext) -> Response:
    require_role(ctx, Role.EDITOR)
    defaults = await run_in_threadpool(
        fire_service.scenario_defaults, request.app.state.session_factory, ctx
    )
    context = await run_in_threadpool(
        _form_context,
        request,
        ctx,
        detail=None,
        defaults=defaults,
        values=None,
        contribution_rows=None,
        event_rows=None,
    )
    return render_template(request, "fire/form.html", context=context)


@router.get("/fire/compare", name="fire_compare")
async def fire_compare_page(request: Request, ctx: CurrentContext) -> Response:
    """Compare up to three saved scenarios; a read-only GET page for every role."""
    require_role(ctx, Role.VIEWER)
    selected = tuple(_uuid(value) for value in request.query_params.getlist("scenario"))
    scenarios = await run_in_threadpool(
        fire_service.list_fire_scenarios, request.app.state.session_factory, ctx
    )
    if not selected:
        selected = _preselected_ids(scenarios)
    comparison = await run_in_threadpool(
        fire_comparison_service.compare_fire_scenarios,
        request.app.state.session_factory,
        ctx,
        selected,
    )
    basis = _basis(request.query_params.get("basis"))
    context = await run_in_threadpool(
        _compare_context,
        request,
        comparison,
        scenarios,
        basis=basis,
    )
    return render_template(request, "fire/compare.html", context=context)


@router.post("/fire", name="fire_scenario_create")
async def fire_scenario_create(request: Request, ctx: CurrentContext) -> Response:
    require_role(ctx, Role.EDITOR)
    values, exclusion_ids = await _form_data(request)
    rows = _editor_rows(values, _CONTRIBUTION_KEY, "contribution", _CONTRIBUTION_FIELDS)
    event_rows = _editor_rows(values, _EVENT_KEY, "event", _EVENT_FIELDS)
    defaults = await run_in_threadpool(
        fire_service.scenario_defaults, request.app.state.session_factory, ctx
    )
    try:
        scenario = _scenario_input(values, request.state.locale, defaults.currency, exclusion_ids)
        scenario_id = await run_in_threadpool(
            fire_service.create_fire_scenario,
            request.app.state.session_factory,
            ctx,
            scenario,
        )
    except fire_service.FireError as exc:
        context = await run_in_threadpool(
            _form_context,
            request,
            ctx,
            detail=None,
            defaults=defaults,
            values=values,
            contribution_rows=rows,
            event_rows=event_rows,
            exclusion_ids=exclusion_ids,
            error=_error(request, exc.code),
            error_code=exc.code,
            error_field=exc.field,
        )
        return render_template(request, "fire/form.html", status_code=400, context=context)
    return _redirect(request, "fire_scenario", notice="created", scenario_id=str(scenario_id))


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
    """The saved plan's sensitivity grid as an HTMX fragment; read-only for every role."""
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
        fire_service.get_fire_scenario,
        request.app.state.session_factory,
        ctx,
        _uuid(scenario_id),
    )
    if detail.scenario.archived:
        return _redirect(
            request,
            "fire_scenario",
            error="archived",
            scenario_id=str(detail.scenario.id),
        )
    url = request.url_for("fire_scenario", scenario_id=str(detail.scenario.id)).path
    return RedirectResponse(f"{url}?edit=1#inputs", status_code=303)


@router.post("/fire/{scenario_id}/preview", name="fire_scenario_preview")
async def fire_scenario_preview(
    request: Request, ctx: CurrentContext, scenario_id: str
) -> Response:
    """Return only the results fragment for the posted inputs; never write anything."""
    require_role(ctx, Role.EDITOR)
    values, exclusion_ids = await _form_data(request)
    detail = await run_in_threadpool(
        fire_service.get_fire_scenario,
        request.app.state.session_factory,
        ctx,
        _uuid(scenario_id),
    )
    basis = _basis(values.get("basis"))
    try:
        scenario = _scenario_input(
            values, request.state.locale, detail.scenario.currency, exclusion_ids
        )
        results = await run_in_threadpool(
            fire_projection_service.preview_scenario_results,
            ctx,
            detail,
            scenario,
            factory=request.app.state.session_factory,
        )
    except fire_service.FireError as exc:
        # The preview never replaces the saved result with a broken one: show what is saved and
        # explain the field the user needs to fix.
        saved = await run_in_threadpool(
            fire_projection_service.scenario_results,
            request.app.state.session_factory,
            ctx,
            detail.scenario.id,
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
    filename = f"fire-scenario-{results.detail.scenario.id}.csv"
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
    values, exclusion_ids = await _form_data(request)
    rows = _editor_rows(values, _CONTRIBUTION_KEY, "contribution", _CONTRIBUTION_FIELDS)
    event_rows = _editor_rows(values, _EVENT_KEY, "event", _EVENT_FIELDS)
    detail = await run_in_threadpool(
        fire_service.get_fire_scenario,
        request.app.state.session_factory,
        ctx,
        _uuid(scenario_id),
    )
    try:
        scenario = _scenario_input(
            values, request.state.locale, detail.scenario.currency, exclusion_ids
        )
        saved_id = await run_in_threadpool(
            fire_service.update_fire_scenario,
            request.app.state.session_factory,
            ctx,
            detail.scenario.id,
            scenario,
        )
    except fire_service.FireError as exc:
        saved = await run_in_threadpool(
            fire_projection_service.scenario_results,
            request.app.state.session_factory,
            ctx,
            detail.scenario.id,
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
            contribution_rows=rows,
            event_rows=event_rows,
            exclusion_ids=exclusion_ids,
        )
        return render_template(request, "fire/results.html", status_code=400, context=context)
    return _redirect(
        request,
        "fire_scenario",
        notice="saved",
        basis=_basis(values.get("basis")),
        scenario_id=str(saved_id),
    )


@router.post("/fire/{scenario_id}/duplicate", name="fire_scenario_duplicate")
async def fire_scenario_duplicate(
    request: Request, ctx: CurrentContext, scenario_id: str
) -> Response:
    try:
        new_id = await run_in_threadpool(
            fire_service.duplicate_fire_scenario,
            request.app.state.session_factory,
            ctx,
            _uuid(scenario_id),
        )
    except fire_service.FireError as exc:
        return _redirect(
            request, "fire_scenario", error=exc.code, scenario_id=str(_uuid(scenario_id))
        )
    return _redirect(request, "fire_scenario", notice="duplicated", scenario_id=str(new_id))


@router.post("/fire/{scenario_id}/default", name="fire_scenario_default")
async def fire_scenario_default(
    request: Request, ctx: CurrentContext, scenario_id: str
) -> Response:
    try:
        await run_in_threadpool(
            fire_service.set_default_fire_scenario,
            request.app.state.session_factory,
            ctx,
            _uuid(scenario_id),
        )
    except fire_service.FireError as exc:
        return _redirect(
            request, "fire_scenario", error=exc.code, scenario_id=str(_uuid(scenario_id))
        )
    return _redirect(request, "fire_scenarios", notice="default_changed")


@router.post("/fire/{scenario_id}/archive", name="fire_scenario_archive")
async def fire_scenario_archive(
    request: Request, ctx: CurrentContext, scenario_id: str
) -> Response:
    try:
        await run_in_threadpool(
            fire_service.archive_fire_scenario,
            request.app.state.session_factory,
            ctx,
            _uuid(scenario_id),
        )
    except fire_service.FireError as exc:
        return _redirect(request, "fire_scenarios", error=exc.code)
    return _redirect(request, "fire_scenarios", notice="archived")


@router.post("/fire/{scenario_id}/loan-payoff", name="fire_scenario_loan_payoff")
async def fire_scenario_loan_payoff(
    request: Request, ctx: CurrentContext, scenario_id: str
) -> Response:
    """One-click loan payoff event; a full POST outside the live-preview form."""
    require_role(ctx, Role.EDITOR)
    form = await request.form()
    loan_value = form.get("loan_id")
    try:
        loan_id = _uuid(loan_value if isinstance(loan_value, str) else "")
        outcome = await run_in_threadpool(
            fire_linked_service.add_loan_payoff_event,
            request.app.state.session_factory,
            ctx,
            _uuid(scenario_id),
            loan_id,
        )
    except fire_linked_service.LinkedError as exc:
        return _redirect(
            request,
            "fire_scenario",
            error=exc.code,
            anchor="fire-loan-suggestions",
            scenario_id=str(_uuid(scenario_id)),
        )
    return _redirect(
        request,
        "fire_scenario",
        notice="loan_event_added" if outcome == "added" else None,
        error=None if outcome == "added" else "loan_event_exists",
        anchor="fire-loan-suggestions",
        scenario_id=str(_uuid(scenario_id)),
    )


@router.post("/fire/{scenario_id}/restore", name="fire_scenario_restore")
async def fire_scenario_restore(
    request: Request, ctx: CurrentContext, scenario_id: str
) -> Response:
    try:
        await run_in_threadpool(
            fire_service.restore_fire_scenario,
            request.app.state.session_factory,
            ctx,
            _uuid(scenario_id),
        )
    except fire_service.FireError as exc:
        return _redirect(request, "fire_scenarios", error=exc.code)
    return _redirect(request, "fire_scenarios", notice="restored")


__all__ = ["router"]
