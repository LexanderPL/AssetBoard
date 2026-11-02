"""import category mappings

Revision ID: 0032_f43_import_mappings
Revises: 0031_f42_import_batches
Create Date: 2026-11-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import UtcDateTime

revision = "0032_f43_import_mappings"
down_revision = "0031_f42_import_batches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "import_category_mappings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("source_name_key", sa.String(length=255), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('expense', 'income')",
            name="ck_import_category_mappings_kind_valid",
        ),
        sa.CheckConstraint(
            "action IN ('map', 'ignore')",
            name="ck_import_category_mappings_action_valid",
        ),
        sa.CheckConstraint(
            "(action = 'map' AND category_id IS NOT NULL) OR "
            "(action = 'ignore' AND category_id IS NULL)",
            name="ck_import_category_mappings_category_required_for_map",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "category_id"],
            ["categories.workspace_id", "categories.id"],
            name="fk_import_category_mappings_category",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "kind",
            "source_name_key",
            name="uq_import_category_mappings_workspace_kind_name",
        ),
    )
    op.create_index(
        "ix_import_category_mappings_workspace_id",
        "import_category_mappings",
        ["workspace_id"],
    )
    op.create_index(
        "ix_import_category_mappings_workspace_kind",
        "import_category_mappings",
        ["workspace_id", "kind"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_import_category_mappings_workspace_kind",
        table_name="import_category_mappings",
    )
    op.drop_index(
        "ix_import_category_mappings_workspace_id",
        table_name="import_category_mappings",
    )
    op.drop_table("import_category_mappings")
