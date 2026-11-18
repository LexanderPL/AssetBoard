"""Home widget split: Net worth range (F9.1 polish)

Revision ID: 0071_f91_home_widget_split
Revises: 0070_f91_home_preferences
Create Date: 2026-11-18

The owner split the Home portfolio card into allocation and value-over-time widgets and made
the Net worth trend range configurable. Existing rows get the one-year Net worth range, which
is what the Home widget showed before; no separate data migration is needed.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.domain.dashboard import (
    DEFAULT_NET_WORTH_RANGE,
    NET_WORTH_RANGES,
)

revision = "0071_f91_home_widget_split"
down_revision = "0070_f91_home_preferences"
branch_labels = None
depends_on = None

_NET_WORTH_RANGES_SQL = ", ".join(f"'{value}'" for value in NET_WORTH_RANGES)


def upgrade() -> None:
    with op.batch_alter_table("dashboard_preferences", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "net_worth_range",
                sa.String(length=8),
                nullable=False,
                server_default=DEFAULT_NET_WORTH_RANGE,
            )
        )
        batch_op.create_check_constraint(
            op.f("ck_dashboard_preferences_net_worth_range_valid"),
            f"net_worth_range IN ({_NET_WORTH_RANGES_SQL})",
        )


def downgrade() -> None:
    with op.batch_alter_table("dashboard_preferences", schema=None) as batch_op:
        batch_op.drop_constraint(
            op.f("ck_dashboard_preferences_net_worth_range_valid"), type_="check"
        )
        batch_op.drop_column("net_worth_range")
