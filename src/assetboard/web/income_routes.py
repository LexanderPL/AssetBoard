from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import TypedDict
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from markupsafe import escape
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, RedirectResponse, Response

from assetboard.db.repositories import NotFound
from assetboard.domain.income import pay_date
from assetboard.domain.money import Money, minor_unit_for
from assetboard.domain.months import YearMonth
from assetboard.domain.taxes import TaxKind
from assetboard.domain.transactions import amount_minor
from assetboard.i18n import gettext
from assetboard.i18n.dates import (
    format_day,
    format_day_long,
    format_day_month,
    format_day_of_month,
    format_month,
    format_month_heading,
    month_names,
    weekday_names,
)
from assetboard.i18n.numbers import (
    format_amount_input,
    format_money,
    format_quantity_input,
    parse_amount,
)
from assetboard.services import income, taxes
from assetboard.services.categories import CategoryList, list_categories
from assetboard.services.context import Context, Role, has_role, require_role
from assetboard.services.months import MonthClosed
from assetboard.services.transactions import InvalidTransaction
from assetboard.web.context import CurrentContext
from assetboard.web.errors import is_htmx_fragment_request
from assetboard.web.rendering import render_template, scoped_url
from assetboard.web.security import require_csrf
from assetboard.web.tax_routes import rate_label

router = APIRouter(dependencies=[Depends(require_csrf)])


class _SourceFields(TypedDict):
    name: str
    kind: str
    category_id: UUID
    fixed_minor: int | None
    rate_minor: int | None
    unit_label: str
    pay_day_value: int
    pay_lag_months: int
    expected_mode: str
    expected_units_value: Decimal | None
    average_months: int
    active_from: date
    active_to: date | None


def _(message: str) -> str:
    return message


ERRORS = {
    "invalid_source": _("Enter valid income source details."),
    "invalid_amount": _("Enter a positive amount in the workspace currency."),
    "amount_too_large": _("The calculated amount is too large."),
    "negative_income_after_tax": _("The tax rules calculate a negative income amount."),
    "invalid_units": _("Enter a non-negative number of units."),
    "invalid_date": _("Enter a valid date between 1900 and 2199."),
    "invalid_work_month": _("Choose a valid work month."),
    "invalid_category": _("Choose an active income category."),
    "unsupported_holidays": _("Working-day estimates are unavailable for this workspace country."),
    "invalid_tax_attachment": _("Choose valid taxes and payment days between 1 and 31."),
    "invalid_tax_category": _("Choose an expense category that is not a transfer."),
    "duplicate_tax_rule": _("This tax is already ticked for the income source."),
    "tax_without_category": _(
        "This tax has no category to record its payments in. Choose one on the tax first."
    ),
    "attachment_in_use": _("A posted tax payment uses this tax, so it cannot be unticked."),
    "month_closed": _("This month is closed. Reopen it before changing the income estimate."),
    "archived": _("Archived income sources cannot be changed."),
    "derived_work_log": _(
        "This month's units come from the work calendar. Change the days in the calendar."
    ),
    "invalid_day_units": _("Enter a number of units above 0 and at most 24."),
    "posted_locked": _("This month's income is already received. Edit the transaction instead."),
    "work_month_closed": _("This month is closed. Reopen it to make changes."),
    "not_received": _("This month's income is not received, so there is no transaction to update."),
    "missing_name": _("Enter a name."),
    "invalid_unit": _("Enter what you are paid per, for example day or hour."),
    "invalid_average": _("Enter a number of months between 1 and 24."),
    "invalid_pay_day": _("Enter a pay day between 1 and 31."),
    "invalid_range": _("The end month cannot be before the start month."),
    "failed": _("The income source could not be saved. Check the form and try again."),
}


def _id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise NotFound from None


def _day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise income.IncomeError("invalid_date") from None


def _month(values: dict[str, str], name: str) -> date | None:
    """The first day of the month chosen in the ``<name>_month`` and ``<name>_year`` selects;
    None when both are empty. A plain ``<name>`` ISO date is still accepted."""
    year, month = values.get(f"{name}_year", "").strip(), values.get(f"{name}_month", "").strip()
    if not year and not month:
        legacy = values.get(name, "").strip()
        return _day(legacy) if legacy else None
    try:
        return date(int(year), int(month), 1)
    except ValueError:
        raise income.IncomeError("invalid_work_month") from None


def _required_month(values: dict[str, str], name: str) -> date:
    chosen = _month(values, name)
    if chosen is None:
        raise income.IncomeError("invalid_work_month")
    return chosen


