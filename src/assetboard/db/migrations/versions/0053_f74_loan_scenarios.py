"""loan what-if scenarios: named overpayment, strategy, rate shift and lump-sum variations

Revision ID: 0053_f74_loan_scenarios
Revises: 0052_f73_loan_day_count
Create Date: 2026-11-12

F7.4. A scenario is a named variation of one loan's plan. Null parameters mean "same as the
loan"; a null rate shift and a null one-off pair mean the scenario does not change them. The
composite foreign key keeps a scenario inside its loan's workspace.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import UtcDateTime
from assetboard.domain.loans import MAX_LOAN_RATE_BP
from assetboard.domain.transactions import MAX_AMOUNT_MINOR

revision = "0053_f74_loan_scenarios"
down_revision = "0052_f73_loan_day_count"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "loan_scenarios",
        sa.Column("loan_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("name_key", sa.String(length=255), nullable=False),
        sa.Column("overpayment_mode", sa.String(length=16), nullable=True),
        sa.Column("overpayment_amount_minor", sa.BigInteger(), nullable=True),
        sa.Column("overpayment_strategy", sa.String(length=16), nullable=True),
        sa.Column("rate_shift_bp", sa.Integer(), nullable=True),
        sa.Column("rate_shift_from", sa.Date(), nullable=True),
        sa.Column("one_off_date", sa.Date(), nullable=True),
        sa.Column("one_off_amount_minor", sa.BigInteger(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "length(name) > 0",
            name=op.f("ck_loan_scenarios_name_not_empty"),
        ),
        sa.CheckConstraint(
            "overpayment_mode IS NULL OR "
            "overpayment_mode IN ('none', 'fixed_extra', 'target_total')",
            name=op.f("ck_loan_scenarios_overpayment_mode_valid"),
        ),
        sa.CheckConstraint(
            "overpayment_strategy IS NULL OR "
            "overpayment_strategy IN ('reduce_term', 'reduce_installment')",
            name=op.f("ck_loan_scenarios_overpayment_strategy_valid"),
        ),
        sa.CheckConstraint(
            "(overpayment_mode IS NULL AND overpayment_amount_minor IS NULL) OR "
            "(overpayment_mode = 'none' AND overpayment_amount_minor IS NULL) OR "
            "(overpayment_mode IN ('fixed_extra', 'target_total') AND "
            "overpayment_amount_minor IS NOT NULL AND overpayment_amount_minor > 0 AND "
            f"overpayment_amount_minor <= {MAX_AMOUNT_MINOR})",
            name=op.f("ck_loan_scenarios_overpayment_amount_valid"),
        ),
        sa.CheckConstraint(
            "(rate_shift_bp IS NULL AND rate_shift_from IS NULL) OR "
            "(rate_shift_bp IS NOT NULL AND rate_shift_bp != 0 AND "
            f"rate_shift_bp BETWEEN -{MAX_LOAN_RATE_BP} AND {MAX_LOAN_RATE_BP} AND "
            "rate_shift_from IS NOT NULL)",
            name=op.f("ck_loan_scenarios_rate_shift_valid"),
        ),
        sa.CheckConstraint(
            "(one_off_date IS NULL AND one_off_amount_minor IS NULL) OR "
            "(one_off_date IS NOT NULL AND one_off_amount_minor IS NOT NULL AND "
            f"one_off_amount_minor > 0 AND one_off_amount_minor <= {MAX_AMOUNT_MINOR})",
            name=op.f("ck_loan_scenarios_one_off_valid"),
        ),
        sa.CheckConstraint(
            "rate_shift_from >= '1900-01-01' AND rate_shift_from <= '2199-12-31'",
            name=op.f("ck_loan_scenarios_rate_shift_from_in_range"),
        ),
        sa.CheckConstraint(
            "one_off_date >= '1900-01-01' AND one_off_date <= '2199-12-31'",
            name=op.f("ck_loan_scenarios_one_off_date_in_range"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "loan_id"],
            ["loans.workspace_id", "loans.id"],
            name="fk_loan_scenarios_workspace_loan",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_loan_scenarios_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_loan_scenarios")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_loan_scenarios_workspace_id_id"),
        sa.UniqueConstraint(
            "workspace_id",
            "loan_id",
            "name_key",
            name="uq_loan_scenarios_workspace_loan_name_key",
        ),
    )
    with op.batch_alter_table("loan_scenarios", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_loan_scenarios_workspace_id"),
            ["workspace_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_loan_scenarios_workspace_loan_name",
            ["workspace_id", "loan_id", "name"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("loan_scenarios", schema=None) as batch_op:
        batch_op.drop_index("ix_loan_scenarios_workspace_loan_name")
        batch_op.drop_index(batch_op.f("ix_loan_scenarios_workspace_id"))
    op.drop_table("loan_scenarios")
