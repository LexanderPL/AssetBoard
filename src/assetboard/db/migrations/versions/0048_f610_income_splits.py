"""investment income and splits

Revision ID: 0048_f610_income_splits
Revises: 0047_f69_investment_import
Create Date: 2026-11-10

Adds the optional link from a dividend transaction to its ledger income row and allows the
``investment`` transaction source. Dividends and splits reuse the existing quantity/unit_price/fee
columns (see docs/archive-format.md), so no amount or ratio columns are added.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0048_f610_income_splits"
down_revision = "0047_f69_investment_import"
branch_labels = None
depends_on = None

_OLD_SOURCES = "source IN ('manual', 'recurring', 'recurring_auto', 'import')"
_NEW_SOURCES = "source IN ('manual', 'recurring', 'recurring_auto', 'import', 'investment')"


def upgrade() -> None:
    with op.batch_alter_table("transactions", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_transactions_source_valid"), type_="check")
        batch_op.create_check_constraint(op.f("ck_transactions_source_valid"), _NEW_SOURCES)
    with op.batch_alter_table("investment_transactions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("income_transaction_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_investment_transactions_workspace_income",
            "transactions",
            ["workspace_id", "income_transaction_id"],
            ["workspace_id", "id"],
        )
    op.create_index(
        "ix_investment_transactions_workspace_income",
        "investment_transactions",
        ["workspace_id", "income_transaction_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_investment_transactions_workspace_income",
        table_name="investment_transactions",
    )
    with op.batch_alter_table("investment_transactions", schema=None) as batch_op:
        batch_op.drop_constraint("fk_investment_transactions_workspace_income", type_="foreignkey")
        batch_op.drop_column("income_transaction_id")
    # The old schema has no ``investment`` source; keep its ledger rows under a valid value.
    op.execute("UPDATE transactions SET source = 'import' WHERE source = 'investment'")
    with op.batch_alter_table("transactions", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_transactions_source_valid"), type_="check")
        batch_op.create_check_constraint(op.f("ck_transactions_source_valid"), _OLD_SOURCES)