def _source_input(values: dict[str, str], locale: str, currency: str) -> _SourceFields:
    kind = values.get("kind", "variable")
    fixed_minor = None
    rate_minor = None
    if kind == "fixed":
        fixed_minor = amount_minor(parse_amount(values.get("fixed_amount", ""), locale), currency)
    else:
        rate_minor = amount_minor(parse_amount(values.get("rate_amount", ""), locale), currency)
    units = (
        parse_amount(values.get("expected_units", ""), locale)
        if values.get("expected_units", "").strip()
        and values.get("expected_mode", "manual") == "fixed"
        else None
    )
    return {
        "name": values.get("name", ""),
        "kind": kind,
        "category_id": _id(values.get("category_id", "")),
        "fixed_minor": fixed_minor,
        "rate_minor": rate_minor,
        "unit_label": _unit_label(values),
        "pay_day_value": int(values.get("pay_day", "1")),
        "pay_lag_months": int(values.get("pay_lag_months", "0")),
        "expected_mode": values.get("expected_mode", "manual"),
        "expected_units_value": units,
        "average_months": int(values.get("average_months", "3")),
        "active_from": _required_month(values, "active_from"),
        "active_to": _month(values, "active_to"),
    }


def _format_work_month(work_month: date, locale: str) -> str:
    return format_month(YearMonth.from_date(work_month), locale)


def _redirect(
    request: Request,
    error: str | None = None,
    form: str | None = None,
    *,
    extra: dict[str, str] | None = None,
    anchor: str = "",
) -> Response:
    """Back to the sources page; ``form`` names the collapsed form to reopen with the error."""
    target = request.url_for("income_sources").path
    query = ({"error": error} | ({"form": form} if form else {}) | (extra or {})) if error else {}
    fragment = f"#{anchor}" if anchor else ""
    return RedirectResponse(
        target + ("?" + urlencode(query) if query else "") + fragment, status_code=303
    )


async def _form(request: Request) -> dict[str, str]:
    data = await request.form()
    return {key: value for key, value in data.items() if isinstance(value, str)}


def _common(request: Request, ctx: Context) -> dict[str, object]:
    today = ctx.clock.today(ctx.workspace.timezone)
    return {
        "taxes_enabled": ctx.workspace.taxes_enabled,
        "can_edit": has_role(ctx, Role.EDITOR),
        "today": today,
        "month_names": month_names(request.state.locale),
        "picker_years": list(range(today.year - 10, today.year + 6)),
        "format_money": format_money,
        "format_amount_input": format_amount_input,
        "format_quantity_input": format_quantity_input,
        "format_month": _format_work_month,
        "format_day": format_day,
        "format_day_of_month": format_day_of_month,
        "rate_label": rate_label,
        "format_day_month": format_day_month,
    }


def _error_text(request: Request, error: str | None) -> str | None:
    if not error:
        return None
    return gettext(ERRORS.get(error, ERRORS["failed"]), request.state.locale)


def _transaction_href(request: Request, transaction_id: UUID, day: date | YearMonth) -> str:
    """The Transactions page of the row's month with its inline edit open."""
    month = day if isinstance(day, YearMonth) else YearMonth.from_date(day)
    path = scoped_url(request, "transactions_month", month=str(month))
    return f"{path}?{urlencode({'edit': str(transaction_id)})}#tx-{transaction_id}"


def _locked_href(request: Request) -> str | None:
    """The posted transaction's edit link after a refused work log (``?tx=<id>&tx_month=``)."""
    if request.query_params.get("error") != "posted_locked":
        return None
    try:
        transaction_id = UUID(request.query_params.get("tx", ""))
        month = YearMonth.parse(request.query_params.get("tx_month", ""))
    except ValueError:
        return None
    return _transaction_href(request, transaction_id, month)


def _edit_anyway_href(request: Request) -> str | None:
    """After a refused (soft-locked) work log: reopen its Log form for the month, with the
    override already chosen (``?log=<source>&month=YYYY-MM&override=1``)."""
    if request.query_params.get("error") != "posted_locked":
        return None
    source = request.query_params.get("source", "")
    try:
        month = YearMonth.parse(request.query_params.get("work_month", ""))
        UUID(source)
    except ValueError:
        return None
    path = scoped_url(request, "income_sources")
    query = urlencode({"log": source, "month": str(month), "override": "1"})
    return f"{path}?{query}#income-source-{source}"


def _closed_href(request: Request, month: YearMonth | None = None) -> str | None:
    """The Month page where a closed month is reopened (``?closed=YYYY-MM`` after an error)."""
    if month is None:
        if request.query_params.get("error") != "work_month_closed":
            return None
        try:
            month = YearMonth.parse(request.query_params.get("closed", ""))
        except ValueError:
            return None
    return scoped_url(request, "month", month=str(month))


def _posted_change(
    request: Request, ctx: Context, source: str | None, month: str | None
) -> income.PostedIncomeChange | None:
    """The received-payment notice after an override save (``?changed=<source>&month=``)."""
    try:
        source_id = UUID(source or "")
        work_month = YearMonth.parse(month or "").first_day
    except ValueError:
        return None
    try:
        return income.posted_income_change(
            request.app.state.session_factory, ctx, source_id, work_month
        )
    except (income.IncomeError, NotFound):
        return None


def _change_context(
    request: Request, change: income.PostedIncomeChange | None
) -> dict[str, object]:
    return {
        "posted_change": change,
        "posted_change_href": (
            _transaction_href(request, change.transaction_id, change.transaction_date)
            if change is not None
            else None
        ),
    }


