"""FIRE archive lineage

Revision ID: 0061_f86_fire_archive
Revises: 0060_f84_fire_expense_exclusions
Create Date: 2026-11-14

F8.6. The four FIRE tables join the workspace archive (format 1.7). ``import_key`` keeps the
archive lineage id across restore/export, following the ledger, net-worth and investment tables.
The unique index is per workspace and ignores NULLs, so ordinary rows keep no key.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision = "0061_f86_fire_archive"
down_revision = "0060_f84_fire_expense_exclusions"
branch_labels = None
depends_on = None

_FIRE_TABLES = (
    ("fire_scenarios", "uq_fire_scenarios_import_key"),
    ("fire_contributions", "uq_fire_contributions_import_key"),
    ("fire_events", "uq_fire_events_import_key"),
    ("fire_expense_exclusions", "uq_fire_expense_exclusions_import_key"),
)


def upgrade() -> None:
    for table, index in _FIRE_TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column("import_key", sa.String(length=67), nullable=True))
        op.create_index(
            index,
            table,
            ["workspace_id", "import_key"],
            unique=True,
            sqlite_where=text("import_key IS NOT NULL"),
            postgresql_where=text("import_key IS NOT NULL"),
        )


def downgrade() -> None:
    for table, index in reversed(_FIRE_TABLES):
        op.drop_index(index, table_name=table)
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_column("import_key")
