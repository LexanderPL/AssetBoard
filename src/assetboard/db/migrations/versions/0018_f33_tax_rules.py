"""F3.3 user-defined tax rules and immutable versions.

Revision ID: 0018_f33
Revises: 0017_f32
Create Date: 2026-10-24 18:50:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import UtcDateTime

revision = "0018_f33"
down_revision = "0017_f32"
branch_labels = None
depends_on = None

# Keep schema limits frozen here; changes belong in a new migration.
MAX_RATE_BP = 2_147_483_647
MAX_AMOUNT_MINOR = 999_999_999_999_999


def upgrade() -> None:
    op.create_table(
        "tax_rules",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("effect", sa.String(length=16), nullable=False),
        sa.Column("base", sa.String(length=16), nullable=False),
        sa.Column("notes", sa.String(length=200), nullable=False),
        sa.Column("archived_at", UtcDateTime(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("effect IN ('added', 'withheld')", name="ck_tax_rules_effect_valid"),
        sa.CheckConstraint("base IN ('net', 'gross')", name="ck_tax_rules_base_valid"),
        sa.CheckConstraint(
            "effect != 'added' OR base = 'net'", name="ck_tax_rules_added_base_valid"
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_tax_rules"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_tax_rules_workspace_id_id"),
    )
    op.create_index("ix_tax_rules_workspace_id", "tax_rules", ["workspace_id"])
    op.create_index("ix_tax_rules_workspace_name", "tax_rules", ["workspace_id", "name"])
    op.create_table(
        "tax_rule_versions",
        sa.Column("tax_rule_id", sa.Uuid(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("rate_bp", sa.Integer(), nullable=True),
        sa.Column("fixed_minor", sa.BigInteger(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("mode IN ('percent', 'fixed')", name="ck_tax_rule_versions_mode_valid"),
        sa.CheckConstraint(
            f"(mode = 'percent' AND rate_bp IS NOT NULL AND rate_bp BETWEEN 0 AND {MAX_RATE_BP} "
            "AND fixed_minor IS NULL) OR "
            f"(mode = 'fixed' AND rate_bp IS NULL AND fixed_minor BETWEEN 0 AND "
            f"{MAX_AMOUNT_MINOR})",
            name="ck_tax_rule_versions_value_valid",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(
            ["workspace_id", "tax_rule_id"],
            ["tax_rules.workspace_id", "tax_rules.id"],
            name="fk_tax_rule_versions_workspace_rule",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tax_rule_versions"),
        sa.UniqueConstraint(
            "workspace_id",
            "tax_rule_id",
            "effective_from",
            name="uq_tax_rule_versions_workspace_rule_effective",
        ),
    )
    op.create_index("ix_tax_rule_versions_workspace_id", "tax_rule_versions", ["workspace_id"])
    op.create_index(
        "ix_tax_rule_versions_workspace_effective",
        "tax_rule_versions",
        ["workspace_id", "effective_from"],
    )


def downgrade() -> None:
    op.drop_index("ix_tax_rule_versions_workspace_effective", table_name="tax_rule_versions")
    op.drop_index("ix_tax_rule_versions_workspace_id", table_name="tax_rule_versions")
    op.drop_table("tax_rule_versions")
    op.drop_index("ix_tax_rules_workspace_name", table_name="tax_rules")
    op.drop_index("ix_tax_rules_workspace_id", table_name="tax_rules")
    op.drop_table("tax_rules")
