"""net worth snapshots

Revision ID: 0034_f53_net_worth_snapshots
Revises: 0033_f51_net_worth
Create Date: 2026-11-02

A snapshot stores assets, liabilities, net worth, currency and a versioned breakdown JSON.
``UNIQUE(workspace_id, as_of, source)`` makes the monthly job and startup catch-up idempotent
even when they race; ``net_minor`` is kept equal to ``assets_minor - liabilities_minor`` by a
check constraint.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import UtcDateTime

revision = "0034_f53_net_worth_snapshots"
down_revision = "0033_f51_net_worth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "networth_snapshots",
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("assets_minor", sa.BigInteger(), nullable=False),
        sa.Column("liabilities_minor", sa.BigInteger(), nullable=False),
        sa.Column("net_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("breakdown", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "as_of >= '1900-01-01' AND as_of <= '2199-12-31'",
            name=op.f("ck_networth_snapshots_as_of_in_range"),
        ),
        sa.CheckConstraint(
            "assets_minor >= 0",
            name=op.f("ck_networth_snapshots_assets_non_negative"),
        ),
        sa.CheckConstraint(
            "liabilities_minor >= 0",
            name=op.f("ck_networth_snapshots_liabilities_non_negative"),
        ),
        sa.CheckConstraint(
            "net_minor = assets_minor - liabilities_minor",
            name=op.f("ck_networth_snapshots_net_matches_totals"),
        ),
        sa.CheckConstraint(
            "length(currency) = 3",
            name=op.f("ck_networth_snapshots_currency_length"),
        ),
        sa.CheckConstraint(
            "source IN ('auto', 'manual', 'backfill')",
            name=op.f("ck_networth_snapshots_source_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_networth_snapshots_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_networth_snapshots")),
        sa.UniqueConstraint(
            "workspace_id",
            "as_of",
            "source",
            name="uq_networth_snapshots_workspace_as_of_source",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "id",
            name="uq_networth_snapshots_workspace_id_id",
        ),
    )
    with op.batch_alter_table("networth_snapshots", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_networth_snapshots_workspace_id"),
            ["workspace_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_networth_snapshots_workspace_as_of",
            ["workspace_id", "as_of"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("networth_snapshots", schema=None) as batch_op:
        batch_op.drop_index("ix_networth_snapshots_workspace_as_of")
        batch_op.drop_index(batch_op.f("ix_networth_snapshots_workspace_id"))
    op.drop_table("networth_snapshots")
