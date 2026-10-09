from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from alembic import command
from alembic.config import Config

from assetboard.config import Settings


def alembic_config(settings: Settings) -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    config.set_main_option("path_separator", "os")
    config.attributes["settings"] = settings
    return config


def configured_database_url(config: Config) -> str:
    settings = config.attributes["settings"]
    database_url = settings.database_url
    if database_url is None:
        raise ValueError("DATABASE_URL was not configured")
    return database_url


def upgrade_database(settings: Settings) -> None:
    command.upgrade(alembic_config(settings), "head")


def check_database_migrations(settings: Settings) -> None:
    command.check(alembic_config(settings))


def create_revision(settings: Settings, message: str) -> None:
    with TemporaryDirectory(prefix="assetboard-revision-") as temporary_directory:
        temporary_path = Path(temporary_directory)
        scratch_settings = settings.model_copy(
            update={
                "data_dir": temporary_path,
                "database_url": f"sqlite:///{temporary_path / 'revision.db'}",
            }
        )
        upgrade_database(scratch_settings)
        command.revision(alembic_config(scratch_settings), message=message, autogenerate=True)
