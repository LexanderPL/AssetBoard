"""Remember account dismissal of the user-entered tax rates warning.

Revision ID: 0024_tax_warning_dismissal
Revises: 0023_one_time_items
Create Date: 2026-10-27 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024_tax_warning_dismissal"
down_revision = "0023_one_time_items"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "tax_warning_dismissed",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("tax_warning_dismissed")
