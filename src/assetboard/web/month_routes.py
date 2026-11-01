"""Month pages (F2.5): ``/m/YYYY-MM`` with navigation, the month picker, closing and reopening.

Since F2.7 the Month page is a summary: F2.3's planned values, F2.4's summary with the flow bar,
and the expense and income donuts (``web.charts``). The transaction tables moved to the
Transactions page (``web.transactions_page_routes``), which shares ``month_header`` and the
navigation partial.
F2.3 fills the summary block with the planned values (``web.plan``; the writes are
in ``web.plan_routes``) and runs the automatic plan copy when an editor opens a month page
(``services.budget.auto_copy_on_open``: a GET that writes at most once per month, by design).
Authorization happens in ``services.months``; plain form posts redirect (303)
back to the month, so the page works without JavaScript. Every state-changing route is covered
by the router-level ``require_csrf`` dependency.
"""

from __future__ import annotations

import re
from typing import Annotated, cast
from urllib.parse import urlencode
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from assetboard.db.repositories import NotFound
from assetboard.domain.months import InvalidMonth, YearMonth
from assetboard.i18n import gettext
from assetboard.i18n.dates import format_day, format_month, format_month_heading, month_names
from assetboard.services.budget import CopyResult, auto_copy_on_open
from assetboard.services.context import Context
from assetboard.services.income import missing_work_logs
from assetboard.services.months import (
    MonthError,
    MonthView,
    close_month,
    current_month,
    get_month,
    reopen_month,
)
from assetboard.web.charts import build_donut
from assetboard.web.context import CurrentContext
from assetboard.web.ledger import parse_filter
from assetboard.web.plan import PLAN_ERRORS, CellState, build_summary, summary_context
from assetboard.web.rendering import render_template, scoped_url
from assetboard.web.security import require_csrf

router = APIRouter(dependencies=[Depends(require_csrf)])


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


# Shown wherever a write is refused because its month is closed (F2.2/F2.3 reuse it; the
# application-wide ``MonthClosed`` handler shows it too).
MONTH_CLOSED_TITLE = _("Month closed")
MONTH_CLOSED_MESSAGE = _(
    "This month is closed. Its entries and planned values cannot change until an owner reopens it."
)
ERRORS = {
    "invalid_month": _("Enter a month as YYYY-MM, for example 2026-10."),
    "month_in_future": _("Future months cannot be closed; they stay open for planning."),
    "month_closed": MONTH_CLOSED_MESSAGE,
    "transaction_gone": _("This entry no longer exists. It may have been deleted elsewhere."),
    "undo_expired": _("Too late to undo: the entry stays deleted."),
    "not_pending": _("This occurrence is no longer pending."),
    "not_planned": _("Only a one-time planned entry can be deleted here."),
    "not_skipped": _("This occurrence is not skipped."),
    "archived_item": _("This recurring item is archived, so its skipped date cannot come back."),
    "plan_category_gone": _("This category no longer exists. It may have been deleted elsewhere."),
    "plan_category_archived": PLAN_ERRORS["category_archived"],
    "plan_category_transfer": PLAN_ERRORS["category_transfer"],
}
NOTICES = {
    "closed": _("Month closed."),
    "reopened": _("Month reopened."),
    "already_closed": _("This month was already closed."),
    "already_open": _("This month is already open."),
    "transaction_added": _("Entry added."),
    "transaction_saved": _("Entry saved."),
    "transaction_restored": _("Entry restored."),
    "outside_filter": _("Entry saved. Its category is not shown while this filter is on."),
    "planned": _("Entry planned."),
    "outside_filter_planned": _(
        "Entry planned. Its category is not shown while this filter is on."
    ),
    "planned_deleted": _("Planned entry deleted."),
    "skipped": _("Skipped. It no longer counts as pending."),
    "skip_undone": _("Skip undone. It is pending again."),
    "plan_saved": _("Planned value saved."),
    "plan_removed": _("Planned value removed."),
}
SAVED_IN = _("Entry saved in %(month)s.")
PLANNED_IN = _("Entry planned in %(month)s.")