def _log_prefill(request: Request) -> tuple[str | None, date | None]:
    """``?log=<source id>&month=YYYY-MM`` opens that source's Log form for the month."""
    source = request.query_params.get("log")
    try:
        month = YearMonth.parse(request.query_params.get("month", "")).first_day
    except ValueError:
        month = None
    return (source if source else None), month


def _load_page_data(
    request: Request, ctx: Context
) -> tuple[
    list[income.IncomeSourceView],
    CategoryList,
    list[taxes.TaxRuleView],
    income.PostedIncomeChange | None,
]:
    factory = request.app.state.session_factory
    return (
        income.list_sources(factory, ctx),
        list_categories(factory, ctx),
        taxes.list_rules(factory, ctx) if ctx.workspace.taxes_enabled else [],
        _posted_change(
            request, ctx, request.query_params.get("changed"), request.query_params.get("month")
        ),
    )


@router.get("/income", name="income_sources")
async def sources_page(request: Request, ctx: CurrentContext) -> Response:
    rows, categories, rules, change = await run_in_threadpool(_load_page_data, request, ctx)
    log_source, log_month = _log_prefill(request)
    open_form = request.query_params.get("form")
    if log_source is not None and not open_form:
        open_form = f"log-{log_source}"
    return render_template(
        request,
        "income/sources.html",
        context={
            "sources": rows,
            "income_categories": categories.income,
            "category_names": {category.id: category.name for category in categories.income},
            "tax_rules": [rule for rule in rules if not rule.archived],
            "rules_by_id": {rule.id: rule for rule in rules},
            "withheld_ids": {rule.id for rule in rules if rule.kind == TaxKind.PAYER_WITHHELD},
            "error": _error_text(request, request.query_params.get("error")),
            "error_href": _locked_href(request),
            "edit_anyway_href": _edit_anyway_href(request),
            "closed_href": _closed_href(request),
            # ``form`` reopens a collapsed form: after an error, or from a "Choose taxes" link.
            "open_form": open_form,
            "log_month": log_month,
            # ``?override=1`` (from "Edit anyway") opens the Log form already overriding.
            "log_override": request.query_params.get("override") == "1",
            "notice": request.query_params.get("notice"),
            **_change_context(request, change),
            **_common(request, ctx),
        },
    )


@router.post("/income", name="income_source_create")
async def create_source(request: Request, ctx: CurrentContext) -> Response:
    """The one-form create of older clients; the app itself uses the "Add income" steps."""
    values = await _form(request)
    try:
        fields = _source_input(values, request.state.locale, ctx.workspace.base_currency)
        await run_in_threadpool(
            lambda: income.create_source(request.app.state.session_factory, ctx, **fields)
        )
        await run_in_threadpool(
            income.generate_workspace,
            request.app.state.session_factory,
            ctx,
            ctx.clock,
            request.app.state.settings,
        )
    except income.IncomeError as exc:
        return _redirect(request, exc.code)
    except (ValueError, ArithmeticError):
        return _redirect(request, "invalid_source")
    return _redirect(request)


# "Add income": four steps without JavaScript. Every step posts all values; the fields of the
# other steps travel in hidden inputs, and each step is validated before the next one opens.

WIZARD_FIELDS = (
    "name",
    "category_id",
    "kind",
    "fixed_amount",
    "rate_amount",
    "unit_choice",
    "unit_other",
    "expected_mode",
    "expected_units",
    "average_months",
    "pay_day",
    "pay_lag_months",
    "active_from_month",
    "active_from_year",
    "active_to_month",
    "active_to_year",
)
# Which fields each step shows; the others are carried in hidden inputs.
STEP_FIELDS = {
    1: ("name", "category_id", "kind"),
    2: (
        "fixed_amount",
        "rate_amount",
        "unit_choice",
        "unit_other",
        "expected_mode",
        "expected_units",
        "average_months",
    ),
    3: (
        "pay_day",
        "pay_lag_months",
        "active_from_month",
        "active_from_year",
        "active_to_month",
        "active_to_year",
    ),
    4: (),
}
PAY_LAGS = (1, 0, 2)
EXPECTED_MODES = ("working_days", "average", "fixed", "manual")


def _wizard_values(
    values: dict[str, str], ctx: Context, categories: CategoryList
) -> dict[str, str]:
    today = ctx.clock.today(ctx.workspace.timezone)
    first_category = next((str(c.id) for c in categories.income), "")
    defaults = {
        "category_id": first_category,
        "kind": "fixed",
        "unit_choice": "day",
        "expected_mode": "working_days",
        "average_months": "3",
        "pay_day": "10",
        "pay_lag_months": "1",
        "active_from_month": str(today.month),
        "active_from_year": str(today.year),
    }
    return {name: values.get(name, defaults.get(name, "")).strip() for name in WIZARD_FIELDS}


def _unit_label(values: dict[str, str]) -> str:
    """ "Per day", "per hour" or a free label; older forms post ``unit_label`` directly."""
    choice = values.get("unit_choice", "")
    if choice in ("day", "hour"):
        return choice
    if choice == "other":
        return values.get("unit_other", "").strip()
    return values.get("unit_label", "day")


