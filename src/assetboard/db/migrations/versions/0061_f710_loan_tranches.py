"""partial loan transfers and interest-only phase

Revision ID: 0061_f710_loan_tranches
Revises: 0060_f84_fire_expense_exclusions
Create Date: 2026-11-14

F7.10. A loan can be paid out in tranches; interest accrues only on the transferred amount.
Existing loans default to the full principal at origination and no interest-only phase.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0061_f710_loan_tranches"
down_revision = "0060_f84_fire_expense_exclusions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("loans", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "drawdown_mode",
                sa.String(length=24),
                nullable=False,
                server_default="full_at_origination",
            )
        )
        batch_op.add_column(
            sa.Column(
                "interest_only_months",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.create_check_constraint(
            op.f("ck_loans_drawdown_mode_valid"),
            "drawdown_mode IN ('full_at_origination', 'tranches')",
        )
        batch_op.create_check_constraint(
            op.f("ck_loans_interest_only_valid"),
            "(kind = 'installment_plan' AND interest_only_months = 0) OR "
            "(kind != 'installment_plan' AND interest_only_months >= 0 AND "
            "interest_only_months < term_months)",
        )

    op.create_table(
        "loan_transfers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("loan_id", sa.Uuid(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("note", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "amount_minor > 0 AND amount_minor <= 999999999999999",
            name=op.f("ck_loan_transfers_amount_valid"),
        ),
        sa.CheckConstraint("length(note) <= 200", name=op.f("ck_loan_transfers_note_length")),
        sa.CheckConstraint(
            "date >= '1900-01-01' AND date <= '2199-12-31'",
            name=op.f("ck_loan_transfers_date_in_range"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_loan_transfers_workspace_id_workspaces"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "loan_id"],
            ["loans.workspace_id", "loans.id"],
            name="fk_loan_transfers_workspace_loan",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_loan_transfers")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_loan_transfers_workspace_id_id"),
    )
    op.create_index(op.f("ix_loan_transfers_workspace_id"), "loan_transfers", ["workspace_id"])
    op.create_index(
        "ix_loan_transfers_workspace_loan_date",
        "loan_transfers",
        ["workspace_id", "loan_id", "date"],
    )


def downgrade() -> None:
    op.drop_index("ix_loan_transfers_workspace_loan_date", table_name="loan_transfers")
    op.drop_index(op.f("ix_loan_transfers_workspace_id"), table_name="loan_transfers")
    op.drop_table("loan_transfers")
    with op.batch_alter_table("loans", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_loans_interest_only_valid"), type_="check")
        batch_op.drop_constraint(op.f("ck_loans_drawdown_mode_valid"), type_="check")
        batch_op.drop_column("interest_only_months")
        batch_op.drop_column("drawdown_mode")
