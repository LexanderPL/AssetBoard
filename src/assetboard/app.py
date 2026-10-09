from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from importlib.metadata import version
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from assetboard.branding import APP_NAME
from assetboard.config import Settings
from assetboard.db.engine import create_database_engine, database_is_ready
from assetboard.db.migrate import upgrade_database
from assetboard.i18n import gettext
from assetboard.logging import configure_logging
from assetboard.middleware import (
    LocaleMiddleware,
    RequestIdMiddleware,
    SecurityHeadersMiddleware,
    add_security_headers,
)
from assetboard.web.rendering import render_template
from assetboard.web.routes import router as web_router

DatabaseChecker = Callable[[], bool]


def create_app(
    settings: Settings | None = None,
    *,
    database_checker: DatabaseChecker | None = None,
) -> FastAPI:
    settings = settings or Settings()
    logger = configure_logging(settings.log_level, settings.log_format)
    settings.ensure_secret_key()
    database_url = settings.database_url
    if database_url is None:
        raise ValueError("DATABASE_URL was not configured")
    engine = create_database_engine(database_url)
    checker = database_checker or (lambda: database_is_ready(engine))

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            if settings.auto_migrate:
                upgrade_database(settings)
            yield
        finally:
            engine.dispose()

    app = FastAPI(title=APP_NAME, version=version("assetboard"), lifespan=lifespan)
    app.state.settings = settings
    app.state.app_name = APP_NAME
    app.state.logger = logger
    app.state.database_engine = engine
    app.state.database_checker = checker
    app.add_middleware(RequestIdMiddleware, logger=logger)
    app.add_middleware(LocaleMiddleware, default_locale=settings.default_locale)
    app.add_middleware(SecurityHeadersMiddleware)
    static_directory = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_directory), name="static")
    app.include_router(web_router)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException) -> Any:
        if exc.status_code not in {403, 404}:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        locale = request.state.locale
        if exc.status_code == 403:
            title = gettext("Access denied", locale)
            message = gettext("You do not have permission to view this page.", locale)
        else:
            title = gettext("Page not found", locale)
            message = gettext("The page you requested could not be found.", locale)
        template_name = (
            "errors/fragment.html" if request.headers.get("hx-request") else "errors/page.html"
        )
        return render_template(
            request,
            template_name,
            context={
                "app_name": APP_NAME,
                "title": title,
                "message": message,
                "status_code": exc.status_code,
            },
            status_code=exc.status_code,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> Any:
        request_id = request.state.request_id
        logger.error(
            "unhandled_request_error request_id=%s exception_type=%s",
            request_id,
            type(exc).__name__,
        )
        locale = request.state.locale
        title = gettext("Something went wrong", locale)
        message = gettext("An unexpected error occurred. Please try again.", locale)
        template_name = (
            "errors/fragment.html" if request.headers.get("hx-request") else "errors/page.html"
        )
        response = render_template(
            request,
            template_name,
            context={
                "app_name": APP_NAME,
                "title": title,
                "message": message,
                "status_code": 500,
            },
            status_code=500,
        )
        add_security_headers(response)
        return response

    @app.get("/healthz")
    async def healthz(request: Request) -> Any:
        return {"status": "ok", "request_id": request.state.request_id}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        if await run_in_threadpool(checker):
            return JSONResponse({"status": "ok", "database": "ready"})
        return JSONResponse(
            {"status": "unavailable", "database": "unavailable"},
            status_code=503,
        )

    return app
