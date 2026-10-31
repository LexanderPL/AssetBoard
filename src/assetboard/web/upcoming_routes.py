from __future__ import annotations

import contextlib
from datetime import date, timedelta
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from assetboard.db.repositories import NotFound
from assetboard.domain.money import Money
from assetboard.domain.months import MAX_YEAR, MIN_YEAR, InvalidMonth, YearMonth
from assetboard.domain.transactions import TransactionDirection, amount_minor
from assetboard.i18n import gettext
from assetboard.i18n.dates import format_day
from assetboard.i18n.numbers import format_amount_input, parse_amount
from assetboard.services import occurrences, scheduled
from assetboard.services.categories import list_categories
from assetboard.services.context import Context, Role, has_role, require_role
from assetboard.services.months import MonthClosed
from assetboard.web.context import CurrentContext
from assetboard.web.rendering import render_template
from assetboard.web.security import require_csrf

router = APIRouter(dependencies=[Depends(require_csrf)])


def _(message: str) -> str:
    return message


ACTION_ERRORS = {
    "invalid_amount": _("Enter a positive amount in the workspace currency."),
    "invalid_date": _("Enter a valid date between 1900 and 2199."),
    "month_closed": _("This month is closed. Reopen it or choose a date in an open month."),
    "not_pending": _("This occurrence is no longer pending."),
    "not_posted": _("This occurrence has no transaction to undo."),
    "undo_expired": _("The 10-second undo window has expired."),
    "category_unavailable": _("The occurrence category is unavailable; edit the recurring rule."),
    "invalid_input": _("Check the amount and date, then try again."),
    "not_planned": _("Only a one-time planned entry can be deleted here."),
    "not_skipped": _("This occurrence is not skipped."),
    "archived_item": _("This recurring item is archived, so its skipped date cannot come back."),
}
INVALID_DIRECTION = _("Choose a valid direction.")


def _id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise NotFound from None


async def _values(request: Request) -> dict[str, str]:
    form = await request.form()
    return {key: value for key, value in form.items() if isinstance(value, str)}


def _redirect(
    request: Request,
    *,
    error: str | None = None,
    posted: UUID | None = None,
    notice: str | None = None,
    back: str = "",
    return_month: YearMonth | None = None,
) -> Response:
    """Back to the Upcoming page, or to Home after a successful action sent from there (F2.7:
    ``back=home``; any other value means the Upcoming page). A confirm sent from an expected row
    of the Transactions page (F3.5: ``return_month``) returns to that month's Transactions page.
    Errors are shown on Upcoming."""
    if back == "home" and error is None:
        return RedirectResponse(request.url_for("home").path, status_code=303)
    params: dict[str, str] = {}
    if error:
        params["error"] = error
    if posted:
        params["posted"] = str(posted)
    if notice:
        params["notice"] = notice
    target = (
        request.url_for("transactions_month", month=str(return_month)).path
        if return_month is not None
        else request.url_for("scheduled_upcoming").path
    )
    return RedirectResponse(target + ("?" + urlencode(params) if params else ""), status_code=303)


ONE_TIME_ERRORS = {
    "name": _("Enter a description."),
    "amount": _("Enter a positive amount in the workspace currency."),
    "day": _("Enter a valid date between 1900 and 2199."),
    "category": _("Choose an active category."),
    "category_kind_mismatch": _(
        "Choose an expense category for an expense, or an income category for income."
    ),
    "month_closed": _("This month is closed. Reopen it or choose a date in an open month."),
    "not_planned": _("Only a one-time payment can be edited here."),
    "not_pending": _("This occurrence is no longer pending."),
}
ONE_TIME_ADDED = _("One-time payment added for %(date)s.")
ONE_TIME_LATER = _("It shows here when it is due within the next %(days)d days.")
NOTICES = {
    "one_time_saved": _("One-time payment saved."),
    "planned_deleted": _("Planned entry deleted."),
}


