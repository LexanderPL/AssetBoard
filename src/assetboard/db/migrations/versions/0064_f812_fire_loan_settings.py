"""Persist the FIRE plan's ignored-loan choices (F8.12)

Revision ID: 0064_f812_fire_loan_settings
Revises: 0063_f88_one_fire_plan
Create Date: 2026-11-15

One row records an explicit choice that differs from the default (a loan is included when no
row exists). Composite workspace foreign keys keep the row inside the plan's workspace and make
it cascade with the plan and stay restricted while the loan exists. Existing plans need no
backfill: every loan is included by default.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import UtcDateTime

revision = "0064_f812_fire_loan_settings"
down_revision = "0063_f88_one_fire_plan"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fire_loan_settings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("scenario_id", sa.Uuid(), nullable=False),
        sa.Column("loan_id", sa.Uuid(), nullable=False),
        sa.Column("included", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_fire_loan_settings_workspace_id_workspaces",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "scenario_id"],
            ["fire_scenarios.workspace_id", "fire_scenarios.id"],
            name="fk_fire_loan_settings_workspace_scenario",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "loan_id"],
            ["loans.workspace_id", "loans.id"],
            name="fk_fire_loan_settings_workspace_loan",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_fire_loan_settings"),
        sa.UniqueConstraint(
            "workspace_id", "scenario_id", "loan_id", name="uq_fire_loan_settings_plan_loan"
        ),
    )
    op.create_index(
        "ix_fire_loan_settings_workspace_id",
        "fire_loan_settings",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        "ix_fire_loan_settings_workspace_scenario",
        "fire_loan_settings",
        ["workspace_id", "scenario_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_fire_loan_settings_workspace_scenario", table_name="fire_loan_settings")
    op.drop_index("ix_fire_loan_settings_workspace_id", table_name="fire_loan_settings")
    op.drop_table("fire_loan_settings")
