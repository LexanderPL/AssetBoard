"""F3.6 notification channels, delivery log, and per-item reminder lead days.

Revision ID: 0021_f36
Revises: 0020_f34
Create Date: 2026-10-25 15:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import UtcDateTime

revision = "0021_f36"
down_revision = "0020_f34"
branch_labels = None
depends_on = None

MAX_LEAD_DAYS = 60


def upgrade() -> None:
    op.create_table(
        "notification_channels",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("url_encrypted", sa.Text(), nullable=False),
        sa.Column("scheme", sa.String(length=32), nullable=False),
        sa.Column("service_name", sa.String(length=100), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("digest_time", sa.Time(), nullable=False),
        sa.Column("default_lead_days", sa.Integer(), nullable=False),
        sa.Column("notify_due_soon", sa.Boolean(), nullable=False),
        sa.Column("notify_overdue", sa.Boolean(), nullable=False),
        sa.Column("notify_work_logs", sa.Boolean(), nullable=False),
        sa.Column("include_amounts", sa.Boolean(), nullable=False),
        sa.Column("last_run_on", sa.Date(), nullable=True),
        sa.Column("last_error", sa.String(length=32), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "mode IN ('digest', 'per_item')", name="ck_notification_channels_mode_valid"
        ),
        sa.CheckConstraint(
            f"default_lead_days BETWEEN 0 AND {MAX_LEAD_DAYS}",
            name="ck_notification_channels_default_lead_days_valid",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_notification_channels_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_notification_channels_workspace_id_workspaces",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_notification_channels"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_notification_channels_workspace_id_id"),
    )
    op.create_index(
        "ix_notification_channels_workspace_id", "notification_channels", ["workspace_id"]
    )
    op.create_index(
        "ix_notification_channels_workspace_user",
        "notification_channels",
        ["workspace_id", "user_id"],
    )

    op.create_table(
        "notification_log",
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("subject_key", sa.String(length=80), nullable=False),
        sa.Column("occurrence_id", sa.Uuid(), nullable=True),
        sa.Column("sent_on", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.String(length=32), nullable=True),
        sa.Column("attempted_at", UtcDateTime(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('due_soon', 'overdue', 'work_log')", name="ck_notification_log_kind_valid"
        ),
        sa.CheckConstraint(
            "status IN ('sending', 'sent', 'failed')", name="ck_notification_log_status_valid"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "channel_id"],
            ["notification_channels.workspace_id", "notification_channels.id"],
            name="fk_notification_log_workspace_channel",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_notification_log_workspace_id_workspaces",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_notification_log"),
        sa.UniqueConstraint(
            "channel_id", "kind", "subject_key", "sent_on", name="uq_notification_log_slot"
        ),
    )
    op.create_index("ix_notification_log_workspace_id", "notification_log", ["workspace_id"])
    op.create_index(
        "ix_notification_log_workspace_channel",
        "notification_log",
        ["workspace_id", "channel_id", "sent_on"],
    )

    with op.batch_alter_table("scheduled_items", schema=None) as batch_op:
        batch_op.add_column(sa.Column("reminder_lead_days", sa.Integer(), nullable=True))
        batch_op.create_check_constraint(
            "ck_scheduled_items_reminder_lead_days_valid",
            f"reminder_lead_days IS NULL OR reminder_lead_days BETWEEN 0 AND {MAX_LEAD_DAYS}",
        )


def downgrade() -> None:
    with op.batch_alter_table("scheduled_items", schema=None) as batch_op:
        batch_op.drop_constraint("ck_scheduled_items_reminder_lead_days_valid", type_="check")
        batch_op.drop_column("reminder_lead_days")
    op.drop_index("ix_notification_log_workspace_channel", table_name="notification_log")
    op.drop_index("ix_notification_log_workspace_id", table_name="notification_log")
    op.drop_table("notification_log")
    op.drop_index("ix_notification_channels_workspace_user", table_name="notification_channels")
    op.drop_index("ix_notification_channels_workspace_id", table_name="notification_channels")
    op.drop_table("notification_channels")
