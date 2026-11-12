"""actual loan payments and unique balance checkpoints

Revision ID: 0054_f75_loan_payments
Revises: 0053_f74_loan_scenarios
Create Date: 2026-11-12

F7.5 records actual installments/overpayments and lets a payment link to one ledger
transaction. A balance checkpoint can exist only once per loan and date; F7.1 created at most
the starting checkpoint.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import UtcDateTime
from assetboard.domain.loans import MAX_NOTE_LENGTH
from assetboard.domain.transactions import MAX_AMOUNT_MINOR

revision = "0054_f75_loan_payments"
down_revision = "0053_f74_loan_scenarios"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("loan_balance_checkpoints", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            "uq_loan_balance_checkpoints_workspace_loan_as_of",
            ["workspace_id", "loan_id", "as_of"],
        )

    op.create_table(
        "loan_payments",
        sa.Column("loan_id", sa.Uuid(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("total_minor", sa.BigInteger(), nullable=False),
        sa.Column("principal_minor", sa.BigInteger(), nullable=False),
        sa.Column("interest_minor", sa.BigInteger(), nullable=False),
        sa.Column("costs_minor", sa.BigInteger(), nullable=False),
        sa.Column("overpayment_minor", sa.BigInteger(), nullable=False),
        sa.Column("note", sa.String(length=MAX_NOTE_LENGTH), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="manual"),
        sa.Column("transaction_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('installment', 'overpayment')",
            name=op.f("ck_loan_payments_kind_valid"),
        ),
        sa.CheckConstraint(
            "source IN ('manual', 'upcoming')",
            name=op.f("ck_loan_payments_source_valid"),
        ),
        sa.CheckConstraint(
            f"total_minor > 0 AND total_minor <= {MAX_AMOUNT_MINOR}",
            name=op.f("ck_loan_payments_total_valid"),
        ),
        sa.CheckConstraint(
            "principal_minor >= 0 AND principal_minor <= "
            f"{MAX_AMOUNT_MINOR} AND interest_minor >= 0 AND interest_minor <= "
            f"{MAX_AMOUNT_MINOR} AND costs_minor >= 0 AND costs_minor <= "
            f"{MAX_AMOUNT_MINOR} AND overpayment_minor >= 0 AND overpayment_minor <= "
            f"{MAX_AMOUNT_MINOR}",
            name=op.f("ck_loan_payments_components_valid"),
        ),
        sa.CheckConstraint(
            "total_minor = principal_minor + interest_minor + costs_minor + overpayment_minor",
            name=op.f("ck_loan_payments_total_matches_components"),
        ),
        sa.CheckConstraint(
            f"length(note) <= {MAX_NOTE_LENGTH}",
            name=op.f("ck_loan_payments_note_length"),
        ),
        sa.CheckConstraint(
            "date >= '1900-01-01' AND date <= '2199-12-31'",
            name=op.f("ck_loan_payments_date_in_range"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "loan_id"],
            ["loans.workspace_id", "loans.id"],
            name="fk_loan_payments_workspace_loan",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "transaction_id"],
            ["transactions.workspace_id", "transactions.id"],
            name="fk_loan_payments_workspace_transaction",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_loan_payments_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_loan_payments")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_loan_payments_workspace_id_id"),
    )
    with op.batch_alter_table("loan_payments", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_loan_payments_workspace_id"),
            ["workspace_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_loan_payments_workspace_loan_date",
            ["workspace_id", "loan_id", "date"],
            unique=False,
        )
        batch_op.create_index(
            "ix_loan_payments_workspace_transaction",
            ["workspace_id", "transaction_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("loan_payments", schema=None) as batch_op:
        batch_op.drop_index("ix_loan_payments_workspace_transaction")
        batch_op.drop_index("ix_loan_payments_workspace_loan_date")
        batch_op.drop_index(batch_op.f("ix_loan_payments_workspace_id"))
    op.drop_table("loan_payments")
    with op.batch_alter_table("loan_balance_checkpoints", schema=None) as batch_op:
        batch_op.drop_constraint("uq_loan_balance_checkpoints_workspace_loan_as_of", type_="unique")
