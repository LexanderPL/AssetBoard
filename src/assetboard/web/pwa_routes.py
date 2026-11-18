"""The installable web app: manifest, service worker and the offline page (F9.10)."""

from __future__ import annotations

import json
from functools import lru_cache

from fastapi import APIRouter, Request
from starlette.responses import HTMLResponse, Response

from assetboard.branding import APP_NAME
from assetboard.web.pwa import (
    DARK_BACKGROUND_COLOR,
    DARK_THEME_COLOR,
    LIGHT_BACKGROUND_COLOR,
    LIGHT_THEME_COLOR,
    MANIFEST_ICONS,
    SERVICE_WORKER_SOURCE,
)
from assetboard.web.rendering import render_template, scoped_url, static_url

router = APIRouter()


@router.get("/manifest.webmanifest", name="manifest")
async def manifest(request: Request) -> Response:
    """The web app manifest. Its URLs carry the ASGI root path, like every other app URL."""
    root = scoped_url(request, "home")
    icons = [
        {
            "src": static_url(request, f"img/{file_name}"),
            "sizes": size,
            "type": "image/png",
            "purpose": purpose,
        }
        for file_name, size, purpose in MANIFEST_ICONS
    ]
    payload = {
        "id": root,
        "name": APP_NAME,
        "short_name": APP_NAME,
        "lang": request.state.locale,
        "start_url": root,
        "scope": root,
        "display": "standalone",
        "theme_color": LIGHT_THEME_COLOR,
        "background_color": LIGHT_BACKGROUND_COLOR,
        # The manifest colours above are the light theme; this draft member (W3C app manifest,
        # 2026-10) carries the dark tokens for browsers that honour it. The theme-color meta
        # tags in the page heads cover today's browsers.
        "color_scheme_dark": {
            "theme_color": DARK_THEME_COLOR,
            "background_color": DARK_BACKGROUND_COLOR,
        },
        "icons": icons,
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return Response(body, media_type="application/manifest+json")


@lru_cache(maxsize=1)
def _service_worker_script() -> str:
    return SERVICE_WORKER_SOURCE.read_text(encoding="utf-8")


@router.get("/service-worker.js", name="service_worker")
async def service_worker(request: Request) -> Response:
    """Serve the worker from the app root so its default scope is the app root path.

    The script reads the app version from its own ``?v=`` query (set by ``app.js``); the
    browser then fetches a new worker and rebuilds the cache for every release.
    """
    root_path = request.scope.get("root_path", "").rstrip("/")
    return Response(
        _service_worker_script(),
        media_type="text/javascript",
        headers={"Service-Worker-Allowed": f"{root_path}/"},
    )


@router.get("/offline", name="offline")
async def offline(request: Request) -> HTMLResponse:
    """The one cached HTML page: translated, with no workspace data and no forms."""
    return render_template(request, "offline.html")
