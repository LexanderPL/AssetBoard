"""``assetboard restore``: checks, refusal cases and a round trip (F9.4)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from assetboard import cli
from assetboard.branding import ENV_PREFIX
from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.db.engine import create_database_engine
from assetboard.services.backups import create_backup


def _env(data_dir: Path) -> dict[str, str]:
    return {f"{ENV_PREFIX}_DATA_DIR": str(data_dir)}


def _prepare(tmp_path: Path) -> tuple[dict[str, str], Settings, Path]:
    data_dir = tmp_path / "restore-data"
    env = _env(data_dir)
    runner = CliRunner()
    migrated = runner.invoke(cli.app, ["migrate"], env=env)
    assert migrated.exit_code == 0, migrated.output
    created = runner.invoke(
        cli.app,
        [
            "create-admin",
            "--email",
            "restore@example.com",
            "--display-name",
            "Restore Test",
            "--password-stdin",
        ],
        input="A Strong Restore Password\n",
        env=env,
    )
    assert created.exit_code == 0, created.output
    settings = Settings(_env_file=None, data_dir=data_dir)
    engine = create_database_engine(settings.database_url or "")
    clock = FixedClock("2026-03-01T12:00:00+00:00")
    factory = sessionmaker(engine, class_=Session, info={"clock": clock}, expire_on_commit=False)
    try:
        backup = create_backup(factory, settings, clock, origin="manual")
    finally:
        engine.dispose()
    return env, settings, backup.path


def _display_name(database: Path) -> str:
    connection = sqlite3.connect(database)
    try:
        return connection.execute(
            "SELECT display_name FROM users WHERE email = 'restore@example.com'"
        ).fetchone()[0]
    finally:
        connection.close()


def test_restore_round_trip_creates_a_pre_restore_copy(tmp_path: Path) -> None:
    env, settings, backup = _prepare(tmp_path)
    database = settings.data_dir / "assetboard.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE users SET display_name = 'Changed' WHERE email = 'restore@example.com'"
        )
        connection.commit()
    finally:
        connection.close()
    result = CliRunner().invoke(cli.app, ["restore", str(backup)], env=env)
    assert result.exit_code == 0, result.output
    assert "Restored" in result.output
    assert "Pre-restore backup:" in result.output
    assert _display_name(database) == "Restore Test"
    pre_restore = list((settings.data_dir / "backups").glob("assetboard-pre-restore-*.db"))
    assert len(pre_restore) == 1
    assert _display_name(pre_restore[0]) == "Changed"


def test_restore_refuses_a_corrupt_file(tmp_path: Path) -> None:
    env, _settings, _backup = _prepare(tmp_path)
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not a database")
    result = CliRunner().invoke(cli.app, ["restore", str(corrupt)], env=env)
    assert result.exit_code == 1
    assert "integrity check" in result.output


def test_restore_refuses_a_newer_schema(tmp_path: Path) -> None:
    env, _settings, _backup = _prepare(tmp_path)
    future = tmp_path / "future.db"
    with sqlite3.connect(future) as connection:
        connection.executescript(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL);"
            "CREATE TABLE users (id TEXT);"
            "CREATE TABLE workspaces (id TEXT);"
            "INSERT INTO alembic_version VALUES ('9999_from_the_future');"
        )
    result = CliRunner().invoke(cli.app, ["restore", str(future)], env=env)
    assert result.exit_code == 1
    assert "newer version" in result.output


def test_restore_refuses_postgresql_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, _settings, backup = _prepare(tmp_path)

    def postgres_settings() -> Settings:
        return Settings.model_construct(
            data_dir=tmp_path / "postgres-data",
            database_url="postgresql://user:secret@db/assetboard",
        )

    monkeypatch.setattr(cli, "Settings", postgres_settings)
    result = CliRunner().invoke(cli.app, ["restore", str(backup)], env=env)
    assert result.exit_code == 1
    assert "SQLite" in result.output
    assert "pg_restore" in result.output
    assert "secret" not in result.output  # never echo the database URL


def test_restore_refuses_the_default_relative_data_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(f"{ENV_PREFIX}_DATA_DIR", raising=False)
    monkeypatch.delenv(f"{ENV_PREFIX}_DATABASE_URL", raising=False)
    backup = tmp_path / "backup.db"
    backup.write_bytes(b"not used")
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.app, ["restore", str(backup)])
    assert result.exit_code == 1
    assert "--data-dir" in result.output
    assert not (tmp_path / "data").exists()


def test_restore_accepts_an_explicit_data_dir(tmp_path: Path) -> None:
    _env, settings, backup = _prepare(tmp_path)
    database = settings.data_dir / "assetboard.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute("UPDATE users SET display_name = 'Changed'")
        connection.commit()
    finally:
        connection.close()
    result = CliRunner().invoke(
        cli.app,
        ["restore", "--data-dir", str(settings.data_dir), str(backup)],
    )
    assert result.exit_code == 0, result.output
    assert _display_name(database) == "Restore Test"
