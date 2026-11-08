"""investment transaction settlement and bulk batch id

Revision ID: 0044_f62a_settlement
Revises: 0043_f65_bond_terms
Create Date: 2026-11-08

F6.2a. Three nullable columns keep the amounts of a row entered in the workspace base currency
(set all or none, enforced by a check constraint), and a nullable ``import_batch_id`` groups the
rows of one bulk add. Existing rows stay null and behave exactly as before.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import DecimalType

revision = "0044_f62a_settlement"
down_revision = "0043_f65_bond_terms"
branch_labels = None
depends_on = None

# A copy of the model's check (migrations must not change with later model edits).
SETTLEMENT_CHECK = (
    "(settlement_currency IS NULL AND settlement_unit_price IS NULL "
    "AND settlement_fee_minor IS NULL) OR "
    "(settlement_currency IS NOT NULL AND settlement_unit_price IS NOT NULL "
    "AND settlement_fee_minor IS NOT NULL "
    "AND length(settlement_currency) = 3 "
    "AND settlement_fee_minor >= 0 AND settlement_fee_minor <= 999999999999999)"
)


def upgrade() -> None:
    with op.batch_alter_table("investment_transactions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("settlement_currency", sa.String(length=3), nullable=True))
        batch_op.add_column(sa.Column("settlement_unit_price", DecimalType(), nullable=True))
        batch_op.add_column(sa.Column("settlement_fee_minor", sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column("import_batch_id", sa.Uuid(), nullable=True))
        batch_op.create_check_constraint(
            "ck_investment_transactions_settlement_all_or_none", SETTLEMENT_CHECK
        )
        batch_op.create_index(
            "ix_investment_transactions_workspace_import_batch",
            ["workspace_id", "import_batch_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("investment_transactions", schema=None) as batch_op:
        batch_op.drop_index("ix_investment_transactions_workspace_import_batch")
        batch_op.drop_constraint("ck_investment_transactions_settlement_all_or_none", type_="check")
        batch_op.drop_column("import_batch_id")
        batch_op.drop_column("settlement_fee_minor")
        batch_op.drop_column("settlement_unit_price")
        batch_op.drop_column("settlement_currency")
