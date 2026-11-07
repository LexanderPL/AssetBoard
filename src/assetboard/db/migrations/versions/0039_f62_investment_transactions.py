"""investment transactions and cost-basis setting

Revision ID: 0039_f62_investment_transactions
Revises: 0038_f61_investments
Create Date: 2026-11-07

F6.2. ``investment_transactions`` stores buys/sells in the instrument's quote currency; the
composite foreign key to ``instruments(workspace_id, id, quote_currency)`` keeps the stored
currency equal to the instrument's. ``workspaces.investment_cost_basis`` selects average or
FIFO for holdings that are recomputed on read.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import DecimalType, UtcDateTime

revision = "0039_f62_investment_transactions"
down_revision = "0038_f61_investments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("workspaces", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "investment_cost_basis",
                sa.String(length=8),
                server_default="average",
                nullable=False,
            )
        )
        batch_op.create_check_constraint(
            op.f("ck_workspaces_investment_cost_basis_valid"),
            "investment_cost_basis IN ('average', 'fifo')",
        )

    op.create_table(
        "investment_transactions",
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("date", sa.Date(), nullable=True),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("quantity", DecimalType(), nullable=False),
        sa.Column("unit_price", DecimalType(), nullable=False),
        sa.Column("quote_currency", sa.String(length=3), nullable=False),
        sa.Column("fee_minor", sa.BigInteger(), nullable=False),
        sa.Column("fx_to_base", DecimalType(), nullable=True),
        sa.Column("note", sa.String(length=200), nullable=False),
        sa.Column("deleted_at", UtcDateTime(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "date IS NULL OR (date >= '1900-01-01' AND date <= '2199-12-31')",
            name=op.f("ck_investment_transactions_date_in_range"),
        ),
        sa.CheckConstraint(
            "fee_minor >= 0 AND fee_minor <= 999999999999999",
            name=op.f("ck_investment_transactions_fee_valid"),
        ),
        sa.CheckConstraint(
            "fx_to_base IS NULL OR fx_to_base > 0",
            name=op.f("ck_investment_transactions_fx_valid"),
        ),
        sa.CheckConstraint(
            "length(note) <= 200",
            name=op.f("ck_investment_transactions_note_length_valid"),
        ),
        sa.CheckConstraint(
            "quantity >= 0",
            name=op.f("ck_investment_transactions_quantity_valid"),
        ),
        sa.CheckConstraint(
            "length(quote_currency) = 3",
            name=op.f("ck_investment_transactions_quote_currency_length"),
        ),
        sa.CheckConstraint(
            "type IN ('buy', 'sell', 'dividend', 'fee', 'split')",
            name=op.f("ck_investment_transactions_type_valid"),
        ),
        sa.CheckConstraint(
            "unit_price >= 0",
            name=op.f("ck_investment_transactions_unit_price_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "instrument_id", "quote_currency"],
            [
                "instruments.workspace_id",
                "instruments.id",
                "instruments.quote_currency",
            ],
            name="fk_investment_transactions_workspace_instrument_currency",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_investment_transactions_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_investment_transactions")),
        sa.UniqueConstraint(
            "workspace_id", "id", name="uq_investment_transactions_workspace_id_id"
        ),
    )
    with op.batch_alter_table("investment_transactions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_investment_transactions_workspace_id"),
            ["workspace_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_investment_transactions_workspace_date",
            ["workspace_id", "date", "created_at"],
            unique=False,
        )
        batch_op.create_index(
            "ix_investment_transactions_workspace_instrument_date",
            ["workspace_id", "instrument_id", "date", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_table("investment_transactions")
    with op.batch_alter_table("workspaces", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_workspaces_investment_cost_basis_valid"), type_="check")
        batch_op.drop_column("investment_cost_basis")
