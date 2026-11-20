"""Index user_sessions.expires_at for the daily session prune (F11.11)

Revision ID: 0072_f1111_session_prune
Revises: 0071_f91_home_widget_split
Create Date: 2026-11-20

The daily ``session-prune`` job deletes rows whose absolute expiry or idle expiry passed. The
existing (user_id, expires_at) index cannot serve the global expiry scan; this adds the missing
one. No data migration: the first job run removes already-expired rows.
"""

from __future__ import annotations

from alembic import op

revision = "0072_f1111_session_prune"
down_revision = "0071_f91_home_widget_split"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        op.f("ix_user_sessions_expires_at"), "user_sessions", ["expires_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_user_sessions_expires_at"), table_name="user_sessions")
