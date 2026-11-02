"""net worth categories, items and manual valuations

Revision ID: 0033_f51_net_worth
Revises: 0032_f43_import_mappings
Create Date: 2026-11-02

``networth_categories`` names are unique per (workspace, side) among non-archived rows through
a partial unique index on the Python-computed ``name_key``. ``UNIQUE(workspace_id, id, side)``
is the target of the item's composite foreign key, so an item can never sit under a category of
the other side. ``UNIQUE(workspace_id, id, currency)`` is the target of the valuation's foreign
key, so a valuation always uses its item's currency.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import UtcDateTime

revision = "0033_f51_net_worth"
down_revision = "0032_f43_import_mappings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "networth_categories",
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("name_key", sa.String(length=255), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("color", sa.String(length=7), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("archived_at", UtcDateTime(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "side IN ('asset', 'liability')",
            name=op.f("ck_networth_categories_side_valid"),
        ),
        sa.CheckConstraint("length(color) = 7", name=op.f("ck_networth_categories_color_length")),
        sa.CheckConstraint("length(name) > 0", name=op.f("ck_networth_categories_name_not_empty")),
        sa.CheckConstraint("sort_order >= 0", name=op.f("ck_networth_categories_sort_order_valid")),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_networth_categories_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_networth_categories")),
        sa.UniqueConstraint(
            "workspace_id",
            "id",
            "side",
            name="uq_networth_categories_workspace_id_id_side",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_networth_categories_workspace_id_id"),
    )
    with op.batch_alter_table("networth_categories", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_networth_categories_workspace_id"),
            ["workspace_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_networth_categories_workspace_side_order",
            ["workspace_id", "side", "sort_order"],
            unique=False,
        )
        batch_op.create_index(
            "uq_networth_categories_active_name",
            ["workspace_id", "side", "name_key"],
            unique=True,
            sqlite_where=sa.text("archived_at IS NULL"),
            postgresql_where=sa.text("archived_at IS NULL"),
        )

    op.create_table(
        "networth_items",
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("notes", sa.String(length=200), nullable=False),
        sa.Column("acquired_on", sa.Date(), nullable=True),
        sa.Column("valuation_mode", sa.String(length=16), nullable=False),
        sa.Column("link_type", sa.String(length=32), nullable=True),
        sa.Column("link_id", sa.Uuid(), nullable=True),
        sa.Column(
            "include_in_net_worth",
            sa.Boolean(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("investable", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("archived_at", UtcDateTime(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "(valuation_mode = 'manual' AND link_type IS NULL AND link_id IS NULL) OR "
            "(valuation_mode = 'linked' AND link_type IS NOT NULL AND link_id IS NOT NULL)",
            name=op.f("ck_networth_items_link_required_for_linked"),
        ),
        sa.CheckConstraint(
            "acquired_on IS NULL OR (acquired_on >= '1900-01-01' AND acquired_on <= '2199-12-31')",
            name=op.f("ck_networth_items_acquired_on_in_range"),
        ),
        sa.CheckConstraint(
            "link_type IS NULL OR link_type IN "
            "('portfolio_asset_class', 'portfolio_holding', 'loan_balance')",
            name=op.f("ck_networth_items_link_type_valid"),
        ),
        sa.CheckConstraint(
            "side IN ('asset', 'liability')", name=op.f("ck_networth_items_side_valid")
        ),
        sa.CheckConstraint(
            "valuation_mode IN ('manual', 'linked')",
            name=op.f("ck_networth_items_valuation_mode_valid"),
        ),
        sa.CheckConstraint("length(currency) = 3", name=op.f("ck_networth_items_currency_length")),
        sa.ForeignKeyConstraint(
            ["workspace_id", "category_id", "side"],
            [
                "networth_categories.workspace_id",
                "networth_categories.id",
                "networth_categories.side",
            ],
            name="fk_networth_items_workspace_category_side",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_networth_items_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_networth_items")),
        sa.UniqueConstraint(
            "workspace_id",
            "id",
            "currency",
            name="uq_networth_items_workspace_id_id_currency",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_networth_items_workspace_id_id"),
    )
    with op.batch_alter_table("networth_items", schema=None) as batch_op:
        batch_op.create_index(
            "ix_networth_items_workspace_category",
            ["workspace_id", "category_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_networth_items_workspace_id"),
            ["workspace_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_networth_items_workspace_side_category",
            ["workspace_id", "side", "category_id"],
            unique=False,
        )

    op.create_table(
        "item_valuations",
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("value_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("note", sa.String(length=200), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "as_of >= '1900-01-01' AND as_of <= '2199-12-31'",
            name=op.f("ck_item_valuations_as_of_in_range"),
        ),
        sa.CheckConstraint("length(currency) = 3", name=op.f("ck_item_valuations_currency_length")),
        sa.CheckConstraint(
            "value_minor >= 0 AND value_minor <= 999999999999999",
            name=op.f("ck_item_valuations_value_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "item_id", "currency"],
            [
                "networth_items.workspace_id",
                "networth_items.id",
                "networth_items.currency",
            ],
            name="fk_item_valuations_workspace_item_currency",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_item_valuations_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_item_valuations")),
    )
    with op.batch_alter_table("item_valuations", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_item_valuations_workspace_id"),
            ["workspace_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_item_valuations_workspace_item_as_of",
            ["workspace_id", "item_id", "as_of", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("item_valuations", schema=None) as batch_op:
        batch_op.drop_index("ix_item_valuations_workspace_item_as_of")
        batch_op.drop_index(batch_op.f("ix_item_valuations_workspace_id"))
    op.drop_table("item_valuations")

    with op.batch_alter_table("networth_items", schema=None) as batch_op:
        batch_op.drop_index("ix_networth_items_workspace_side_category")
        batch_op.drop_index(batch_op.f("ix_networth_items_workspace_id"))
        batch_op.drop_index("ix_networth_items_workspace_category")
    op.drop_table("networth_items")

    with op.batch_alter_table("networth_categories", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_networth_categories_active_name",
            sqlite_where=sa.text("archived_at IS NULL"),
            postgresql_where=sa.text("archived_at IS NULL"),
        )
        batch_op.drop_index("ix_networth_categories_workspace_side_order")
        batch_op.drop_index(batch_op.f("ix_networth_categories_workspace_id"))
    op.drop_table("networth_categories")
