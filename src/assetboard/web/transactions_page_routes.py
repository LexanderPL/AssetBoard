"""The Transactions page (F2.7): ``/transactions/YYYY-MM``, the entry page of one month.

It holds F2.2's quick-add rows and the month's income and expense tables with inline edit,
delete and undo (``web.ledger``; the writes stay at ``/m/{month}/transactions/...`` in
``web.transaction_routes``), F2.4 part B's category filter and the month navigation shared with
the Month page. It has no flow bar or KPI line (owner, F2.7: entry only).

The old addresses of F2.6's list redirect to the History page (``/history``): a filtered
``/transactions?...`` (302), the CSV export (301) and the bulk recategorize posts (308, so the
method and body are kept and the CSRF check runs at the target). Every target is built from a
route name under the app's root path, and only known parameters are copied (no open redirect).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Annotated
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from assetboard.db.repositories import NotFound
from assetboard.domain.months import InvalidMonth, YearMonth
from assetboard.i18n.dates import format_month
from assetboard.services.context import Context
from assetboard.services.months import MonthView, current_month
from assetboard.services.transactions import get_transaction, pending_undo
from assetboard.web.context import CurrentContext
from assetboard.web.ledger import (
    DIRECTIONS,
    ENTRY_DAY,
    SectionState,
    build_ledger,
    edit_values,
    parse_filter,
)
from assetboard.web.month_routes import (
    SAVED_IN,
    _optional_month,
    _optional_uuid,
    _parse,
    _t,
    month_header,
)
from assetboard.web.rendering import render_template, scoped_url
from assetboard.web.security import require_csrf

router = APIRouter(dependencies=[Depends(require_csrf)])

# F2.6's query parameters: any of them on ``/transactions`` means an old History link.
HISTORY_PARAMS = (
    *("from", "to", "direction", "category", "q", "min", "max", "sort", "page", "export"),
)
# The export adds its CSV options.
EXPORT_PARAMS = (*HISTORY_PARAMS, "delimiter", "decimal", "bom", "guard")


def transactions_path(request: Request, month: YearMonth, query: str = "") -> str:
    """The Transactions page of ``month`` (with an optional, already encoded query)."""
    path = scoped_url(request, "transactions_month", month=str(month))
    return f"{path}?{query}" if query else path


def _known(request: Request, names: tuple[str, ...]) -> str:
    """The request's query, reduced to ``names`` (repeated parameters kept, in order)."""
    pairs = [(k, v) for k, v in request.query_params.multi_items() if k in names]
    return urlencode(pairs)


def _to(request: Request, route_name: str, query: str, status_code: int) -> Response:
    path = scoped_url(request, route_name)
    return RedirectResponse(f"{path}?{query}" if query else path, status_code=status_code)


@router.get("/transactions", name="transactions")
async def transactions_current(
    request: Request,
    ctx: CurrentContext,
    month: str | None = None,
    month_month: str | None = None,
    month_year: str | None = None,
    from_: Annotated[str, Query(alias="from")] = "",
) -> Response:
    """Today's month (303); the month chosen in the picker (``?month=...``, 303); or, with any
    of F2.6's filter parameters, the History page with the same filters (302)."""
    if any(name in request.query_params for name in HISTORY_PARAMS) and month is None:
        return _to(request, "history", _known(request, HISTORY_PARAMS), 302)
    if month is None and (month_month is not None or month_year is not None):
        try:
            month = f"{int(month_year or ''):04d}-{int(month_month or ''):02d}"
        except ValueError:
            month = ""
    if month is None:
        return RedirectResponse(transactions_path(request, current_month(ctx)), status_code=303)
    try:
        target = YearMonth.parse_loose(month)
    except InvalidMonth:
        try:
            back = YearMonth.parse(from_)
        except InvalidMonth:
            back = current_month(ctx)
        return RedirectResponse(
            transactions_path(request, back, "error=invalid_month"), status_code=303
        )
    return RedirectResponse(transactions_path(request, target), status_code=303)


