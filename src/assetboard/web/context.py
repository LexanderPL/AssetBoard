"""The per-request ``Context`` dependency for routes that work on workspace data."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from starlette.concurrency import run_in_threadpool

from assetboard.services.context import (
    Context,
    NoWorkspace,
    TwoFactorRequired,
    WorkspaceNav,
    build_context,
    workspace_nav,
)
from assetboard.services.instance_settings import workspace_creation_allowed
from assetboard.services.update_check import update_notice
from assetboard.web.errors import is_htmx_fragment_request
from assetboard.web.security import web_session


async def current_context(request: Request) -> Context:
    """Build the Context from the signed-in session (the access gate guarantees one).

    Raises ``NoWorkspace`` (rendered as a 403 page) when the user has no membership. Also sets
    ``request.state.workspace_nav`` for the page chrome (workspace name, switcher, owner links).
    A Context already built for this request (see ``load_workspace_nav``) is reused.
    """
    existing: Context | None = getattr(request.state, "context", None)
    if existing is not None:
        return existing
    signed_in = web_session(request)
    if signed_in is None:
        raise RuntimeError("current_context needs a signed-in request")
    state = request.app.state

    def build() -> tuple[Context, WorkspaceNav]:
        with state.session_factory() as session:
            ctx = build_context(session, signed_in, clock=state.clock)
            can_create = workspace_creation_allowed(
                session, is_instance_admin=signed_in.is_instance_admin
            )
            return ctx, workspace_nav(session, ctx, can_create_workspace=can_create)

    ctx, nav = await run_in_threadpool(build)
    request.state.context = ctx
    request.state.workspace_nav = nav
    return ctx


async def load_workspace_nav(request: Request) -> None:
    """App-wide dependency: the page chrome of every signed-in page shows the workspace nav.

    Account, admin, audit and similar pages take no Context, but ``base.html`` still shows the
    full sidebar for the current workspace. A user without a membership keeps the reduced nav.
    HTMX fragments do not render the chrome, so they skip the lookup. Instance administrators
    also get the update notice (``request.state.update_notice``) for the sidebar.
    """
    signed_in = web_session(request)
    if signed_in is None or is_htmx_fragment_request(request):
        return
    if signed_in.is_instance_admin:
        # The opt-in update check's result (ADR-0023), shown to administrators only.
        request.state.update_notice = await run_in_threadpool(
            update_notice, request.app.state.session_factory, request.app.version
        )
    try:
        await current_context(request)
    except (NoWorkspace, TwoFactorRequired):
        # No nav when there is no usable workspace context; the routes that need one raise it.
        return


CurrentContext = Annotated[Context, Depends(current_context)]
