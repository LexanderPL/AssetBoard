"""estimate values between entries

Revision ID: 0037_f56_estimate_between
Revises: 0036_f55_net_worth_archive
Create Date: 2026-11-03

Adds the per-item straight-line backfill switch. Existing assets default on and existing
liabilities off; the temporary server default is removed after the data update.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0037_f56_estimate_between"
down_revision = "0036_f55_net_worth_archive"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("networth_items", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "estimate_between",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
    items = sa.table(
        "networth_items",
        sa.column("side", sa.String(length=16)),
        sa.column("estimate_between", sa.Boolean()),
    )
    op.execute(
        items.update().where(items.c.side == "liability").values(estimate_between=sa.false())
    )
    with op.batch_alter_table("networth_items", schema=None) as batch_op:
        batch_op.alter_column("estimate_between", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("networth_items", schema=None) as batch_op:
        batch_op.drop_column("estimate_between")
