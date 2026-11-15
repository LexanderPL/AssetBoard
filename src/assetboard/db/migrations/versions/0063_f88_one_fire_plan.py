"""One FIRE plan per workspace, saving overrides and the archive 2.0 model

Revision ID: 0063_f88_one_fire_plan
Revises: 0062_f86_fire_archive
Create Date: 2026-11-15

F8.8. The FIRE tables are rebuilt in place. Old scenarios stay readable through a temporary
``legacy`` JSON payload until the user chooses one in the app; ``is_live`` marks the chosen plan
and a partial unique index enforces one live plan per workspace. Contribution overrides become
``saving_override`` events, one-click loan-payoff events are removed and the contribution and
expense-exclusion tables are dropped.

The data is copied in Python and the tables are rebuilt instead of using SQLite batch mode: the
new schema changes check constraints and drops two child tables, and the rebuild works the same on
SQLite and PostgreSQL. A one-way data migration is intentional; the downgrade is structural and
lossy (deleted scenarios, converted loan-payoff events and exclusions cannot be recreated).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import UtcDateTime
from assetboard.domain.fire import (
    MAX_LABEL_LENGTH,
    MAX_YEAR,
    MIN_YEAR,
    RATE_HARD_MAX_BP,
    RATE_HARD_MIN_BP,
    SWR_HARD_MAX_BP,
    SWR_HARD_MIN_BP,
)
from assetboard.domain.transactions import MAX_AMOUNT_MINOR

revision = "0063_f88_one_fire_plan"
down_revision = "0062_f86_fire_archive"
branch_labels = None
depends_on = None

_OLD_SCENARIOS = sa.table(
    "fire_scenarios",
    sa.column("id", sa.Uuid()),
    sa.column("workspace_id", sa.Uuid()),
    sa.column("created_at", UtcDateTime()),
    sa.column("updated_at", UtcDateTime()),
    sa.column("name", sa.String()),
    sa.column("name_key", sa.String()),
    sa.column("currency", sa.String()),
    sa.column("birth_year", sa.Integer()),
    sa.column("start_year", sa.Integer()),
    sa.column("inflation_bp", sa.Integer()),
    sa.column("real_return_bp", sa.Integer()),
    sa.column("income_growth_bp", sa.Integer()),
    sa.column("stop_working_age", sa.Integer()),
    sa.column("horizon_age", sa.Integer()),
    sa.column("rule", sa.String()),
    sa.column("swr_bp", sa.Integer()),
    sa.column("income_mode", sa.String()),
    sa.column("income_manual_minor", sa.BigInteger()),
    sa.column("expenses_mode", sa.String()),
    sa.column("expenses_manual_minor", sa.BigInteger()),
    sa.column("portfolio_mode", sa.String()),
    sa.column("portfolio_manual_minor", sa.BigInteger()),
    sa.column("is_default", sa.Boolean()),
    sa.column("archived_at", UtcDateTime()),
    sa.column("import_key", sa.String()),
)
_OLD_EVENTS = sa.table(
    "fire_events",
    sa.column("id", sa.Uuid()),
    sa.column("workspace_id", sa.Uuid()),
    sa.column("scenario_id", sa.Uuid()),
    sa.column("year", sa.Integer()),
    sa.column("kind", sa.String()),
    sa.column("amount_minor", sa.BigInteger()),
    sa.column("label", sa.String()),
    sa.column("source_loan_id", sa.Uuid()),
    sa.column("import_key", sa.String()),
    sa.column("created_at", UtcDateTime()),
    sa.column("updated_at", UtcDateTime()),
)
_OLD_CONTRIBUTIONS = sa.table(
    "fire_contributions",
    sa.column("id", sa.Uuid()),
    sa.column("workspace_id", sa.Uuid()),
    sa.column("scenario_id", sa.Uuid()),
    sa.column("year", sa.Integer()),
    sa.column("net_amount_minor", sa.BigInteger()),
    sa.column("import_key", sa.String()),
    sa.column("created_at", UtcDateTime()),
    sa.column("updated_at", UtcDateTime()),
)
_OLD_EXCLUSIONS = sa.table(
    "fire_expense_exclusions",
    sa.column("id", sa.Uuid()),
    sa.column("workspace_id", sa.Uuid()),
    sa.column("scenario_id", sa.Uuid()),
    sa.column("category_id", sa.Uuid()),
    sa.column("import_key", sa.String()),
    sa.column("created_at", UtcDateTime()),
    sa.column("updated_at", UtcDateTime()),
)

_NEW_SCENARIOS = sa.table(
    "fire_scenarios",
    sa.column("id", sa.Uuid()),
    sa.column("workspace_id", sa.Uuid()),
    sa.column("created_at", UtcDateTime()),
    sa.column("updated_at", UtcDateTime()),
    sa.column("currency", sa.String()),
    sa.column("birth_year", sa.Integer()),
    sa.column("stop_working_age", sa.Integer()),
    sa.column("horizon_age", sa.Integer()),
    sa.column("inflation_bp", sa.Integer()),
    sa.column("real_return_bp", sa.Integer()),
    sa.column("income_growth_bp", sa.Integer()),
    sa.column("rule", sa.String()),
    sa.column("swr_bp", sa.Integer()),
    sa.column("income_minor", sa.BigInteger()),
    sa.column("expenses_minor", sa.BigInteger()),
    sa.column("portfolio_minor", sa.BigInteger()),
    sa.column("needs_review", sa.Boolean()),
    sa.column("is_live", sa.Boolean()),
    sa.column("legacy", sa.Text()),
    sa.column("import_key", sa.String()),
)
_NEW_EVENTS = sa.table(
    "fire_events",
    sa.column("id", sa.Uuid()),
    sa.column("workspace_id", sa.Uuid()),
    sa.column("scenario_id", sa.Uuid()),
    sa.column("year", sa.Integer()),
    sa.column("kind", sa.String()),
    sa.column("amount_minor", sa.BigInteger()),
    sa.column("label", sa.String()),
    sa.column("import_key", sa.String()),
    sa.column("created_at", UtcDateTime()),
    sa.column("updated_at", UtcDateTime()),
)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _legacy_json(row: dict[str, Any], exclusions: list[str]) -> str:
    return json.dumps(
        {
            "v": 1,
            "name": row["name"],
            "name_key": row["name_key"],
            "start_year": row["start_year"],
            "is_default": bool(row["is_default"]),
            "archived_at": _iso(row["archived_at"]),
            "income_mode": row["income_mode"],
            "expenses_mode": row["expenses_mode"],
            "portfolio_mode": row["portfolio_mode"],
            "income_manual_minor": row["income_manual_minor"],
            "expenses_manual_minor": row["expenses_manual_minor"],
            "portfolio_manual_minor": row["portfolio_manual_minor"],
            "expense_exclusion_ids": exclusions,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _create_scenarios_table() -> None:
    op.create_table(
        "fire_scenarios",
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("birth_year", sa.Integer(), nullable=False),
        sa.Column("stop_working_age", sa.Integer(), nullable=False),
        sa.Column("horizon_age", sa.Integer(), nullable=False),
        sa.Column("inflation_bp", sa.Integer(), nullable=False),
        sa.Column("real_return_bp", sa.Integer(), nullable=False),
        sa.Column("income_growth_bp", sa.Integer(), nullable=False),
        sa.Column("rule", sa.String(length=32), nullable=False),
        sa.Column("swr_bp", sa.Integer(), nullable=True),
        sa.Column("income_minor", sa.BigInteger(), nullable=True),
        sa.Column("expenses_minor", sa.BigInteger(), nullable=True),
        sa.Column("portfolio_minor", sa.BigInteger(), nullable=True),
        sa.Column("needs_review", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_live", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("legacy", sa.Text(), nullable=True),
        sa.Column("import_key", sa.String(length=67), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "length(currency) = 3",
            name=op.f("ck_fire_scenarios_currency_length"),
        ),
        sa.CheckConstraint(
            f"birth_year BETWEEN {MIN_YEAR} AND {MAX_YEAR}",
            name=op.f("ck_fire_scenarios_birth_year_in_range"),
        ),
        sa.CheckConstraint(
            "horizon_age BETWEEN 1 AND 120",
            name=op.f("ck_fire_scenarios_horizon_age_valid"),
        ),
        sa.CheckConstraint(
            "stop_working_age BETWEEN 0 AND 120",
            name=op.f("ck_fire_scenarios_stop_working_age_valid"),
        ),
        sa.CheckConstraint(
            f"inflation_bp BETWEEN {RATE_HARD_MIN_BP} AND {RATE_HARD_MAX_BP}",
            name=op.f("ck_fire_scenarios_inflation_valid"),
        ),
        sa.CheckConstraint(
            f"real_return_bp BETWEEN {RATE_HARD_MIN_BP} AND {RATE_HARD_MAX_BP}",
            name=op.f("ck_fire_scenarios_real_return_valid"),
        ),
        sa.CheckConstraint(
            f"income_growth_bp BETWEEN {RATE_HARD_MIN_BP} AND {RATE_HARD_MAX_BP}",
            name=op.f("ck_fire_scenarios_income_growth_valid"),
        ),
        sa.CheckConstraint(
            "rule IN ('returns_cover_expenses', 'swr')",
            name=op.f("ck_fire_scenarios_rule_valid"),
        ),
        sa.CheckConstraint(
            "(rule = 'swr' AND swr_bp IS NOT NULL AND "
            f"swr_bp BETWEEN {SWR_HARD_MIN_BP} AND {SWR_HARD_MAX_BP}) OR "
            "(rule = 'returns_cover_expenses' AND swr_bp IS NULL)",
            name=op.f("ck_fire_scenarios_swr_valid"),
        ),
        sa.CheckConstraint(
            f"income_minor IS NULL OR income_minor BETWEEN 0 AND {MAX_AMOUNT_MINOR}",
            name=op.f("ck_fire_scenarios_income_amount_valid"),
        ),
        sa.CheckConstraint(
            f"expenses_minor IS NULL OR expenses_minor BETWEEN 0 AND {MAX_AMOUNT_MINOR}",
            name=op.f("ck_fire_scenarios_expenses_amount_valid"),
        ),
        sa.CheckConstraint(
            f"portfolio_minor IS NULL OR portfolio_minor BETWEEN 0 AND {MAX_AMOUNT_MINOR}",
            name=op.f("ck_fire_scenarios_portfolio_amount_valid"),
        ),
        sa.CheckConstraint(
            "NOT is_live OR (income_minor IS NOT NULL AND expenses_minor IS NOT NULL "
            "AND portfolio_minor IS NOT NULL)",
            name=op.f("ck_fire_scenarios_live_complete"),
        ),
        sa.CheckConstraint(
            "is_live OR legacy IS NOT NULL",
            name=op.f("ck_fire_scenarios_live_or_legacy"),
        ),
        sa.CheckConstraint(
            "NOT (is_live AND legacy IS NOT NULL)",
            name=op.f("ck_fire_scenarios_live_not_legacy"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_fire_scenarios_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fire_scenarios")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_fire_scenarios_workspace_id_id"),
    )
    op.create_index(
        op.f("ix_fire_scenarios_workspace_id"),
        "fire_scenarios",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        "uq_fire_scenarios_live_workspace",
        "fire_scenarios",
        ["workspace_id"],
        unique=True,
        sqlite_where=sa.text("is_live"),
        postgresql_where=sa.text("is_live"),
    )
    op.create_index(
        "uq_fire_scenarios_import_key",
        "fire_scenarios",
        ["workspace_id", "import_key"],
        unique=True,
        sqlite_where=sa.text("import_key IS NOT NULL"),
        postgresql_where=sa.text("import_key IS NOT NULL"),
    )


def _create_events_table() -> None:
    op.create_table(
        "fire_events",
        sa.Column("scenario_id", sa.Uuid(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("label", sa.String(length=MAX_LABEL_LENGTH), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("import_key", sa.String(length=67), nullable=True),
        sa.CheckConstraint(
            f"year BETWEEN {MIN_YEAR} AND {MAX_YEAR}",
            name=op.f("ck_fire_events_year_in_range"),
        ),
        sa.CheckConstraint(
            "kind IN ('cash', 'expense_change', 'income_change', 'saving_override')",
            name=op.f("ck_fire_events_kind_valid"),
        ),
        sa.CheckConstraint(
            f"amount_minor BETWEEN -{MAX_AMOUNT_MINOR} AND {MAX_AMOUNT_MINOR}",
            name=op.f("ck_fire_events_amount_valid"),
        ),
        sa.CheckConstraint(
            f"length(label) > 0 AND length(label) <= {MAX_LABEL_LENGTH}",
            name=op.f("ck_fire_events_label_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "scenario_id"],
            ["fire_scenarios.workspace_id", "fire_scenarios.id"],
            name="fk_fire_events_workspace_scenario",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_fire_events_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fire_events")),
    )
    op.create_index(
        op.f("ix_fire_events_workspace_id"),
        "fire_events",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        "ix_fire_events_workspace_scenario_year",
        "fire_events",
        ["workspace_id", "scenario_id", "year"],
        unique=False,
    )
    op.create_index(
        "uq_fire_events_saving_override",
        "fire_events",
        ["workspace_id", "scenario_id", "year"],
        unique=True,
        sqlite_where=sa.text("kind = 'saving_override'"),
        postgresql_where=sa.text("kind = 'saving_override'"),
    )
    op.create_index(
        "uq_fire_events_import_key",
        "fire_events",
        ["workspace_id", "import_key"],
        unique=True,
        sqlite_where=sa.text("import_key IS NOT NULL"),
        postgresql_where=sa.text("import_key IS NOT NULL"),
    )


def upgrade() -> None:
    connection = op.get_bind()
    old_scenarios = [dict(row) for row in connection.execute(sa.select(_OLD_SCENARIOS)).mappings()]
    old_events = [dict(row) for row in connection.execute(sa.select(_OLD_EVENTS)).mappings()]
    old_contributions = [
        dict(row) for row in connection.execute(sa.select(_OLD_CONTRIBUTIONS)).mappings()
    ]
    old_exclusions = [
        dict(row) for row in connection.execute(sa.select(_OLD_EXCLUSIONS)).mappings()
    ]

    exclusions_by_scenario: dict[UUID, list[str]] = {}
    for row in old_exclusions:
        exclusions_by_scenario.setdefault(row["scenario_id"], []).append(str(row["category_id"]))

    used_event_ids: set[UUID] = {row["id"] for row in old_events}
    used_import_keys: set[tuple[UUID, str]] = {
        (row["workspace_id"], row["import_key"])
        for row in old_events
        if row["import_key"] is not None
    }

    scenario_rows: list[dict[str, Any]] = []
    for row in old_scenarios:
        exclusions = sorted(exclusions_by_scenario.get(row["id"], []))
        scenario_rows.append(
            {
                "id": row["id"],
                "workspace_id": row["workspace_id"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "currency": row["currency"],
                "birth_year": row["birth_year"],
                "stop_working_age": row["stop_working_age"],
                "horizon_age": row["horizon_age"],
                "inflation_bp": row["inflation_bp"],
                "real_return_bp": row["real_return_bp"],
                "income_growth_bp": row["income_growth_bp"],
                "rule": row["rule"],
                "swr_bp": row["swr_bp"],
                "income_minor": (
                    row["income_manual_minor"] if row["income_mode"] == "manual" else None
                ),
                "expenses_minor": (
                    row["expenses_manual_minor"] if row["expenses_mode"] == "manual" else None
                ),
                "portfolio_minor": (
                    row["portfolio_manual_minor"] if row["portfolio_mode"] == "manual" else None
                ),
                "needs_review": True,
                "is_live": False,
                "legacy": _legacy_json(row, exclusions),
                "import_key": row["import_key"],
            }
        )

    event_rows: list[dict[str, Any]] = []
    for row in old_events:
        if row["source_loan_id"] is not None:
            continue
        event_rows.append(
            {
                "id": row["id"],
                "workspace_id": row["workspace_id"],
                "scenario_id": row["scenario_id"],
                "year": row["year"],
                "kind": row["kind"],
                "amount_minor": row["amount_minor"],
                "label": row["label"],
                "import_key": row["import_key"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )
    for row in old_contributions:
        event_id = row["id"]
        if event_id in used_event_ids:
            event_id = uuid4()
        used_event_ids.add(event_id)
        import_key = row["import_key"]
        if import_key is not None and (row["workspace_id"], import_key) in used_import_keys:
            import_key = None
        if import_key is not None:
            used_import_keys.add((row["workspace_id"], import_key))
        event_rows.append(
            {
                "id": event_id,
                "workspace_id": row["workspace_id"],
                "scenario_id": row["scenario_id"],
                "year": row["year"],
                "kind": "saving_override",
                "amount_minor": row["net_amount_minor"],
                "label": "Saving override",
                "import_key": import_key,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )

    op.drop_table("fire_contributions")
    op.drop_table("fire_expense_exclusions")
    op.drop_table("fire_events")
    op.drop_table("fire_scenarios")

    _create_scenarios_table()
    _create_events_table()
    if scenario_rows:
        connection.execute(sa.insert(_NEW_SCENARIOS), scenario_rows)
    if event_rows:
        connection.execute(sa.insert(_NEW_EVENTS), event_rows)


def _old_tables() -> None:
    op.create_table(
        "fire_scenarios",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("name_key", sa.String(length=255), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("birth_year", sa.Integer(), nullable=False),
        sa.Column("start_year", sa.Integer(), nullable=False),
        sa.Column("inflation_bp", sa.Integer(), nullable=False),
        sa.Column("real_return_bp", sa.Integer(), nullable=False),
        sa.Column("income_growth_bp", sa.Integer(), nullable=False),
        sa.Column("stop_working_age", sa.Integer(), nullable=False),
        sa.Column("horizon_age", sa.Integer(), nullable=False),
        sa.Column("rule", sa.String(length=32), nullable=False),
        sa.Column("swr_bp", sa.Integer(), nullable=True),
        sa.Column("income_mode", sa.String(length=16), nullable=False),
        sa.Column("income_manual_minor", sa.BigInteger(), nullable=True),
        sa.Column("expenses_mode", sa.String(length=16), nullable=False),
        sa.Column("expenses_manual_minor", sa.BigInteger(), nullable=True),
        sa.Column("portfolio_mode", sa.String(length=16), nullable=False),
        sa.Column("portfolio_manual_minor", sa.BigInteger(), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("archived_at", UtcDateTime(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("import_key", sa.String(length=67), nullable=True),
        sa.CheckConstraint("length(name) > 0", name=op.f("ck_fire_scenarios_name_not_empty")),
        sa.CheckConstraint("length(currency) = 3", name=op.f("ck_fire_scenarios_currency_length")),
        sa.CheckConstraint(
            f"birth_year BETWEEN {MIN_YEAR} AND {MAX_YEAR}",
            name=op.f("ck_fire_scenarios_birth_year_in_range"),
        ),
        sa.CheckConstraint(
            f"start_year BETWEEN {MIN_YEAR} AND {MAX_YEAR}",
            name=op.f("ck_fire_scenarios_start_year_in_range"),
        ),
        sa.CheckConstraint(
            "birth_year <= start_year",
            name=op.f("ck_fire_scenarios_birth_not_after_start"),
        ),
        sa.CheckConstraint(
            "horizon_age BETWEEN 1 AND 120",
            name=op.f("ck_fire_scenarios_horizon_age_valid"),
        ),
        sa.CheckConstraint(
            "stop_working_age BETWEEN 0 AND 120",
            name=op.f("ck_fire_scenarios_stop_working_age_valid"),
        ),
        sa.CheckConstraint(
            "horizon_age > start_year - birth_year",
            name=op.f("ck_fire_scenarios_horizon_after_current_age"),
        ),
        sa.CheckConstraint(
            "stop_working_age >= start_year - birth_year",
            name=op.f("ck_fire_scenarios_stop_not_before_current_age"),
        ),
        sa.CheckConstraint(
            f"inflation_bp BETWEEN {RATE_HARD_MIN_BP} AND {RATE_HARD_MAX_BP}",
            name=op.f("ck_fire_scenarios_inflation_valid"),
        ),
        sa.CheckConstraint(
            f"real_return_bp BETWEEN {RATE_HARD_MIN_BP} AND {RATE_HARD_MAX_BP}",
            name=op.f("ck_fire_scenarios_real_return_valid"),
        ),
        sa.CheckConstraint(
            f"income_growth_bp BETWEEN {RATE_HARD_MIN_BP} AND {RATE_HARD_MAX_BP}",
            name=op.f("ck_fire_scenarios_income_growth_valid"),
        ),
        sa.CheckConstraint(
            "rule IN ('returns_cover_expenses', 'swr')",
            name=op.f("ck_fire_scenarios_rule_valid"),
        ),
        sa.CheckConstraint(
            "(rule = 'swr' AND swr_bp IS NOT NULL AND "
            f"swr_bp BETWEEN {SWR_HARD_MIN_BP} AND {SWR_HARD_MAX_BP}) OR "
            "(rule = 'returns_cover_expenses' AND swr_bp IS NULL)",
            name=op.f("ck_fire_scenarios_swr_valid"),
        ),
        sa.CheckConstraint(
            "income_mode IN ('manual', 'trailing_12m')",
            name=op.f("ck_fire_scenarios_income_mode_valid"),
        ),
        sa.CheckConstraint(
            "expenses_mode IN ('manual', 'trailing_12m')",
            name=op.f("ck_fire_scenarios_expenses_mode_valid"),
        ),
        sa.CheckConstraint(
            "portfolio_mode IN ('manual', 'linked_investable')",
            name=op.f("ck_fire_scenarios_portfolio_mode_valid"),
        ),
        sa.CheckConstraint(
            "(income_mode = 'manual' AND income_manual_minor IS NOT NULL AND "
            f"income_manual_minor BETWEEN 0 AND {MAX_AMOUNT_MINOR}) OR "
            "(income_mode != 'manual' AND income_manual_minor IS NULL)",
            name=op.f("ck_fire_scenarios_income_manual_valid"),
        ),
        sa.CheckConstraint(
            "(expenses_mode = 'manual' AND expenses_manual_minor IS NOT NULL AND "
            f"expenses_manual_minor BETWEEN 0 AND {MAX_AMOUNT_MINOR}) OR "
            "(expenses_mode != 'manual' AND expenses_manual_minor IS NULL)",
            name=op.f("ck_fire_scenarios_expenses_manual_valid"),
        ),
        sa.CheckConstraint(
            "(portfolio_mode = 'manual' AND portfolio_manual_minor IS NOT NULL AND "
            f"portfolio_manual_minor BETWEEN 0 AND {MAX_AMOUNT_MINOR}) OR "
            "(portfolio_mode != 'manual' AND portfolio_manual_minor IS NULL)",
            name=op.f("ck_fire_scenarios_portfolio_manual_valid"),
        ),
        sa.CheckConstraint(
            "NOT (is_default AND archived_at IS NOT NULL)",
            name=op.f("ck_fire_scenarios_default_not_archived"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_fire_scenarios_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fire_scenarios")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_fire_scenarios_workspace_id_id"),
    )
    op.create_index(
        op.f("ix_fire_scenarios_workspace_id"),
        "fire_scenarios",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        "ix_fire_scenarios_workspace_name",
        "fire_scenarios",
        ["workspace_id", "name"],
        unique=False,
    )
    op.create_index(
        "uq_fire_scenarios_active_name_key",
        "fire_scenarios",
        ["workspace_id", "name_key"],
        unique=True,
        sqlite_where=sa.text("archived_at IS NULL"),
        postgresql_where=sa.text("archived_at IS NULL"),
    )
    op.create_index(
        "uq_fire_scenarios_active_default",
        "fire_scenarios",
        ["workspace_id"],
        unique=True,
        sqlite_where=sa.text("is_default AND archived_at IS NULL"),
        postgresql_where=sa.text("is_default AND archived_at IS NULL"),
    )
    op.create_index(
        "uq_fire_scenarios_import_key",
        "fire_scenarios",
        ["workspace_id", "import_key"],
        unique=True,
        sqlite_where=sa.text("import_key IS NOT NULL"),
        postgresql_where=sa.text("import_key IS NOT NULL"),
    )

    op.create_table(
        "fire_contributions",
        sa.Column("scenario_id", sa.Uuid(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("net_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("import_key", sa.String(length=67), nullable=True),
        sa.CheckConstraint(
            f"year BETWEEN {MIN_YEAR} AND {MAX_YEAR}",
            name=op.f("ck_fire_contributions_year_in_range"),
        ),
        sa.CheckConstraint(
            f"net_amount_minor BETWEEN -{MAX_AMOUNT_MINOR} AND {MAX_AMOUNT_MINOR}",
            name=op.f("ck_fire_contributions_net_amount_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "scenario_id"],
            ["fire_scenarios.workspace_id", "fire_scenarios.id"],
            name="fk_fire_contributions_workspace_scenario",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_fire_contributions_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fire_contributions")),
        sa.UniqueConstraint(
            "workspace_id",
            "scenario_id",
            "year",
            name="uq_fire_contributions_workspace_scenario_year",
        ),
    )
    op.create_index(
        op.f("ix_fire_contributions_workspace_id"),
        "fire_contributions",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        "uq_fire_contributions_import_key",
        "fire_contributions",
        ["workspace_id", "import_key"],
        unique=True,
        sqlite_where=sa.text("import_key IS NOT NULL"),
        postgresql_where=sa.text("import_key IS NOT NULL"),
    )

    op.create_table(
        "fire_expense_exclusions",
        sa.Column("scenario_id", sa.Uuid(), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("import_key", sa.String(length=67), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id", "scenario_id"],
            ["fire_scenarios.workspace_id", "fire_scenarios.id"],
            name="fk_fire_expense_exclusions_workspace_scenario",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "category_id"],
            ["categories.workspace_id", "categories.id"],
            name="fk_fire_expense_exclusions_workspace_category",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fire_expense_exclusions")),
        sa.UniqueConstraint(
            "workspace_id",
            "scenario_id",
            "category_id",
            name="uq_fire_expense_exclusions_workspace_scenario_category",
        ),
    )
    op.create_index(
        op.f("ix_fire_expense_exclusions_workspace_id"),
        "fire_expense_exclusions",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        "ix_fire_expense_exclusions_workspace_scenario",
        "fire_expense_exclusions",
        ["workspace_id", "scenario_id"],
        unique=False,
    )
    op.create_index(
        "uq_fire_expense_exclusions_import_key",
        "fire_expense_exclusions",
        ["workspace_id", "import_key"],
        unique=True,
        sqlite_where=sa.text("import_key IS NOT NULL"),
        postgresql_where=sa.text("import_key IS NOT NULL"),
    )

    op.create_table(
        "fire_events",
        sa.Column("scenario_id", sa.Uuid(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("label", sa.String(length=MAX_LABEL_LENGTH), nullable=False),
        sa.Column("source_loan_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("import_key", sa.String(length=67), nullable=True),
        sa.CheckConstraint(
            f"year BETWEEN {MIN_YEAR} AND {MAX_YEAR}",
            name=op.f("ck_fire_events_year_in_range"),
        ),
        sa.CheckConstraint(
            "kind IN ('cash', 'expense_change', 'income_change')",
            name=op.f("ck_fire_events_kind_valid"),
        ),
        sa.CheckConstraint(
            f"amount_minor BETWEEN -{MAX_AMOUNT_MINOR} AND {MAX_AMOUNT_MINOR}",
            name=op.f("ck_fire_events_amount_valid"),
        ),
        sa.CheckConstraint(
            f"length(label) > 0 AND length(label) <= {MAX_LABEL_LENGTH}",
            name=op.f("ck_fire_events_label_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "scenario_id"],
            ["fire_scenarios.workspace_id", "fire_scenarios.id"],
            name="fk_fire_events_workspace_scenario",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_loan_id"],
            ["loans.workspace_id", "loans.id"],
            name="fk_fire_events_workspace_source_loan",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_fire_events_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fire_events")),
    )
    op.create_index(
        op.f("ix_fire_events_workspace_id"),
        "fire_events",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        "ix_fire_events_workspace_scenario_year",
        "fire_events",
        ["workspace_id", "scenario_id", "year"],
        unique=False,
    )
    op.create_index(
        "ix_fire_events_workspace_source_loan",
        "fire_events",
        ["workspace_id", "source_loan_id"],
        unique=False,
    )
    op.create_index(
        "uq_fire_events_import_key",
        "fire_events",
        ["workspace_id", "import_key"],
        unique=True,
        sqlite_where=sa.text("import_key IS NOT NULL"),
        postgresql_where=sa.text("import_key IS NOT NULL"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    scenarios = [dict(row) for row in connection.execute(sa.select(_NEW_SCENARIOS)).mappings()]
    events = [dict(row) for row in connection.execute(sa.select(_NEW_EVENTS)).mappings()]

    op.drop_table("fire_events")
    op.drop_table("fire_scenarios")
    _old_tables()

    scenario_rows: list[dict[str, Any]] = []
    for row in scenarios:
        # The downgrade has no clock. Fill the removed start year from the ages the v2 row
        # keeps, so the downgraded row still satisfies the old start-year checks.
        derived_start_year = row["birth_year"] + max(
            0, min(row["horizon_age"] - 1, row["stop_working_age"])
        )
        legacy: dict[str, Any] = {}
        if row["legacy"]:
            try:
                legacy = json.loads(row["legacy"])
            except ValueError:
                legacy = {}
        if legacy:
            name = str(legacy.get("name") or "FIRE plan")
            name_key = str(legacy.get("name_key") or name.casefold())
            start_year = int(legacy.get("start_year") or derived_start_year)
            is_default = bool(legacy.get("is_default"))
            archived_at = None
            archived_text = legacy.get("archived_at")
            if archived_text:
                try:
                    archived_at = datetime.fromisoformat(str(archived_text).replace("Z", "+00:00"))
                except ValueError:
                    archived_at = None
            income_mode = str(legacy.get("income_mode") or "manual")
            expenses_mode = str(legacy.get("expenses_mode") or "manual")
            portfolio_mode = str(legacy.get("portfolio_mode") or "manual")
        else:
            name = "FIRE plan"
            name_key = "fire plan"
            # Satisfy the old start-year-relative checks while staying inside the horizon.
            age = min(row["horizon_age"] - 1, row["stop_working_age"])
            start_year = row["birth_year"] + max(0, age)
            is_default = True
            archived_at = None
            income_mode = expenses_mode = portfolio_mode = "manual"
        scenario_rows.append(
            {
                "id": row["id"],
                "workspace_id": row["workspace_id"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "name": name,
                "name_key": name_key,
                "currency": row["currency"],
                "birth_year": row["birth_year"],
                "start_year": start_year,
                "inflation_bp": row["inflation_bp"],
                "real_return_bp": row["real_return_bp"],
                "income_growth_bp": row["income_growth_bp"],
                "stop_working_age": row["stop_working_age"],
                "horizon_age": row["horizon_age"],
                "rule": row["rule"],
                "swr_bp": row["swr_bp"],
                "income_mode": income_mode,
                "income_manual_minor": row["income_minor"],
                "expenses_mode": expenses_mode,
                "expenses_manual_minor": row["expenses_minor"],
                "portfolio_mode": portfolio_mode,
                "portfolio_manual_minor": row["portfolio_minor"],
                "is_default": is_default,
                "archived_at": archived_at,
                "import_key": row["import_key"],
            }
        )
    if scenario_rows:
        connection.execute(sa.insert(_OLD_SCENARIOS), scenario_rows)

    contribution_rows: list[dict[str, Any]] = []
    event_rows: list[dict[str, Any]] = []
    for row in events:
        if row["kind"] == "saving_override":
            contribution_rows.append(
                {
                    "id": row["id"],
                    "workspace_id": row["workspace_id"],
                    "scenario_id": row["scenario_id"],
                    "year": row["year"],
                    "net_amount_minor": row["amount_minor"],
                    "import_key": row["import_key"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )
            continue
        event_rows.append(
            {
                "id": row["id"],
                "workspace_id": row["workspace_id"],
                "scenario_id": row["scenario_id"],
                "year": row["year"],
                "kind": row["kind"],
                "amount_minor": row["amount_minor"],
                "label": row["label"],
                "source_loan_id": None,
                "import_key": row["import_key"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )
    if contribution_rows:
        connection.execute(sa.insert(_OLD_CONTRIBUTIONS), contribution_rows)
    if event_rows:
        connection.execute(sa.insert(_OLD_EVENTS), event_rows)
