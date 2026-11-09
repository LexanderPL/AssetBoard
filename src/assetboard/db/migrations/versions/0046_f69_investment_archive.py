"""investment archive extension

Revision ID: 0046_f69_investment_archive
Revises: 0045_f611_risk
Create Date: 2026-11-09

Adds the archive lineage and batch tracking columns to the investment models (F6.9), following
the net-worth pattern from F5.5. ``InvestmentTransaction.import_batch_id`` already existed for
the F6.2a bulk add, so it gains only ``import_key`` and keeps its foreign key absent: a bulk-add
batch id has no ``import_batches`` row and must never be treated as an import.

Risk targets are a workspace singleton archived through ``workspace.json``; they gain only
``import_batch_id`` so a restore rollback can remove exactly the row it wrote.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision = "0046_f69_investment_archive"
down_revision = "0045_f611_risk"
branch_labels = None
depends_on = None

_IMPORTED_TABLES = (
    ("asset_classes", "fk_asset_classes_import_batch"),
    ("instruments", "fk_instruments_import_batch"),
    ("instrument_allocations", "fk_instrument_allocations_import_batch"),
    ("bond_terms", "fk_bond_terms_import_batch"),
    ("price_points", "fk_price_points_import_batch"),
    ("fx_rates", "fk_fx_rates_import_batch"),
    ("price_source_configs", "fk_price_source_configs_import_batch"),
)
_IMPORT_KEYS = (
    ("asset_classes", "uq_asset_classes_import_key"),
    ("instruments", "uq_instruments_import_key"),
    ("instrument_allocations", "uq_instrument_allocations_import_key"),
    ("investment_transactions", "uq_investment_transactions_import_key"),
    ("bond_terms", "uq_bond_terms_import_key"),
    ("price_points", "uq_price_points_import_key"),
    ("fx_rates", "uq_fx_rates_import_key"),
    ("price_source_configs", "uq_price_source_configs_import_key"),
)
_BATCH_INDEXES = (
    ("asset_classes", "ix_asset_classes_workspace_import_batch"),
    ("instruments", "ix_instruments_workspace_import_batch"),
    ("instrument_allocations", "ix_instrument_allocations_workspace_import_batch"),
    ("investment_transactions", "ix_investment_transactions_workspace_import_batch"),
    ("bond_terms", "ix_bond_terms_workspace_import_batch"),
    ("price_points", "ix_price_points_workspace_import_batch"),
    ("fx_rates", "ix_fx_rates_workspace_import_batch"),
    ("price_source_configs", "ix_price_source_configs_workspace_import_batch"),
)


def _create_key_index(table: str, name: str) -> None:
    op.create_index(
        name,
        table,
        ["workspace_id", "import_key"],
        unique=True,
        sqlite_where=text("import_key IS NOT NULL"),
        postgresql_where=text("import_key IS NOT NULL"),
    )


def upgrade() -> None:
    for table, foreign_key in _IMPORTED_TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column("import_key", sa.String(length=67), nullable=True))
            batch_op.add_column(sa.Column("import_batch_id", sa.Uuid(), nullable=True))
            batch_op.create_foreign_key(
                foreign_key,
                "import_batches",
                ["workspace_id", "import_batch_id"],
                ["workspace_id", "id"],
            )
    with op.batch_alter_table("investment_transactions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("import_key", sa.String(length=67), nullable=True))
    with op.batch_alter_table("risk_target_policies", schema=None) as batch_op:
        batch_op.add_column(sa.Column("import_batch_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_risk_target_policies_import_batch",
            "import_batches",
            ["workspace_id", "import_batch_id"],
            ["workspace_id", "id"],
        )
    for table, name in _IMPORT_KEYS:
        _create_key_index(table, name)
    for table, name in _BATCH_INDEXES:
        if table == "investment_transactions":
            # F6.2a already created this index; only the lineage index is new.
            continue
        op.create_index(name, table, ["workspace_id", "import_batch_id"])
    op.create_index(
        "ix_risk_target_policies_workspace_import_batch",
        "risk_target_policies",
        ["workspace_id", "import_batch_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_risk_target_policies_workspace_import_batch",
        table_name="risk_target_policies",
    )
    for table, name in reversed(_BATCH_INDEXES):
        if table == "investment_transactions":
            continue
        op.drop_index(name, table_name=table)
    for table, name in reversed(_IMPORT_KEYS):
        op.drop_index(name, table_name=table)
    with op.batch_alter_table("risk_target_policies", schema=None) as batch_op:
        batch_op.drop_constraint("fk_risk_target_policies_import_batch", type_="foreignkey")
        batch_op.drop_column("import_batch_id")
    with op.batch_alter_table("investment_transactions", schema=None) as batch_op:
        batch_op.drop_column("import_key")
    for table, foreign_key in reversed(_IMPORTED_TABLES):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_constraint(foreign_key, type_="foreignkey")
            batch_op.drop_column("import_batch_id")
            batch_op.drop_column("import_key")
