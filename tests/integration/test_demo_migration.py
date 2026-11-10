"""Migration 0050 (demo mode) upgrades, enforces its partial indexes and downgrades."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from assetboard.config import Settings
from assetboard.db.engine import create_database_engine
from assetboard.db.migrate import alembic_config, check_database_migrations, upgrade_database

STAMP = "'2026-11-10 00:00:00'"


def _insert_user(connection, *, email: str, is_demo: int = 0) -> str:
    user_id = email.split("@")[0].ljust(32, "0")[:32]
    connection.exec_driver_sql(
        "INSERT INTO users (id, email, display_name, theme, is_active, is_instance_admin, "
        "must_change_password, highlight_large_amounts, life_expectancy, is_demo, created_at, "
        f"updated_at) VALUES (?, ?, 'Demo', 'system', 1, 0, 0, 1, 88, ?, {STAMP}, {STAMP})",
        (user_id, email, is_demo),
    )
    return user_id


def _insert_workspace(connection, *, name: str, is_demo: int = 0) -> str:
    workspace_id = name.lower().ljust(32, "0")[:32]
    connection.exec_driver_sql(
        "INSERT INTO workspaces (id, name, base_currency, locale, timezone, is_demo, "
        f"created_at, updated_at) VALUES (?, ?, 'EUR', 'en', 'UTC', ?, {STAMP}, {STAMP})",
        (workspace_id, name, is_demo),
    )
    return workspace_id


def test_0050_demo_mode_is_reversible_and_has_the_demo_flags(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'demo.db'}",
    )
    upgrade_database(settings)
    engine = create_database_engine(settings.database_url or "")
    try:
        inspector = inspect(engine)
        assert {"is_demo"} <= {column["name"] for column in inspector.get_columns("users")}
        assert {"is_demo"} <= {column["name"] for column in inspector.get_columns("workspaces")}
        settings_columns = {column["name"] for column in inspector.get_columns("instance_settings")}
        assert {
            "demo_last_reset_at",
            "demo_last_reset_error",
            "demo_dataset_version",
        } <= settings_columns
        with engine.connect() as connection:
            indexes = {
                row[0]: row[1]
                for row in connection.exec_driver_sql(
                    "SELECT name, sql FROM sqlite_master WHERE type = 'index' "
                    "AND name IN ('uq_users_is_demo', 'uq_workspaces_is_demo')"
                )
            }
            check = connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'user_sessions'"
            ).scalar_one()
            assert "demo" in str(check)
        assert set(indexes) == {"uq_users_is_demo", "uq_workspaces_is_demo"}
        assert "WHERE is_demo = 1" in str(indexes["uq_users_is_demo"])
        assert "WHERE is_demo = 1" in str(indexes["uq_workspaces_is_demo"])

        with engine.begin() as connection:
            user_id = _insert_user(connection, email="demo@assetboard.invalid", is_demo=1)
            workspace_id = _insert_workspace(connection, name="Demo", is_demo=1)
            connection.exec_driver_sql(
                "INSERT INTO user_sessions (id_hash, user_id, created_at, last_seen_at, "
                "expires_at, auth_method) VALUES (?, ?, '2026-11-10 00:00:00', "
                "'2026-11-10 00:00:00', '2027-01-10 00:00:00', 'demo')",
                ("a" * 64, user_id),
            )
        with pytest.raises(IntegrityError), engine.begin() as connection:
            _insert_user(connection, email="second@example.com", is_demo=1)
        with pytest.raises(IntegrityError), engine.begin() as connection:
            _insert_workspace(connection, name="Second", is_demo=1)

        command.downgrade(alembic_config(settings), "0049_auto_link_portfolio")
        with engine.connect() as connection:
            assert "is_demo" not in {
                column["name"] for column in inspect(connection).get_columns("users")
            }
            assert "is_demo" not in {
                column["name"] for column in inspect(connection).get_columns("workspaces")
            }
            assert "demo_last_reset_at" not in {
                column["name"] for column in inspect(connection).get_columns("instance_settings")
            }
            sessions = connection.exec_driver_sql(
                "SELECT count(*) FROM user_sessions WHERE auth_method = 'demo'"
            ).scalar_one()
            assert sessions == 0
            assert connection.exec_driver_sql("SELECT count(*) FROM users").scalar_one() == 1
            assert connection.exec_driver_sql("SELECT count(*) FROM workspaces").scalar_one() == 1
        assert workspace_id and user_id

        upgrade_database(settings)
        check_database_migrations(settings)
    finally:
        engine.dispose()