@router.get("/transactions/export.csv", name="transactions_export_moved")
async def transactions_export_moved(request: Request, ctx: CurrentContext) -> Response:
    del ctx  # signed-in members only, like the target
    return _to(request, "history_export", _known(request, EXPORT_PARAMS), 301)


@router.post("/transactions/recategorize", name="transactions_recategorize_moved")
async def transactions_recategorize_moved(request: Request, ctx: CurrentContext) -> Response:
    del ctx
    return _to(request, "history_recategorize_preview", "", 308)


@router.post("/transactions/recategorize/confirm", name="transactions_recategorize_confirm_moved")
async def transactions_recategorize_confirm_moved(
    request: Request, ctx: CurrentContext
) -> Response:
    del ctx
    return _to(request, "history_recategorize", "", 308)


def transactions_context(
    request: Request,
    ctx: Context,
    target: YearMonth,
    *,
    notice: str = "",
    error: str = "",
    states: dict[str, SectionState] | None = None,
    deleted: UUID | None = None,
    saved_in: YearMonth | None = None,
    filter_id: UUID | None = None,
) -> dict[str, object]:
    """The Transactions page's context (blocking: run in a thread). ``states`` carry the
    tables' form values and messages; ``deleted`` offers Undo (pages without JavaScript) while
    that row of this month can still be restored."""
    factory = request.app.state.session_factory
    context = month_header(request, ctx, target, notice=notice, error=error)
    view = context["view"]
    assert isinstance(view, MonthView)
    ledger = build_ledger(factory, ctx, request, view, states, filter_id)
    pending = None
    if deleted is not None and ledger.can_write:
        pending = pending_undo(factory, ctx, deleted)
        if pending is not None and pending.month != target:
            pending = None
    if saved_in is not None and saved_in != target:
        locale = request.state.locale
        context["notice"] = _t(request, SAVED_IN) % {"month": format_month(saved_in, locale)}
        context["notice_href"] = transactions_path(request, saved_in)
    return {**context, "ledger": ledger, "deleted": pending.id if pending is not None else None}


def edit_states(
    request: Request, ctx: Context, month: YearMonth, transaction_id: UUID
) -> dict[str, SectionState]:
    """The section state that opens ``transaction_id`` for editing (none if it is gone)."""
    try:
        row = get_transaction(request.app.state.session_factory, ctx, transaction_id)
    except NotFound:
        return {}
    values = edit_values(row, month, request.state.locale)
    return {row.direction: SectionState(editing=row.id, edit_values=values, focus="edit:day")}


@router.get("/transactions/{month}", name="transactions_month")
async def transactions_page(
    request: Request,
    ctx: CurrentContext,
    month: str,
    notice: str = "",
    error: str = "",
    edit: str = "",
    deleted: str = "",
    saved_in: str = "",
    entry: str = "",
    day: str = "",
    category: str = "",
) -> Response:
    """The Transactions page. For browsers without JavaScript: ``?edit=<id>`` opens a row for
    editing, ``?deleted=<id>`` offers Undo, ``?entry=<direction>&day=<day>`` prefills that
    table's entry row with the day just used. ``?saved_in=YYYY-MM`` links an entry saved to
    another month. ``?category=<id>`` shows that category's entries only (F2.4; an unknown id
    shows everything)."""
    target = _parse(month)
    states: dict[str, SectionState] = {}
    editing = _optional_uuid(edit)
    if editing is not None:
        states = await run_in_threadpool(edit_states, request, ctx, target, editing)
    if entry in DIRECTIONS and ENTRY_DAY.fullmatch(day):
        kept = SectionState(entry_values={"day": day}, focus="entry:day")
        if entry in states:
            kept = replace(states[entry], entry_values=kept.entry_values)
        states[entry] = kept
    context = await run_in_threadpool(
        lambda: transactions_context(
            request,
            ctx,
            target,
            notice=notice,
            error=error,
            states=states,
            deleted=_optional_uuid(deleted),
            saved_in=_optional_month(saved_in),
            filter_id=parse_filter(category),
        )
    )
    return render_template(request, "transactions/page.html", context=context)
