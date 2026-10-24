"""email tokens and open registration setting

Revision ID: 0014_email_tokens
Revises: 0013_instance_settings
Create Date: 2026-10-19 12:00:00.000000

Pre-release B (ADR-0017): ``email_tokens`` holds the SHA-256 of registration and password reset
links sent by email; ``instance_settings.allow_registration`` (default false) turns open
registration on.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import UtcDateTime

revision = "0014_email_tokens"
down_revision = "0013_instance_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("instance_settings") as batch_op:
        batch_op.add_column(
            sa.Column("allow_registration", sa.Boolean(), server_default=sa.false(), nullable=False)
        )
    op.create_table(
        "email_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("locale", sa.String(length=8), nullable=True),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("expires_at", UtcDateTime(), nullable=False),
        sa.CheckConstraint(
            "length(token_hash) = 64", name=op.f("ck_email_tokens_token_hash_sha256_length")
        ),
        sa.CheckConstraint(
            "purpose IN ('register', 'reset')", name=op.f("ck_email_tokens_purpose_valid")
        ),
        sa.CheckConstraint("email = lower(email)", name=op.f("ck_email_tokens_email_lowercase")),
        sa.CheckConstraint(
            "(purpose = 'reset') = (user_id IS NOT NULL)",
            name=op.f("ck_email_tokens_reset_has_user"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_email_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_tokens")),
        sa.UniqueConstraint("token_hash", name="uq_email_tokens_token_hash"),
    )
    op.create_index("ix_email_tokens_purpose_email", "email_tokens", ["purpose", "email"])
    op.create_index("ix_email_tokens_expires_at", "email_tokens", ["expires_at"])
    op.create_index(op.f("ix_email_tokens_user_id"), "email_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_email_tokens_user_id"), table_name="email_tokens")
    op.drop_index("ix_email_tokens_expires_at", table_name="email_tokens")
    op.drop_index("ix_email_tokens_purpose_email", table_name="email_tokens")
    op.drop_table("email_tokens")
    with op.batch_alter_table("instance_settings") as batch_op:
        batch_op.drop_column("allow_registration")
