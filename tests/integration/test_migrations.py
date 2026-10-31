from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, inspect
from sqlalchemy.exc import IntegrityError

from assetboard.config import Settings
from assetboard.db.engine import create_database_engine
from assetboard.db.migrate import (
    alembic_config,
    check_database_migrations,
    create_revision,
    database_is_at_head,
    upgrade_database,
)


def test_alembic_upgrade_and_check_on_empty_sqlite_database(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'empty.db'}"
    settings = Settings(_env_file=None, data_dir=tmp_path, database_url=database_url)

    upgrade_database(settings)
    check_database_migrations(settings)

    engine = create_database_engine(database_url)
    try:
        tables = set(inspect(engine).get_table_names())
        with engine.connect() as connection:
            current_heads = MigrationContext.configure(connection).get_current_heads()
            setup_rows = connection.exec_driver_sql("SELECT id FROM setup_state").scalars().all()
    finally:
        engine.dispose()

    assert tables == {
        "alembic_version",
        "audit_events",
        "auth_throttles",
        "budget_lines",
        "categories",
        "email_tokens",
        "external_identities",
        "income_source_tax_rules",
        "income_sources",
        "instance_settings",
        "invitations",
        "login_tickets",
        "memberships",
        "month_states",
        "notification_channels",
        "notification_log",
        "setup_state",
        "transactions",
        "scheduled_items",
        "scheduled_occurrences",
        "totp_factors",
        "totp_recovery_codes",
        "tax_rules",
        "tax_rule_versions",
        "user_sessions",
        "users",
        "workspaces",
        "work_logs",
        "work_days",
    }
    assert setup_rows == [1]
    assert current_heads == tuple(ScriptDirectory.from_config(alembic_config(settings)).get_heads())
    config = alembic_config(settings)
    script_location = config.get_main_option("script_location")
    assert script_location is not None
    assert Path(script_location).is_dir()
    assert config.get_main_option("sqlalchemy.url") is None


def test_0026_two_step_login_ticket_migration_is_reversible(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'tickets.db'}"
    )
    upgrade_database(settings)
    engine = create_database_engine(settings.database_url or "")
    try:
        assert "login_tickets" in inspect(engine).get_table_names()
        command.downgrade(alembic_config(settings), "0025_amount_shading")
        assert "login_tickets" not in inspect(engine).get_table_names()
        upgrade_database(settings)
        assert "login_tickets" in inspect(engine).get_table_names()
        check_database_migrations(settings)
    finally:
        engine.dispose()


def test_0018_tax_rules_migration_is_reversible(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'tax.db'}"
    )
    upgrade_database(settings)
    engine = create_database_engine(settings.database_url or "")
    try:
        assert {"tax_rules", "tax_rule_versions"} <= set(inspect(engine).get_table_names())
        command.downgrade(alembic_config(settings), "0017_f32")
        assert not {"tax_rules", "tax_rule_versions"} & set(inspect(engine).get_table_names())
        upgrade_database(settings)
        assert {"tax_rules", "tax_rule_versions"} <= set(inspect(engine).get_table_names())
        check_database_migrations(settings)
    finally:
        engine.dispose()


def test_0020_income_sources_migration_is_reversible(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'income.db'}"
    )
    upgrade_database(settings)
    engine = create_database_engine(settings.database_url or "")
    try:
        inspector = inspect(engine)
        assert {"income_sources", "income_source_tax_rules", "work_logs"} <= set(
            inspector.get_table_names()
        )
        assert {
            "income_source_id",
            "work_month",
            "parent_occurrence_id",
            "income_source_tax_rule_id",
        } <= {column["name"] for column in inspector.get_columns("scheduled_occurrences")}
        command.downgrade(alembic_config(settings), "0019_f32_prev_txn")
        assert not {
            "income_sources",
            "income_source_tax_rules",
            "work_logs",
        } & set(inspect(engine).get_table_names())
        assert "income_source_id" not in {
            column["name"] for column in inspect(engine).get_columns("scheduled_occurrences")
        }
        upgrade_database(settings)
        check_database_migrations(settings)
    finally:
        engine.dispose()


def test_0021_notifications_migration_is_reversible(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'notify.db'}"
    )
    upgrade_database(settings)
    engine = create_database_engine(settings.database_url or "")
    try:
        inspector = inspect(engine)
        assert {"notification_channels", "notification_log"} <= set(inspector.get_table_names())
        assert "reminder_lead_days" in {
            column["name"] for column in inspector.get_columns("scheduled_items")
        }
        unique = {
            tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("notification_log")
        }
        assert ("channel_id", "kind", "subject_key", "sent_on") in unique
        command.downgrade(alembic_config(settings), "0020_f34")
        assert not {"notification_channels", "notification_log"} & set(
            inspect(engine).get_table_names()
        )
        assert "reminder_lead_days" not in {
            column["name"] for column in inspect(engine).get_columns("scheduled_items")
        }
        upgrade_database(settings)
        check_database_migrations(settings)
    finally:
        engine.dispose()


def test_0020_downgrade_drops_income_occurrences(tmp_path: Path) -> None:
    from datetime import date

    from sqlalchemy.orm import Session

    from assetboard.clock import FixedClock
    from assetboard.db.models.identity import Workspace
    from assetboard.db.models.scheduled import ScheduledOccurrence
    from tests.tenant.factories import make_income_source

    settings = Settings(
        _env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'data.db'}"
    )
    upgrade_database(settings)
    engine = create_database_engine(settings.database_url or "")
    try:
        clock = FixedClock("2026-01-01T00:00:00+00:00")
        with Session(engine, info={"clock": clock}) as session, session.begin():
            workspace = Workspace(name="W", base_currency="EUR", locale="en", timezone="UTC")
            session.add(workspace)
            session.flush()
            source = make_income_source(session, workspace.id)
            session.add(source)
            session.flush()
            session.add(
                ScheduledOccurrence(
                    workspace_id=workspace.id,
                    income_source_id=source.id,
                    work_month=date(2026, 1, 1),
                    direction="income",
                    category_id=source.category_id,
                    nominal_date=date(2026, 2, 10),
                    due_date=date(2026, 2, 10),
                    expected_minor=100,
                    currency="EUR",
                    amount_state="estimated",
                    generated_by="income",
                    label="Salary",
                )
            )
        command.downgrade(alembic_config(settings), "0019_f32_prev_txn")
        with engine.connect() as connection:
            count = connection.exec_driver_sql("SELECT count(*) FROM scheduled_occurrences")
            assert count.scalar_one() == 0
        upgrade_database(settings)
    finally:
        engine.dispose()