def _t(request: Request, message: str) -> str:
    return gettext(message, request.state.locale)


def _parse(value: str) -> YearMonth:
    """The URL month; anything else (bad format, out of range) is a plain 404."""
    try:
        return YearMonth.parse(value)
    except InvalidMonth:
        raise NotFound from None


def _month_path(request: Request, month: YearMonth, query: str = "") -> str:
    path = request.url_for("month", month=str(month)).path
    return f"{path}?{query}" if query else path


@router.get("/m", name="month_current")
async def month_current(
    request: Request,
    ctx: CurrentContext,
    month: str | None = None,
    from_: Annotated[str, Query(alias="from")] = "",
) -> Response:
    """Today's month, or the month chosen in the picker (``?month=...&from=YYYY-MM``)."""
    if month is None:
        return RedirectResponse(_month_path(request, current_month(ctx)), status_code=303)
    try:
        target = YearMonth.parse_loose(month)
    except InvalidMonth:
        try:
            back = YearMonth.parse(from_)
        except InvalidMonth:
            back = current_month(ctx)
        return RedirectResponse(_month_path(request, back, "error=invalid_month"), status_code=303)
    return RedirectResponse(_month_path(request, target), status_code=303)


def month_header(
    request: Request, ctx: Context, target: YearMonth, *, notice: str = "", error: str = ""
) -> dict[str, object]:
    """What the Month and Transactions pages share (blocking: run in a thread): the month,
    its headings, navigation names, closed state, the notice or error of the last action and
    the F3.5 missing-work-log banner."""
    view = get_month(request.app.state.session_factory, ctx, target)
    locale = request.state.locale
    raw_year = request.query_params.get("picker_year", "")
    picker_open = (
        len(raw_year) == 4
        and raw_year.isascii()
        and raw_year.isdigit()
        and 1900 <= int(raw_year) <= 2199
    )
    picker_year = int(raw_year) if picker_open else view.month.year
    closed_on = None
    if view.closed_at is not None:
        local = view.closed_at.astimezone(ZoneInfo(ctx.workspace.timezone)).date()
        closed_on = format_day(local, locale)
    return {
        "view": view,
        "month_heading": format_month_heading(view.month, locale),  # heading and title
        "month_name": format_month(view.month, locale),  # in sentences (confirmations)
        "prev_name": format_month(view.prev, locale) if view.prev else None,
        "next_name": format_month(view.next, locale) if view.next else None,
        "picker_open": picker_open,
        "picker_year": picker_year,
        "month_names": month_names(locale),
        "closed_on": closed_on,
        "notice": _t(request, NOTICES[notice]) if notice in NOTICES else None,
        "notice_href": None,
        "error": _t(request, ERRORS[error]) if error in ERRORS else None,
        "missing_work_logs": [
            (source_id, name, format_month(YearMonth.from_date(work_month), locale))
            for source_id, name, work_month in missing_work_logs(
                request.app.state.session_factory, ctx, view.month.first_day
            )
        ],
    }


def month_context(
    request: Request,
    ctx: Context,
    target: YearMonth,
    *,
    notice: str = "",
    error: str = "",
    plan_cell: CellState | None = None,
    copied: CopyResult | None = None,
    automatic: bool = False,
) -> dict[str, object]:
    """The Month page's context (blocking: run in a thread): the summary with its flow bar
    and planned values, and the expense and income donuts (F2.7: no entry form, no transaction
    list).
    ``plan_cell`` is a refused planned value and ``copied`` a plan copy to report."""
    context = month_header(request, ctx, target, notice=notice, error=error)
    view = cast(MonthView, context["view"])
    summary = build_summary(
        request.app.state.session_factory,
        ctx,
        request,
        view,
        cell=plan_cell,
        copied=copied,
        automatic=automatic,
    )
    donuts = ()
    if summary.source is not None:
        donuts = tuple(
            build_donut(summary.source, block, request.state.locale)
            for block in ("expense", "income")
        )
    return {**context, **summary_context(summary), "donuts": donuts}


