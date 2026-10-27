"""Short-lived login tickets for two-step TOTP sign-in.

Revision ID: 0024_two_step_totp_login
Revises: 0023_one_time_items
Create Date: 2026-10-27 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024_two_step_totp_login"
down_revision = "0023_one_time_items"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "login_tickets",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("password_hash", sa.String(length=512), nullable=False),
        sa.Column("replacement_password_hash", sa.String(length=512), nullable=True),
        sa.Column("next_path", sa.String(length=512), nullable=False),
        sa.Column("ip_key", sa.String(length=64), nullable=False),
        sa.Column("ip_window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ip_block_set", sa.DateTime(timezone=True), nullable=True),
        sa.Column("code_attempted", sa.Boolean(), nullable=False),
        sa.Column("require_instance_admin", sa.Boolean(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(token_hash) = 64", name=op.f("ck_login_tickets_token_hash_sha256_length")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
            name=op.f("fk_login_tickets_user_id_users"),
        ),
        sa.PrimaryKeyConstraint("token_hash", name=op.f("pk_login_tickets")),
    )
    op.create_index("ix_login_tickets_expires_at", "login_tickets", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_login_tickets_expires_at", table_name="login_tickets")
    op.drop_table("login_tickets")
