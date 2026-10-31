"""highlight large transactions

Revision ID: 0025_amount_shading
Revises: 0024_tax_warning
Create Date: 2026-10-26 15:00:00.000000

``users.highlight_large_amounts``: the profile setting that tints larger rows on the
Transactions page. Existing accounts get ``true`` (on by default).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025_amount_shading"
down_revision = "0024_tax_warning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "highlight_large_amounts", sa.Boolean(), server_default=sa.true(), nullable=False
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("highlight_large_amounts")
