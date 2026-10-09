from __future__ import annotations

import asyncio
from importlib.metadata import version
from pathlib import Path

from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import HTTPException
from fastapi.testclient import TestClient

from assetboard.app import create_app
from assetboard.config import Settings
from assetboard.db.migrate import alembic_config


def test_app_factory_ensures_secret_key_after_setup() -> None:
    settings = Settings(_env_file=None)
    assert settings.secret_key is None

    app = create_app(settings, database_checker=lambda: True)

    assert settings.secret_key
    assert (settings.data_dir / "secret_key").read_text(encoding="utf-8") == settings.secret_key
    assert app.state.settings is settings
    assert app.version == version("assetboard")


def test_app_runs_migrations_during_lifespan_not_factory(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path, secret_key="test-secret")
    app = create_app(settings)
    engine = app.state.database_engine

    with engine.connect() as connection:
        tables = (
            connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")
            .scalars()
            .all()
        )
    assert "alembic_version" not in tables

    with TestClient(app), engine.connect() as connection:
        version_rows = (
            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalars().all()
        )
        current_heads = MigrationContext.configure(connection).get_current_heads()

    assert current_heads == tuple(ScriptDirectory.from_config(alembic_config(settings)).get_heads())
    assert version_rows == list(current_heads)


def test_health_endpoints_report_process_and_database() -> None:
    app = create_app(
        Settings(_env_file=None, secret_key="test-secret"),
        database_checker=lambda: True,
    )

    with TestClient(app) as client:
        health = client.get("/healthz")
        ready = client.get("/readyz")

    assert health.status_code == 200
    assert health.json() == {"status": "ok", "request_id": health.headers["x-request-id"]}
    assert ready.status_code == 200
    assert ready.json() == {"status": "ok", "database": "ready"}


def test_readyz_reports_database_failure() -> None:
    app = create_app(
        Settings(_env_file=None, secret_key="test-secret"),
        database_checker=lambda: False,
    )

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "database": "unavailable"}


def test_readyz_checks_the_real_database(tmp_path: Path) -> None:
    app = create_app(Settings(_env_file=None, data_dir=tmp_path, secret_key="test-secret"))

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ready"}


def test_readyz_runs_synchronous_checker_in_threadpool() -> None:
    def check_not_on_event_loop() -> bool:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return True
        return False

    app = create_app(
        Settings(_env_file=None, secret_key="test-secret", auto_migrate=False),
        database_checker=check_not_on_event_loop,
    )

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 200


def test_root_renders_translatable_shell_and_local_assets() -> None:
    app = create_app(
        Settings(_env_file=None, secret_key="test-secret", auto_migrate=False),
    )

    with TestClient(app) as client:
        response = client.get("/", headers={"accept-language": "pl-PL, en;q=0.8"})
        htmx = client.get("/static/vendor/htmx.min.js")
        alpine = client.get("/static/vendor/alpinejs-csp.min.js")
        charts = client.get("/static/vendor/echarts.min.js")

    assert response.status_code == 200
    assert '<html lang="pl" class="h-full">' in response.text
    assert "Podsumowanie miesiąca" in response.text
    assert "cdn." not in response.text
    assert "unsafe-inline" not in response.headers["content-security-policy"]
    assert "unsafe-eval" not in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert all(asset.status_code == 200 for asset in (htmx, alpine, charts))


def test_error_pages_are_htmx_aware_and_have_security_headers() -> None:
    app = create_app(Settings(_env_file=None, secret_key="test-secret", auto_migrate=False))

    @app.get("/_test/forbidden")
    async def forbidden() -> None:
        raise HTTPException(status_code=403)

    @app.get("/_test/error")
    async def unexpected_error() -> None:
        raise RuntimeError("Synthetic failure")

    with TestClient(app, raise_server_exceptions=False) as client:
        full_page = client.get("/_test/forbidden")
        partial = client.get("/missing", headers={"HX-Request": "true"})
        server_error = client.get("/_test/error")

    assert full_page.status_code == 403
    assert "Page not found" not in full_page.text
    assert "<!doctype html>" in full_page.text.lower()
    assert partial.status_code == 404
    assert '<div role="alert"' in partial.text
    assert "<!doctype html>" not in partial.text.lower()
    assert server_error.status_code == 500
    assert server_error.headers["content-security-policy"]