def test_create_revision_migrates_a_temporary_database_first(tmp_path, monkeypatch) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path)
    calls: list[tuple[str, str]] = []

    def fake_upgrade(scratch_settings: Settings) -> None:
        assert scratch_settings.database_url is not None
        calls.append(("upgrade", scratch_settings.database_url))

    def fake_revision(config, *, message: str, autogenerate: bool) -> None:
        scratch_settings = config.attributes["settings"]
        assert scratch_settings.database_url is not None
        assert autogenerate
        calls.append(("revision", scratch_settings.database_url))

    monkeypatch.setattr("assetboard.db.migrate.upgrade_database", fake_upgrade)
    monkeypatch.setattr(command, "revision", fake_revision)
    original_database_url = settings.database_url

    create_revision(settings, "scratch revision")

    assert [call[0] for call in calls] == ["upgrade", "revision"]
    assert calls[0][1] == calls[1][1]
    assert calls[0][1] != original_database_url
    assert not Path(calls[0][1].removeprefix("sqlite:///")).parent.exists()


def test_identity_migration_is_reversible_and_head_detection_is_read_only(tmp_path: Path) -> None:
    database_path = tmp_path / "reversible.db"
    settings = Settings(
        _env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{database_path}"
    )
    assert not database_is_at_head(settings)
    assert not database_path.exists()

    upgrade_database(settings)
    assert database_is_at_head(settings)
    engine = create_database_engine(f"sqlite:///{database_path}")
    try:
        unique = inspect(engine).get_unique_constraints("users")
        assert {"name": "uq_users_email_normalized", "column_names": ["email"]} in [
            {"name": item["name"], "column_names": item["column_names"]} for item in unique
        ]

        command.downgrade(alembic_config(settings), "0001_f03")
        assert set(inspect(engine).get_table_names()) == {"alembic_version"}
        assert not database_is_at_head(settings)

        upgrade_database(settings)
        assert database_is_at_head(settings)
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT count(*) FROM setup_state").scalar_one() == 1
    finally:
        engine.dispose()


def test_0003_round_trip(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'f12.db'}"
    )
    upgrade_database(settings)
    script = ScriptDirectory.from_config(alembic_config(settings))
    revision = script.get_revision("0003_f12")
    assert revision is not None and revision.down_revision == "0002_f11"
    # Reachable from every head, so later migrations keep it in the chain.
    for head in script.get_heads():
        assert "0003_f12" in {rev.revision for rev in script.iterate_revisions(head, "base")}
    engine = create_database_engine(settings.database_url or "")
    try:
        columns = {column["name"] for column in inspect(engine).get_columns("user_sessions")}
        assert "current_workspace_id" in columns
        command.downgrade(alembic_config(settings), "0002_f11")
        columns = {column["name"] for column in inspect(engine).get_columns("user_sessions")}
        assert "current_workspace_id" not in columns
        assert "ix_user_sessions_user_expires" in {
            index["name"] for index in inspect(engine).get_indexes("user_sessions")
        }
        upgrade_database(settings)
        assert database_is_at_head(settings)
        check_database_migrations(settings)
    finally:
        engine.dispose()


def test_session_workspace_is_nulled_on_workspace_delete(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'fk.db'}"
    )
    upgrade_database(settings)
    engine = create_database_engine(settings.database_url or "")
    workspace, user = "a" * 32, "b" * 32
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO workspaces (id, name, base_currency, locale, timezone, created_at, "
                f"updated_at) VALUES ('{workspace}', 'W', 'EUR', 'en', 'UTC', "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
            connection.exec_driver_sql(
                "INSERT INTO users (id, email, display_name, locale, theme, is_active, "
                f"is_instance_admin, created_at, updated_at) VALUES ('{user}', 'u@example.com', "
                "'U', 'en', 'system', 1, 0, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
            connection.exec_driver_sql(
                "INSERT INTO user_sessions (id_hash, user_id, created_at, last_seen_at, "
                f"expires_at, current_workspace_id) VALUES ('{'c' * 64}', '{user}', "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00', '2026-02-01 00:00:00', "
                f"'{workspace}')"
            )
            connection.exec_driver_sql(f"DELETE FROM workspaces WHERE id = '{workspace}'")
            selected = connection.exec_driver_sql(
                "SELECT current_workspace_id FROM user_sessions"
            ).scalar_one()
        assert selected is None
    finally:
        engine.dispose()


def test_0004_round_trip(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'f12b.db'}"
    )
    upgrade_database(settings)
    script = ScriptDirectory.from_config(alembic_config(settings))
    revision = script.get_revision("0004_f12")
    assert revision is not None and revision.down_revision == "0003_f12"
    for head in script.get_heads():
        assert "0004_f12" in {rev.revision for rev in script.iterate_revisions(head, "base")}
    engine = create_database_engine(settings.database_url or "")
    try:
        inspector = inspect(engine)
        assert "invitations" in inspector.get_table_names()
        unique = {item["name"] for item in inspector.get_unique_constraints("invitations")}
        assert "uq_invitations_token_hash" in unique
        command.downgrade(alembic_config(settings), "0003_f12")
        assert "invitations" not in inspect(engine).get_table_names()
        upgrade_database(settings)
        assert database_is_at_head(settings)
        check_database_migrations(settings)
    finally:
        engine.dispose()


def test_0005_round_trip_keeps_sessions_as_password_logins(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'f13.db'}"
    )
    upgrade_database(settings)
    script = ScriptDirectory.from_config(alembic_config(settings))
    revision = script.get_revision("0005_f13")
    assert revision is not None and revision.down_revision == "0004_f12"
    engine = create_database_engine(settings.database_url or "")
    try:
        unique = {
            item["name"] for item in inspect(engine).get_unique_constraints("external_identities")
        }
        assert "uq_external_identities_provider_subject" in unique
        command.downgrade(alembic_config(settings), "0004_f12")
        inspector = inspect(engine)
        assert "external_identities" not in inspector.get_table_names()
        # OIDC-only users need no schema change: the column was nullable from the start.
        password = {c["name"]: c for c in inspector.get_columns("users")}["password_hash"]
        assert password["nullable"] is True
        with engine.begin() as connection:
            user_id = "00000000000000000000000000000001"
            connection.exec_driver_sql(
                "INSERT INTO users (id, email, display_name, locale, theme, is_active, "
                "is_instance_admin, created_at, updated_at) VALUES (?, 'a@example.com', 'A', "
                "'en', 'system', 1, 0, '2026-01-01 00:00:00', '2026-01-01 00:00:00')",
                (user_id,),
            )
            connection.exec_driver_sql(
                "INSERT INTO user_sessions (id_hash, user_id, created_at, last_seen_at, "
                "expires_at) VALUES (?, ?, '2026-01-01 00:00:00', '2026-01-01 00:00:00', "
                "'2026-02-01 00:00:00')",
                ("a" * 64, user_id),
            )
        upgrade_database(settings)
        with engine.connect() as connection:
            methods = connection.exec_driver_sql("SELECT auth_method FROM user_sessions")
            assert methods.scalars().all() == ["password"]
        check_database_migrations(settings)
    finally:
        engine.dispose()


