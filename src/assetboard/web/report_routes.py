"""The Reports page (F10.1–F10.3): ``/reports``, ledger report sections for a range of years.

A plain GET form: every view is a bookmarkable URL and works without JavaScript. The category
filter is repeatable (``?category=<id>&category=<id>``).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from starlette.concurrency import run_in_threadpool

from assetboard.services.reports import reports_data
from assetboard.web.context import CurrentContext
from assetboard.web.rendering import render_template
from assetboard.web.reports import build_reports_view
from assetboard.web.security import require_csrf

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
        )
    )
    view = build_reports_view(request, data, request.state.locale)
    return render_template(request, "reports/page.html", context={"view": view})


__all__ = ["router"]
