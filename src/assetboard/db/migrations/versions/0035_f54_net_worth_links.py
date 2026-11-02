"""active linked net-worth items are unique per target

Revision ID: 0035_f54_net_worth_links
Revises: 0034_f53_net_worth_snapshots
Create Date: 2026-11-02

Two active items must not link to the same target, or net worth counts it twice. Archived items
are outside the index so an item can be archived and replaced, and restored only when no other
active item still links to the same target.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0035_f54_net_worth_links"
down_revision = "0034_f53_net_worth_snapshots"
branch_labels = None
depends_on = None

_ACTIVE_LINK = "valuation_mode = 'linked' AND archived_at IS NULL"


def upgrade() -> None:
    op.create_index(
        "uq_networth_items_active_link",
        "networth_items",
        ["workspace_id", "link_type", "link_id"],
        unique=True,
        sqlite_where=sa.text(_ACTIVE_LINK),
        postgresql_where=sa.text(_ACTIVE_LINK),
    )


def downgrade() -> None:
    with op.batch_alter_table("networth_items", schema=None) as batch_op:
        batch_op.drop_index("uq_networth_items_active_link")