def test_0006_round_trip_allows_proxy_sessions_and_drops_them_on_downgrade(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'f14.db'}"
    )
    upgrade_database(settings)
    script = ScriptDirectory.from_config(alembic_config(settings))
    revision = script.get_revision("0006_f14")
    assert revision is not None and revision.down_revision == "0005_f13"
    engine = create_database_engine(settings.database_url or "")
    user_id = "00000000000000000000000000000001"

    def add_session(connection: Connection, id_hash: str, method: str) -> None:
        connection.exec_driver_sql(
            "INSERT INTO user_sessions (id_hash, user_id, created_at, last_seen_at, "
            "expires_at, auth_method) VALUES (?, ?, '2026-01-01 00:00:00', "
            "'2026-01-01 00:00:00', '2026-02-01 00:00:00', ?)",
            (id_hash, user_id, method),
        )

    def methods() -> list[str]:
        with engine.connect() as connection:
            rows = connection.exec_driver_sql(
                "SELECT auth_method FROM user_sessions ORDER BY auth_method"
            )
            return list(rows.scalars())

    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO users (id, email, display_name, locale, theme, is_active, "
                "is_instance_admin, created_at, updated_at) VALUES (?, 'a@example.com', 'A', "
                "'en', 'system', 1, 0, '2026-01-01 00:00:00', '2026-01-01 00:00:00')",
                (user_id,),
            )
            add_session(connection, "a" * 64, "password")
            add_session(connection, "b" * 64, "oidc")
            add_session(connection, "c" * 64, "proxy")
        with pytest.raises(IntegrityError), engine.begin() as connection:
            add_session(connection, "d" * 64, "header")
        assert methods() == ["oidc", "password", "proxy"]

        command.downgrade(alembic_config(settings), "0005_f13")
        # Proxy sessions do not fit the old constraint; the others survive.
        assert methods() == ["oidc", "password"]
        with pytest.raises(IntegrityError), engine.begin() as connection:
            add_session(connection, "c" * 64, "proxy")

        upgrade_database(settings)
        assert database_is_at_head(settings)
        check_database_migrations(settings)
    finally:
        engine.dispose()


def test_0007_round_trip_makes_the_user_locale_nullable_and_keeps_every_row(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'f15.db'}",
        default_locale="pl",
    )
    upgrade_database(settings)
    command.downgrade(alembic_config(settings), "0006_f14")
    engine = create_database_engine(settings.database_url or "")
    user_id, workspace_id = "00000000000000000000000000000001", "00000000000000000000000000000002"
    stamp = "'2026-01-01 00:00:00'"

    def counts() -> dict[str, int]:
        with engine.connect() as connection:
            return {
                table: connection.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one()
                for table in ("users", "user_sessions", "external_identities", "memberships")
            }

    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO users (id, email, display_name, locale, theme, is_active, "
                f"is_instance_admin, created_at, updated_at) VALUES (?, 'a@example.com', 'A', "
                f"'en', 'system', 1, 0, {stamp}, {stamp})",
                (user_id,),
            )
            connection.exec_driver_sql(
                "INSERT INTO workspaces (id, name, base_currency, locale, timezone, created_at, "
                f"updated_at) VALUES (?, 'Home', 'EUR', 'en', 'UTC', {stamp}, {stamp})",
                (workspace_id,),
            )
            connection.exec_driver_sql(
                "INSERT INTO memberships (id, workspace_id, user_id, role, created_at, "
                f"updated_at) VALUES ('00000000000000000000000000000003', ?, ?, 'owner', "
                f"{stamp}, {stamp})",
                (workspace_id, user_id),
            )
            connection.exec_driver_sql(
                "INSERT INTO user_sessions (id_hash, user_id, created_at, last_seen_at, "
                f"expires_at, auth_method) VALUES (?, ?, {stamp}, {stamp}, "
                "'2026-02-01 00:00:00', 'oidc')",
                ("a" * 64, user_id),
            )
            connection.exec_driver_sql(
                "INSERT INTO external_identities (id, user_id, provider, subject, created_at, "
                f"updated_at) VALUES ('00000000000000000000000000000004', ?, 'proxy', 'a', "
                f"{stamp}, {stamp})",
                (user_id,),
            )
        before = counts()
        assert before == {
            "users": 1,
            "user_sessions": 1,
            "external_identities": 1,
            "memberships": 1,
        }

        upgrade_database(settings)
        # Rebuilding users must not cascade into the rows that reference it.
        assert counts() == before
        columns = {c["name"]: c for c in inspect(engine).get_columns("users")}
        assert columns["locale"]["nullable"] is True
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT locale FROM users").scalar_one() is None
            # The workspace locale is untouched.
            assert connection.exec_driver_sql("SELECT locale FROM workspaces").scalar_one() == "en"

        command.downgrade(alembic_config(settings), "0006_f14")
        assert counts() == before
        columns = {c["name"]: c for c in inspect(engine).get_columns("users")}
        assert columns["locale"]["nullable"] is False
        with engine.connect() as connection:
            # NULL becomes DEFAULT_LOCALE on the way down.
            assert connection.exec_driver_sql("SELECT locale FROM users").scalar_one() == "pl"

        upgrade_database(settings)
        assert database_is_at_head(settings)
        check_database_migrations(settings)
    finally:
        engine.dispose()


