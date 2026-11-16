"""The retired net-worth history URL (F5.9): redirect to the Summary trend.

The trend is part of the Summary page now. The old bookmarkable range is preserved and
normalised, then the browser lands on the ``#net-worth-trend`` section. There are no snapshot
actions any more.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request
from starlette.responses import RedirectResponse, Response

from assetboard.domain.networth_history import normalize_history_range
from assetboard.web.rendering import scoped_url

router = APIRouter()

RangeQuery = Annotated[str, Query(alias="range")]


@router.get("/net-worth/history", name="networth_history")
async def networth_history_page(
    request: Request,
    range_code: RangeQuery = "1y",
) -> Response:
    code = normalize_history_range(range_code)
    target = f"{scoped_url(request, 'networth_summary')}?range={code}#net-worth-trend"
    return RedirectResponse(target, status_code=302)
