"""net worth archive extension

Revision ID: 0036_f55_net_worth_archive
Revises: 0035_f54_net_worth_links
Create Date: 2026-11-02

Adds archive lineage and batch tracking to the four net-worth models, following the ledger
tables: ``import_key`` keeps the origin id across restore/export and ``import_batch_id`` lets a
restore or merge rollback remove exactly the rows it wrote. The import batch source gains the
standalone ``item_valuations_csv`` importer.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision = "0036_f55_net_worth_archive"
down_revision = "0035_f54_net_worth_links"
branch_labels = None
depends_on = None

_OLD_SOURCES = "source IN ('archive', 'transactions_csv', 'budget_lines_csv', 'categories_csv')"
_NEW_SOURCES = (
    "source IN ('archive', 'transactions_csv', 'budget_lines_csv', 'categories_csv', "
    "'item_valuations_csv')"
)

_NET_WORTH_TABLES = (
    ("networth_categories", "fk_networth_categories_import_batch"),
    ("networth_items", "fk_networth_items_import_batch"),
    ("item_valuations", "fk_item_valuations_import_batch"),
    ("networth_snapshots", "fk_networth_snapshots_import_batch"),
)
_IMPORT_KEYS = (
    ("networth_categories", "uq_networth_categories_import_key"),
    ("networth_items", "uq_networth_items_import_key"),
    ("item_valuations", "uq_item_valuations_import_key"),
    ("networth_snapshots", "uq_networth_snapshots_import_key"),
)


def upgrade() -> None:
    for table, foreign_key in _NET_WORTH_TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column("import_key", sa.String(length=67), nullable=True))
            batch_op.add_column(sa.Column("import_batch_id", sa.Uuid(), nullable=True))
            batch_op.create_foreign_key(
                foreign_key,
                "import_batches",
                ["workspace_id", "import_batch_id"],
                ["workspace_id", "id"],
            )
    for table, index in _IMPORT_KEYS:
        op.create_index(
            index,
            table,
            ["workspace_id", "import_key"],
            unique=True,
            sqlite_where=text("import_key IS NOT NULL"),
            postgresql_where=text("import_key IS NOT NULL"),
        )
    with op.batch_alter_table("import_batches", schema=None) as batch_op:
        # 0031 wrote the already-prefixed name through the naming convention, so the
        # database has the doubled name; replace it with the model's canonical one.
        batch_op.drop_constraint(
            op.f("ck_import_batches_ck_import_batches_source_valid"), type_="check"
        )
        batch_op.create_check_constraint(op.f("ck_import_batches_source_valid"), _NEW_SOURCES)


def downgrade() -> None:
    with op.batch_alter_table("import_batches", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_import_batches_source_valid"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_import_batches_ck_import_batches_source_valid"), _OLD_SOURCES
        )
    for table, index in reversed(_IMPORT_KEYS):
        op.drop_index(index, table_name=table)
    for table, foreign_key in reversed(_NET_WORTH_TABLES):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_constraint(foreign_key, type_="foreignkey")
            batch_op.drop_column("import_batch_id")
            batch_op.drop_column("import_key")
