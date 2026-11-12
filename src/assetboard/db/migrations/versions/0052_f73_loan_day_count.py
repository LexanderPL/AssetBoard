"""loan day count: monthly or actual/365

Revision ID: 0052_f73_loan_day_count
Revises: 0051_f71_loans
Create Date: 2026-11-12

F7.3 exposes the F7.2 schedule engine's day-count choice. Existing loans keep the monthly
convention through the server default, so the migration is data-preserving.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0052_f73_loan_day_count"
down_revision = "0051_f71_loans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("loans", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "day_count",
                sa.String(length=16),
                nullable=False,
                server_default=sa.text("'monthly'"),
            )
        )
        batch_op.create_check_constraint(
            "ck_loans_day_count_valid", "day_count IN ('monthly', 'actual_365')"
        )


def downgrade() -> None:
    with op.batch_alter_table("loans", schema=None) as batch_op:
        batch_op.drop_constraint("ck_loans_day_count_valid", type_="check")
        batch_op.drop_column("day_count")