def test_0008_round_trip_adds_must_change_password_and_keeps_every_row(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'f15b.db'}"
    )
    upgrade_database(settings)
    command.downgrade(alembic_config(settings), "0007_f15")
    engine = create_database_engine(settings.database_url or "")
    user_id = "00000000000000000000000000000001"
    stamp = "'2026-01-01 00:00:00'"

    def counts() -> tuple[int, int]:
        with engine.connect() as connection:
            users = connection.exec_driver_sql("SELECT count(*) FROM users").scalar_one()
            sessions = connection.exec_driver_sql("SELECT count(*) FROM user_sessions").scalar_one()
            return users, sessions

    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO users (id, email, display_name, theme, is_active, "
                f"is_instance_admin, created_at, updated_at) VALUES (?, 'a@example.com', 'A', "
                f"'system', 1, 0, {stamp}, {stamp})",
                (user_id,),
            )
            connection.exec_driver_sql(
                "INSERT INTO user_sessions (id_hash, user_id, created_at, last_seen_at, "
                f"expires_at, auth_method) VALUES (?, ?, {stamp}, {stamp}, "
                "'2026-02-01 00:00:00', 'password')",
                ("a" * 64, user_id),
            )
        upgrade_database(settings)
        assert counts() == (1, 1)
        columns = {c["name"]: c for c in inspect(engine).get_columns("users")}
        assert columns["must_change_password"]["nullable"] is False
        with engine.connect() as connection:
            flag = connection.exec_driver_sql("SELECT must_change_password FROM users")
            assert flag.scalar_one() == 0  # existing accounts: false

        command.downgrade(alembic_config(settings), "0007_f15")
        assert counts() == (1, 1)
        assert "must_change_password" not in {
            c["name"] for c in inspect(engine).get_columns("users")
        }
        upgrade_database(settings)
        assert database_is_at_head(settings)
        check_database_migrations(settings)
    finally:
        engine.dispose()


def test_0009_round_trip_creates_categories_with_a_partial_unique_name_index(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'f21.db'}"
    )
    upgrade_database(settings)
    engine = create_database_engine(settings.database_url or "")
    workspace_id = "00000000000000000000000000000001"
    stamp = "'2026-01-01 00:00:00'"

    def insert(connection: Connection, row_id: str, name: str, archived: bool = False) -> None:
        connection.exec_driver_sql(
            "INSERT INTO categories (id, workspace_id, name, name_key, kind, treatment, color, "
            f"sort_order, archived_at, created_at, updated_at) VALUES (?, ?, ?, ?, 'expense', "
            f"'regular', '#16a34a', 0, ?, {stamp}, {stamp})",
            (row_id, workspace_id, name, name.casefold(), stamp.strip("'") if archived else None),
        )

    try:
        with engine.connect() as connection:
            indexes = dict(
                connection.exec_driver_sql(
                    "SELECT name, sql FROM sqlite_master WHERE type = 'index' "
                    "AND tbl_name = 'categories'"
                ).all()
            )
        assert "WHERE archived_at IS NULL" in indexes["uq_categories_active_name"]
        assert "UNIQUE" in indexes["uq_categories_active_name"]
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO workspaces (id, name, base_currency, locale, timezone, created_at, "
                f"updated_at) VALUES (?, 'Home', 'EUR', 'en', 'UTC', {stamp}, {stamp})",
                (workspace_id,),
            )
            insert(connection, "0" * 31 + "a", "Żywność")
            insert(connection, "0" * 31 + "b", "ŻYWNOŚĆ", archived=True)  # archived: no clash
        with pytest.raises(IntegrityError), engine.begin() as connection:
            insert(connection, "0" * 31 + "c", "żywność")

        command.downgrade(alembic_config(settings), "0008_f15")
        assert "categories" not in inspect(engine).get_table_names()
        upgrade_database(settings)
        assert database_is_at_head(settings)
        check_database_migrations(settings)
    finally:
        engine.dispose()


