"""Instance administrator backup actions (F9.4).

* ``POST /admin/backups/now``: make a manual copy now, then return to Instance settings.
* ``GET /admin/backups/{run_id}``: download one successful copy (the database file only).

The scheduled job lives in ``recurring_jobs``; retention and history are in
``services.backups``. Only active instance administrators reach these routes.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, RedirectResponse, Response

from assetboard.services.admin_users import NotInstanceAdmin
from assetboard.services.auth import WebSession
from assetboard.services.backups import (
    BackupError,
    BackupUnavailable,
    create_backup,
    download_path,
)
from assetboard.web.rendering import scoped_url
from assetboard.web.security import require_csrf, web_session

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_csrf)])


def _require_admin(request: Request) -> WebSession:
    current = web_session(request)
    if current is None:  # the gate redirects first; this guards direct use
        raise HTTPException(status_code=404)
    if not current.is_instance_admin:
        raise NotInstanceAdmin
    return current


def _settings(request: Request, notice: str) -> str:
    return f"{scoped_url(request, 'admin_settings')}?backup={notice}"


@router.post("/admin/backups/now")
async def backup_now(request: Request) -> Response:
    _require_admin(request)
    state = request.app.state
    try:
        await run_in_threadpool(
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
    return RedirectResponse(_settings(request, "created"), status_code=303)


@router.get("/admin/backups/{run_id}")
async def backup_download(request: Request, run_id: UUID) -> Response:
    _require_admin(request)
    state = request.app.state
    found = await run_in_threadpool(download_path, state.session_factory, state.settings, run_id)
    if found is None:
        raise HTTPException(status_code=404)
    path, filename = found
    return FileResponse(
        path,
        media_type="application/vnd.sqlite3",
        filename=filename,
    )
