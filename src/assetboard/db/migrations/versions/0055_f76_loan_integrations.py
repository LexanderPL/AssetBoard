"""loan integrations: property link, auto-record setting and from-loan scheduled items

Revision ID: 0055_f76_loan_integrations
Revises: 0054_f75_loan_payments
Create Date: 2026-11-12

F7.6 links a loan to its property (a net-worth asset item) and lets one scheduled item derive
its expected installment from the loan schedule. Existing scheduled items keep positive fixed
or estimate amounts; only ``from_loan`` items store zero and carry a loan reference.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.domain.transactions import MAX_AMOUNT_MINOR

revision = "0055_f76_loan_integrations"
down_revision = "0054_f75_loan_payments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("loans", schema=None) as batch_op:
        batch_op.add_column(sa.Column("property_item_id", sa.Uuid(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "auto_record_payments",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.create_foreign_key(
            "fk_loans_workspace_property_item",
            "networth_items",
            ["workspace_id", "property_item_id"],
            ["workspace_id", "id"],
        )
        batch_op.create_index(
            "ix_loans_workspace_property_item",
            ["workspace_id", "property_item_id"],
            unique=False,
        )

    with op.batch_alter_table("scheduled_items", schema=None) as batch_op:
        batch_op.add_column(sa.Column("loan_id", sa.Uuid(), nullable=True))
        batch_op.drop_constraint(op.f("ck_scheduled_items_amount_mode_valid"), type_="check")
        batch_op.drop_constraint(op.f("ck_scheduled_items_amount_valid"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_scheduled_items_amount_mode_valid"),
            "amount_mode IN ('fixed', 'estimate', 'from_loan')",
        )
        batch_op.create_check_constraint(
            op.f("ck_scheduled_items_amount_valid"),
            f"amount_minor = 0 OR (amount_minor > 0 AND amount_minor <= {MAX_AMOUNT_MINOR})",
        )
        batch_op.create_check_constraint(
            op.f("ck_scheduled_items_loan_reference_valid"),
            "(amount_mode = 'from_loan' AND loan_id IS NOT NULL) OR "
            "(amount_mode != 'from_loan' AND loan_id IS NULL)",
        )
        batch_op.create_check_constraint(
            op.f("ck_scheduled_items_amount_mode_value_valid"),
            "(amount_mode = 'from_loan' AND amount_minor = 0) OR "
            "(amount_mode != 'from_loan' AND amount_minor > 0)",
        )
        batch_op.create_foreign_key(
            "fk_scheduled_items_workspace_loan",
            "loans",
            ["workspace_id", "loan_id"],
            ["workspace_id", "id"],
        )
        batch_op.create_index(
            "ix_scheduled_items_workspace_loan",
            ["workspace_id", "loan_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("scheduled_items", schema=None) as batch_op:
        batch_op.drop_index("ix_scheduled_items_workspace_loan")
        batch_op.drop_constraint("fk_scheduled_items_workspace_loan", type_="foreignkey")
        batch_op.drop_constraint(op.f("ck_scheduled_items_amount_mode_value_valid"), type_="check")
        batch_op.drop_constraint(op.f("ck_scheduled_items_loan_reference_valid"), type_="check")
        batch_op.drop_constraint(op.f("ck_scheduled_items_amount_valid"), type_="check")
        batch_op.drop_constraint(op.f("ck_scheduled_items_amount_mode_valid"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_scheduled_items_amount_mode_valid"), "amount_mode IN ('fixed', 'estimate')"
        )
        batch_op.create_check_constraint(
            op.f("ck_scheduled_items_amount_valid"),
            f"amount_minor > 0 AND amount_minor <= {MAX_AMOUNT_MINOR}",
        )
        batch_op.drop_column("loan_id")

    with op.batch_alter_table("loans", schema=None) as batch_op:
        batch_op.drop_index("ix_loans_workspace_property_item")
        batch_op.drop_constraint("fk_loans_workspace_property_item", type_="foreignkey")
        batch_op.drop_column("auto_record_payments")
        batch_op.drop_column("property_item_id")
