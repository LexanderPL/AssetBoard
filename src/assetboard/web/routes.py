"""Home (F2.7): the current month's dashboard at ``/``.

The F2.4 flow bar (the same partial as the Month page), the expense and income donuts
(``web.charts``) and up to ten upcoming F3.2 items (overdue ones and the
workspace's Upcoming window, ``occurrences.window_days``). Every
figure comes from existing services: ``services.summary`` (through ``web.plan.build_summary``)
and ``services.occurrences``. The month is the workspace's current month (Clock, time zone).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from assetboard.i18n.dates import format_day, format_month_heading
from assetboard.i18n.numbers import format_amount_input
from assetboard.services import occurrences
from assetboard.services.context import Context, Role, has_role
from assetboard.services.months import current_month, get_month
from assetboard.web.charts import build_donut
from assetboard.web.context import CurrentContext
from assetboard.web.plan import build_summary
from assetboard.web.rendering import render_template
from assetboard.web.security import require_csrf

# Every router carries the CSRF dependency, so a new state-changing route is covered by default.
router = APIRouter(dependencies=[Depends(require_csrf)])

UPCOMING_LIMIT = 10


def home_context(request: Request, ctx: Context) -> dict[str, object]:
    """The Home page's context (blocking: run in a thread)."""
    factory = request.app.state.session_factory
    locale = request.state.locale
    view = get_month(factory, ctx, current_month(ctx))
    summary = build_summary(factory, ctx, request, view)
    donuts = ()
    if summary.source is not None:
        source = summary.source
        donuts = tuple(build_donut(source, block, locale) for block in ("expense", "income"))
    days = occurrences.window_days(ctx, request.app.state.settings.upcoming_days)
    weeks = occurrences.upcoming(factory, ctx, days=days)
    items = sorted((o for w in weeks for o in w.occurrences), key=lambda o: o.due_date)
    return {
        "workspace": ctx.workspace,
        "view": view,
        "month_heading": format_month_heading(view.month, locale),
        "summary": summary,
        "donuts": donuts,
        "upcoming": items[:UPCOMING_LIMIT],
        "upcoming_more": len(items) > UPCOMING_LIMIT,
        "upcoming_days": days,
        "today": ctx.clock.today(ctx.workspace.timezone),
        "can_edit": has_role(ctx, Role.EDITOR),
        "format_day": format_day,
        "amount_input": lambda amount: format_amount_input(amount, locale),
    }


@router.get("/", name="home")
async def home(request: Request, ctx: CurrentContext) -> object:
    context = await run_in_threadpool(home_context, request, ctx)
    return render_template(request, "home.html", context=context)