def _page_context(request: Request, ctx: Context) -> dict[str, object]:
    """The Upcoming page (blocking: run in a thread)."""
    factory = request.app.state.session_factory
    locale = request.state.locale
    direction = request.query_params.get("direction") or None
    if direction not in (None, "expense", "income"):
        raise HTTPException(status_code=400, detail=gettext(INVALID_DIRECTION, locale))
    category_text = request.query_params.get("category") or None
    category_id = _id(category_text) if category_text else None
    days = occurrences.window_days(ctx, request.app.state.settings.upcoming_days)
    weeks = occurrences.upcoming(
        factory, ctx, days=days, direction=direction, category_id=category_id
    )
    categories = list_categories(factory, ctx)
    posted_text = request.query_params.get("posted")
    undo = occurrences.recently_posted(factory, ctx, _id(posted_text)) if posted_text else None
    error = request.query_params.get("error")
    today = ctx.clock.today(ctx.workspace.timezone)
    notice = None
    added_text = request.query_params.get("added", "")
    with contextlib.suppress(ValueError):
        added = date.fromisoformat(added_text)
        notice = gettext(ONE_TIME_ADDED, locale) % {"date": format_day(added, locale)}
        if added > today + timedelta(days=days):
            notice += " " + gettext(ONE_TIME_LATER, locale) % {"days": days}
    notice_code = request.query_params.get("notice", "")
    if notice_code in NOTICES:
        notice = gettext(NOTICES[notice_code], locale)
    return {
        "weeks": weeks,
        "today": today,
        "days": days,
        "direction": direction or "",
        "category_id": str(category_id) if category_id else "",
        "categories": categories.expense + categories.income + categories.archived,
        "expense_categories": categories.expense,
        "income_categories": categories.income,
        "can_edit": has_role(ctx, Role.EDITOR),
        "undo": undo,
        "notice": notice,
        "open_form": request.query_params.get("form"),
        "one_time_values": {"direction": "expense", "day": today.isoformat()},
        "one_time_errors": {},
        "edit_errors": {},
        "edit_values": {},
        "editing": None,
        "amount_input": lambda amount: format_amount_input(amount, locale),
        "format_day": format_day,
        "error": gettext(ACTION_ERRORS.get(error, ACTION_ERRORS["invalid_input"]), locale)
        if error
        else None,
    }


@router.get("/upcoming", name="scheduled_upcoming")
async def page(request: Request, ctx: CurrentContext) -> Response:
    context = await run_in_threadpool(_page_context, request, ctx)
    return render_template(request, "scheduled/upcoming.html", context=context)


def _one_time_input(
    values: dict[str, str], ctx: Context, locale: str
) -> scheduled.OneTimeInput | str:
    """The one-time payment form, or the code of its first wrong field."""
    if not values.get("name", "").strip():
        return "name"
    try:
        minor = amount_minor(
            parse_amount(values.get("amount", ""), locale), ctx.workspace.base_currency
        )
    except ValueError:
        return "amount"
    if minor <= 0:
        return "amount"
    try:
        day = date.fromisoformat(values.get("day", ""))
    except ValueError:
        return "day"
    if not MIN_YEAR <= day.year <= MAX_YEAR:
        return "day"
    try:
        category_id = UUID(values.get("category_id", ""))
        direction = TransactionDirection(values.get("direction", "expense"))
    except ValueError:
        return "category"
    return scheduled.OneTimeInput(
        name=values.get("name", ""),
        direction=direction,
        category_id=category_id,
        amount=Money(minor, ctx.workspace.base_currency),
        day=day,
    )


def _one_time_code(exc: Exception) -> str:
    if isinstance(exc, MonthClosed):
        return "month_closed"
    if isinstance(exc, NotFound):
        return "category"
    code = str(exc)
    if code == "invalid_item":
        return "amount"
    if code in ("not_planned", "not_pending", "category_kind_mismatch"):
        return code
    return "category"


