"""F3.7 work calendar: day-level work entries and derived month work logs.

Revision ID: 0022_f37
Revises: 0021_f36
Create Date: 2026-10-25 18:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import DecimalType, UtcDateTime

revision = "0022_f37"
down_revision = "0021_f36"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "work_days",
        sa.Column("income_source_id", sa.Uuid(), nullable=False),
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("units", DecimalType(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("units > 0 AND units <= 24", name="ck_work_days_units_valid"),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name="fk_work_days_workspace_id_workspaces"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "income_source_id"],
            ["income_sources.workspace_id", "income_sources.id"],
            name="fk_work_days_workspace_source",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_work_days"),
        sa.UniqueConstraint(
            "workspace_id", "income_source_id", "work_date", name="uq_work_days_source_date"
        ),
    )
    op.create_index("ix_work_days_workspace_id", "work_days", ["workspace_id"])
    op.create_index(
        "ix_work_days_workspace_source_date",
        "work_days",
        ["workspace_id", "income_source_id", "work_date"],
    )
    with op.batch_alter_table("work_logs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("derived_from_days", sa.Boolean(), server_default=sa.false(), nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("work_logs", schema=None) as batch_op:
        batch_op.drop_column("derived_from_days")
    op.drop_index("ix_work_days_workspace_source_date", table_name="work_days")
    op.drop_index("ix_work_days_workspace_id", table_name="work_days")
    op.drop_table("work_days")
