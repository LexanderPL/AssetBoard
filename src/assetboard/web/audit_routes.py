"""The audit log page (F1.5 part C): ``GET /audit?scope=workspace|instance``.

Filters (query parameters):

* ``group``: an action group of the scope (workspace: members, invitations, settings;
  instance: sign_in, failures, links, accounts);
* ``from`` / ``to``: a date range in UTC, both days included (``YYYY-MM-DD``);
* ``user``: instance scope only, the affected account's email;
* ``cursor``: the keyset position from the previous page's "Older" link;
* ``limit``: page size, at most ``MAX_PAGE_SIZE``.

A malformed cursor, date, group or limit answers 400. Who may see which scope is decided in
``services.audit_view`` (owner of the current workspace; instance administrator).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from assetboard.i18n import gettext
from assetboard.services.admin_users import NotInstanceAdmin
from assetboard.services.audit_view import (
    GROUP_LABELS,
    GROUPS,
    KEY_LABELS,
    MAX_PAGE_SIZE,
    PAGE_SIZE,
    VALUE_LABELS,
    AuditPage,
    AuditQuery,
    AuditRow,
    BadAuditRequest,
    Scope,
    decode_cursor,
    instance_page,
    parse_day,
    workspace_page,
)
from assetboard.web.context import current_context
from assetboard.web.errors import error_response
from assetboard.web.rendering import render_template
from assetboard.web.security import web_session

router = APIRouter()


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


ACTOR_KINDS = {"cli": _("Command line"), "system": _("System")}


def _t(request: Request, message: str) -> str:
    return gettext(message, request.state.locale)


def _value(request: Request, value: str | int | bool) -> str:
    if isinstance(value, bool):
        return _t(request, _("yes") if value else _("no"))
    if isinstance(value, int):
        return str(value)
    if "," in value:  # a list of codes, such as changed workspace fields
        return ", ".join(_value(request, part) for part in value.split(",") if part)
    label = VALUE_LABELS.get(value)
    return _t(request, label) if label else value


def _row(request: Request, row: AuditRow) -> dict[str, Any]:
    actor = row.actor_name
    if actor is None:
        kind = ACTOR_KINDS.get(row.actor_kind)
        actor = _t(request, kind) if kind else None
    return {
        "when": row.ts.strftime("%Y-%m-%d %H:%M UTC"),
        "label": _t(request, row.label),
        "actor": actor,
        "actor_email": row.actor_email,
        "target_name": row.target_name,
        "target_email": row.target_email,
        "ip_prefix": row.ip_prefix,
        "details": [
            (_t(request, KEY_LABELS[key]), _value(request, value)) for key, value in row.details
        ],
    }


def _bad_request(request: Request) -> Response:
    return error_response(
        request,
        400,
        _t(request, _("Invalid request")),
        _t(
            request,
            _("The filters or the page position are not valid. Start again from the first page."),
        ),
    )


@router.get("/audit", name="audit")
async def audit(request: Request) -> Response:
    current = web_session(request)
    if current is None:  # the gate redirects first
        return error_response(request, 404, _t(request, _("Page not found")), "")
    params = request.query_params
    scope_value = params.get("scope", "workspace")
    if scope_value not in GROUPS:
        return _bad_request(request)
    scope: Scope = "instance" if scope_value == "instance" else "workspace"
    state = request.app.state
    try:
        limit = int(params.get("limit", PAGE_SIZE))
        if not 1 <= limit <= MAX_PAGE_SIZE:
            raise BadAuditRequest("limit")
        cursor_value = params.get("cursor", "")
        query = AuditQuery(
            scope=scope,
            group=params.get("group") or None,
            since=parse_day(params.get("from", "")),
            until=parse_day(params.get("to", "")),
            user_email=(params.get("user") or None) if scope == "instance" else None,
            cursor=decode_cursor(cursor_value) if cursor_value else None,
            limit=limit,
        )
        if scope == "instance":
            if not current.is_instance_admin:
                raise NotInstanceAdmin
            page: AuditPage = await run_in_threadpool(
                lambda: instance_page(state.session_factory, actor_id=current.user_id, query=query)
            )
        else:
            ctx = await current_context(request)
            page = await run_in_threadpool(
                lambda: workspace_page(state.session_factory, ctx, query)
            )
    except (BadAuditRequest, ValueError):
        return _bad_request(request)

    filters = {
        key: value
        for key in ("group", "from", "to", "user", "limit")
        if (value := params.get(key)) and (key != "user" or scope == "instance")
    }
    next_url = (
        "/audit?" + urlencode({"scope": scope, **filters, "cursor": page.next_cursor})
        if page.next_cursor
        else None
    )
    return render_template(
        request,
        "audit/list.html",
        context={
            "scope": scope,
            "rows": [_row(request, row) for row in page.rows],
            "groups": [(key, _t(request, GROUP_LABELS[key])) for key in GROUPS[scope]],
            "filters": filters,
            "next_url": next_url,
            "first_url": "/audit?" + urlencode({"scope": scope, **filters}),
            "paged": bool(params.get("cursor")),
        },
    )
