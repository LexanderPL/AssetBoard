"""FIRE scenario expense-category exclusions

Revision ID: 0060_f84_fire_expense_exclusions
Revises: 0059_f81_fire_scenarios
Create Date: 2026-11-14

F8.4. Each row excludes one workspace expense category from a scenario's linked trailing
expenses. Composite foreign keys keep the scenario and category in the same workspace; the
unique constraint makes reconciliation by category id idempotent. The table is outside the
archive format until F8.6.

The same revision adds ``fire_events.source_loan_id`` for one-click loan-payoff events: the loan
lineage makes the action idempotent regardless of the current translation. The composite foreign
key keeps the loan in the scenario's workspace; it restricts deletion because loans are archived,
never hard-deleted, and a composite SET NULL cannot preserve the non-null ``workspace_id``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import UtcDateTime

revision = "0060_f84_fire_expense_exclusions"
down_revision = "0059_f81_fire_scenarios"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fire_expense_exclusions",
        sa.Column("scenario_id", sa.Uuid(), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_fire_expense_exclusions_workspace_id_workspaces"),
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

    # A one-click loan-payoff event remembers its loan, so idempotency never depends on the
    # translated label. Loans are archived, never hard-deleted; SET NULL on the composite key
    # would also clear the non-null workspace_id, so deletion is restricted instead.
    with op.batch_alter_table("fire_events", schema=None) as batch_op:
        batch_op.add_column(sa.Column("source_loan_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_fire_events_workspace_source_loan",
            "loans",
            ["workspace_id", "source_loan_id"],
            ["workspace_id", "id"],
            ondelete="RESTRICT",
        )
        batch_op.create_index(
            "ix_fire_events_workspace_source_loan",
            ["workspace_id", "source_loan_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("fire_events", schema=None) as batch_op:
        batch_op.drop_index("ix_fire_events_workspace_source_loan")
        batch_op.drop_constraint("fk_fire_events_workspace_source_loan", type_="foreignkey")
        batch_op.drop_column("source_loan_id")

    op.drop_index(
        "ix_fire_expense_exclusions_workspace_scenario",
        table_name="fire_expense_exclusions",
    )
    op.drop_index(
        op.f("ix_fire_expense_exclusions_workspace_id"),
        table_name="fire_expense_exclusions",
    )
    op.drop_table("fire_expense_exclusions")
