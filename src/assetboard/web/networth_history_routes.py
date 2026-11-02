"""Net-worth history page and snapshot actions (F5.3).

Reads are available to every member; taking a snapshot and rebuilding history need editor or
owner (checked in the service, inside the write transaction). Every POST is covered by the
router-level CSRF dependency and redirects (303) to the history page.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from assetboard.i18n import gettext
from assetboard.services.context import Role, has_role
from assetboard.services.networth_snapshots import (
    SnapshotDateInFuture,
    backfill_snapshots,
    networth_history,
    take_manual_snapshot,
)
from assetboard.web.context import CurrentContext
from assetboard.web.networth_history import build_history_view
from assetboard.web.rendering import render_template
from assetboard.web.security import require_csrf

router = APIRouter(dependencies=[Depends(require_csrf)])

FormStr = Annotated[str, Form()]
RangeQuery = Annotated[str, Query(alias="range")]


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


NOTICES = {
    "snapshot_taken": _("Snapshot saved."),
    "snapshot_replaced": _("Snapshot replaced."),
    "backfill_done": _("History rebuilt from stored values."),
    "backfill_empty": _("There are no stored values to backfill yet."),
}
ERRORS = {
    "future_date": _("A snapshot cannot be dated in the future."),
}


def _t(request: Request, message: str) -> str:
    return gettext(message, request.state.locale)


def _history_url(request: Request, range_code: str, *, notice: str = "", error: str = "") -> str:
    path = request.url_for("networth_history").path
    query = f"?range={range_code}"
    if notice:
        query += f"&notice={notice}"
    if error:
        query += f"&error={error}"
    return path + query


@router.get("/net-worth/history", name="networth_history")
async def networth_history_page(
    request: Request,
    ctx: CurrentContext,
    range_code: RangeQuery = "1y",
    notice: str = "",
    error: str = "",
) -> Response:
    data = await run_in_threadpool(
        networth_history, request.app.state.session_factory, ctx, range_code
    )
    view = build_history_view(
        data,
        request.state.locale,
        can_edit=has_role(ctx, Role.EDITOR),
        notice=_t(request, NOTICES[notice]) if notice in NOTICES else None,
        error=_t(request, ERRORS[error]) if error in ERRORS else None,
    )
    return render_template(request, "networth/history.html", context={"view": view})


@router.post("/net-worth/history/snapshot", name="networth_snapshot_take")
async def networth_snapshot_take(
    request: Request,
    ctx: CurrentContext,
    range_code: FormStr = "1y",
) -> Response:
    try:
        _as_of, replaced = await run_in_threadpool(
            take_manual_snapshot, request.app.state.session_factory, ctx
        )
    except SnapshotDateInFuture:
        return RedirectResponse(
            _history_url(request, range_code, error="future_date"), status_code=303
        )
    notice = "snapshot_replaced" if replaced else "snapshot_taken"
    return RedirectResponse(_history_url(request, range_code, notice=notice), status_code=303)


@router.post("/net-worth/history/backfill", name="networth_snapshot_backfill")
async def networth_snapshot_backfill(
    request: Request,
    ctx: CurrentContext,
    range_code: FormStr = "1y",
) -> Response:
    result = await run_in_threadpool(backfill_snapshots, request.app.state.session_factory, ctx)
    notice = "backfill_empty" if result.empty else "backfill_done"
    return RedirectResponse(_history_url(request, range_code, notice=notice), status_code=303)
