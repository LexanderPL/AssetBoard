"""Instance administrator backup actions (F9.4).

* ``GET /admin/backups/now``: the confirmation page; ``POST``: confirm, then make a copy.
* ``GET /admin/backups/{run_id}``: the confirmation page; ``POST``: confirm, then download.
* ``POST`` actions require the administrator's password (or a fresh external sign-in) through
  the shared confirmation path, and record instance-level ``backup.created`` /
  ``backup.downloaded`` audit events.

The scheduled job lives in ``recurring_jobs``; retention and history are in
``services.backups``. Only active instance administrators reach these routes.
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, RedirectResponse, Response

from assetboard.i18n import gettext
from assetboard.services.admin_users import NotInstanceAdmin
from assetboard.services.auth import WebSession, record_auth_event
from assetboard.services.backups import (
    BackupError,
    BackupUnavailable,
    create_backup,
    download_path,
)
from assetboard.web.confirm import ConfirmView, Refusal, confirm_only, confirm_view, refusal
from assetboard.web.errors import BUSY_RETRY_AFTER
from assetboard.web.rendering import render_template, scoped_url
from assetboard.web.security import client_host, require_csrf, web_session

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_csrf)])

FormStr = Annotated[str, Form()]


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


BACKUP_NOW_TITLE = _("Back up now")
BACKUP_NOW_EXPLANATION = _(
    "Create a copy of the database now. The file can contain every workspace and account on "
    "this instance; keep it somewhere safe."
)
BACKUP_DOWNLOAD_TITLE = _("Download backup")
BACKUP_DOWNLOAD_EXPLANATION = _(
    "Download this database copy. The file can contain every workspace and account on this "
    "instance; keep it somewhere safe."
)


def _t(request: Request, message: str, **values: str) -> str:
    return (
        gettext(message, request.state.locale) % values
        if values
        else gettext(message, request.state.locale)
    )


def _require_admin(request: Request) -> WebSession:
    current = web_session(request)
    if current is None:  # the gate redirects first; this guards direct use
        raise HTTPException(status_code=404)
    if not current.is_instance_admin:
        raise NotInstanceAdmin
    return current


def _settings(request: Request, notice: str) -> str:
    return f"{scoped_url(request, 'admin_settings')}?backup={notice}"


async def _confirm_page(
    request: Request,
    current: WebSession,
    *,
    action: str,
    title: str,
    explanation: str,
    action_url: str,
    filename: str | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    confirm: ConfirmView = await confirm_view(request, current)
    response = render_template(
        request,
        "admin/backup_confirm.html",
        context={
            "action": action,
            "title": title,
            "explanation": explanation,
            "action_url": action_url,
            "filename": filename,
            "confirm": confirm,
            "error": error,
        },
        status_code=status_code,
    )
    if status_code == 503:
        response.headers["Retry-After"] = BUSY_RETRY_AFTER
    return response


def _record(
    request: Request,
    current: WebSession,
    action: str,
    run_id: UUID,
    data: dict[str, str | int | bool] | None = None,
) -> None:
    state = request.app.state
    with state.session_factory() as session, session.begin():
        record_auth_event(
            session,
            state.clock,
            action=action,
            entity_type="backup",
            entity_id=str(run_id),
            actor_user_id=current.user_id,
            host=client_host(request),
            user_agent=request.headers.get("user-agent"),
            data=data,
        )


def _refused(response: Response, refused: Refusal) -> Response:
    if refused.retry_after is not None:
        response.headers["Retry-After"] = refused.retry_after
    return response


@router.get("/admin/backups/now", name="backup_now_confirm")
async def backup_now_confirm(request: Request) -> Response:
    current = _require_admin(request)
    return await _confirm_page(
        request,
        current,
        action="create",
        title=_t(request, BACKUP_NOW_TITLE),
        explanation=_t(request, BACKUP_NOW_EXPLANATION),
        action_url=scoped_url(request, "backup_now"),
    )


@router.post("/admin/backups/now", name="backup_now")
async def backup_now(request: Request, confirm_password: FormStr = "") -> Response:
    current = _require_admin(request)
    outcome = await confirm_only(
        request, current, purpose="backup_create", password=confirm_password
    )
    if outcome.status != "confirmed":
        refused = refusal(request, outcome)
        page = await _confirm_page(
            request,
            current,
            action="create",
            title=_t(request, BACKUP_NOW_TITLE),
            explanation=_t(request, BACKUP_NOW_EXPLANATION),
            action_url=scoped_url(request, "backup_now"),
            error=refused.message,
            status_code=refused.status_code,
        )
        return _refused(page, refused)
    state = request.app.state
    try:
        result = await run_in_threadpool(
            create_backup,
            state.session_factory,
            state.settings,
            state.clock,
            origin="manual",
            notifier=state.notifier,
        )
    except BackupUnavailable:
        return RedirectResponse(_settings(request, "unavailable"), status_code=303)
    except BackupError as exc:
        logger.warning("manual_backup_failed code=%s", exc.code)
        return RedirectResponse(_settings(request, "failed"), status_code=303)
    if result.id is not None:
        await run_in_threadpool(
            _record, request, current, "backup.created", result.id, {"origin": "manual"}
        )
    return RedirectResponse(_settings(request, "created"), status_code=303)


@router.get("/admin/backups/{run_id}", name="backup_download")
async def backup_download(request: Request, run_id: UUID) -> Response:
    current = _require_admin(request)
    state = request.app.state
    found = await run_in_threadpool(download_path, state.session_factory, state.settings, run_id)
    if found is None:
        raise HTTPException(status_code=404)
    _path, filename = found
    return await _confirm_page(
        request,
        current,
        action="download",
        title=_t(request, BACKUP_DOWNLOAD_TITLE),
        explanation=_t(request, BACKUP_DOWNLOAD_EXPLANATION),
        action_url=scoped_url(request, "backup_download_file", run_id=str(run_id)),
        filename=filename,
    )


@router.post("/admin/backups/{run_id}", name="backup_download_file")
async def backup_download_file(
    request: Request, run_id: UUID, confirm_password: FormStr = ""
) -> Response:
    current = _require_admin(request)
    outcome = await confirm_only(
        request, current, purpose="backup_download", password=confirm_password
    )
    state = request.app.state
    found = await run_in_threadpool(download_path, state.session_factory, state.settings, run_id)
    if outcome.status != "confirmed":
        refused = refusal(request, outcome)
        page = await _confirm_page(
            request,
            current,
            action="download",
            title=_t(request, BACKUP_DOWNLOAD_TITLE),
            explanation=_t(request, BACKUP_DOWNLOAD_EXPLANATION),
            action_url=scoped_url(request, "backup_download_file", run_id=str(run_id)),
            filename=found[1] if found is not None else None,
            error=refused.message,
            status_code=refused.status_code,
        )
        return _refused(page, refused)
    if found is None:
        raise HTTPException(status_code=404)
    path, filename = found
    # "Downloaded" means authorised and served; an aborted client read still leaves the event.
    await run_in_threadpool(_record, request, current, "backup.downloaded", run_id)
    return FileResponse(
        path,
        media_type="application/vnd.sqlite3",
        filename=filename,
    )