def _check_step(
    step: int, values: dict[str, str], locale: str, currency: str, categories: CategoryList
) -> None:
    """Raise ``IncomeError`` with the first problem of ``step``'s fields."""
    if step == 1:
        if not values["name"] or len(values["name"]) > 200:
            raise income.IncomeError("missing_name")
        if values["category_id"] not in {str(c.id) for c in categories.income}:
            raise income.IncomeError("invalid_category")
        if values["kind"] not in ("fixed", "variable"):
            raise income.IncomeError("invalid_source")
    elif step == 2:
        field = "fixed_amount" if values["kind"] == "fixed" else "rate_amount"
        try:
            minor = amount_minor(parse_amount(values[field], locale), currency)
        except (ValueError, ArithmeticError):
            raise income.IncomeError("invalid_amount") from None
        if minor <= 0:
            raise income.IncomeError("invalid_amount")
        if values["kind"] == "fixed":
            return
        if not 0 < len(_unit_label(values)) <= 40:
            raise income.IncomeError("invalid_unit")
        if values["expected_mode"] not in EXPECTED_MODES:
            raise income.IncomeError("invalid_source")
        if values["expected_mode"] == "fixed":
            try:
                units = parse_amount(values["expected_units"], locale)
            except ValueError:
                raise income.IncomeError("invalid_units") from None
            if units < 0:
                raise income.IncomeError("invalid_units")
        if values["expected_mode"] == "average" and not _int_in(values["average_months"], 1, 24):
            raise income.IncomeError("invalid_average")
    elif step == 3:
        if not _int_in(values["pay_day"], 1, 31):
            raise income.IncomeError("invalid_pay_day")
        if not _int_in(values["pay_lag_months"], 0, 2):
            raise income.IncomeError("invalid_source")
        start = _required_month(values, "active_from")
        end = _month(values, "active_to")
        if end is not None and end < start:
            raise income.IncomeError("invalid_range")


def _int_in(value: str, low: int, high: int) -> bool:
    try:
        return low <= int(value) <= high
    except ValueError:
        return False


def _payday_sentence(values: dict[str, str], locale: str, today: date) -> str | None:
    """ "Work done in October is paid on November 10." for the chosen start month."""
    try:
        work_month = _month(values, "active_from") or today.replace(day=1)
        paid_on = pay_date(work_month, int(values["pay_day"]), int(values["pay_lag_months"]))
    except (ValueError, income.IncomeError):
        return None
    return gettext(_("Work done in %(month)s is paid on %(date)s."), locale) % {
        "month": month_names(locale)[work_month.month - 1],
        "date": format_day_month(paid_on, locale),
    }


def _wizard_example(
    request: Request, ctx: Context, values: dict[str, str], rule_ids: list[UUID]
) -> taxes.IncomeTaxExample | None:
    """The step-4 example: the fixed amount, or a sample of 10 000 for paid-per-unit income,
    paid on the first pay date, with the ticked taxes."""
    currency = ctx.workspace.base_currency
    try:
        if values["kind"] == "fixed":
            amount = parse_amount(values["fixed_amount"], request.state.locale)
            minor = amount_minor(amount, currency)
        else:
            minor = taxes.EXAMPLE_NET_MAJOR * 10 ** minor_unit_for(currency)
        work_month = _required_month(values, "active_from")
        paid_on = pay_date(work_month, int(values["pay_day"]), int(values["pay_lag_months"]))
    except (ValueError, ArithmeticError, income.IncomeError):
        return None
    return taxes.income_example(
        request.app.state.session_factory,
        ctx,
        rule_ids,
        net=Money(minor, currency),
        paid_on=paid_on,
    )


def _selected_rules(form_rules: list[str], rules: list[taxes.TaxRuleView]) -> list[UUID]:
    active = {str(rule.id): rule.id for rule in rules if not rule.archived}
    return [active[value] for value in dict.fromkeys(form_rules) if value in active]


async def _render_wizard(
    request: Request,
    ctx: Context,
    step: int,
    raw: dict[str, str],
    form_rules: list[str],
    *,
    error: str | None = None,
) -> Response:
    categories, rules = await run_in_threadpool(
        lambda: (
            list_categories(request.app.state.session_factory, ctx),
            taxes.list_rules(request.app.state.session_factory, ctx)
            if ctx.workspace.taxes_enabled
            else [],
        )
    )
    values = _wizard_values(raw, ctx, categories)
    selected = _selected_rules(form_rules, rules)
    last_step = 4 if ctx.workspace.taxes_enabled else 3
    example = (
        await run_in_threadpool(_wizard_example, request, ctx, values, selected)
        if step == 4
        else None
    )
    labels = [_("Basics"), _("Amount"), _("Payday"), _("Taxes")][:last_step]
    return render_template(
        request,
        "income/new.html",
        context={
            "step": step,
            "last_step": last_step,
            "steps": [gettext(label, request.state.locale) for label in labels],
            "values": values,
            "hidden_fields": [name for name in WIZARD_FIELDS if name not in STEP_FIELDS[step]],
            "income_categories": categories.income,
            "tax_rules": [rule for rule in rules if not rule.archived],
            "selected_rules": selected,
            "example": example,
            "payday_sentence": _payday_sentence(
                values, request.state.locale, ctx.clock.today(ctx.workspace.timezone)
            ),
            "active_from": _month_or_none(values, "active_from"),
            "active_to": _month_or_none(values, "active_to"),
            "error": _error_text(request, error),
            **_common(request, ctx),
        },
        status_code=400 if error else 200,
    )


