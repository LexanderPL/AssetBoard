"""Home (F2.7/F9.1): the current month's dashboard at ``/``.

Since F9.1 the page is a shell of independently lazy-loaded widgets. The shell only knows the
user's ordered widget list and the current month; each widget fragment calls the existing
service that owns its figures (``web.dashboard``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from assetboard.web.context import CurrentContext
from assetboard.web.dashboard import home_context
from assetboard.web.rendering import render_template
from assetboard.web.security import require_csrf

# Every router carries the CSRF dependency, so a new state-changing route is covered by default.
router = APIRouter(dependencies=[Depends(require_csrf)])


@router.get("/", name="home")
async def home(request: Request, ctx: CurrentContext) -> object:
    context = await run_in_threadpool(home_context, request, ctx)
    return render_template(request, "home.html", context=context)


__all__ = ["home_context", "router"]
