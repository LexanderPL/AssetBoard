"""Store the personal sidebar order and visibility (F9.12)

Revision ID: 0065_f912_sidebar_items
Revises: 0064_f812_fire_loan_settings
Create Date: 2026-11-17

``users.sidebar_items`` is nullable JSON: ``NULL`` means the canonical order with every
destination shown, as for every existing account and every account that never customises it.
The list holds ``{"key": ..., "visible": ...}`` entries; services and templates normalise
malformed stored data instead of failing.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0065_f912_sidebar_items"
down_revision = "0064_f812_fire_loan_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("sidebar_items", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("sidebar_items")