def _month_or_none(values: dict[str, str], name: str) -> date | None:
    try:
        return _month(values, name)
    except income.IncomeError:
        return None


@router.get("/income/new", name="income_new")
async def new_income_page(request: Request, ctx: CurrentContext) -> Response:
    require_role(ctx, Role.EDITOR)
    return await _render_wizard(request, ctx, 1, {}, [])


@router.post("/income/new", name="income_new_step")
async def new_income_step(request: Request, ctx: CurrentContext) -> Response:
    """One step of "Add income": ``go=back`` returns without checks, ``go=next`` checks this
    step's fields and opens the next step, or saves the source after the last step."""
    require_role(ctx, Role.EDITOR)
    form = await request.form()
    raw = {key: value for key, value in form.items() if isinstance(value, str)}
    form_rules = [str(value) for value in form.getlist("tax_rule_id")]
    last_step = 4 if ctx.workspace.taxes_enabled else 3
    step = _int_or(raw.get("step", "1"), 1)
    step = min(max(step, 1), last_step)
    if raw.get("go") == "back":
        return await _render_wizard(request, ctx, max(step - 1, 1), raw, form_rules)
    categories = await run_in_threadpool(list_categories, request.app.state.session_factory, ctx)
    values = _wizard_values(raw, ctx, categories)
    # Every step up to this one is checked again: hidden values can be edited too.
    for checked in range(1, step + 1):
        try:
            _check_step(
                checked, values, request.state.locale, ctx.workspace.base_currency, categories
            )
        except income.IncomeError as exc:
            return await _render_wizard(request, ctx, checked, raw, form_rules, error=exc.code)
    if step < last_step:
        return await _render_wizard(request, ctx, step + 1, raw, form_rules)
    rules = (
        await run_in_threadpool(taxes.list_rules, request.app.state.session_factory, ctx)
        if ctx.workspace.taxes_enabled
        else []
    )
    try:
        fields = _source_input(
            values | {"unit_label": _unit_label(values)},
            request.state.locale,
            ctx.workspace.base_currency,
        )
        selected = tuple(_selected_rules(form_rules, rules))
        await run_in_threadpool(
            lambda: income.create_source(
                request.app.state.session_factory, ctx, **fields, tax_rule_ids=selected
            )
        )
        await run_in_threadpool(
            income.generate_workspace,
            request.app.state.session_factory,
            ctx,
            ctx.clock,
            request.app.state.settings,
        )
    except income.IncomeError as exc:
        return await _render_wizard(request, ctx, step, raw, form_rules, error=exc.code)
    except (ValueError, ArithmeticError):
        return await _render_wizard(request, ctx, step, raw, form_rules, error="invalid_source")
    return _redirect(request)


@router.post("/income/new/example", name="income_new_example")
async def new_income_example(request: Request, ctx: CurrentContext) -> Response:
    """The example on the last step of "Add income" (htmx swaps it when a tax is ticked)."""
    if not ctx.workspace.taxes_enabled:
        raise NotFound
    form = await request.form()
    raw = {key: value for key, value in form.items() if isinstance(value, str)}
    categories, rules = await run_in_threadpool(
        lambda: (
            list_categories(request.app.state.session_factory, ctx),
            taxes.list_rules(request.app.state.session_factory, ctx),
        )
    )
    values = _wizard_values(raw, ctx, categories)
    selected = _selected_rules([str(value) for value in form.getlist("tax_rule_id")], rules)
    example = await run_in_threadpool(_wizard_example, request, ctx, values, selected)
    return render_template(
        request, "income/_example.html", context={"example": example, **_common(request, ctx)}
    )


@router.post("/income/payday-sentence", name="income_payday_sentence")
async def payday_sentence(request: Request, ctx: CurrentContext) -> Response:
    """The live payday sentence of the income forms (htmx swaps it on change)."""
    values = await _form(request)
    sentence = _payday_sentence(
        values, request.state.locale, ctx.clock.today(ctx.workspace.timezone)
    )
    return HTMLResponse(escape(sentence or ""))


def _int_or(value: str, default: int) -> int:
    try:
        return int(value)
    except ValueError:
        return default


def _values_from_source(source: income.IncomeSourceView, locale: str) -> dict[str, str]:
    """The edit form's field values, in the same shape as the "Add income" steps."""
    known_unit = source.unit_label in ("day", "hour")
    return {
        "name": source.name,
        "category_id": str(source.category_id),
        "kind": source.kind.value,
        "fixed_amount": format_amount_input(Money(source.fixed_minor, source.currency), locale)
        if source.fixed_minor is not None
        else "",
        "rate_amount": format_amount_input(Money(source.rate_minor, source.currency), locale)
        if source.rate_minor is not None
        else "",
        "unit_choice": source.unit_label if known_unit else "other",
        "unit_other": "" if known_unit else source.unit_label,
        "expected_mode": source.expected_mode.value,
        "expected_units": format_quantity_input(source.expected_units, locale)
        if source.expected_units is not None
        else "",
        "average_months": str(source.average_months),
        "pay_day": str(source.pay_day),
        "pay_lag_months": str(source.pay_lag_months),
    }


