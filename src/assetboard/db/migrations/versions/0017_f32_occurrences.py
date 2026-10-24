"""F3.2 occurrence workflow

Revision ID: 0017_f32
Revises: 0016_f16_totp
Create Date: 2026-10-24 17:45:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import UtcDateTime

revision = "0017_f32"
down_revision = "0016_f16_totp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("transactions", schema=None) as batch_op:
        batch_op.create_unique_constraint("uq_transactions_workspace_id_id", ["workspace_id", "id"])

    with op.batch_alter_table("scheduled_items", schema=None) as batch_op:
        batch_op.add_column(sa.Column("auto_post", sa.Boolean(), nullable=True))
    op.execute(sa.text("UPDATE scheduled_items SET auto_post = false"))
    with op.batch_alter_table("scheduled_items", schema=None) as batch_op:
        batch_op.alter_column("auto_post", nullable=False)

    with op.batch_alter_table("scheduled_occurrences", schema=None) as batch_op:
        batch_op.add_column(sa.Column("nominal_date", sa.Date(), nullable=True))
        batch_op.add_column(sa.Column("generated_by", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("transaction_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("rescheduled_at", UtcDateTime(), nullable=True))
        batch_op.add_column(sa.Column("amount_overridden", sa.Boolean(), nullable=True))
        batch_op.drop_constraint("uq_scheduled_occurrences_item_due", type_="unique")

    op.execute(sa.text("UPDATE scheduled_occurrences SET nominal_date = due_date"))
    op.execute(sa.text("UPDATE scheduled_occurrences SET generated_by = 'rule'"))
    op.execute(sa.text("UPDATE scheduled_occurrences SET amount_overridden = false"))

    with op.batch_alter_table("scheduled_occurrences", schema=None) as batch_op:
        batch_op.alter_column("nominal_date", nullable=False)
        batch_op.alter_column("generated_by", nullable=False)
        batch_op.alter_column("amount_overridden", nullable=False)
        batch_op.create_unique_constraint(
            "uq_scheduled_occurrences_item_nominal",
            ["scheduled_item_id", "nominal_date"],
        )
        batch_op.create_check_constraint(
            op.f("ck_scheduled_occurrences_generated_by_valid"),
            "generated_by IN ('rule', 'manual')",
        )
        batch_op.create_foreign_key(
            "fk_scheduled_occurrences_workspace_transaction",
            "transactions",
            ["workspace_id", "transaction_id"],
            ["workspace_id", "id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("scheduled_occurrences", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_scheduled_occurrences_workspace_transaction", type_="foreignkey"
        )
        batch_op.drop_constraint(op.f("ck_scheduled_occurrences_generated_by_valid"), type_="check")
        batch_op.drop_constraint("uq_scheduled_occurrences_item_nominal", type_="unique")
        batch_op.drop_column("amount_overridden")
        batch_op.drop_column("rescheduled_at")
        batch_op.drop_column("transaction_id")
        batch_op.drop_column("generated_by")
        batch_op.drop_column("nominal_date")
        batch_op.create_unique_constraint(
            "uq_scheduled_occurrences_item_due", ["scheduled_item_id", "due_date"]
        )

    with op.batch_alter_table("scheduled_items", schema=None) as batch_op:
        batch_op.drop_column("auto_post")

    with op.batch_alter_table("transactions", schema=None) as batch_op:
        batch_op.drop_constraint("uq_transactions_workspace_id_id", type_="unique")
