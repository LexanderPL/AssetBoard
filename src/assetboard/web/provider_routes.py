"""Provider settings page (F6.4), owner-only because it holds secrets."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from assetboard.i18n import gettext
from assetboard.i18n.dates import format_day
from assetboard.services.context import Context, Role, has_role
from assetboard.services.price_refresh import RefreshStats, refresh_now
from assetboard.services.price_sources import (
    UnknownProvider,
    provider_page,
    save_provider_config,
)
from assetboard.web.context import CurrentContext
from assetboard.web.errors import is_htmx_fragment_request
from assetboard.web.rendering import render_template
from assetboard.web.security import require_csrf

router = APIRouter(dependencies=[Depends(require_csrf)])
FormStr = Annotated[str, Form()]
PATH = "/investments/providers"


def _(message: str) -> str:
    return message


def _t(request: Request, message: str) -> str:
    return gettext(message, request.state.locale)


def _enabled(value: str) -> bool:
    return value.strip().casefold() in {"1", "true", "on", "yes"}


def _refresh_message(request: Request, stats: RefreshStats) -> str | None:
    retry_seconds = stats.retry_after if stats.retry_after and stats.retry_after > 0 else 60
    retry_minutes = max(1, (retry_seconds + 59) // 60)
    if stats.status == "rate_limited":
        return _t(
            request,
            _("Rate limited; try again in %(minutes)s minutes."),
        ) % {"minutes": retry_minutes}
    if stats.status == "ok":
        return _t(
            request,
            _("Refresh complete: %(prices)s prices and %(rates)s FX rates."),
        ) % {"prices": stats.prices, "rates": stats.rates}
    if stats.status == "partial":
        return _t(
            request,
            _("Refresh complete with %(errors)s errors."),
        ) % {"errors": stats.errors}
    if stats.status == "disabled":
        return _t(request, _("Enable the provider before refreshing."))
    if stats.status == "no_targets":
        return _t(request, _("No held instruments need this provider."))
    return _t(request, _("The provider could not be refreshed."))


def _render(
    request: Request,
    ctx: Context,
    *,
    notice: str | None = None,
    error: str | None = None,
    refresh: RefreshStats | None = None,
    status_code: int = 200,
) -> Response:
    page = provider_page(request.app.state.session_factory, ctx)
    context: dict[str, Any] = {
        "page": page,
        "can_manage_providers": has_role(ctx, Role.OWNER),
        "base_currency": ctx.workspace.base_currency,
        "notice": _t(request, notice) if notice else None,
        "error": _t(request, error) if error else None,
        "refresh_message": _refresh_message(request, refresh) if refresh else None,
        "format_day": format_day,
        "locale": request.state.locale,
    }
    template = (
        "investments/_providers_body.html"
        if is_htmx_fragment_request(request)
        else "investments/providers.html"
    )
    response = render_template(request, template, context=context, status_code=status_code)
    response.headers["Vary"] = "HX-Request"
    return response


def _query_refresh(request: Request) -> RefreshStats | None:
    status = request.query_params.get("refresh", "")
    if not status:
        return None
    values = request.query_params
    try:
        prices = int(values.get("prices", "0"))
        rates = int(values.get("rates", "0"))
        errors = int(values.get("errors", "0"))
        retry_after = int(values.get("retry_after", "0")) or None
    except ValueError:
        return None
    return RefreshStats(
        provider_key="",
        status=status,
        prices=max(0, prices),
        rates=max(0, rates),
        errors=max(0, errors),
        retry_after=retry_after,
    )


@router.get("/investments/providers", name="investments_providers")
async def providers_page(request: Request, ctx: CurrentContext, notice: str = "") -> Response:
    return await run_in_threadpool(
        lambda: _render(
            request,
            ctx,
            notice=_t(request, _("Provider settings saved.")) if notice == "saved" else None,
            refresh=_query_refresh(request),
        )
    )


@router.post("/investments/providers/{provider_key}", name="investments_provider_update")
async def provider_update(
    request: Request,
    ctx: CurrentContext,
    provider_key: str,
    enabled: FormStr = "",
) -> Response:
    try:
        result = await run_in_threadpool(
            lambda: save_provider_config(
                request.app.state.session_factory,
                ctx,
                request.app.state.settings,
                provider_key,
                enabled=_enabled(enabled),
            )
        )
    except UnknownProvider:
        return await run_in_threadpool(
            lambda: _render(
                request,
                ctx,
                error=_("The provider settings could not be saved."),
                status_code=400,
            )
        )
    if result.newly_enabled:
        jobs = getattr(request.app.state, "recurring_jobs", None)
        if jobs is not None:
            jobs.request_backfill(ctx.workspace.id, provider_key)
    if is_htmx_fragment_request(request):
        return await run_in_threadpool(
            lambda: _render(request, ctx, notice=_("Provider settings saved."))
        )
    return RedirectResponse(f"{PATH}?notice=saved", status_code=303)


@router.post(
    "/investments/providers/{provider_key}/refresh",
    name="investments_provider_refresh",
)
async def provider_refresh(
    request: Request,
    ctx: CurrentContext,
    provider_key: str,
) -> Response:
    stats = await run_in_threadpool(
        lambda: refresh_now(
            request.app.state.session_factory,
            ctx,
            provider_key,
            request.app.state.provider_runtime,
        )
    )
    if is_htmx_fragment_request(request):
        return await run_in_threadpool(lambda: _render(request, ctx, refresh=stats))
    if stats.status in {"rate_limited", "disabled", "no_targets"}:
        return RedirectResponse(
            f"{PATH}?refresh={stats.status}&retry_after={stats.retry_after or 0}",
            status_code=303,
        )
    return RedirectResponse(
        f"{PATH}?refresh={stats.status}&prices={stats.prices}&rates={stats.rates}"
        f"&errors={stats.errors}",
        status_code=303,
    )
