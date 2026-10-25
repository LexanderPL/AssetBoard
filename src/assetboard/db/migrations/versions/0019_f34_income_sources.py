"""F3.4 income sources, work logs, and generated tax payments.

Revision ID: 0019_f34
Revises: 0018_f33
Create Date: 2026-10-25 11:45:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import DecimalType, UtcDateTime

revision = "0019_f34"
down_revision = "0018_f33"
branch_labels = None
depends_on = None

MAX_AMOUNT_MINOR = 999_999_999_999_999


def upgrade() -> None:
    op.create_table(
        "income_sources",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("fixed_minor", sa.BigInteger(), nullable=True),
        sa.Column("rate_minor", sa.BigInteger(), nullable=True),
        sa.Column("unit_label", sa.String(length=40), nullable=False),
        sa.Column("pay_day", sa.Integer(), nullable=False),
        sa.Column("pay_lag_months", sa.Integer(), nullable=False),
        sa.Column("expected_mode", sa.String(length=24), nullable=False),
        sa.Column("expected_units", DecimalType(), nullable=True),
        sa.Column("average_months", sa.Integer(), nullable=False),
        sa.Column("record_as", sa.String(length=24), nullable=False),
        sa.Column("active_from", sa.Date(), nullable=False),
        sa.Column("active_to", sa.Date(), nullable=True),
        sa.Column("archived_at", UtcDateTime(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("kind IN ('fixed', 'variable')", name="ck_income_sources_kind_valid"),
        sa.CheckConstraint(
            f"fixed_minor IS NULL OR fixed_minor BETWEEN 1 AND {MAX_AMOUNT_MINOR}",
            name="ck_income_sources_fixed_amount_valid",
        ),
        sa.CheckConstraint(
            f"rate_minor IS NULL OR rate_minor BETWEEN 1 AND {MAX_AMOUNT_MINOR}",
            name="ck_income_sources_rate_valid",
        ),
        sa.CheckConstraint("pay_day BETWEEN 1 AND 31", name="ck_income_sources_pay_day_valid"),
        sa.CheckConstraint(
            "pay_lag_months BETWEEN 0 AND 24", name="ck_income_sources_pay_lag_valid"
        ),
        sa.CheckConstraint(
            "expected_mode IN ('working_days', 'fixed', 'average', 'manual')",
            name="ck_income_sources_expected_mode_valid",
        ),
        sa.CheckConstraint(
            "average_months BETWEEN 1 AND 24", name="ck_income_sources_average_months_valid"
        ),
        sa.CheckConstraint(
            "record_as IN ('cash_received', 'net', 'take_home')",
            name="ck_income_sources_record_as_valid",
        ),
        sa.CheckConstraint(
            "active_to IS NULL OR active_to >= active_from",
            name="ck_income_sources_active_range_valid",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(
            ["workspace_id", "category_id"],
            ["categories.workspace_id", "categories.id"],
            name="fk_income_sources_workspace_category",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_income_sources"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_income_sources_workspace_id_id"),
    )
    op.create_index("ix_income_sources_workspace_id", "income_sources", ["workspace_id"])
    op.create_index(
        "ix_income_sources_workspace_active",
        "income_sources",
        ["workspace_id", "active_from", "active_to"],
    )

    op.create_table(
        "work_logs",
        sa.Column("income_source_id", sa.Uuid(), nullable=False),
        sa.Column("work_month", sa.Date(), nullable=False),
        sa.Column("units", DecimalType(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("units >= 0", name="ck_work_logs_units_valid"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(
            ["workspace_id", "income_source_id"],
            ["income_sources.workspace_id", "income_sources.id"],
            name="fk_work_logs_workspace_source",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_work_logs"),
        sa.UniqueConstraint(
            "workspace_id", "income_source_id", "work_month", name="uq_work_logs_source_month"
        ),
    )
    op.create_index("ix_work_logs_workspace_id", "work_logs", ["workspace_id"])
    op.create_index("ix_work_logs_workspace_month", "work_logs", ["workspace_id", "work_month"])

    op.create_table(
        "income_source_tax_rules",
        sa.Column("income_source_id", sa.Uuid(), nullable=False),
        sa.Column("tax_rule_id", sa.Uuid(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("generate_payment", sa.Boolean(), nullable=False),
        sa.Column("payment_category_id", sa.Uuid(), nullable=False),
        sa.Column("due_day", sa.Integer(), nullable=False),
        sa.Column("due_month_offset", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("sort_order >= 0", name="ck_income_source_tax_rules_sort_order_valid"),
        sa.CheckConstraint(
            "due_day BETWEEN 1 AND 31", name="ck_income_source_tax_rules_due_day_valid"
        ),
        sa.CheckConstraint(
            "due_month_offset BETWEEN 0 AND 24",
            name="ck_income_source_tax_rules_due_month_offset_valid",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(
            ["workspace_id", "income_source_id"],
            ["income_sources.workspace_id", "income_sources.id"],
            name="fk_income_source_tax_workspace_source",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "tax_rule_id"],
            ["tax_rules.workspace_id", "tax_rules.id"],
            name="fk_income_source_tax_workspace_rule",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "payment_category_id"],
            ["categories.workspace_id", "categories.id"],
            name="fk_income_source_tax_workspace_category",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_income_source_tax_rules"),
        sa.UniqueConstraint(
            "workspace_id",
            "id",
            name="uq_income_source_tax_rules_workspace_id_id",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "income_source_id",
            "tax_rule_id",
            name="uq_income_source_tax_rule",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "income_source_id",
            "sort_order",
            name="uq_income_source_tax_order",
        ),
    )
    op.create_index(
        "ix_income_source_tax_rules_workspace_id",
        "income_source_tax_rules",
        ["workspace_id"],
    )
    op.create_index(
        "ix_income_source_tax_workspace_source",
        "income_source_tax_rules",
        ["workspace_id", "income_source_id"],
    )

    with op.batch_alter_table("scheduled_occurrences", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_scheduled_occurrences_generated_by_valid"), type_="check")
        batch_op.drop_constraint(op.f("ck_scheduled_occurrences_amount_valid"), type_="check")
        batch_op.alter_column("scheduled_item_id", existing_type=sa.Uuid(), nullable=True)
        batch_op.add_column(sa.Column("income_source_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("work_month", sa.Date(), nullable=True))
        batch_op.add_column(sa.Column("parent_occurrence_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("income_source_tax_rule_id", sa.Uuid(), nullable=True))
        batch_op.create_check_constraint(
            op.f("ck_scheduled_occurrences_generated_by_valid"),
            "generated_by IN ('rule', 'manual', 'income', 'tax')",
        )
        batch_op.create_check_constraint(
            "ck_scheduled_occurrences_source_reference_valid",
            "(generated_by IN ('rule', 'manual') AND scheduled_item_id IS NOT NULL "
            "AND income_source_id IS NULL) OR "
            "(generated_by IN ('income', 'tax') AND scheduled_item_id IS NULL)",
        )
        batch_op.create_check_constraint(
            op.f("ck_scheduled_occurrences_amount_valid"),
            f"expected_minor >= 0 AND expected_minor <= {MAX_AMOUNT_MINOR}",
        )
        batch_op.create_foreign_key(
            "fk_scheduled_occurrences_workspace_income_source",
            "income_sources",
            ["workspace_id", "income_source_id"],
            ["workspace_id", "id"],
        )
        batch_op.create_foreign_key(
            "fk_scheduled_occurrences_workspace_parent",
            "scheduled_occurrences",
            ["workspace_id", "parent_occurrence_id"],
            ["workspace_id", "id"],
        )
        batch_op.create_foreign_key(
            "fk_scheduled_occurrences_workspace_tax_attachment",
            "income_source_tax_rules",
            ["workspace_id", "income_source_tax_rule_id"],
            ["workspace_id", "id"],
        )
        batch_op.create_unique_constraint(
            "uq_scheduled_occurrences_income_work_month",
            ["workspace_id", "income_source_id", "work_month"],
        )
        batch_op.create_unique_constraint(
            "uq_scheduled_occurrences_parent_tax_attachment",
            ["workspace_id", "parent_occurrence_id", "income_source_tax_rule_id"],
        )
        batch_op.create_unique_constraint(
            "uq_scheduled_occurrences_workspace_id_id", ["workspace_id", "id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("scheduled_occurrences", schema=None) as batch_op:
        batch_op.drop_constraint("uq_scheduled_occurrences_workspace_id_id", type_="unique")
        batch_op.drop_constraint("uq_scheduled_occurrences_parent_tax_attachment", type_="unique")
        batch_op.drop_constraint("uq_scheduled_occurrences_income_work_month", type_="unique")
        batch_op.drop_constraint(
            "fk_scheduled_occurrences_workspace_tax_attachment", type_="foreignkey"
        )
        batch_op.drop_constraint("fk_scheduled_occurrences_workspace_parent", type_="foreignkey")
        batch_op.drop_constraint(
            "fk_scheduled_occurrences_workspace_income_source", type_="foreignkey"
        )
        batch_op.drop_constraint("ck_scheduled_occurrences_source_reference_valid", type_="check")
        batch_op.drop_constraint(op.f("ck_scheduled_occurrences_generated_by_valid"), type_="check")
        batch_op.drop_constraint(op.f("ck_scheduled_occurrences_amount_valid"), type_="check")
        batch_op.drop_column("income_source_tax_rule_id")
        batch_op.drop_column("parent_occurrence_id")
        batch_op.drop_column("work_month")
        batch_op.drop_column("income_source_id")
        batch_op.alter_column("scheduled_item_id", existing_type=sa.Uuid(), nullable=False)
        batch_op.create_check_constraint(
            op.f("ck_scheduled_occurrences_generated_by_valid"),
            "generated_by IN ('rule', 'manual')",
        )
        batch_op.create_check_constraint(
            op.f("ck_scheduled_occurrences_amount_valid"),
            f"expected_minor > 0 AND expected_minor <= {MAX_AMOUNT_MINOR}",
        )
    op.drop_index("ix_income_source_tax_workspace_source", table_name="income_source_tax_rules")
    op.drop_index("ix_income_source_tax_rules_workspace_id", table_name="income_source_tax_rules")
    op.drop_table("income_source_tax_rules")
    op.drop_index("ix_work_logs_workspace_month", table_name="work_logs")
    op.drop_index("ix_work_logs_workspace_id", table_name="work_logs")
    op.drop_table("work_logs")
    op.drop_index("ix_income_sources_workspace_active", table_name="income_sources")
    op.drop_index("ix_income_sources_workspace_id", table_name="income_sources")
    op.drop_table("income_sources")
