from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import TypedDict
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from assetboard.db.repositories import NotFound
from assetboard.domain.months import YearMonth
from assetboard.domain.transactions import amount_minor
from assetboard.i18n import gettext
from assetboard.i18n.dates import (
    format_day,
    format_day_long,
    format_month,
    format_month_heading,
    weekday_names,
)
from assetboard.i18n.numbers import (
    format_amount_input,
    format_money,
    format_quantity_input,
    parse_amount,
)
from assetboard.services import income, taxes
from assetboard.services.categories import list_categories
from assetboard.services.context import Context, Role, has_role
from assetboard.web.context import CurrentContext
from assetboard.web.errors import is_htmx_fragment_request
from assetboard.web.rendering import render_template
from assetboard.web.security import require_csrf

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
    record_as: str
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
    "invalid_work_month": _("Choose the first day of a work month."),
    "invalid_category": _("Choose an active income category."),
    "unsupported_holidays": _("Working-day estimates are unavailable for this workspace country."),
    "invalid_tax_attachment": _("Choose a valid tax rule and payment category."),
    "invalid_tax_category": _("Choose an expense category that is not a transfer."),
    "duplicate_tax_rule": _("This tax rule is already attached to the income source."),
    "attachment_in_use": _("A posted tax payment uses this attachment, so it cannot be removed."),
    "month_closed": _("This month is closed. Reopen it before changing the income estimate."),
    "archived": _("Archived income sources cannot be changed."),
    "derived_work_log": _(
        "This month's units come from the work calendar. Change the days in the calendar."
    ),
    "invalid_day_units": _("Enter a number of units above 0 and at most 24."),
    "posted": _(
        "The work log was saved; posted income was not changed. "
        "Adjust its transaction manually if needed."
    ),
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
        else None
    )
    return {
        "name": values.get("name", ""),
        "kind": kind,
        "category_id": _id(values.get("category_id", "")),
        "fixed_minor": fixed_minor,
        "rate_minor": rate_minor,
        "unit_label": values.get("unit_label", "day"),
        "pay_day_value": int(values.get("pay_day", "1")),
        "pay_lag_months": int(values.get("pay_lag_months", "0")),
        "expected_mode": values.get("expected_mode", "manual"),
        "expected_units_value": units,
        "average_months": int(values.get("average_months", "3")),
        "record_as": values.get("record_as", "cash_received"),
        "active_from": _day(values.get("active_from", "")),
        "active_to": _day(values["active_to"]) if values.get("active_to") else None,
    }


def _format_work_month(work_month: date, locale: str) -> str:
    return format_month(YearMonth.from_date(work_month), locale)


def _redirect(request: Request, error: str | None = None) -> Response:
    target = request.url_for("income_sources").path
    return RedirectResponse(
        target + ("?" + urlencode({"error": error}) if error else ""), status_code=303
    )


async def _form(request: Request) -> dict[str, str]:
    data = await request.form()
    return {key: value for key, value in data.items() if isinstance(value, str)}


@router.get("/income", name="income_sources")
async def sources_page(request: Request, ctx: CurrentContext) -> Response:
    rows, categories, rules = await run_in_threadpool(
        lambda: (
            income.list_sources(request.app.state.session_factory, ctx),
            list_categories(request.app.state.session_factory, ctx),
            taxes.list_rules(request.app.state.session_factory, ctx),
        )
    )
    error = request.query_params.get("error")
    return render_template(
        request,
        "income/sources.html",
        context={
            "sources": rows,
            "income_categories": categories.income,
            "expense_categories": categories.expense,
            "tax_rules": [rule for rule in rules if not rule.archived],
            "can_edit": has_role(ctx, Role.EDITOR),
            "today": ctx.clock.today(ctx.workspace.timezone),
            "error": gettext(ERRORS.get(error, ERRORS["failed"]), request.state.locale)
            if error
            else None,
            "format_money": format_money,
            "format_amount_input": format_amount_input,
            "format_quantity_input": format_quantity_input,
            "format_month": _format_work_month,
            "format_day": format_day,
            "tax_rule_names": {rule.id: rule.name for rule in rules},
        },
    )


@router.post("/income", name="income_source_create")
async def create_source(request: Request, ctx: CurrentContext) -> Response:
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
        return _redirect(request, exc.code)
    except (ValueError, ArithmeticError):
        return _redirect(request, "invalid_source")
    return _redirect(request)


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


@router.post("/income/{source_id}/tax-rules", name="income_tax_attach")
async def attach_tax_rule(request: Request, ctx: CurrentContext, source_id: str) -> Response:
    values = await _form(request)
    try:
        await run_in_threadpool(
            lambda: income.attach_tax_rule(
                request.app.state.session_factory,
                ctx,
                _id(source_id),
                tax_rule_id=_id(values.get("tax_rule_id", "")),
                payment_category_id=_id(values.get("payment_category_id", "")),
                due_day=int(values.get("due_day", "1")),
                due_month_offset=int(values.get("due_month_offset", "0")),
                generate_payment=values.get("generate_payment") == "1",
            )
        )
    except income.IncomeError as exc:
        return _redirect(request, exc.code)
    except ValueError:
        return _redirect(request, "invalid_tax_attachment")
    return _redirect(request)


