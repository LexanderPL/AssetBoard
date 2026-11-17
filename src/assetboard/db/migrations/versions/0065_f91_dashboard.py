"""Per-user Home widget preferences (F9.1)

Revision ID: 0065_f91_dashboard
Revises: 0064_f812_fire_loan_settings
Create Date: 2026-11-17

One row per (workspace, user) holds the ordered list of enabled Home widgets. No row means the
default catalogue, so existing users need no backfill. The table is workspace-owned and cascades
with the workspace; deleting a user also removes their rows.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import UtcDateTime

revision = "0065_f91_dashboard"
down_revision = "0064_f812_fire_loan_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dashboard_preferences",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("widgets", sa.JSON(), nullable=False),
        sa.Column("customised_at", UtcDateTime(), nullable=True),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_dashboard_preferences_workspace_id_workspaces",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_dashboard_preferences_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_dashboard_preferences"),
        sa.UniqueConstraint(
            "workspace_id", "user_id", name="uq_dashboard_preferences_workspace_user"
        ),
    )
    op.create_index(
        "ix_dashboard_preferences_workspace_id",
        "dashboard_preferences",
        ["workspace_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_dashboard_preferences_workspace_id", table_name="dashboard_preferences")
    op.drop_table("dashboard_preferences")
