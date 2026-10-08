from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config

from assetboard.config import Settings


def alembic_config(database_url: str | None = None) -> Config:
    project_root = Path(__file__).resolve().parents[3]
    config = Config(str(project_root / "alembic.ini"))
    if database_url is not None:
        config.attributes["database_url"] = database_url
    return config


def configured_database_url(config: Config) -> str:
    override: Any = config.attributes.get("database_url")
    if override is not None:
        return str(override)
    database_url = Settings(_env_file=None).database_url
    if database_url is None:
        raise ValueError("DATABASE_URL was not configured")
    return database_url


def upgrade_database(database_url: str | None = None) -> None:
    command.upgrade(alembic_config(database_url), "head")


def check_database_migrations(database_url: str | None = None) -> None:
    command.check(alembic_config(database_url))