@router.post("/income/{source_id}/tax-rules/{attachment_id}/remove", name="income_tax_remove")
async def remove_tax_rule(
    request: Request, ctx: CurrentContext, source_id: str, attachment_id: str
) -> Response:
    try:
        await run_in_threadpool(
            income.remove_tax_attachment,
            request.app.state.session_factory,
            ctx,
            _id(source_id),
            _id(attachment_id),
        )
    except income.IncomeError as exc:
        return _redirect(request, exc.code)
    return _redirect(request)


@router.post("/income/{source_id}/work-logs", name="income_work_log")
async def save_work_log(request: Request, ctx: CurrentContext, source_id: str) -> Response:
    values = await _form(request)
    try:
        units = parse_amount(values.get("units", ""), request.state.locale)
        posted = await run_in_threadpool(
            income.log_work,
            request.app.state.session_factory,
            ctx,
            _id(source_id),
            _day(values.get("work_month", "")),
            units,
        )
    except income.IncomeError as exc:
        return _redirect(request, exc.code)
    except ValueError:
        return _redirect(request, "invalid_units")
    if posted:
        return _redirect(request, "posted")
    return _redirect(request)


# F3.7 work calendar


def _calendar_month(value: str, ctx: Context) -> date:
    """``YYYY-MM`` from the query or form; the workspace's current month when absent."""
    if not value:
        return ctx.clock.today(ctx.workspace.timezone).replace(day=1)
    try:
        return YearMonth.parse(value).first_day
    except ValueError:
        raise income.IncomeError("invalid_work_month") from None


def _calendar_url(request: Request, source_id: UUID, month: date, error: str | None = None) -> str:
    query = {"month": f"{month:%Y-%m}"} | ({"error": error} if error else {})
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
) -> Response:
    """The whole calendar page, or only ``#work-calendar`` for an HTMX request."""
    locale = request.state.locale
    calendar = income.work_calendar(request.app.state.session_factory, ctx, source_id, month)
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
            "can_edit": has_role(ctx, Role.EDITOR) and calendar.editable,
            "month_heading": format_month_heading(year_month, locale),
            "month_value": f"{month:%Y-%m}",
            "previous_month": str(previous_month) if previous_month else None,
            "next_month": str(next_month) if next_month else None,
            "leading_blanks": month.weekday(),
            "weekday_names": weekday_names(locale),
            "error": gettext(ERRORS.get(error, ERRORS["failed"]), locale) if error else None,
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
    try:
        return await run_in_threadpool(
            lambda: _render_calendar(request, ctx, source, month, error=error)
        )
    except income.IncomeError as exc:
        return _redirect(request, exc.code)


async def _calendar_change(
    request: Request, ctx: Context, source_id: UUID, month: date, change: Callable[[], bool]
) -> Response:
    error: str | None = None
    try:
        if await run_in_threadpool(change):
            error = "posted"
    except income.IncomeError as exc:
        error = exc.code
    if is_htmx_fragment_request(request):
        status = 409 if error not in (None, "posted") else 200
        return await run_in_threadpool(
            lambda: _render_calendar(
                request, ctx, source_id, month, error=error, status_code=status
            )
        )
    return RedirectResponse(_calendar_url(request, source_id, month, error), status_code=303)


@router.post("/income/{source_id}/calendar/days/{day}/toggle", name="income_calendar_toggle")
async def toggle_day(request: Request, ctx: CurrentContext, source_id: str, day: str) -> Response:
    source, work_date = _id(source_id), _day(day)
    factory = request.app.state.session_factory
    return await _calendar_change(
        request,
        ctx,
        source,
        work_date.replace(day=1),
        lambda: income.toggle_work_day(factory, ctx, source, work_date),
    )


@router.post("/income/{source_id}/calendar/days/{day}", name="income_calendar_day")
async def save_day(request: Request, ctx: CurrentContext, source_id: str, day: str) -> Response:
    source, work_date = _id(source_id), _day(day)
    values = await _form(request)
    factory = request.app.state.session_factory

    def change() -> bool:
        try:
            units = parse_amount(values.get("units", ""), request.state.locale)
        except ValueError:
            raise income.IncomeError("invalid_day_units") from None
        return income.set_work_day(factory, ctx, source, work_date, units)

    return await _calendar_change(request, ctx, source, work_date.replace(day=1), change)


@router.post("/income/{source_id}/calendar/fill", name="income_calendar_fill")
async def fill_month(request: Request, ctx: CurrentContext, source_id: str) -> Response:
    source = _id(source_id)
    month = _calendar_month((await _form(request)).get("month", ""), ctx)
    factory = request.app.state.session_factory
    return await _calendar_change(
        request, ctx, source, month, lambda: income.fill_working_days(factory, ctx, source, month)
    )


@router.post("/income/{source_id}/calendar/clear", name="income_calendar_clear")
async def clear_month(request: Request, ctx: CurrentContext, source_id: str) -> Response:
    source = _id(source_id)
    month = _calendar_month((await _form(request)).get("month", ""), ctx)
    factory = request.app.state.session_factory
    return await _calendar_change(
        request, ctx, source, month, lambda: income.clear_work_month(factory, ctx, source, month)
    )
