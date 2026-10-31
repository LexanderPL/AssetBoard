"""opt-in update check

Revision ID: 0030_update_check
Revises: 0029_upcoming_days
Create Date: 2026-10-31 20:00:00.000000

``instance_settings.check_for_updates`` (default off, ADR-0023): once a day, ask GitHub for the
latest release. ``latest_release_tag`` and ``update_checked_at`` keep the last successful
result on the same singleton row.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import UtcDateTime

revision = "0030_update_check"
down_revision = "0029_upcoming_days"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("instance_settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("check_for_updates", sa.Boolean(), server_default=sa.false(), nullable=False)
        )
        batch_op.add_column(sa.Column("latest_release_tag", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("update_checked_at", UtcDateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("instance_settings", schema=None) as batch_op:
        batch_op.drop_column("update_checked_at")
        batch_op.drop_column("latest_release_tag")
        batch_op.drop_column("check_for_updates")
