"""Automatic portfolio links in net worth

Revision ID: 0049_auto_link_portfolio
Revises: 0048_f610_income_splits
Create Date: 2026-11-09

Adds the workspace switch that keeps one linked net-worth item per active asset class with a
value. Existing workspaces keep it off (the server default); newly created workspaces set it on
explicitly in ``create_owned_workspace``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0049_auto_link_portfolio"
down_revision = "0048_f610_income_splits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("workspaces", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "auto_link_portfolio", sa.Boolean(), server_default=sa.false(), nullable=False
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("workspaces", schema=None) as batch_op:
        batch_op.drop_column("auto_link_portfolio")
