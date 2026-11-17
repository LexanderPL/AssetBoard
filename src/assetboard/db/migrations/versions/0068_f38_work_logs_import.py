"""allow the standalone work-log import source

Revision ID: 0068_f38_work_logs_import
Revises: 0067_f91_dashboard
Create Date: 2026-11-17

F3.8. A lone ``work_logs.csv`` can be merged by income source name.
"""

from __future__ import annotations

from alembic import op

revision = "0068_f38_work_logs_import"
down_revision = "0067_f91_dashboard"
branch_labels = None
depends_on = None

_OLD_SOURCES = (
    "source IN ('archive', 'transactions_csv', 'budget_lines_csv', 'categories_csv', "
    "'item_valuations_csv', 'investment_transactions_csv', 'reference_rate_values_csv', "
    "'loans_csv', 'loan_rate_changes_csv', 'reference_rates_csv', "
    "'loan_cost_components_csv', 'loan_overpayments_csv', 'loan_payments_csv', "
    "'loan_balance_checkpoints_csv', 'loan_scenarios_csv', 'installment_plans_csv')"
)
_NEW_SOURCES = (
    "source IN ('archive', 'transactions_csv', 'budget_lines_csv', 'categories_csv', "
    "'item_valuations_csv', 'investment_transactions_csv', 'reference_rate_values_csv', "
    "'loans_csv', 'loan_rate_changes_csv', 'reference_rates_csv', "
    "'loan_cost_components_csv', 'loan_overpayments_csv', 'loan_payments_csv', "
    "'loan_balance_checkpoints_csv', 'loan_scenarios_csv', 'installment_plans_csv', "
    "'work_logs_csv')"
)


def upgrade() -> None:
    with op.batch_alter_table("import_batches", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_import_batches_source_valid"), type_="check")
        batch_op.create_check_constraint(op.f("ck_import_batches_source_valid"), _NEW_SOURCES)


def downgrade() -> None:
    with op.batch_alter_table("import_batches", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_import_batches_source_valid"), type_="check")
        batch_op.create_check_constraint(op.f("ck_import_batches_source_valid"), _OLD_SOURCES)