@router.get("/income/{source_id}/edit", name="income_source_edit")
async def edit_page(request: Request, ctx: CurrentContext, source_id: str) -> Response:
    require_role(ctx, Role.EDITOR)
    wanted = _id(source_id)
    rows, categories, _rules, _change = await run_in_threadpool(_load_page_data, request, ctx)
    source = next((row for row in rows if row.id == wanted and not row.archived), None)
    if source is None:
        raise NotFound
    return render_template(
        request,
        "income/edit.html",
        context={
            "source": source,
            "values": _values_from_source(source, request.state.locale),
            "income_categories": categories.income
            + tuple(c for c in categories.archived if c.id == source.category_id),
            "error": _error_text(request, request.query_params.get("error")),
            "payday_sentence": _payday_sentence(
                {
                    "pay_day": str(source.pay_day),
                    "pay_lag_months": str(source.pay_lag_months),
                    "active_from_month": str(source.active_from.month),
                    "active_from_year": str(source.active_from.year),
                },
                request.state.locale,
                ctx.clock.today(ctx.workspace.timezone),
            ),
            **_common(request, ctx),
        },
    )


@router.post("/income/{source_id}", name="income_source_update")
async def update_source(request: Request, ctx: CurrentContext, source_id: str) -> Response:
    values = await _form(request)
    try:
        fields = _source_input(values, request.state.locale, ctx.workspace.base_currency)
        await run_in_threadpool(
            lambda: income.update_source(
                request.app.state.session_factory, ctx, _id(source_id), **fields
            )
        )
        # New pay dates or a longer active range can bring new work months into the horizon.
        await run_in_threadpool(
            income.generate_workspace,
            request.app.state.session_factory,
            ctx,
            ctx.clock,
            request.app.state.settings,
        )
    except income.IncomeError as exc:
        return _edit_redirect(request, source_id, exc.code)
    except (ValueError, ArithmeticError):
        return _edit_redirect(request, source_id, "invalid_source")
    return _redirect(request)


def _edit_redirect(request: Request, source_id: str, error: str) -> Response:
    target = request.url_for("income_source_edit", source_id=source_id).path
    return RedirectResponse(f"{target}?{urlencode({'error': error})}", status_code=303)


@router.post("/income/{source_id}/archive", name="income_source_archive")
async def archive_source(request: Request, ctx: CurrentContext, source_id: str) -> Response:
    try:
        await run_in_threadpool(
            income.archive_source,
            request.app.state.session_factory,
            ctx,
            _id(source_id),
        )
    except income.IncomeError as exc:
        return _redirect(request, exc.code)
    return _redirect(request)


@router.post("/income/{source_id}/taxes", name="income_taxes_save")
async def save_taxes(request: Request, ctx: CurrentContext, source_id: str) -> Response:
    """ "Taxes on this income": the ticked taxes, each with an optional payment-day override."""
    if not ctx.workspace.taxes_enabled:
        raise NotFound
    form = await request.form()
    selection: dict[UUID, int | None] = {}
    try:
        for value in form.getlist("tax_rule_id"):
            rule_id = _id(str(value))
            day = str(form.get(f"due_day_{rule_id}", "")).strip()
            selection[rule_id] = int(day) if day else None
        await run_in_threadpool(
            income.set_source_taxes,
            request.app.state.session_factory,
            ctx,
            _id(source_id),
            selection,
        )
    except income.IncomeError as exc:
        return _redirect(request, exc.code, f"taxes-{source_id}")
    except ValueError:
        return _redirect(request, "invalid_tax_attachment", f"taxes-{source_id}")
    return _redirect(request)


@router.post("/income/{source_id}/work-logs", name="income_work_log")
async def save_work_log(request: Request, ctx: CurrentContext, source_id: str) -> Response:
    """Add or change a month's work log (the Log form, or a payment row's inline Edit)."""
    values = await _form(request)
    form = f"log-{source_id}"
    anchor = f"income-source-{source_id}"
    # "Edit anyway" (F3.4): only an explicit flag lets a received month's log change.
    override = values.get("override") == "1"
    try:
        work_month = _required_month(values, "work_month")
    except income.IncomeError as exc:
        return _redirect(request, exc.code, form, anchor=anchor)
    try:
        units = parse_amount(values.get("units", ""), request.state.locale)
        received = await run_in_threadpool(
            lambda: income.log_work(
                request.app.state.session_factory,
                ctx,
                _id(source_id),
                work_month,
                units,
                override=override,
            )
        )
    except income.WorkLogLocked as exc:
        extra = {"source": source_id, "work_month": str(YearMonth.from_date(work_month))} | (
            {
                "tx": str(exc.transaction_id),
                "tx_month": str(YearMonth.from_date(exc.transaction_date)),
            }
            if exc.transaction_id is not None and exc.transaction_date is not None
            else {}
        )
        return _redirect(request, exc.code, form, extra=extra, anchor=anchor)
    except income.WorkMonthClosed as exc:
        return _redirect(request, exc.code, form, extra={"closed": str(exc.month)}, anchor=anchor)
    except income.IncomeError as exc:
        return _redirect(request, exc.code, form, anchor=anchor)
    except ValueError:
        return _redirect(request, "invalid_units", form, anchor=anchor)
    if received:
        target = request.url_for("income_sources").path
        query = urlencode({"changed": source_id, "month": str(YearMonth.from_date(work_month))})
        return RedirectResponse(f"{target}?{query}#posted-change", status_code=303)
    return _redirect(request, anchor=anchor)


