"""installment plans: fixed-count liabilities linked to a placeholder loan

Revision ID: 0056_f77_installment_plans
Revises: 0055_f76_loan_integrations
Create Date: 2026-11-12

F7.7. The plan's Loan row carries the common name/currency/payment day and a synthetic principal
and term; the plan row owns the fixed installment and remaining count.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import UtcDateTime
from assetboard.domain.loans import MAX_TERM_MONTHS
from assetboard.domain.transactions import MAX_AMOUNT_MINOR

revision = "0056_f77_installment_plans"
down_revision = "0055_f76_loan_integrations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("loans", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_loans_kind_valid"), type_="check")
        batch_op.drop_constraint(op.f("ck_loans_term_valid"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_loans_kind_valid"),
            "kind IN ('annuity', 'equal_principal', 'installment_plan')",
        )
        batch_op.create_check_constraint(
            op.f("ck_loans_term_valid"),
            "(kind = 'installment_plan' AND term_months BETWEEN 0 AND "
            f"{MAX_TERM_MONTHS}) OR (kind != 'installment_plan' AND "
            f"term_months BETWEEN 1 AND {MAX_TERM_MONTHS})",
        )

    op.create_table(
        "installment_plans",
        sa.Column("loan_id", sa.Uuid(), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.Column("installment_minor", sa.BigInteger(), nullable=False),
        sa.Column("remaining_count", sa.Integer(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("closed_at", UtcDateTime(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            f"installment_minor > 0 AND installment_minor <= {MAX_AMOUNT_MINOR}",
            name=op.f("ck_installment_plans_installment_valid"),
        ),
        sa.CheckConstraint(
            f"remaining_count >= 0 AND remaining_count <= {MAX_TERM_MONTHS}",
            name=op.f("ck_installment_plans_remaining_count_valid"),
        ),
        sa.CheckConstraint(
            "end_date >= '1900-01-01' AND end_date <= '2199-12-31'",
            name=op.f("ck_installment_plans_end_date_in_range"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "loan_id"],
            ["loans.workspace_id", "loans.id"],
            name="fk_installment_plans_workspace_loan",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "category_id"],
            ["categories.workspace_id", "categories.id"],
            name="fk_installment_plans_workspace_category",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_installment_plans_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_installment_plans")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_installment_plans_workspace_id_id"),
        sa.UniqueConstraint(
            "workspace_id",
            "loan_id",
            name="uq_installment_plans_workspace_loan",
        ),
    )
    with op.batch_alter_table("installment_plans", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_installment_plans_workspace_id"),
            ["workspace_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_installment_plans_workspace_loan",
            ["workspace_id", "loan_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("installment_plans", schema=None) as batch_op:
        batch_op.drop_index("ix_installment_plans_workspace_loan")
        batch_op.drop_index(batch_op.f("ix_installment_plans_workspace_id"))
    op.drop_table("installment_plans")
    with op.batch_alter_table("loans", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_loans_kind_valid"), type_="check")
        batch_op.drop_constraint(op.f("ck_loans_term_valid"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_loans_kind_valid"),
            "kind IN ('annuity', 'equal_principal')",
        )
        batch_op.create_check_constraint(
            op.f("ck_loans_term_valid"),
            f"term_months BETWEEN 1 AND {MAX_TERM_MONTHS}",
        )
