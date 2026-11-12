"""allow the loan archive sources on import batches

Revision ID: 0057_f78_loan_import_sources
Revises: 0056_f77_installment_plans
Create Date: 2026-11-12

F7.8. A lone ``reference_rate_values.csv`` is importable; the other loan archive files are
recognised so they can be refused as restore-only instead of being mistaken for a ledger CSV.
"""

from __future__ import annotations

from alembic import op

revision = "0057_f78_loan_import_sources"
down_revision = "0056_f77_installment_plans"
branch_labels = None
depends_on = None

_OLD_SOURCES = (
    "source IN ('archive', 'transactions_csv', 'budget_lines_csv', 'categories_csv', "
    "'item_valuations_csv', 'investment_transactions_csv')"
)
_NEW_SOURCES = (
    "source IN ('archive', 'transactions_csv', 'budget_lines_csv', 'categories_csv', "
    "'item_valuations_csv', 'investment_transactions_csv', 'reference_rate_values_csv', "
    "'loans_csv', 'loan_rate_changes_csv', 'reference_rates_csv', "
    "'loan_cost_components_csv', 'loan_overpayments_csv', 'loan_payments_csv', "
    "'loan_balance_checkpoints_csv', 'loan_scenarios_csv', 'installment_plans_csv')"
)


def upgrade() -> None:
    with op.batch_alter_table("import_batches", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_import_batches_source_valid"), type_="check")
        batch_op.create_check_constraint(op.f("ck_import_batches_source_valid"), _NEW_SOURCES)


def downgrade() -> None:
    with op.batch_alter_table("import_batches", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_import_batches_source_valid"), type_="check")
        batch_op.create_check_constraint(op.f("ck_import_batches_source_valid"), _OLD_SOURCES)
