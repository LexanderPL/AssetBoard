from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from assetboard.app import create_app
from assetboard.branding import CLI_NAME, ENV_PREFIX
from assetboard.cli import app as cli_app
from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.logging import JsonFormatter

runner = CliRunner()


def test_settings_parse_and_persist_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv(f"{ENV_PREFIX}_ENV", "dev")
    monkeypatch.setenv(f"{ENV_PREFIX}_DATA_DIR", str(data_dir))
    monkeypatch.setenv(f"{ENV_PREFIX}_BASE_URL", "https://assetboard.example")
    monkeypatch.setenv(f"{ENV_PREFIX}_DEFAULT_LOCALE", "pl")
    monkeypatch.setenv(f"{ENV_PREFIX}_DEFAULT_CURRENCY", "PLN")
    monkeypatch.delenv(f"{ENV_PREFIX}_SECRET_KEY", raising=False)

    settings = Settings()

    assert settings.env == "dev"
    assert settings.data_dir == data_dir
    assert settings.database_url == f"sqlite:///{data_dir / 'assetboard.db'}"
    assert settings.base_url == "https://assetboard.example"
    assert settings.default_locale == "pl"
    assert settings.default_currency == "PLN"
    assert settings.auto_migrate is True
    assert settings.secret_key
    assert (data_dir / "secret_key").read_text(encoding="utf-8") == settings.secret_key


def test_settings_reject_invalid_values() -> None:
    with pytest.raises(ValueError, match="ENV"):
        Settings(env="staging")

    with pytest.raises(ValueError, match="LOG_LEVEL"):
        Settings(log_level="verbose")

    with pytest.raises(ValueError, match="LOG_FORMAT"):
        Settings(log_format="yaml")

    with pytest.raises(ValueError, match="DEFAULT_LOCALE"):
        Settings(default_locale="de_DE")

    with pytest.raises(ValueError, match="DEFAULT_CURRENCY"):
        Settings(default_currency="US$")

    with pytest.raises(ValueError, match="TRUSTED_PROXIES"):
        Settings(trusted_proxies=["not-a-cidr"])


def test_settings_require_database_url_on_non_sqlite() -> None:
    with pytest.raises(ValueError, match="DATABASE_URL"):
        Settings(database_url="postgresql://example.com/app")


def test_clock_now_and_today() -> None:
    clock = FixedClock("2026-10-08T12:34:56+00:00")

    assert clock.now().isoformat() == "2026-10-08T12:34:56+00:00"
    assert clock.today("Europe/Warsaw").isoformat() == "2026-10-08"


def test_request_id_middleware_generates_and_propagates_id() -> None:
    app = create_app(Settings(), database_checker=lambda: True)

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.headers["x-request-id"]
    assert response.headers["x-request-id"] == response.json()["request_id"]


def test_health_endpoints_report_process_and_database() -> None:
    app = create_app(Settings(), database_checker=lambda: True)

    with TestClient(app) as client:
        health = client.get("/healthz")
        ready = client.get("/readyz")

    assert health.status_code == 200
    assert health.json() == {"status": "ok", "request_id": health.headers["x-request-id"]}
    assert ready.status_code == 200
    assert ready.json() == {"status": "ok", "database": "ready"}


def test_readyz_reports_database_failure() -> None:
    app = create_app(Settings(), database_checker=lambda: False)

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "database": "unavailable"}


def test_cli_serve_and_migrate_commands() -> None:
    serve = runner.invoke(cli_app, ["serve", "--help"])
    migrate = runner.invoke(cli_app, ["migrate", "--help"])

    assert serve.exit_code == 0
    assert f"{CLI_NAME} serve" in serve.stdout
    assert migrate.exit_code == 0
    assert f"{CLI_NAME} migrate" in migrate.stdout


def test_cli_migrate_runs_without_database_models() -> None:
    result = runner.invoke(cli_app, ["migrate"], env={"ASSETBOARD_DATA_DIR": ".tmp-assetboard"})

    assert result.exit_code == 0
    assert "Migration check complete" in result.stdout


def test_logging_uses_structured_json() -> None:
    settings = Settings(log_format="json")
    app = create_app(settings, database_checker=lambda: True)

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    logger = app.state.logger
    assert logger is not None
    assert isinstance(logger.handlers[0].formatter, JsonFormatter)
