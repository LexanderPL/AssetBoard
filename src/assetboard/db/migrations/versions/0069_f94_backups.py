"""Scheduled database backups and restore support (F9.4)

Revision ID: 0069_f94_backups
Revises: 0068_f38_work_logs_import
Create Date: 2026-11-17

``backup_runs`` is instance-level operational history: no workspace owns it and only instance
administrators can read it. ``instance_settings`` gains the grandfather-father-son retention
counts used by the daily backup job.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import UtcDateTime

revision = "0069_f94_backups"
down_revision = "0068_f38_work_logs_import"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "backup_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("started_at", UtcDateTime(), nullable=False),
        sa.Column("finished_at", UtcDateTime(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("path", sa.String(length=255), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("error", sa.String(length=32), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'ok', 'failed')", name=op.f("ck_backup_runs_status_valid")
        ),
        sa.CheckConstraint(
            "origin IN ('scheduled', 'manual', 'pre_restore')",
            name=op.f("ck_backup_runs_origin_valid"),
        ),
        sa.CheckConstraint(
            "size_bytes IS NULL OR size_bytes >= 0",
            name=op.f("ck_backup_runs_size_bytes_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backup_runs")),
    )
    op.create_index(
        op.f("ix_backup_runs_started_at"),
        "backup_runs",
        ["started_at"],
        unique=False,
    )
    with op.batch_alter_table("instance_settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("backup_daily_retention", sa.Integer(), nullable=False, server_default="7")
        )
        batch_op.add_column(
            sa.Column("backup_weekly_retention", sa.Integer(), nullable=False, server_default="4")
        )
        batch_op.add_column(
            sa.Column("backup_monthly_retention", sa.Integer(), nullable=False, server_default="6")
        )
        batch_op.create_check_constraint(
            op.f("ck_instance_settings_backup_daily_retention_valid"),
            "backup_daily_retention BETWEEN 0 AND 365",
        )
        batch_op.create_check_constraint(
            op.f("ck_instance_settings_backup_weekly_retention_valid"),
            "backup_weekly_retention BETWEEN 0 AND 365",
        )
        batch_op.create_check_constraint(
            op.f("ck_instance_settings_backup_monthly_retention_valid"),
            "backup_monthly_retention BETWEEN 0 AND 365",
        )


def downgrade() -> None:
    with op.batch_alter_table("instance_settings", schema=None) as batch_op:
        batch_op.drop_constraint(
            op.f("ck_instance_settings_backup_daily_retention_valid"), type_="check"
        )
        batch_op.drop_constraint(
            op.f("ck_instance_settings_backup_weekly_retention_valid"), type_="check"
        )
        batch_op.drop_constraint(
            op.f("ck_instance_settings_backup_monthly_retention_valid"), type_="check"
        )
        batch_op.drop_column("backup_daily_retention")
        batch_op.drop_column("backup_weekly_retention")
        batch_op.drop_column("backup_monthly_retention")
    op.drop_index(op.f("ix_backup_runs_started_at"), table_name="backup_runs")
    op.drop_table("backup_runs")