async def _render_with_errors(
    request: Request, ctx: Context, overrides: dict[str, object]
) -> Response:
    context = await run_in_threadpool(_page_context, request, ctx)
    return render_template(
        request, "scheduled/upcoming.html", context=context | overrides, status_code=400
    )


def _error_text(request: Request, code: str) -> dict[str, str]:
    """The form's message and the field it marks (``field``: the error code)."""
    message = ONE_TIME_ERRORS.get(code, ONE_TIME_ERRORS["category"])
    return {"form": gettext(message, request.state.locale), "field": code}


@router.post("/upcoming/one-time", name="upcoming_one_time_create")
async def create_one_time(request: Request, ctx: CurrentContext) -> Response:
    """The Upcoming page's "Add one-time payment" (a one-time scheduled item)."""
    require_role(ctx, Role.EDITOR)
    values = await _values(request)
    data = _one_time_input(values, ctx, request.state.locale)
    if isinstance(data, scheduled.OneTimeInput):
        try:
            await run_in_threadpool(
                scheduled.create_one_time,
                request.app.state.session_factory,
                ctx,
                data,
                request.app.state.settings,
            )
        except (ValueError, MonthClosed, NotFound) as exc:
            data = _one_time_code(exc)
        else:
            target = request.url_for("scheduled_upcoming").path
            return RedirectResponse(
                f"{target}?{urlencode({'added': data.day.isoformat()})}", status_code=303
            )
    return await _render_with_errors(
        request,
        ctx,
        {
            "open_form": "one-time",
            "one_time_values": values,
            "one_time_errors": _error_text(request, data),
        },
    )


@router.post("/upcoming/{occurrence_id}/one-time", name="upcoming_one_time_update")
async def update_one_time(request: Request, ctx: CurrentContext, occurrence_id: str) -> Response:
    """Edit a one-time payment from its Upcoming row (it is not on the Recurring page)."""
    require_role(ctx, Role.EDITOR)
    values = await _values(request)
    target = _id(occurrence_id)
    data = _one_time_input(values, ctx, request.state.locale)
    if isinstance(data, scheduled.OneTimeInput):
        try:
            await run_in_threadpool(
                scheduled.update_one_time,
                request.app.state.session_factory,
                ctx,
                target,
                data,
            )
        except (ValueError, MonthClosed) as exc:
            data = _one_time_code(exc)
        else:
            path = request.url_for("scheduled_upcoming").path
            return RedirectResponse(f"{path}?notice=one_time_saved", status_code=303)
    return await _render_with_errors(
        request,
        ctx,
        {"editing": target, "edit_values": values, "edit_errors": _error_text(request, data)},
    )


@router.post("/upcoming/{occurrence_id}/post", name="upcoming_post")
async def post(request: Request, ctx: CurrentContext, occurrence_id: str) -> Response:
    values = await _values(request)
    try:
        amount = amount_minor(
            parse_amount(values.get("amount", ""), request.state.locale),
            ctx.workspace.base_currency,
        )
    except ValueError:
        return _redirect(request, error="invalid_amount")
    try:
        day = date.fromisoformat(values.get("day", ""))
    except ValueError:
        return _redirect(request, error="invalid_date")
    try:
        await run_in_threadpool(
            lambda: occurrences.post_occurrence(
                request.app.state.session_factory,
                ctx,
                _id(occurrence_id),
                amount_minor=amount,
                day=day,
            )
        )
    except occurrences.OccurrenceError as exc:
        return _redirect(request, error=exc.code)
    return_month = None
    with contextlib.suppress(InvalidMonth):
        return_month = YearMonth.parse(values.get("return_month", ""))
    return _redirect(
        request, posted=_id(occurrence_id), back=values.get("back", ""), return_month=return_month
    )


def _return_month(values: dict[str, str]) -> YearMonth | None:
    with contextlib.suppress(InvalidMonth):
        return YearMonth.parse(values.get("return_month", ""))
    return None


