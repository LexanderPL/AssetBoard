"""One-time scheduled items: the ``once`` frequency.

Revision ID: 0023_one_time_items
Revises: 0022_f37
Create Date: 2026-10-26 12:00:00.000000

Downgrade keeps every one-time item as the equivalent recurring rule: monthly on the day of its
date, at most one occurrence. Its occurrences are untouched.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023_one_time_items"
down_revision = "0022_f37"
branch_labels = None
depends_on = None

NAME = "ck_scheduled_items_freq_valid"


def upgrade() -> None:
    with op.batch_alter_table("scheduled_items", schema=None) as batch_op:
        batch_op.drop_constraint(op.f(NAME), type_="check")
        batch_op.create_check_constraint(
            op.f(NAME), "freq IN ('once', 'weekly', 'monthly', 'yearly')"
        )


def downgrade() -> None:
    items = sa.table(
        "scheduled_items",
        sa.column("id", sa.Uuid()),
        sa.column("freq", sa.String()),
        sa.column("start_date", sa.Date()),
        sa.column("interval", sa.Integer()),
        sa.column("month_day", sa.String()),
        sa.column("max_count", sa.Integer()),
        sa.column("end_date", sa.Date()),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(items.c.id, items.c.start_date).where(items.c.freq == "once")
    ).all()
    for item_id, start_date in rows:
        connection.execute(
            items.update()
            .where(items.c.id == item_id)
            .values(
                freq="monthly",
                interval=1,
                month_day=str(start_date.day),
                max_count=1,
                end_date=None,
            )
        )
    with op.batch_alter_table("scheduled_items", schema=None) as batch_op:
        batch_op.drop_constraint(op.f(NAME), type_="check")
        batch_op.create_check_constraint(op.f(NAME), "freq IN ('weekly', 'monthly', 'yearly')")
