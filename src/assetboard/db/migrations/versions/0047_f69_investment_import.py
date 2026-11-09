"""standalone investment transaction import

Revision ID: 0047_f69_investment_import
Revises: 0046_f69_investment_archive
Create Date: 2026-11-09

Registers the standalone ``investment_transactions_csv`` importer as an import-batch source.
"""

from __future__ import annotations

from alembic import op

revision = "0047_f69_investment_import"
down_revision = "0046_f69_investment_archive"
branch_labels = None
depends_on = None

_OLD_SOURCES = (
    "source IN ('archive', 'transactions_csv', 'budget_lines_csv', 'categories_csv', "
    "'item_valuations_csv')"
)
_NEW_SOURCES = (
    "source IN ('archive', 'transactions_csv', 'budget_lines_csv', 'categories_csv', "
    "'item_valuations_csv', 'investment_transactions_csv')"
)


def upgrade() -> None:
    with op.batch_alter_table("import_batches", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_import_batches_source_valid"), type_="check")
        batch_op.create_check_constraint(op.f("ck_import_batches_source_valid"), _NEW_SOURCES)


def downgrade() -> None:
    with op.batch_alter_table("import_batches", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_import_batches_source_valid"), type_="check")
        batch_op.create_check_constraint(op.f("ck_import_batches_source_valid"), _OLD_SOURCES)