@router.post(
    "/income/{source_id}/work-logs/{month}/update-transaction", name="income_update_posted"
)
async def update_posted(
    request: Request, ctx: CurrentContext, source_id: str, month: str
) -> Response:
    """ "Update the transaction to Y" after a received month's log changed (editors, audited).

    The amount is recalculated by the service; nothing amount-like is read from the form.
    ``return_to=calendar`` goes back to the work calendar instead of the Income page.
    """
    values = await _form(request)
    source = _id(source_id)
    try:
        work_month = YearMonth.parse(month).first_day
    except ValueError:
        return _redirect(request, "invalid_work_month")
    calendar = values.get("return_to") == "calendar"
    error: str | None = None
    extra: dict[str, str] = {}
    try:
        await run_in_threadpool(
            income.update_posted_income, request.app.state.session_factory, ctx, source, work_month
        )
    except income.WorkMonthClosed as exc:
        error, extra = exc.code, {"closed": str(exc.month)}
    except MonthClosed as exc:
        error, extra = "work_month_closed", {"closed": str(exc.month)}
    except income.IncomeError as exc:
        error = exc.code
    except (InvalidTransaction, NotFound):
        error = "failed"
    if calendar:
        url = _calendar_url(request, source, work_month, error)
        if error is None:
            url += "&" + urlencode({"notice": "transaction_updated"})
        elif extra:
            url += "&" + urlencode(extra)
        return RedirectResponse(url, status_code=303)
    anchor = f"income-source-{source_id}"
    if error is not None:
        return _redirect(request, error, extra=extra, anchor=anchor)
    target = request.url_for("income_sources").path
    return RedirectResponse(
        f"{target}?{urlencode({'notice': 'transaction_updated'})}#{anchor}", status_code=303
    )


# F3.7 work calendar


def _calendar_month(value: str, ctx: Context) -> date:
    """``YYYY-MM`` from the query or form; the workspace's current month when absent."""
    if not value:
        return ctx.clock.today(ctx.workspace.timezone).replace(day=1)
    try:
        return YearMonth.parse(value).first_day
    except ValueError:
        raise income.IncomeError("invalid_work_month") from None


def _calendar_url(
    request: Request,
    source_id: UUID,
    month: date,
    error: str | None = None,
    *,
    override: bool = False,
) -> str:
    query = (
        {"month": f"{month:%Y-%m}"}
        | ({"error": error} if error else {})
        | ({"override": "1"} if override else {})
    )
    path = request.url_for("income_calendar", source_id=str(source_id)).path
    return f"{path}?{urlencode(query)}"


def _render_calendar(
    request: Request,
    ctx: Context,
    source_id: UUID,
    month: date,
    *,
    error: str | None = None,
    status_code: int = 200,
    override: bool = False,
    changed: bool = False,
    closed: YearMonth | None = None,
    notice: str | None = None,
) -> Response:
    """The whole calendar page, or only ``#work-calendar`` for an HTMX request.

    A received month is read-only until ``override`` ("Edit anyway"); after an override save
    (``changed``) the received payment and the recalculated amount are shown side by side.
    """
    locale = request.state.locale
    factory = request.app.state.session_factory
    calendar = income.work_calendar(factory, ctx, source_id, month)
    lock = calendar.lock
    overriding = override and lock.state == "received"
    change = (
        income.posted_income_change(factory, ctx, source_id, month)
        if changed and lock.state == "received"
        else None
    )
    if closed is None and lock.state == "closed":
        closed = lock.closed_month
    year_month = YearMonth.from_date(month)
    previous_month, next_month = year_month.prev(), year_month.next()
    template = (
        "income/_calendar.html" if is_htmx_fragment_request(request) else "income/calendar.html"
    )
    response = render_template(
        request,
        template,
        context={
            "calendar": calendar,
            "can_edit": has_role(ctx, Role.EDITOR)
            and calendar.editable
            and (lock.state == "open" or overriding),
            "can_override": has_role(ctx, Role.EDITOR) and calendar.editable,
            "overriding": overriding,
            "override_url": _calendar_url(request, source_id, month, override=True),
            "transaction_href": (
                _transaction_href(request, lock.transaction_id, lock.transaction_date)
                if lock.transaction_id is not None and lock.transaction_date is not None
                else None
            ),
            "closed_href": _closed_href(request, closed) if closed is not None else None,
            "notice": notice,
            **_change_context(request, change),
            "month_heading": format_month_heading(year_month, locale),
            "month_value": f"{month:%Y-%m}",
            "previous_month": str(previous_month) if previous_month else None,
            "next_month": str(next_month) if next_month else None,
            "leading_blanks": month.weekday(),
            "weekday_names": weekday_names(locale),
            "error": gettext(ERRORS.get(error, ERRORS["failed"]), locale) if error else None,
            "error_code": error,
            "format_money": format_money,
            "format_day_long": format_day_long,
            "format_quantity_input": format_quantity_input,
        },
        status_code=status_code,
    )
    response.headers["Vary"] = "HX-Request"
    return response


