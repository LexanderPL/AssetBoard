"""provider quota counters

Revision ID: 0042_f64b_quota
Revises: 0041_f64_price_sources
Create Date: 2026-11-08

F6.4b. EODHD's free tier has a daily call budget; the counter is per workspace, per provider
and per UTC day. NBP and ECB leave the fields at their defaults.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0042_f64b_quota"
down_revision = "0041_f64_price_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("price_source_configs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("daily_call_budget", sa.Integer(), nullable=False, server_default="20")
        )
        batch_op.add_column(sa.Column("quota_day", sa.Date(), nullable=True))
        batch_op.add_column(
            sa.Column("quota_used", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.create_check_constraint(
            "ck_price_source_configs_daily_call_budget_valid", "daily_call_budget >= 1"
        )
        batch_op.create_check_constraint(
            "ck_price_source_configs_quota_used_valid", "quota_used >= 0"
        )


def downgrade() -> None:
    with op.batch_alter_table("price_source_configs", schema=None) as batch_op:
        batch_op.drop_constraint("ck_price_source_configs_quota_used_valid", type_="check")
        batch_op.drop_constraint("ck_price_source_configs_daily_call_budget_valid", type_="check")
        batch_op.drop_column("quota_used")
        batch_op.drop_column("quota_day")
        batch_op.drop_column("daily_call_budget")
