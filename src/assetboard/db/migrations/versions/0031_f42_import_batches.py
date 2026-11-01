"""import batches and ledger lineage

Revision ID: 0031_f42_import_batches
Revises: 0030_update_check
Create Date: 2026-11-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import UtcDateTime

revision = "0031_f42_import_batches"
down_revision = "0030_update_check"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "import_batches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("file_sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("applied_at", UtcDateTime(), nullable=True),
        sa.Column("rolled_back_at", UtcDateTime(), nullable=True),
        sa.Column("report", sa.JSON(), nullable=True),
        sa.Column("previous_settings", sa.JSON(), nullable=True),
        sa.CheckConstraint(
            "source IN ('archive', 'transactions_csv', 'budget_lines_csv', 'categories_csv')",
            name="ck_import_batches_source_valid",
        ),
        sa.CheckConstraint("mode IN ('restore', 'merge')", name="ck_import_batches_mode_valid"),
        sa.CheckConstraint(
            "status IN ('uploaded', 'planned', 'applied', 'rolled_back', 'cancelled', 'failed')",
            name="ck_import_batches_status_valid",
        ),
        sa.CheckConstraint("size_bytes >= 0", name="ck_import_batches_size_valid"),
        sa.CheckConstraint("length(file_sha256) = 64", name="ck_import_batches_sha256_length"),
        sa.ForeignKeyConstraint(["started_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_import_batches_workspace_id_id"),
    )
    op.create_index("ix_import_batches_workspace_id", "import_batches", ["workspace_id"])

    for table in ("categories", "transactions", "budget_lines"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(sa.Column("import_batch_id", sa.Uuid(), nullable=True))
            batch_op.create_foreign_key(
                f"fk_{table}_import_batch",
                "import_batches",
                ["workspace_id", "import_batch_id"],
                ["workspace_id", "id"],
            )


def downgrade() -> None:
    for table in ("budget_lines", "transactions", "categories"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_constraint(f"fk_{table}_import_batch", type_="foreignkey")
            batch_op.drop_column("import_batch_id")
    op.drop_index("ix_import_batches_workspace_id", table_name="import_batches")
    op.drop_table("import_batches")
