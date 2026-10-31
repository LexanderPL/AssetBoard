from __future__ import annotations

import contextlib
from datetime import date
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from assetboard.db.repositories import NotFound
from assetboard.domain.months import InvalidMonth, YearMonth
from assetboard.domain.transactions import amount_minor
from assetboard.i18n import gettext
from assetboard.i18n.dates import format_day
from assetboard.i18n.numbers import format_amount_input, parse_amount
from assetboard.services import occurrences, scheduled
from assetboard.services.categories import list_categories
from assetboard.services.context import Role, has_role
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
    if notice and return_month is not None:
        params["notice"] = notice
    target = (
        request.url_for("transactions_month", month=str(return_month)).path
        if return_month is not None
        else request.url_for("scheduled_upcoming").path
    )
    return RedirectResponse(target + ("?" + urlencode(params) if params else ""), status_code=303)


@router.get("/upcoming", name="scheduled_upcoming")
async def page(request: Request, ctx: CurrentContext) -> Response:
    direction = request.query_params.get("direction") or None
    if direction not in (None, "expense", "income"):
        raise HTTPException(
            status_code=400,
            detail=gettext(INVALID_DIRECTION, request.state.locale),
        )
    category_text = request.query_params.get("category") or None
    category_id = _id(category_text) if category_text else None
    days = occurrences.window_days(ctx, request.app.state.settings.upcoming_days)
    weeks = await run_in_threadpool(
        lambda: occurrences.upcoming(
            request.app.state.session_factory,
            ctx,
            days=days,
            direction=direction,
            category_id=category_id,
        )
    )
    categories = await run_in_threadpool(list_categories, request.app.state.session_factory, ctx)
    posted_text = request.query_params.get("posted")
    undo = (
        await run_in_threadpool(
            occurrences.recently_posted,
            request.app.state.session_factory,
            ctx,
            _id(posted_text),
        )
        if posted_text
        else None
    )
    error = request.query_params.get("error")
    return render_template(
        request,
        "scheduled/upcoming.html",
        context={
            "weeks": weeks,
            "today": ctx.clock.today(ctx.workspace.timezone),
            "days": days,
            "direction": direction or "",
            "category_id": str(category_id) if category_id else "",
            "categories": categories.expense + categories.income + categories.archived,
            "can_edit": has_role(ctx, Role.EDITOR),
            "undo": undo,
            "amount_input": lambda amount: format_amount_input(amount, request.state.locale),
            "format_day": format_day,
            "error": gettext(
                ACTION_ERRORS.get(error, ACTION_ERRORS["invalid_input"]), request.state.locale
            )
            if error
            else None,
        },
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


@router.post("/upcoming/{occurrence_id}/skip", name="upcoming_skip")
async def skip(request: Request, ctx: CurrentContext, occurrence_id: str) -> Response:
    values = await _values(request)
    try:
        await run_in_threadpool(
            occurrences.skip_occurrence,
            request.app.state.session_factory,
            ctx,
            _id(occurrence_id),
        )
    except occurrences.OccurrenceError as exc:
        return _redirect(request, error=exc.code)
    return _redirect(request, back=values.get("back", ""))


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