def test_0010_round_trip_creates_month_states_unique_per_workspace_month(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'f25.db'}"
    )
    upgrade_database(settings)
    engine = create_database_engine(settings.database_url or "")
    workspace_id, user_id = "0" * 31 + "1", "0" * 31 + "2"
    stamp = "'2026-01-01 00:00:00'"

    def insert(connection: Connection, row_id: str, closed_by: str | None = None) -> None:
        connection.exec_driver_sql(
            "INSERT INTO month_states (id, workspace_id, month, closed_at, closed_by_user_id, "
            f"created_at, updated_at) VALUES (?, ?, '2026-10-01', {stamp}, ?, {stamp}, {stamp})",
            (row_id, workspace_id, closed_by),
        )

    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO workspaces (id, name, base_currency, locale, timezone, created_at, "
                f"updated_at) VALUES (?, 'Home', 'EUR', 'en', 'UTC', {stamp}, {stamp})",
                (workspace_id,),
            )
            connection.exec_driver_sql(
                "INSERT INTO users (id, email, display_name, theme, is_active, is_instance_admin, "
                "must_change_password, created_at, updated_at) "
                f"VALUES (?, 'a@example.com', 'A', 'system', 1, 0, 0, {stamp}, {stamp})",
                (user_id,),
            )
            insert(connection, "0" * 31 + "a", closed_by=user_id)
        with pytest.raises(IntegrityError), engine.begin() as connection:
            insert(connection, "0" * 31 + "b")  # the same (workspace, month) again
        with engine.begin() as connection:
            connection.exec_driver_sql("DELETE FROM users WHERE id = ?", (user_id,))
            closed = connection.exec_driver_sql(
                "SELECT closed_at, closed_by_user_id FROM month_states"
            ).one()
        assert closed[0] is not None and closed[1] is None  # still closed, closer unknown

        command.downgrade(alembic_config(settings), "0009_f21")
        assert "month_states" not in inspect(engine).get_table_names()
        upgrade_database(settings)
        assert database_is_at_head(settings)
        check_database_migrations(settings)
    finally:
        engine.dispose()


def test_0011_round_trip_creates_transactions_with_checks_and_a_composite_category_key(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'f22.db'}"
    )
    upgrade_database(settings)
    engine = create_database_engine(settings.database_url or "")
    home, other = "0" * 31 + "1", "0" * 31 + "2"
    food, foreign_food = "0" * 31 + "c", "0" * 31 + "d"
    stamp = "'2026-01-01 00:00:00'"
    rows = iter(f"{n:032x}" for n in range(100, 200))

    def insert(connection: Connection, **values: object) -> None:
        row: dict[str, object] = {
            "workspace_id": home, "category_id": food, "direction": "expense",
            "date": "2026-10-07", "amount_minor": 1250, "currency": "EUR", "source": "manual",
        } | values  # fmt: skip
        connection.exec_driver_sql(
            "INSERT INTO transactions (id, workspace_id, category_id, direction, date, "
            "amount_minor, currency, description, description_key, source, created_at, "
            f"updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, '', '', ?, {stamp}, {stamp})",
            (
                next(rows), row["workspace_id"], row["category_id"], row["direction"],
                row["date"], row["amount_minor"], row["currency"], row["source"],
            ),
        )  # fmt: skip

    try:
        with engine.connect() as connection:
            indexes = {
                name
                for (name,) in connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'index' "
                    "AND tbl_name = 'transactions'"
                )
            }
        assert {
            "ix_transactions_workspace_id",
            "ix_transactions_workspace_date",
            "ix_transactions_workspace_category_date",
            "ix_transactions_workspace_direction_description",
        } <= indexes
        with engine.begin() as connection:
            for workspace_id, category_id in ((home, food), (other, foreign_food)):
                connection.exec_driver_sql(
                    "INSERT INTO workspaces (id, name, base_currency, locale, timezone, "
                    "created_at, updated_at) "
                    f"VALUES (?, 'W', 'EUR', 'en', 'UTC', {stamp}, {stamp})",
                    (workspace_id,),
                )
                connection.exec_driver_sql(
                    "INSERT INTO categories (id, workspace_id, name, name_key, kind, treatment, "
                    f"color, sort_order, created_at, updated_at) VALUES (?, ?, 'Food', 'food', "
                    f"'expense', 'regular', '#16a34a', 0, {stamp}, {stamp})",
                    (category_id, workspace_id),
                )
            insert(connection)
            insert(connection, source="import", amount_minor=999_999_999_999_999)
        for bad in (
            {"category_id": foreign_food},  # another workspace's category
            {"amount_minor": 0},
            {"amount_minor": -1},
            {"amount_minor": 10**15},
            {"direction": "transfer"},
            {"currency": "EURO"},
            {"source": "api"},
            {"date": "1899-12-31"},
            {"date": "2200-01-01"},
        ):
            with pytest.raises(IntegrityError), engine.begin() as connection:
                insert(connection, **bad)

        command.downgrade(alembic_config(settings), "0010_f25")
        assert "transactions" not in inspect(engine).get_table_names()
        upgrade_database(settings)
        assert database_is_at_head(settings)
        check_database_migrations(settings)
    finally:
        engine.dispose()


def test_0013_round_trip_creates_the_instance_settings_row(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'is.db'}"
    )
    upgrade_database(settings)
    engine = create_database_engine(settings.database_url or "")
    try:
        with engine.connect() as connection:
            rows = connection.exec_driver_sql(
                "SELECT id, allow_workspace_creation, updated_at, updated_by_user_id "
                "FROM instance_settings"
            ).all()
        assert [tuple(row) for row in rows] == [(1, 1, None, None)]
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO instance_settings (id, allow_workspace_creation) VALUES (2, 1)"
            )
        command.downgrade(alembic_config(settings), "0012_f23")
        assert "instance_settings" not in inspect(engine).get_table_names()
        upgrade_database(settings)
        assert database_is_at_head(settings)
        check_database_migrations(settings)
    finally:
        engine.dispose()


