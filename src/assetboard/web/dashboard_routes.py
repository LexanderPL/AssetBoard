"""Home widget fragments and the Customise page (F9.1).

The Home shell itself stays in ``web.routes``. This router serves one lazy fragment per widget
and the plain-form Customise page. Every state-changing POST is covered by the router-level
CSRF dependency; no route touches an ORM model or session directly.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from assetboard.db.repositories import NotFound
from assetboard.domain.dashboard import WIDGET_KEYS, selected_widgets
from assetboard.services.dashboard import (
    reset_dashboard_preferences,
    save_dashboard_preferences,
)
from assetboard.web.context import CurrentContext
from assetboard.web.dashboard import (
    WIDGETS_BY_KEY,
    customise_context,
    moved_order,
    parse_move,
    parse_order,
    widget_context,
)
from assetboard.web.errors import is_htmx_fragment_request
from assetboard.web.rendering import render_template, scoped_url
from assetboard.web.security import require_csrf

router = APIRouter(dependencies=[Depends(require_csrf)])


async def _form_values(request: Request) -> tuple[tuple[str, ...], set[str]]:
    form = await request.form()
    order = parse_order(str(form.get("order", "")))
    enabled = {str(value) for value in form.getlist("enabled") if isinstance(value, str)}
    return order, enabled


@router.get("/dashboard/widgets/{widget_key}", name="dashboard_widget")
async def dashboard_widget(request: Request, ctx: CurrentContext, widget_key: str) -> Response:
    widget = WIDGETS_BY_KEY.get(widget_key)
    if widget is None:
        raise NotFound
    if not is_htmx_fragment_request(request):
        return RedirectResponse(widget.module_url(request, ctx), status_code=303)
    context = await run_in_threadpool(widget_context, request, ctx, widget)
    response = render_template(request, "dashboard/widget.html", context=context)
    response.headers["Vary"] = "HX-Request"
    return response


@router.get("/dashboard/customise", name="dashboard_customise")
async def dashboard_customise(request: Request, ctx: CurrentContext) -> Response:
    context = await run_in_threadpool(customise_context, request, ctx)
    return render_template(request, "dashboard/customise.html", context=context)


@router.post("/dashboard/customise/move", name="dashboard_customise_move")
async def dashboard_customise_move(request: Request, ctx: CurrentContext) -> Response:
    """Rerender the draft with one widget moved; nothing is saved until Save."""
    order, enabled = await _form_values(request)
    form = await request.form()
    move = parse_move(str(form.get("move", "")))
    if move is not None:
        order = moved_order(order, move[0], move[1])
    context = await run_in_threadpool(
        lambda: customise_context(request, ctx, order=order, enabled=enabled)
    )
    return render_template(request, "dashboard/customise.html", context=context)


@router.post("/dashboard/customise", name="dashboard_customise_save")
async def dashboard_customise_save(request: Request, ctx: CurrentContext) -> Response:
    order, enabled = await _form_values(request)
    widgets = selected_widgets(order, enabled, WIDGET_KEYS)
    await run_in_threadpool(
        save_dashboard_preferences,
        request.app.state.session_factory,
        ctx,
        widgets,
    )
    return RedirectResponse(scoped_url(request, "home") + "?saved=1", status_code=303)


@router.post("/dashboard/customise/reset", name="dashboard_customise_reset")
async def dashboard_customise_reset(request: Request, ctx: CurrentContext) -> Response:
    await run_in_threadpool(
        reset_dashboard_preferences,
        request.app.state.session_factory,
        ctx,
    )
    return RedirectResponse(scoped_url(request, "home") + "?reset=1", status_code=303)
