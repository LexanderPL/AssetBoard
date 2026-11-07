"""price points and FX rates

Revision ID: 0040_f63_prices_fx
Revises: 0039_f62_investment_transactions
Create Date: 2026-11-07

F6.3. ``price_points`` stores manual and provider prices only; transaction prices are derived
from live investment transactions at read time. ``fx_rates`` stores base-currency units per one
quote-currency unit and uses the provider key as ``source`` (``manual``, ``nbp``, ``ecb``, ...).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import DecimalType, UtcDateTime

revision = "0040_f63_prices_fx"
down_revision = "0039_f62_investment_transactions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "price_points",
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("price", DecimalType(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "source IN ('manual', 'provider')",
            name=op.f("ck_price_points_source_valid"),
        ),
        sa.CheckConstraint("price >= 0", name=op.f("ck_price_points_price_valid")),
        sa.CheckConstraint(
            "date >= '1900-01-01' AND date <= '2199-12-31'",
            name=op.f("ck_price_points_date_in_range"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "instrument_id"],
            ["instruments.workspace_id", "instruments.id"],
            name="fk_price_points_workspace_instrument",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_price_points_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_price_points")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_price_points_workspace_id_id"),
        sa.UniqueConstraint(
            "workspace_id",
            "instrument_id",
            "date",
            "source",
            name="uq_price_points_workspace_instrument_date_source",
        ),
    )
    with op.batch_alter_table("price_points", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_price_points_workspace_id"), ["workspace_id"], unique=False
        )
        batch_op.create_index(
            "ix_price_points_workspace_instrument_date",
            ["workspace_id", "instrument_id", "date"],
            unique=False,
        )

    op.create_table(
        "fx_rates",
        sa.Column("base_currency", sa.String(length=3), nullable=False),
        sa.Column("quote_currency", sa.String(length=3), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("rate", DecimalType(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "length(base_currency) = 3",
            name=op.f("ck_fx_rates_base_currency_length"),
        ),
        sa.CheckConstraint(
            "length(quote_currency) = 3",
            name=op.f("ck_fx_rates_quote_currency_length"),
        ),
        sa.CheckConstraint("rate > 0", name=op.f("ck_fx_rates_rate_positive")),
        sa.CheckConstraint("length(source) > 0", name=op.f("ck_fx_rates_source_not_empty")),
        sa.CheckConstraint(
            "date >= '1900-01-01' AND date <= '2199-12-31'",
            name=op.f("ck_fx_rates_date_in_range"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_fx_rates_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fx_rates")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_fx_rates_workspace_id_id"),
        sa.UniqueConstraint(
            "workspace_id",
            "base_currency",
            "quote_currency",
            "date",
            "source",
            name="uq_fx_rates_workspace_pair_date_source",
        ),
    )
    with op.batch_alter_table("fx_rates", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_fx_rates_workspace_id"), ["workspace_id"], unique=False
        )
        batch_op.create_index(
            "ix_fx_rates_workspace_pair_date",
            ["workspace_id", "base_currency", "quote_currency", "date"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_table("fx_rates")
    op.drop_table("price_points")