def test_0017_round_trip_backfills_nominal_dates_and_keeps_occurrences(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'f32.db'}"
    )
    upgrade_database(settings)
    command.downgrade(alembic_config(settings), "0016_f16_totp")
    engine = create_database_engine(settings.database_url or "")
    home, rent, item, row = "0" * 31 + "1", "0" * 31 + "c", "0" * 31 + "e", "0" * 31 + "f"
    stamp = "'2026-01-01 00:00:00'"
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO workspaces (id, name, base_currency, locale, timezone, created_at, "
                f"updated_at) VALUES (?, 'W', 'EUR', 'en', 'UTC', {stamp}, {stamp})",
                (home,),
            )
            connection.exec_driver_sql(
                "INSERT INTO categories (id, workspace_id, name, name_key, kind, treatment, "
                "color, sort_order, created_at, updated_at) VALUES (?, ?, 'Rent', 'rent', "
                f"'expense', 'regular', '#16a34a', 0, {stamp}, {stamp})",
                (rent, home),
            )
            connection.exec_driver_sql(
                "INSERT INTO scheduled_items (id, workspace_id, name, direction, category_id, "
                "amount_mode, amount_minor, currency, start_date, freq, interval, weekdays, "
                "month_day, month, weekend_policy, notes, created_at, updated_at) VALUES "
                "(?, ?, 'Rent', 'expense', ?, 'fixed', 10000, 'EUR', '2026-03-01', 'monthly', "
                f"1, '[]', '1', 1, 'none', '', {stamp}, {stamp})",
                (item, home, rent),
            )
            connection.exec_driver_sql(
                "INSERT INTO scheduled_occurrences (id, workspace_id, scheduled_item_id, "
                "direction, category_id, due_date, expected_minor, currency, amount_state, "
                "status, label, created_at, updated_at) VALUES (?, ?, ?, 'expense', ?, "
                f"'2026-03-01', 10000, 'EUR', 'confirmed', 'pending', 'Rent', {stamp}, {stamp})",
                (row, home, item, rent),
            )
        upgrade_database(settings)
        assert database_is_at_head(settings)
        check_database_migrations(settings)
        with engine.connect() as connection:
            migrated = connection.exec_driver_sql(
                "SELECT nominal_date, due_date, generated_by, transaction_id, rescheduled_at, "
                "amount_overridden FROM scheduled_occurrences"
            ).all()
            auto_post = connection.exec_driver_sql("SELECT auto_post FROM scheduled_items").all()
        assert [tuple(value) for value in migrated] == [
            ("2026-03-01", "2026-03-01", "rule", None, None, 0)
        ]
        assert [tuple(value) for value in auto_post] == [(0,)]

        command.downgrade(alembic_config(settings), "0016_f16_totp")
        columns = {
            column["name"] for column in inspect(engine).get_columns("scheduled_occurrences")
        }
        assert "nominal_date" not in columns
        with engine.connect() as connection:
            count = connection.exec_driver_sql("SELECT COUNT(*) FROM scheduled_occurrences")
            assert count.scalar_one() == 1
        upgrade_database(settings)
        assert database_is_at_head(settings)
    finally:
        engine.dispose()


def test_0022_work_calendar_migration_is_reversible(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'days.db'}"
    )
    upgrade_database(settings)
    engine = create_database_engine(settings.database_url or "")
    try:
        inspector = inspect(engine)
        assert "work_days" in inspector.get_table_names()
        assert "derived_from_days" in {
            column["name"] for column in inspector.get_columns("work_logs")
        }
        command.downgrade(alembic_config(settings), "0021_f36")
        assert "work_days" not in inspect(engine).get_table_names()
        assert "derived_from_days" not in {
            column["name"] for column in inspect(engine).get_columns("work_logs")
        }
        upgrade_database(settings)
        assert "work_days" in inspect(engine).get_table_names()
        check_database_migrations(settings)
    finally:
        engine.dispose()


def test_0023_one_time_items_round_trip_converts_once_to_a_single_monthly(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'once.db'}"
    )
    upgrade_database(settings)
    engine = create_database_engine(settings.database_url or "")
    home, gift, item = "0" * 31 + "1", "0" * 31 + "c", "0" * 31 + "e"
    stamp = "'2026-01-01 00:00:00'"
    insert_item = (
        "INSERT INTO scheduled_items (id, workspace_id, name, direction, category_id, "
        "amount_mode, amount_minor, currency, start_date, freq, interval, weekdays, "
        "month_day, month, weekend_policy, notes, auto_post, created_at, updated_at) VALUES "
        "(?, ?, 'Gift', 'expense', ?, 'fixed', 5000, 'EUR', '2026-05-30', ?, "
        f"1, '[]', '1', 1, 'next_working_day', '', 0, {stamp}, {stamp})"
    )
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO workspaces (id, name, base_currency, locale, timezone, created_at, "
                f"updated_at) VALUES (?, 'W', 'EUR', 'en', 'UTC', {stamp}, {stamp})",
                (home,),
            )
            connection.exec_driver_sql(
                "INSERT INTO categories (id, workspace_id, name, name_key, kind, treatment, "
                "color, sort_order, created_at, updated_at) VALUES (?, ?, 'Gifts', 'gifts', "
                f"'expense', 'regular', '#16a34a', 0, {stamp}, {stamp})",
                (gift, home),
            )
            connection.exec_driver_sql(insert_item, (item, home, gift, "once"))
        command.downgrade(alembic_config(settings), "0022_f37")
        with engine.connect() as connection:
            row = connection.exec_driver_sql(
                "SELECT freq, interval, month_day, max_count, end_date, start_date, "
                "weekend_policy FROM scheduled_items"
            ).one()
        assert tuple(row) == ("monthly", 1, "30", 1, None, "2026-05-30", "next_working_day")
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.exec_driver_sql(insert_item, ("0" * 31 + "f", home, gift, "once"))
        upgrade_database(settings)
        assert database_is_at_head(settings)
        check_database_migrations(settings)
        with engine.begin() as connection:
            connection.exec_driver_sql(insert_item, ("0" * 31 + "f", home, gift, "once"))
    finally:
        engine.dispose()