def _optional_uuid(value: str) -> UUID | None:
    try:
        return UUID(value) if value else None
    except ValueError:
        return None


_COUNT = re.compile(r"[0-9]{1,6}")  # ASCII digits only: str.isdigit() also accepts "²"


def _copied(month: YearMonth, copied: str, skipped: str) -> CopyResult | None:
    """The copy reported after a plain form post (``?plan_copied=N&plan_skipped=M``); anything
    malformed is ignored."""
    source = month.prev()
    skipped = skipped or "0"
    if source is None or not _COUNT.fullmatch(copied) or not _COUNT.fullmatch(skipped):
        return None
    return CopyResult(source=source, copied=int(copied), skipped=int(skipped), kept=0)


def _auto_copy(request: Request, ctx: Context, month: YearMonth) -> CopyResult | None:
    """The automatic plan copy (F2.3) for a full page view (blocking). HEAD never gets here
    (405); the check keeps a future HEAD route from writing."""
    if request.method != "GET":
        return None
    return auto_copy_on_open(request.app.state.session_factory, ctx, month)


def _optional_month(value: str) -> YearMonth | None:
    try:
        return YearMonth.parse(value) if value else None
    except InvalidMonth:
        return None


@router.get("/m/{month}", name="month")
async def month_page(
    request: Request,
    ctx: CurrentContext,
    month: str,
    notice: str = "",
    error: str = "",
    plan_copied: str = "",
    plan_skipped: str = "",
) -> Response:
    """The Month page (F2.7: a summary). ``?plan_copied=N&plan_skipped=M`` reports a plan
    copy. Opening the page may copy the previous month's plan (workspace setting,
    ``services.budget.auto_copy_on_open``). The F2.4 category filter moved to the Transactions
    page: ``?category=<id>`` redirects there (302), keeping a valid id only."""
    target = _parse(month)
    if "category" in request.query_params:
        location = scoped_url(request, "transactions_month", month=str(target))
        chosen = parse_filter(request.query_params["category"])
        if chosen is not None:
            location += "?" + urlencode({"category": str(chosen)})
        return RedirectResponse(location, status_code=302)
    copied, automatic = _copied(target, plan_copied, plan_skipped), False
    if copied is None:
        copied = await run_in_threadpool(_auto_copy, request, ctx, target)
        automatic = copied is not None
    context = await run_in_threadpool(
        lambda: month_context(
            request,
            ctx,
            target,
            notice=notice,
            error=error,
            copied=copied,
            automatic=automatic,
        )
    )
    return render_template(request, "months/page.html", context=context)


@router.post("/m/{month}/close", name="month_close")
async def month_close(request: Request, ctx: CurrentContext, month: str) -> Response:
    target = _parse(month)
    try:
        changed = await run_in_threadpool(
            close_month, request.app.state.session_factory, ctx, target
        )
    except MonthError as exc:
        return RedirectResponse(_month_path(request, target, f"error={exc.code}"), status_code=303)
    notice = "closed" if changed else "already_closed"  # a no-op says so instead
    return RedirectResponse(_month_path(request, target, f"notice={notice}"), status_code=303)


@router.post("/m/{month}/reopen", name="month_reopen")
async def month_reopen(request: Request, ctx: CurrentContext, month: str) -> Response:
    target = _parse(month)
    changed = await run_in_threadpool(reopen_month, request.app.state.session_factory, ctx, target)
    notice = "reopened" if changed else "already_open"  # a no-op says so instead
    return RedirectResponse(_month_path(request, target, f"notice={notice}"), status_code=303)
