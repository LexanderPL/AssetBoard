"""The Reports page (F10.1–F10.5 and F9.2): ``/reports``.

A plain GET form: every view is a bookmarkable URL and works without JavaScript. The category
filter is repeatable (``?category=<id>&category=<id>``). The last resolved filters are kept in a
one-year browser cookie; a valid explicit report query parameter wins and refreshes it, while
an invalid one shows its errors without replacing it. ``?clear=1`` deletes it and redirects to
the clean page.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from assetboard.services.reports import reports_data
from assetboard.web.context import CurrentContext
from assetboard.web.rendering import render_template, scoped_url
from assetboard.web.report_filters import (
    MAX_REPORT_FILTER_COOKIE_LENGTH,
    REPORT_FILTER_KEYS,
    ReportFilterSource,
    read_report_filter_cookie,
    serialize_report_filters,
)
from assetboard.web.reports import build_reports_view
from assetboard.web.security import (
    clear_cookie,
    report_filters_cookie_name,
    require_csrf,
    set_preference_cookie,
)

router = APIRouter(dependencies=[Depends(require_csrf)])


@router.get("/reports", name="reports")
async def reports(
    request: Request,
    ctx: CurrentContext,
    from_year: Annotated[str, Query(alias="from")] = "",
    to_year: Annotated[str, Query(alias="to")] = "",
    step: str = "",
    currency: str = "",
    category: Annotated[list[str] | None, Query()] = None,
    savings: str = "",
    year: str = "",
    trend: str = "",
    clear: str = "",
) -> Response:
    settings = request.app.state.settings
    cookie_name = report_filters_cookie_name(settings)
    if clear == "1":
        response = RedirectResponse(scoped_url(request, "reports"), status_code=303)
        clear_cookie(response, settings, cookie_name)
        return response

    explicit = any(key in REPORT_FILTER_KEYS for key in request.query_params)
    source = (
        ReportFilterSource(
            from_year=from_year,
            to_year=to_year,
            step=step,
            currency=currency,
            category_ids=tuple(category or ()),
            savings=savings,
            year=year,
            trend=trend,
            explicit=True,
        )
        if explicit
        else read_report_filter_cookie(request)
    )
    factory = request.app.state.session_factory
    data = await run_in_threadpool(
        lambda: reports_data(
            factory,
            ctx,
            from_year=source.from_year,
            to_year=source.to_year,
            step=source.step,
            currency=source.currency,
            category=source.category_ids,
            savings=source.savings,
            year=source.year,
            trend=source.trend,
        )
    )
    # A malformed or stale cookie must not surface validation errors: forget it and show the
    # current defaults. An explicit invalid URL keeps its errors and does not refresh the cookie.
    stale_cookie = not explicit and bool(data.filters.errors)
    if stale_cookie:
        data = await run_in_threadpool(lambda: reports_data(factory, ctx))
    view = build_reports_view(request, data, request.state.locale)
    response = render_template(request, "reports/page.html", context={"view": view})
    if stale_cookie:
        clear_cookie(response, settings, cookie_name)
    if explicit and not data.filters.errors:
        value = serialize_report_filters(data.filters)
        if len(value) <= MAX_REPORT_FILTER_COOKIE_LENGTH:
            set_preference_cookie(response, settings, cookie_name, value)
        else:
            # Never leave an older, unrelated set behind a request it cannot represent.
            clear_cookie(response, settings, cookie_name)
    return response


__all__ = ["router"]