def test_0025_amount_shading_round_trip_turns_the_setting_on_for_existing_users(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'shade.db'}"
    )
    upgrade_database(settings)
    command.downgrade(alembic_config(settings), "0024_tax_warning")
    engine = create_database_engine(settings.database_url or "")
    stamp = "'2026-01-01 00:00:00'"

    def columns() -> dict[str, bool]:  # name -> nullable
        return {c["name"]: c["nullable"] for c in inspect(engine).get_columns("users")}

    try:
        assert "highlight_large_amounts" not in columns()
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO users (id, email, display_name, theme, is_active, "
                "is_instance_admin, must_change_password, created_at, updated_at) VALUES "
                f"(?, 'a@example.com', 'A', 'system', 1, 0, 0, {stamp}, {stamp})",
                ("00000000000000000000000000000001",),
            )
        upgrade_database(settings)
        assert columns()["highlight_large_amounts"] is False  # NOT NULL
        with engine.connect() as connection:
            flag = connection.exec_driver_sql("SELECT highlight_large_amounts FROM users")
            assert flag.scalar_one() == 1  # existing accounts: on
        command.downgrade(alembic_config(settings), "0024_tax_warning")
        assert "highlight_large_amounts" not in columns()
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT count(*) FROM users").scalar_one() == 1
        upgrade_database(settings)
        assert database_is_at_head(settings)
        check_database_migrations(settings)
    finally:
        engine.dispose()


def test_0027_f41_lineage_round_trip_keeps_rows_and_scopes_keys(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'lineage.db'}"
    )
    upgrade_database(settings)
    command.downgrade(alembic_config(settings), "0026_two_step_login")
    engine = create_database_engine(settings.database_url or "")
    stamp = "'2026-01-01 00:00:00'"

    def columns(table: str) -> set[str]:
        return {c["name"] for c in inspect(engine).get_columns(table)}

    try:
        assert "import_key" not in columns("categories")
        assert "import_key" not in columns("transactions")
        with engine.begin() as connection:
            for workspace in ("0" * 31 + "1", "0" * 31 + "2"):
                connection.exec_driver_sql(
                    "INSERT INTO workspaces (id, name, base_currency, timezone, locale, "
                    "created_at, updated_at) "
                    f"VALUES (?, 'W', 'EUR', 'UTC', 'en', {stamp}, {stamp})",
                    (workspace,),
                )
        upgrade_database(settings)
        insert = (
            "INSERT INTO categories (id, workspace_id, name, name_key, kind, treatment, color, "
            "sort_order, import_key, created_at, updated_at) VALUES (?, ?, ?, ?, "
            f"'expense', 'regular', '#16a34a', 0, ?, {stamp}, {stamp})"
        )
        one, two = "0" * 31 + "1", "0" * 31 + "2"
        key = "id:2b0e8c1e-5f7a-4c6e-9a51-3f1d2c7b9e01"
        with engine.begin() as connection:
            connection.exec_driver_sql(insert, ("a" * 32, one, "A", "a", key))
            connection.exec_driver_sql(insert, ("b" * 32, two, "A", "a", key))  # other workspace
            connection.exec_driver_sql(insert, ("c" * 32, one, "C", "c", None))
            connection.exec_driver_sql(insert, ("d" * 32, one, "D", "d", None))  # NULLs repeat
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.exec_driver_sql(insert, ("e" * 32, one, "E", "e", key))
        command.downgrade(alembic_config(settings), "0026_two_step_login")
        assert "import_key" not in columns("categories")
        assert "import_key" not in columns("transactions")
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT count(*) FROM categories").scalar_one() == 4
        upgrade_database(settings)
        assert database_is_at_head(settings)
        check_database_migrations(settings)
    finally:
        engine.dispose()


