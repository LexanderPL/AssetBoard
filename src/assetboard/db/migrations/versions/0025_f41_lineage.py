"""Archive lineage IDs (F4.1).

Revision ID: 0025_f41
Revises: 0024_tax_warning
"""

import sqlalchemy as sa
from alembic import op

revision = "0025_f41"
down_revision = "0024_tax_warning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("categories", "transactions"):
        op.add_column(table, sa.Column("import_key", sa.String(67), nullable=True))
        op.create_index(
            f"uq_{table}_import_key",
            table,
            ["workspace_id", "import_key"],
            unique=True,
            sqlite_where=sa.text("import_key IS NOT NULL"),
            postgresql_where=sa.text("import_key IS NOT NULL"),
        )


def downgrade() -> None:
    for table in ("transactions", "categories"):
        op.drop_index(f"uq_{table}_import_key", table_name=table)
        op.drop_column(table, "import_key")
