"""risk tiers, target policy and profile planning fields

Revision ID: 0045_f611_risk
Revises: 0044_f62a_settlement
Create Date: 2026-11-09

F6.11. Asset classes gain an optional tier; instruments already carry the optional override.
The workspace target policy is a singleton row with the fixed targets plus tolerance; age-based
modes use speculative_bp as X and compute the safe/growth split from the viewer's profile. The
profile fields are per user and are also used by the later FIRE planner (M8).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import UtcDateTime

revision = "0045_f611_risk"
down_revision = "0044_f62a_settlement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("birth_year", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "life_expectancy",
                sa.Integer(),
                server_default="90",
                nullable=False,
            )
        )
        batch_op.create_check_constraint(
            op.f("ck_users_birth_year_valid"),
            "birth_year IS NULL OR birth_year BETWEEN 1900 AND 2199",
        )
        batch_op.create_check_constraint(
            op.f("ck_users_life_expectancy_valid"),
            "life_expectancy BETWEEN 50 AND 120",
        )

    with op.batch_alter_table("asset_classes", schema=None) as batch_op:
        batch_op.add_column(sa.Column("risk_tier", sa.String(length=16), nullable=True))
        batch_op.create_check_constraint(
            op.f("ck_asset_classes_risk_tier_valid"),
            "risk_tier IS NULL OR risk_tier IN ('safe', 'growth', 'speculative')",
        )

    op.create_table(
        "risk_target_policies",
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("safe_bp", sa.Integer(), nullable=False),
        sa.Column("growth_bp", sa.Integer(), nullable=False),
        sa.Column("speculative_bp", sa.Integer(), nullable=False),
        sa.Column("tolerance_bp", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "mode IN ('fixed', 'age_scaled', 'age_in_bonds')",
            name=op.f("ck_risk_target_policies_mode_valid"),
        ),
        sa.CheckConstraint(
            "safe_bp BETWEEN 0 AND 10000 AND growth_bp BETWEEN 0 AND 10000 "
            "AND speculative_bp BETWEEN 0 AND 10000",
            name=op.f("ck_risk_target_policies_targets_valid"),
        ),
        sa.CheckConstraint(
            "mode != 'fixed' OR safe_bp + growth_bp + speculative_bp = 10000",
            name=op.f("ck_risk_target_policies_fixed_targets_total"),
        ),
        sa.CheckConstraint(
            "tolerance_bp BETWEEN 0 AND 10000",
            name=op.f("ck_risk_target_policies_tolerance_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_risk_target_policies_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_target_policies")),
        sa.UniqueConstraint("workspace_id", name="uq_risk_target_policies_workspace_id"),
    )
    op.create_index(
        op.f("ix_risk_target_policies_workspace_id"),
        "risk_target_policies",
        ["workspace_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_risk_target_policies_workspace_id"),
        table_name="risk_target_policies",
    )
    op.drop_table("risk_target_policies")

    with op.batch_alter_table("asset_classes", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_asset_classes_risk_tier_valid"), type_="check")
        batch_op.drop_column("risk_tier")

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_users_life_expectancy_valid"), type_="check")
        batch_op.drop_constraint(op.f("ck_users_birth_year_valid"), type_="check")
        batch_op.drop_column("life_expectancy")
        batch_op.drop_column("birth_year")