def test_0028_taxes_redesign_maps_kinds_and_schedules_and_round_trips(tmp_path: Path) -> None:
    """ADR-0022 data mapping: effect + record_as -> kind, first attachment -> rule defaults,
    equal attachment values -> NULL; downgrade restores what the old model can express."""
    settings = Settings(
        _env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'taxes.db'}"
    )
    config = alembic_config(settings)
    command.upgrade(config, "0027_f41_lineage")
    engine = create_database_engine(settings.database_url or "")
    ws, empty_ws = "0" * 31 + "1", "0" * 31 + "2"
    salary, taxes_cat, social = "0" * 30 + "a1", "0" * 30 + "a2", "0" * 30 + "a3"
    contract, job = "0" * 30 + "b1", "0" * 30 + "b2"
    vat, pit, zus, orphan = ("0" * 30 + f"c{i}" for i in range(1, 5))
    stamp = "'2026-01-01 00:00:00'"
    try:
        with engine.begin() as c:
            for workspace in (ws, empty_ws):
                c.exec_driver_sql(
                    "INSERT INTO workspaces (id, name, base_currency, locale, timezone, "
                    "created_at, updated_at) VALUES "
                    f"(?, 'W', 'EUR', 'en', 'UTC', {stamp}, {stamp})",
                    (workspace,),
                )
            for category, name, kind, order in (
                (salary, "Salary", "income", 0),
                (taxes_cat, "Taxes", "expense", 1),
                (social, "Social", "expense", 2),
            ):
                c.exec_driver_sql(
                    "INSERT INTO categories (id, workspace_id, name, name_key, kind, treatment, "
                    "color, sort_order, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'regular', "
                    f"'#16a34a', ?, {stamp}, {stamp})",
                    (category, ws, name, name.lower(), kind, order),
                )
            for source, record_as in ((contract, "cash_received"), (job, "take_home")):
                c.exec_driver_sql(
                    "INSERT INTO income_sources (id, workspace_id, name, kind, category_id, "
                    "currency, fixed_minor, unit_label, pay_day, pay_lag_months, expected_mode, "
                    "average_months, record_as, active_from, created_at, updated_at) VALUES "
                    "(?, ?, 'S', 'fixed', ?, 'EUR', 100000, 'day', 10, 0, 'manual', 3, ?, "
                    f"'2026-01-01', {stamp}, {stamp})",
                    (source, ws, salary, record_as),
                )
            for rule, effect in (
                (vat, "added"),
                (pit, "withheld"),
                (zus, "withheld"),
                (orphan, "withheld"),
            ):
                c.exec_driver_sql(
                    "INSERT INTO tax_rules (id, workspace_id, name, effect, base, notes, "
                    f"created_at, updated_at) VALUES (?, ?, 'T', ?, 'net', '', {stamp}, {stamp})",
                    (rule, ws, effect),
                )
            attachments = (
                # id, source, rule, order, category, day, offset, created
                ("d1", contract, vat, 0, taxes_cat, 25, 1, "2026-01-01"),
                ("d2", contract, zus, 1, social, 15, 2, "2026-01-01"),
                ("d3", contract, pit, 2, taxes_cat, 5, 1, "2026-02-01"),
                ("d4", job, pit, 0, taxes_cat, 20, 0, "2026-01-15"),
                ("d5", job, zus, 1, taxes_cat, 15, 2, "2026-03-01"),
            )
            for key, source, rule, order, category, day, offset, created in attachments:
                c.exec_driver_sql(
                    "INSERT INTO income_source_tax_rules (id, workspace_id, income_source_id, "
                    "tax_rule_id, sort_order, generate_payment, payment_category_id, due_day, "
                    "due_month_offset, created_at, updated_at) VALUES "
                    f"(?, ?, ?, ?, ?, 1, ?, ?, ?, ?, {stamp})",
                    ("0" * 30 + key, ws, source, rule, order, category, day, offset, created),
                )
        upgrade_database(settings)
        with engine.connect() as c:
            rules = {
                row[0][-2:]: tuple(row[1:])
                for row in c.exec_driver_sql(
                    "SELECT id, kind, payment_day, payment_timing, payment_category_id "
                    "FROM tax_rules"
                )
            }
            rows = {
                row[0][-2:]: tuple(row[1:])
                for row in c.exec_driver_sql(
                    "SELECT id, due_day, due_month_offset, payment_category_id "
                    "FROM income_source_tax_rules"
                )
            }
            enabled = dict(c.exec_driver_sql("SELECT id, taxes_enabled FROM workspaces").all())
        assert rules == {
            "c1": ("added", 25, "next_month", taxes_cat),  # first attachment d1 (offset 1)
            "c2": ("payer_withheld", 20, "same_month", taxes_cat),  # on a take-home source; d4
            # Also on the take-home source (d5): payer-withheld; defaults from d2 (offset 2).
            "c3": ("payer_withheld", 15, "next_month", social),
            "c4": ("self_paid", 10, "next_month", None),  # no attachment
        }
        assert rows == {
            "d1": (None, None, None),
            "d2": (None, 2, None),  # offset 2 differs from next_month (1)
            "d3": (5, 1, None),  # day 5 and offset 1 differ from the rule (20, same month)
            "d4": (None, None, None),
            "d5": (None, 2, taxes_cat),
        }
        assert enabled == {ws: 1, empty_ws: 0}
        assert "record_as" not in {
            col["name"] for col in inspect(engine).get_columns("income_sources")
        }
        check_database_migrations(settings)

        command.downgrade(config, "0027_f41_lineage")
        with engine.connect() as c:
            effects = dict(c.exec_driver_sql("SELECT id, effect FROM tax_rules").all())
            record_as = dict(c.exec_driver_sql("SELECT id, record_as FROM income_sources").all())
            restored = {
                row[0][-2:]: tuple(row[1:])
                for row in c.exec_driver_sql(
                    "SELECT id, due_day, due_month_offset, payment_category_id "
                    "FROM income_source_tax_rules"
                )
            }
        assert effects == {vat: "added", pit: "withheld", zus: "withheld", orphan: "withheld"}
        # Lossy: the contract source has payer-withheld taxes attached (d2, d3): take-home.
        assert record_as == {contract: "take_home", job: "take_home"}
        assert restored == {
            "d1": (25, 1, taxes_cat),
            "d2": (15, 2, social),
            "d3": (5, 1, taxes_cat),
            "d4": (20, 0, taxes_cat),
            "d5": (15, 2, taxes_cat),
        }
        upgrade_database(settings)
        assert database_is_at_head(settings)
        check_database_migrations(settings)
    finally:
        engine.dispose()


def test_0029_upcoming_days_round_trip_keeps_workspaces_on_the_instance_default(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'upcoming.db'}"
    )
    config = alembic_config(settings)
    command.upgrade(config, "0028_taxes_redesign")
    engine = create_database_engine(settings.database_url or "")
    stamp = "'2026-01-01 00:00:00'"

    def columns() -> dict[str, bool]:  # name -> nullable
        return {c["name"]: c["nullable"] for c in inspect(engine).get_columns("workspaces")}

    try:
        assert "upcoming_days" not in columns()
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO workspaces (id, name, base_currency, locale, timezone, created_at, "
                f"updated_at) VALUES (?, 'Home', 'EUR', 'en', 'UTC', {stamp}, {stamp})",
                ("0" * 31 + "1",),
            )
        upgrade_database(settings)
        assert columns()["upcoming_days"] is True  # nullable
        with engine.begin() as connection:
            days = connection.exec_driver_sql("SELECT upcoming_days FROM workspaces")
            assert days.scalar_one() is None  # existing workspaces: the instance setting
            connection.exec_driver_sql("UPDATE workspaces SET upcoming_days = 45")
        command.downgrade(config, "0028_taxes_redesign")
        assert "upcoming_days" not in columns()
        with engine.connect() as connection:
            count = connection.exec_driver_sql("SELECT count(*) FROM workspaces").scalar_one()
            assert count == 1
        upgrade_database(settings)
        assert database_is_at_head(settings)
        check_database_migrations(settings)
    finally:
        engine.dispose()
