"""loan configuration: loans, rate changes, reference rates, costs, overpayments, checkpoints

Revision ID: 0051_f71_loans
Revises: 0050_demo_mode
Create Date: 2026-11-12

F7.1. Terms only: no schedule rows. Every child reference is composite on ``workspace_id`` so a
row can never point at another workspace's loan or reference rate. Money is stored in the loan's
currency, which is the workspace base currency at creation.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import UtcDateTime
from assetboard.domain.loans import MAX_LOAN_RATE_BP, MAX_TERM_MONTHS
from assetboard.domain.transactions import MAX_AMOUNT_MINOR

revision = "0051_f71_loans"
down_revision = "0050_demo_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reference_rates",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("name_key", sa.String(length=255), nullable=False),
        sa.Column("notes", sa.String(length=200), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("length(name) > 0", name=op.f("ck_reference_rates_name_not_empty")),
        sa.CheckConstraint("length(notes) <= 200", name=op.f("ck_reference_rates_notes_length")),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_reference_rates_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reference_rates")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_reference_rates_workspace_id_id"),
        sa.UniqueConstraint(
            "workspace_id", "name_key", name="uq_reference_rates_workspace_name_key"
        ),
    )
    with op.batch_alter_table("reference_rates", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_reference_rates_workspace_id"), ["workspace_id"], unique=False
        )
        batch_op.create_index(
            "ix_reference_rates_workspace_name", ["workspace_id", "name"], unique=False
        )

    op.create_table(
        "reference_rate_values",
        sa.Column("reference_rate_id", sa.Uuid(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("value_bp", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            f"value_bp BETWEEN 0 AND {MAX_LOAN_RATE_BP}",
            name=op.f("ck_reference_rate_values_value_valid"),
        ),
        sa.CheckConstraint(
            "effective_from >= '1900-01-01' AND effective_from <= '2199-12-31'",
            name=op.f("ck_reference_rate_values_effective_from_in_range"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "reference_rate_id"],
            ["reference_rates.workspace_id", "reference_rates.id"],
            name="fk_reference_rate_values_workspace_rate",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_reference_rate_values_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reference_rate_values")),
        sa.UniqueConstraint(
            "workspace_id",
            "reference_rate_id",
            "effective_from",
            name="uq_reference_rate_values_workspace_rate_effective",
        ),
    )
    with op.batch_alter_table("reference_rate_values", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_reference_rate_values_workspace_id"),
            ["workspace_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_reference_rate_values_workspace_rate_effective",
            ["workspace_id", "reference_rate_id", "effective_from"],
            unique=False,
        )

    op.create_table(
        "loans",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("principal_original_minor", sa.BigInteger(), nullable=True),
        sa.Column("origination_date", sa.Date(), nullable=True),
        sa.Column("first_payment_date", sa.Date(), nullable=False),
        sa.Column("term_months", sa.Integer(), nullable=False),
        sa.Column("payment_day", sa.Integer(), nullable=False),
        sa.Column("rate_mode", sa.String(length=16), nullable=False),
        sa.Column("margin_bp", sa.Integer(), nullable=True),
        sa.Column("reference_rate_id", sa.Uuid(), nullable=True),
        sa.Column("installment_override_minor", sa.BigInteger(), nullable=True),
        sa.Column("overpayment_mode", sa.String(length=16), nullable=False),
        sa.Column("overpayment_amount_minor", sa.BigInteger(), nullable=True),
        sa.Column("overpayment_strategy", sa.String(length=16), nullable=False),
        sa.Column("archived_at", UtcDateTime(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("length(name) > 0", name=op.f("ck_loans_name_not_empty")),
        sa.CheckConstraint("length(currency) = 3", name=op.f("ck_loans_currency_length")),
        sa.CheckConstraint(
            "kind IN ('annuity', 'equal_principal')", name=op.f("ck_loans_kind_valid")
        ),
        sa.CheckConstraint(
            "rate_mode IN ('fixed', 'variable')", name=op.f("ck_loans_rate_mode_valid")
        ),
        sa.CheckConstraint(
            "overpayment_mode IN ('none', 'fixed_extra', 'target_total')",
            name=op.f("ck_loans_overpayment_mode_valid"),
        ),
        sa.CheckConstraint(
            "overpayment_strategy IN ('reduce_term', 'reduce_installment')",
            name=op.f("ck_loans_overpayment_strategy_valid"),
        ),
        sa.CheckConstraint("payment_day BETWEEN 1 AND 31", name=op.f("ck_loans_payment_day_valid")),
        sa.CheckConstraint(
            f"term_months BETWEEN 1 AND {MAX_TERM_MONTHS}", name=op.f("ck_loans_term_valid")
        ),
        sa.CheckConstraint(
            "principal_original_minor IS NULL OR (principal_original_minor > 0 AND "
            f"principal_original_minor <= {MAX_AMOUNT_MINOR})",
            name=op.f("ck_loans_principal_original_valid"),
        ),
        sa.CheckConstraint(
            "installment_override_minor IS NULL OR (installment_override_minor > 0 AND "
            f"installment_override_minor <= {MAX_AMOUNT_MINOR})",
            name=op.f("ck_loans_installment_override_valid"),
        ),
        sa.CheckConstraint(
            "(overpayment_mode = 'none' AND overpayment_amount_minor IS NULL) OR "
            "(overpayment_mode IN ('fixed_extra', 'target_total') AND "
            f"overpayment_amount_minor > 0 AND overpayment_amount_minor <= {MAX_AMOUNT_MINOR})",
            name=op.f("ck_loans_overpayment_amount_valid"),
        ),
        sa.CheckConstraint(
            "overpayment_mode != 'target_total' OR installment_override_minor IS NULL OR "
            "overpayment_amount_minor >= installment_override_minor",
            name=op.f("ck_loans_target_not_below_override"),
        ),
        sa.CheckConstraint(
            "(rate_mode = 'fixed' AND reference_rate_id IS NULL AND margin_bp IS NULL) OR "
            "(rate_mode = 'variable' AND reference_rate_id IS NOT NULL AND margin_bp IS NOT NULL "
            f"AND margin_bp BETWEEN 0 AND {MAX_LOAN_RATE_BP})",
            name=op.f("ck_loans_rate_configuration_valid"),
        ),
        sa.CheckConstraint(
            "kind = 'annuity' OR installment_override_minor IS NULL",
            name=op.f("ck_loans_override_annuity_only"),
        ),
        sa.CheckConstraint(
            "origination_date IS NULL OR first_payment_date > origination_date",
            name=op.f("ck_loans_first_payment_after_origination"),
        ),
        sa.CheckConstraint(
            "origination_date IS NULL OR (origination_date >= '1900-01-01' AND "
            "origination_date <= '2199-12-31')",
            name=op.f("ck_loans_origination_date_in_range"),
        ),
        sa.CheckConstraint(
            "first_payment_date >= '1900-01-01' AND first_payment_date <= '2199-12-31'",
            name=op.f("ck_loans_first_payment_date_in_range"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "reference_rate_id"],
            ["reference_rates.workspace_id", "reference_rates.id"],
            name="fk_loans_workspace_reference_rate",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_loans_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_loans")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_loans_workspace_id_id"),
    )
    with op.batch_alter_table("loans", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_loans_workspace_id"), ["workspace_id"], unique=False)
        batch_op.create_index(
            "ix_loans_workspace_archived", ["workspace_id", "archived_at"], unique=False
        )
        batch_op.create_index("ix_loans_workspace_name", ["workspace_id", "name"], unique=False)

    op.create_table(
        "loan_rate_changes",
        sa.Column("loan_id", sa.Uuid(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("rate_bp", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            f"rate_bp BETWEEN 0 AND {MAX_LOAN_RATE_BP}",
            name=op.f("ck_loan_rate_changes_rate_valid"),
        ),
        sa.CheckConstraint(
            "effective_from >= '1900-01-01' AND effective_from <= '2199-12-31'",
            name=op.f("ck_loan_rate_changes_effective_from_in_range"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "loan_id"],
            ["loans.workspace_id", "loans.id"],
            name="fk_loan_rate_changes_workspace_loan",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_loan_rate_changes_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_loan_rate_changes")),
        sa.UniqueConstraint(
            "workspace_id",
            "loan_id",
            "effective_from",
            name="uq_loan_rate_changes_workspace_loan_effective",
        ),
    )
    with op.batch_alter_table("loan_rate_changes", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_loan_rate_changes_workspace_id"),
            ["workspace_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_loan_rate_changes_workspace_loan_effective",
            ["workspace_id", "loan_id", "effective_from"],
            unique=False,
        )

    op.create_table(
        "loan_cost_components",
        sa.Column("loan_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("rate_bp", sa.Integer(), nullable=True),
        sa.Column("fixed_minor", sa.BigInteger(), nullable=True),
        sa.Column("basis", sa.String(length=32), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("length(name) > 0", name=op.f("ck_loan_cost_components_name_not_empty")),
        sa.CheckConstraint(
            "mode IN ('percent', 'fixed')", name=op.f("ck_loan_cost_components_mode_valid")
        ),
        sa.CheckConstraint(
            "basis = 'opening_balance'", name=op.f("ck_loan_cost_components_basis_valid")
        ),
        sa.CheckConstraint(
            "(mode = 'percent' AND rate_bp IS NOT NULL AND "
            f"rate_bp BETWEEN 0 AND {MAX_LOAN_RATE_BP} AND fixed_minor IS NULL) OR "
            "(mode = 'fixed' AND rate_bp IS NULL AND fixed_minor IS NOT NULL AND "
            f"fixed_minor > 0 AND fixed_minor <= {MAX_AMOUNT_MINOR})",
            name=op.f("ck_loan_cost_components_value_valid"),
        ),
        sa.CheckConstraint(
            "effective_from >= '1900-01-01' AND effective_from <= '2199-12-31'",
            name=op.f("ck_loan_cost_components_effective_from_in_range"),
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR (effective_to >= '1900-01-01' AND "
            "effective_to <= '2199-12-31')",
            name=op.f("ck_loan_cost_components_effective_to_in_range"),
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name=op.f("ck_loan_cost_components_effective_range_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "loan_id"],
            ["loans.workspace_id", "loans.id"],
            name="fk_loan_cost_components_workspace_loan",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_loan_cost_components_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_loan_cost_components")),
    )
    with op.batch_alter_table("loan_cost_components", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_loan_cost_components_workspace_id"),
            ["workspace_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_loan_cost_components_workspace_loan_effective",
            ["workspace_id", "loan_id", "effective_from"],
            unique=False,
        )

    op.create_table(
        "loan_overpayments",
        sa.Column("loan_id", sa.Uuid(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("note", sa.String(length=200), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            f"amount_minor > 0 AND amount_minor <= {MAX_AMOUNT_MINOR}",
            name=op.f("ck_loan_overpayments_amount_valid"),
        ),
        sa.CheckConstraint("length(note) <= 200", name=op.f("ck_loan_overpayments_note_length")),
        sa.CheckConstraint(
            "date >= '1900-01-01' AND date <= '2199-12-31'",
            name=op.f("ck_loan_overpayments_date_in_range"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "loan_id"],
            ["loans.workspace_id", "loans.id"],
            name="fk_loan_overpayments_workspace_loan",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_loan_overpayments_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_loan_overpayments")),
    )
    with op.batch_alter_table("loan_overpayments", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_loan_overpayments_workspace_id"),
            ["workspace_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_loan_overpayments_workspace_loan_date",
            ["workspace_id", "loan_id", "date"],
            unique=False,
        )

    op.create_table(
        "loan_balance_checkpoints",
        sa.Column("loan_id", sa.Uuid(), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("remaining_principal_minor", sa.BigInteger(), nullable=False),
        sa.Column("note", sa.String(length=200), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            f"remaining_principal_minor BETWEEN 0 AND {MAX_AMOUNT_MINOR}",
            name=op.f("ck_loan_balance_checkpoints_amount_valid"),
        ),
        sa.CheckConstraint(
            "length(note) <= 200", name=op.f("ck_loan_balance_checkpoints_note_length")
        ),
        sa.CheckConstraint(
            "as_of >= '1900-01-01' AND as_of <= '2199-12-31'",
            name=op.f("ck_loan_balance_checkpoints_as_of_in_range"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "loan_id"],
            ["loans.workspace_id", "loans.id"],
            name="fk_loan_balance_checkpoints_workspace_loan",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_loan_balance_checkpoints_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_loan_balance_checkpoints")),
    )
    with op.batch_alter_table("loan_balance_checkpoints", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_loan_balance_checkpoints_workspace_id"),
            ["workspace_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_loan_balance_checkpoints_workspace_loan_as_of",
            ["workspace_id", "loan_id", "as_of"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_table("loan_balance_checkpoints")
    op.drop_table("loan_overpayments")
    op.drop_table("loan_cost_components")
    op.drop_table("loan_rate_changes")
    op.drop_table("loans")
    op.drop_table("reference_rate_values")
    op.drop_table("reference_rates")
