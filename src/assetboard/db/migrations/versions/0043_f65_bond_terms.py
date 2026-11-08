"""bond model terms

Revision ID: 0043_f65_bond_terms
Revises: 0042_f64b_quota
Create Date: 2026-11-08

F6.5. One editable terms row per bond-model instrument. Face value and any fixed fee are in
the instrument's quote currency, so no currency columns are stored here. The rate schedule is
an ordered JSON list of annual periods validated by the service.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import UtcDateTime

revision = "0043_f65_bond_terms"
down_revision = "0042_f64b_quota"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bond_terms",
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("series_code", sa.String(length=64), nullable=True),
        sa.Column("face_value_minor", sa.BigInteger(), nullable=False),
        sa.Column("maturity_date", sa.Date(), nullable=False),
        sa.Column("interest_mode", sa.String(length=32), nullable=False),
        sa.Column("rate_schedule", sa.JSON(), nullable=False),
        sa.Column("tax_rate_bp", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("early_redemption_fee_mode", sa.String(length=8), nullable=True),
        sa.Column("early_redemption_fee_rate_bp", sa.Integer(), nullable=True),
        sa.Column("early_redemption_fee_minor", sa.BigInteger(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("face_value_minor > 0", name=op.f("ck_bond_terms_face_value_valid")),
        sa.CheckConstraint(
            "tax_rate_bp BETWEEN 0 AND 10000", name=op.f("ck_bond_terms_tax_rate_valid")
        ),
        sa.CheckConstraint(
            "interest_mode IN ('capitalized_annually', 'paid_out_annually', 'at_maturity_simple')",
            name=op.f("ck_bond_terms_interest_mode_valid"),
        ),
        sa.CheckConstraint(
            "(early_redemption_fee_mode IS NULL "
            "AND early_redemption_fee_rate_bp IS NULL "
            "AND early_redemption_fee_minor IS NULL) OR "
            "(early_redemption_fee_mode = 'percent' "
            "AND early_redemption_fee_rate_bp IS NOT NULL "
            "AND early_redemption_fee_rate_bp BETWEEN 0 AND 10000 "
            "AND early_redemption_fee_minor IS NULL) OR "
            "(early_redemption_fee_mode = 'fixed' "
            "AND early_redemption_fee_rate_bp IS NULL "
            "AND early_redemption_fee_minor IS NOT NULL "
            "AND early_redemption_fee_minor >= 0)",
            name=op.f("ck_bond_terms_early_redemption_fee_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "instrument_id"],
            ["instruments.workspace_id", "instruments.id"],
            name="fk_bond_terms_workspace_instrument",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_bond_terms_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bond_terms")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_bond_terms_workspace_id_id"),
        sa.UniqueConstraint(
            "workspace_id",
            "instrument_id",
            name="uq_bond_terms_workspace_instrument",
        ),
    )
    with op.batch_alter_table("bond_terms", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_bond_terms_workspace_id"), ["workspace_id"], unique=False
        )


def downgrade() -> None:
    op.drop_table("bond_terms")
