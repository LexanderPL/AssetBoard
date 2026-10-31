"""workspace upcoming window

Revision ID: 0029_upcoming_days
Revises: 0028_taxes_redesign
Create Date: 2026-10-31 18:00:00.000000

``workspaces.upcoming_days``: how many days ahead Upcoming payments and the Home page look
(1 to 365, validated by the service). NULL, the value of every existing workspace, means the
instance setting ``ASSETBOARD_UPCOMING_DAYS`` applies.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0029_upcoming_days"
down_revision = "0028_taxes_redesign"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("workspaces", schema=None) as batch_op:
        batch_op.add_column(sa.Column("upcoming_days", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("workspaces", schema=None) as batch_op:
        batch_op.drop_column("upcoming_days")
