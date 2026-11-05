"""asset classes, instruments and allocation weights

Revision ID: 0038_f61_investments
Revises: 0037_f56_estimate_between
Create Date: 2026-11-05

F6.1. Asset-class names are unique per workspace among non-archived rows through a partial
unique index on the Python-computed ``name_key``; instrument names use the same rule. Allocation
weights are integer basis points and every child reference is composite, so a row can never point
at another workspace's instrument or class.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import UtcDateTime

revision = "0038_f61_investments"
down_revision = "0037_f56_estimate_between"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "asset_classes",
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("name_key", sa.String(length=255), nullable=False),
        sa.Column("investable", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("color", sa.String(length=7), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("archived_at", UtcDateTime(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("length(color) = 7", name=op.f("ck_asset_classes_color_length")),
        sa.CheckConstraint("length(name) > 0", name=op.f("ck_asset_classes_name_not_empty")),
        sa.CheckConstraint("sort_order >= 0", name=op.f("ck_asset_classes_sort_order_valid")),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_asset_classes_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_asset_classes")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_asset_classes_workspace_id_id"),
    )
    with op.batch_alter_table("asset_classes", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_asset_classes_workspace_id"), ["workspace_id"], unique=False
        )
        batch_op.create_index(
            "ix_asset_classes_workspace_order",
            ["workspace_id", "sort_order"],
            unique=False,
        )
        batch_op.create_index(
            "uq_asset_classes_active_name",
            ["workspace_id", "name_key"],
            unique=True,
            sqlite_where=sa.text("archived_at IS NULL"),
            postgresql_where=sa.text("archived_at IS NULL"),
        )

    op.create_table(
        "instruments",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("name_key", sa.String(length=255), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=True),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("quote_currency", sa.String(length=3), nullable=False),
        sa.Column("unit_label", sa.String(length=32), nullable=False),
        sa.Column("valuation_source", sa.String(length=16), nullable=False),
        sa.Column("provider_key", sa.String(length=32), nullable=True),
        sa.Column("provider_symbol", sa.String(length=64), nullable=True),
        sa.Column("risk_tier_override", sa.String(length=16), nullable=True),
        sa.Column("archived_at", UtcDateTime(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("length(name) > 0", name=op.f("ck_instruments_name_not_empty")),
        sa.CheckConstraint(
            "length(quote_currency) = 3",
            name=op.f("ck_instruments_quote_currency_length"),
        ),
        sa.CheckConstraint(
            "length(unit_label) > 0", name=op.f("ck_instruments_unit_label_not_empty")
        ),
        sa.CheckConstraint(
            "type IN ('equity', 'etf', 'bond_market', 'bond_model', 'metal', 'crypto', "
            "'currency', 'real_estate', 'other')",
            name=op.f("ck_instruments_type_valid"),
        ),
        sa.CheckConstraint(
            "valuation_source IN ('provider', 'manual', 'bond_model')",
            name=op.f("ck_instruments_valuation_source_valid"),
        ),
        sa.CheckConstraint(
            "risk_tier_override IS NULL OR risk_tier_override IN ('safe', 'growth', 'speculative')",
            name=op.f("ck_instruments_risk_tier_override_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_instruments_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_instruments")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_instruments_workspace_id_id"),
        sa.UniqueConstraint(
            "workspace_id",
            "id",
            "quote_currency",
            name="uq_instruments_workspace_id_id_quote_currency",
        ),
    )
    with op.batch_alter_table("instruments", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_instruments_workspace_id"), ["workspace_id"], unique=False
        )
        batch_op.create_index(
            "ix_instruments_workspace_type", ["workspace_id", "type"], unique=False
        )
        batch_op.create_index(
            "uq_instruments_active_name",
            ["workspace_id", "name_key"],
            unique=True,
            sqlite_where=sa.text("archived_at IS NULL"),
            postgresql_where=sa.text("archived_at IS NULL"),
        )

    op.create_table(
        "instrument_allocations",
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("asset_class_id", sa.Uuid(), nullable=False),
        sa.Column("weight_bp", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "weight_bp > 0 AND weight_bp <= 10000",
            name=op.f("ck_instrument_allocations_weight_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "instrument_id"],
            ["instruments.workspace_id", "instruments.id"],
            name="fk_instrument_allocations_workspace_instrument",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "asset_class_id"],
            ["asset_classes.workspace_id", "asset_classes.id"],
            name="fk_instrument_allocations_workspace_asset_class",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_instrument_allocations_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_instrument_allocations")),
        sa.UniqueConstraint(
            "workspace_id",
            "id",
            name="uq_instrument_allocations_workspace_id_id",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "instrument_id",
            "asset_class_id",
            name="uq_instrument_allocations_workspace_instrument_class",
        ),
    )
    with op.batch_alter_table("instrument_allocations", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_instrument_allocations_workspace_id"),
            ["workspace_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_instrument_allocations_workspace_instrument",
            ["workspace_id", "instrument_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_instrument_allocations_workspace_asset_class",
            ["workspace_id", "asset_class_id"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_table("instrument_allocations")
    op.drop_table("instruments")
    op.drop_table("asset_classes")
