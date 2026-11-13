"""FIRE scenarios, contribution overrides and one-off events

Revision ID: 0059_f81_fire_scenarios
Revises: 0058_loan_mixed_rate_timeline
Create Date: 2026-11-13

F8.1. A scenario snapshots its currency and start year at creation. Active names are unique
ignoring case, and at most one active scenario is the default; both rules are partial unique
indexes so archived rows may reuse a name. Contribution and event amounts are signed minor
units (see the model docstring and the M8 spec).
"""

from __future__ import annotations

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

revision = "0059_f81_fire_scenarios"
down_revision = "0058_loan_mixed_rate_timeline"
branch_labels = None
depends_on = None


def upgrade() -> None:
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

    op.create_table(
        "fire_contributions",
        sa.Column("scenario_id", sa.Uuid(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("net_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
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


def downgrade() -> None:
    op.drop_index("ix_fire_events_workspace_scenario_year", table_name="fire_events")
    op.drop_index(op.f("ix_fire_events_workspace_id"), table_name="fire_events")
    op.drop_table("fire_events")
    op.drop_index(op.f("ix_fire_contributions_workspace_id"), table_name="fire_contributions")
    op.drop_table("fire_contributions")
    op.drop_index("uq_fire_scenarios_active_default", table_name="fire_scenarios")
    op.drop_index("uq_fire_scenarios_active_name_key", table_name="fire_scenarios")
    op.drop_index("ix_fire_scenarios_workspace_name", table_name="fire_scenarios")
    op.drop_index(op.f("ix_fire_scenarios_workspace_id"), table_name="fire_scenarios")
    op.drop_table("fire_scenarios")
