"""The Reports page (F10.1–F10.3 and F9.2): ``/reports`` and the year-overview CSV.

A plain GET form: every view is a bookmarkable URL and works without JavaScript. The category
filter is repeatable (``?category=<id>&category=<id>``). The CSV is a GET with the same filters,
so the downloaded grid matches the one on screen.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from assetboard.domain.report_csv import year_overview_csv
from assetboard.i18n import gettext
from assetboard.services.reports import reports_data
from assetboard.web.context import CurrentContext
from assetboard.web.rendering import render_template
from assetboard.web.reports import build_reports_view
from assetboard.web.security import require_csrf

router = APIRouter(dependencies=[Depends(require_csrf)])

YEAR_OVERVIEW_FILENAME = "reports-year-overview.csv"


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
    yoy_category: str = "",
) -> object:
    data = await run_in_threadpool(
        lambda: reports_data(
            request.app.state.session_factory,
            ctx,
            from_year=from_year,
            to_year=to_year,
            step=step,
            currency=currency,
            category=category or (),
            savings=savings,
            year=year,
            trend=trend,
            yoy_category=yoy_category,
        )
    )
    view = build_reports_view(request, data, request.state.locale)
    return render_template(request, "reports/page.html", context={"view": view})


@router.get("/reports/year-overview.csv", name="reports_year_overview_csv")
async def reports_year_overview_csv(
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
    yoy_category: str = "",
) -> Response:
    data = await run_in_threadpool(
        lambda: reports_data(
            request.app.state.session_factory,
            ctx,
            from_year=from_year,
            to_year=to_year,
            step=step,
            currency=currency,
            category=category or (),
            savings=savings,
            year=year,
            trend=trend,
            yoy_category=yoy_category,
        )
    )
    if data.filters.errors:
        return Response(
            content=gettext("Fix the filters to export the year overview.", request.state.locale),
            status_code=400,
            media_type="text/plain; charset=utf-8",
        )
    content = year_overview_csv(data.year_grid, year=data.filters.year)
    return Response(
        content=content.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{YEAR_OVERVIEW_FILENAME}"',
            "Cache-Control": "no-store",
        },
    )


__all__ = ["router"]
