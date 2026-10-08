from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect

from assetboard.db.engine import create_database_engine
from assetboard.db.migrations import check_database_migrations, upgrade_database


def test_alembic_upgrade_and_check_on_empty_sqlite_database(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'empty.db'}"

    upgrade_database(database_url)
    check_database_migrations(database_url)

    engine = create_database_engine(database_url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert "alembic_version" in tables
    assert tables == {"alembic_version"}
