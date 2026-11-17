"""Home widget settings: upcoming limit and portfolio chart range (F9.1)

Revision ID: 0069_f91_home_preferences
Revises: 0068_f38_work_logs_import
Create Date: 2026-11-17

The owner's polish request adds two per-user settings to the existing row. No row still means
the default catalogue, and an existing row is backfilled by the server defaults (10 upcoming
payments, one year of portfolio history), so no separate data migration is needed.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.domain.dashboard import (
    DEFAULT_PORTFOLIO_RANGE,
    DEFAULT_UPCOMING_LIMIT,
    PORTFOLIO_RANGES,
    UPCOMING_LIMIT_MAX,
    UPCOMING_LIMIT_MIN,
)

revision = "0069_f91_home_preferences"
down_revision = "0068_f38_work_logs_import"
branch_labels = None
depends_on = None

_RANGES_SQL = ", ".join(f"'{value}'" for value in PORTFOLIO_RANGES)


def upgrade() -> None:
    with op.batch_alter_table("dashboard_preferences", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "upcoming_limit",
                sa.Integer(),
                nullable=False,
                server_default=str(DEFAULT_UPCOMING_LIMIT),
            )
        )
        batch_op.add_column(
            sa.Column(
                "portfolio_range",
                sa.String(length=8),
                nullable=False,
                server_default=DEFAULT_PORTFOLIO_RANGE,
            )
        )
        batch_op.create_check_constraint(
            op.f("ck_dashboard_preferences_upcoming_limit_valid"),
            f"upcoming_limit BETWEEN {UPCOMING_LIMIT_MIN} AND {UPCOMING_LIMIT_MAX}",
        )
        batch_op.create_check_constraint(
            op.f("ck_dashboard_preferences_portfolio_range_valid"),
            f"portfolio_range IN ({_RANGES_SQL})",
        )


def downgrade() -> None:
    with op.batch_alter_table("dashboard_preferences", schema=None) as batch_op:
        batch_op.drop_constraint(
            op.f("ck_dashboard_preferences_portfolio_range_valid"), type_="check"
        )
        batch_op.drop_constraint(
            op.f("ck_dashboard_preferences_upcoming_limit_valid"), type_="check"
        )
        batch_op.drop_column("portfolio_range")
        batch_op.drop_column("upcoming_limit")