@router.post("/upcoming/{occurrence_id}/skip", name="upcoming_skip")
async def skip(request: Request, ctx: CurrentContext, occurrence_id: str) -> Response:
    """Skip one date; from a ledger's expected row (``return_month``) back to that month."""
    values = await _values(request)
    return_month = _return_month(values)
    try:
        await run_in_threadpool(
            occurrences.skip_occurrence,
            request.app.state.session_factory,
            ctx,
            _id(occurrence_id),
        )
    except occurrences.OccurrenceError as exc:
        return _redirect(request, error=exc.code, return_month=return_month)
    return _redirect(
        request, notice="skipped", back=values.get("back", ""), return_month=return_month
    )


@router.post("/upcoming/{occurrence_id}/unskip", name="upcoming_unskip")
async def unskip(request: Request, ctx: CurrentContext, occurrence_id: str) -> Response:
    """Undo a skip (the ledger's greyed-out "Skipped" row): the date is pending again."""
    values = await _values(request)
    return_month = _return_month(values)
    try:
        await run_in_threadpool(
            occurrences.unskip_occurrence,
            request.app.state.session_factory,
            ctx,
            _id(occurrence_id),
        )
    except occurrences.OccurrenceError as exc:
        return _redirect(request, error=exc.code, return_month=return_month)
    return _redirect(request, notice="skip_undone", return_month=return_month)


@router.post("/upcoming/{occurrence_id}/delete", name="upcoming_delete")
async def delete(request: Request, ctx: CurrentContext, occurrence_id: str) -> Response:
    """Delete a one-time planned entry from its expected row (the Plan button's item)."""
    values = await _values(request)
    return_month = None
    with contextlib.suppress(InvalidMonth):
        return_month = YearMonth.parse(values.get("return_month", ""))
    try:
        await run_in_threadpool(
            scheduled.delete_planned_occurrence,
            request.app.state.session_factory,
            ctx,
            _id(occurrence_id),
        )
    except ValueError as exc:
        code = str(exc)
        if code not in ACTION_ERRORS:
            code = "invalid_input"
        return _redirect(request, error=code, return_month=return_month)
    return _redirect(request, notice="planned_deleted", return_month=return_month)


@router.post("/upcoming/{occurrence_id}/reschedule", name="upcoming_reschedule")
async def reschedule(request: Request, ctx: CurrentContext, occurrence_id: str) -> Response:
    values = await _values(request)
    try:
        day = date.fromisoformat(values.get("day", ""))
        await run_in_threadpool(
            occurrences.reschedule_occurrence,
            request.app.state.session_factory,
            ctx,
            _id(occurrence_id),
            day,
        )
    except occurrences.OccurrenceError as exc:
        return _redirect(request, error=exc.code)
    except ValueError:
        return _redirect(request, error="invalid_date")
    return _redirect(request)


@router.post("/upcoming/{occurrence_id}/amount", name="upcoming_amount")
async def edit_amount(request: Request, ctx: CurrentContext, occurrence_id: str) -> Response:
    values = await _values(request)
    try:
        amount = amount_minor(
            parse_amount(values.get("amount", ""), request.state.locale),
            ctx.workspace.base_currency,
        )
        await run_in_threadpool(
            occurrences.edit_occurrence_amount,
            request.app.state.session_factory,
            ctx,
            _id(occurrence_id),
            amount,
        )
    except occurrences.OccurrenceError as exc:
        return _redirect(request, error=exc.code)
    except ValueError:
        return _redirect(request, error="invalid_amount")
    return _redirect(request)


@router.post("/upcoming/{occurrence_id}/undo", name="upcoming_undo")
async def undo(request: Request, ctx: CurrentContext, occurrence_id: str) -> Response:
    try:
        await run_in_threadpool(
            occurrences.undo_post,
            request.app.state.session_factory,
            ctx,
            _id(occurrence_id),
        )
    except occurrences.OccurrenceError as exc:
        return _redirect(request, error=exc.code)
    return _redirect(request)
