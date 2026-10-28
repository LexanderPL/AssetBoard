"""Remember a dismissed tax warning on the account.

Revision ID: 0024_tax_warning
Revises: 0023_one_time_items
Create Date: 2026-10-28 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024_tax_warning"
down_revision = "0023_one_time_items"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "tax_warning_dismissed",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("tax_warning_dismissed")