@router.get("/income/{source_id}/calendar", name="income_calendar")
async def calendar_page(request: Request, ctx: CurrentContext, source_id: str) -> Response:
    source = _id(source_id)
    try:
        month = _calendar_month(request.query_params.get("month", ""), ctx)
    except income.IncomeError:
        month = _calendar_month("", ctx)
    error = request.query_params.get("error")
    params = request.query_params
    try:
        closed = YearMonth.parse(params.get("closed", "")) if params.get("closed") else None
    except ValueError:
        closed = None
    notice = params.get("notice") if params.get("notice") == "transaction_updated" else None
    try:
        return await run_in_threadpool(
            lambda: _render_calendar(
                request,
                ctx,
                source,
                month,
                error=error,
                override=params.get("override") == "1",
                changed=params.get("changed") == "1",
                closed=closed,
                notice=notice,
            )
        )
    except income.IncomeError as exc:
        return _redirect(request, exc.code)


async def _override(request: Request) -> bool:
    """ "Edit anyway" (F3.7): the calendar's forms carry ``override=1`` once it was chosen."""
    return (await _form(request)).get("override") == "1"


async def _calendar_change(
    request: Request,
    ctx: Context,
    source_id: UUID,
    month: date,
    change: Callable[[], bool],
    *,
    override: bool,
) -> Response:
    error: str | None = None
    changed = False
    closed: YearMonth | None = None
    try:
        changed = await run_in_threadpool(change)
    except income.WorkMonthClosed as exc:
        error, closed = exc.code, exc.month
    except income.IncomeError as exc:
        error = exc.code
    if is_htmx_fragment_request(request):
        status = 409 if error is not None else 200
        return await run_in_threadpool(
            lambda: _render_calendar(
                request,
                ctx,
                source_id,
                month,
                error=error,
                status_code=status,
                override=override,
                changed=changed,
                closed=closed,
            )
        )
    url = _calendar_url(request, source_id, month, error, override=override)
    if changed:
        url += "&changed=1"
    if closed is not None:
        url += "&" + urlencode({"closed": str(closed)})
    return RedirectResponse(url, status_code=303)


@router.post("/income/{source_id}/calendar/days/{day}/toggle", name="income_calendar_toggle")
async def toggle_day(request: Request, ctx: CurrentContext, source_id: str, day: str) -> Response:
    source, work_date = _id(source_id), _day(day)
    factory = request.app.state.session_factory
    override = await _override(request)
    return await _calendar_change(
        request,
        ctx,
        source,
        work_date.replace(day=1),
        lambda: income.toggle_work_day(factory, ctx, source, work_date, override=override),
        override=override,
    )


@router.post("/income/{source_id}/calendar/days/{day}", name="income_calendar_day")
async def save_day(request: Request, ctx: CurrentContext, source_id: str, day: str) -> Response:
    source, work_date = _id(source_id), _day(day)
    values = await _form(request)
    factory = request.app.state.session_factory
    override = values.get("override") == "1"

    def change() -> bool:
        try:
            units = parse_amount(values.get("units", ""), request.state.locale)
        except ValueError:
            raise income.IncomeError("invalid_day_units") from None
        return income.set_work_day(factory, ctx, source, work_date, units, override=override)

    return await _calendar_change(
        request, ctx, source, work_date.replace(day=1), change, override=override
    )


@router.post("/income/{source_id}/calendar/fill", name="income_calendar_fill")
async def fill_month(request: Request, ctx: CurrentContext, source_id: str) -> Response:
    source = _id(source_id)
    values = await _form(request)
    month = _calendar_month(values.get("month", ""), ctx)
    factory = request.app.state.session_factory
    override = values.get("override") == "1"
    return await _calendar_change(
        request,
        ctx,
        source,
        month,
        lambda: income.fill_working_days(factory, ctx, source, month, override=override),
        override=override,
    )


@router.post("/income/{source_id}/calendar/clear", name="income_calendar_clear")
async def clear_month(request: Request, ctx: CurrentContext, source_id: str) -> Response:
    source = _id(source_id)
    values = await _form(request)
    month = _calendar_month(values.get("month", ""), ctx)
    factory = request.app.state.session_factory
    override = values.get("override") == "1"
    return await _calendar_change(
        request,
        ctx,
        source,
        month,
        lambda: income.clear_work_month(factory, ctx, source, month, override=override),
        override=override,
    )
