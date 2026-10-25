"""F3.2 fix: remember the transaction an occurrence was posted as after it is deleted.

Revision ID: 0019_f32_prev_txn
Revises: 0018_f33
Create Date: 2026-10-25 10:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_f32_prev_txn"
down_revision = "0018_f33"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("scheduled_occurrences", schema=None) as batch_op:
        batch_op.add_column(sa.Column("previous_transaction_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_scheduled_occurrences_workspace_previous_transaction",
            "transactions",
            ["workspace_id", "previous_transaction_id"],
            ["workspace_id", "id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("scheduled_occurrences", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_scheduled_occurrences_workspace_previous_transaction", type_="foreignkey"
        )
        batch_op.drop_column("previous_transaction_id")
