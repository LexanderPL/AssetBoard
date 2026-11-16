"""Add the horizon-based FIRE independence rule (F8.21)

Revision ID: 0065_f821_horizon_rule
Revises: 0064_f812_fire_loan_settings
Create Date: 2026-11-17

The FIRE plan reuses the ``rule`` string column and still stores no withdrawal rate for rules
other than ``swr``. Only the two check constraints change: ``lasts_to_horizon`` is accepted and
must keep ``swr_bp`` null.

The downgrade converts any horizon-rule plan to ``returns_cover_expenses`` before restoring the
old constraints; that rule choice cannot be represented by the old schema and is therefore lost.
"""

from __future__ import annotations

from alembic import op

revision = "0065_f821_horizon_rule"
down_revision = "0064_f812_fire_loan_settings"
branch_labels = None
depends_on = None

_OLD_RULE = "rule IN ('returns_cover_expenses', 'swr')"
_NEW_RULE = "rule IN ('returns_cover_expenses', 'swr', 'lasts_to_horizon')"
_OLD_SWR = (
    "(rule = 'swr' AND swr_bp IS NOT NULL AND swr_bp BETWEEN 50 AND 2000) OR "
    "(rule = 'returns_cover_expenses' AND swr_bp IS NULL)"
)
_NEW_SWR = (
    "(rule = 'swr' AND swr_bp IS NOT NULL AND swr_bp BETWEEN 50 AND 2000) OR "
    "(rule IN ('returns_cover_expenses', 'lasts_to_horizon') AND swr_bp IS NULL)"
)


def upgrade() -> None:
    with op.batch_alter_table("fire_scenarios", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_fire_scenarios_rule_valid"), type_="check")
        batch_op.drop_constraint(op.f("ck_fire_scenarios_swr_valid"), type_="check")
        batch_op.create_check_constraint(op.f("ck_fire_scenarios_rule_valid"), _NEW_RULE)
        batch_op.create_check_constraint(op.f("ck_fire_scenarios_swr_valid"), _NEW_SWR)


def downgrade() -> None:
    op.execute(
        "UPDATE fire_scenarios SET rule = 'returns_cover_expenses' WHERE rule = 'lasts_to_horizon'"
    )
    with op.batch_alter_table("fire_scenarios", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_fire_scenarios_rule_valid"), type_="check")
        batch_op.drop_constraint(op.f("ck_fire_scenarios_swr_valid"), type_="check")
        batch_op.create_check_constraint(op.f("ck_fire_scenarios_rule_valid"), _OLD_RULE)
        batch_op.create_check_constraint(op.f("ck_fire_scenarios_swr_valid"), _OLD_SWR)
