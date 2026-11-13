"""mixed fixed/reference-rate loan timeline

Revision ID: 0058_loan_mixed_rate_timeline
Revises: 0057_f78_loan_import_sources
Create Date: 2026-11-13

F7.9. A rate change can now start a fixed segment or a variable segment (reference rate plus
margin). Existing rows become fixed, so their behaviour is unchanged. Variable rows keep their
reference link and are resolved into the schedule's flat rate timeline on read.

The downgrade removes variable segments, which the previous schema cannot represent; fixed rows
are preserved.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0058_loan_mixed_rate_timeline"
down_revision = "0057_f78_loan_import_sources"
branch_labels = None
depends_on = None

_OLD_RATE_VALID = "rate_bp BETWEEN 0 AND 10000"
_MODE_VALID = "mode IN ('fixed', 'variable')"
_NEW_RATE_VALID = (
    "(mode = 'fixed' AND rate_bp IS NOT NULL AND rate_bp BETWEEN 0 AND 10000 AND "
    "reference_rate_id IS NULL AND margin_bp IS NULL) OR "
    "(mode = 'variable' AND rate_bp IS NULL AND reference_rate_id IS NOT NULL AND "
    "margin_bp IS NOT NULL AND margin_bp BETWEEN 0 AND 10000)"
)


def upgrade() -> None:
    with op.batch_alter_table("loan_rate_changes", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "mode",
                sa.String(length=16),
                nullable=False,
                server_default=sa.text("'fixed'"),
            )
        )
        batch_op.add_column(sa.Column("reference_rate_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("margin_bp", sa.Integer(), nullable=True))
        batch_op.alter_column("rate_bp", existing_type=sa.Integer(), nullable=True)
        batch_op.drop_constraint(op.f("ck_loan_rate_changes_rate_valid"), type_="check")
        batch_op.create_check_constraint(op.f("ck_loan_rate_changes_mode_valid"), _MODE_VALID)
        batch_op.create_check_constraint(
            op.f("ck_loan_rate_changes_rate_configuration_valid"), _NEW_RATE_VALID
        )
        batch_op.create_foreign_key(
            op.f("fk_loan_rate_changes_workspace_reference_rate"),
            "reference_rates",
            ["workspace_id", "reference_rate_id"],
            ["workspace_id", "id"],
        )
        batch_op.create_index(
            "ix_loan_rate_changes_workspace_reference_rate",
            ["workspace_id", "reference_rate_id"],
        )


def downgrade() -> None:
    # The previous model has one rate per row and no reference link. Variable segments have no
    # valid representation there, so they are dropped instead of silently becoming 0%.
    op.execute("DELETE FROM loan_rate_changes WHERE mode = 'variable'")
    with op.batch_alter_table("loan_rate_changes", schema=None) as batch_op:
        batch_op.drop_index("ix_loan_rate_changes_workspace_reference_rate")
        batch_op.drop_constraint(
            op.f("fk_loan_rate_changes_workspace_reference_rate"), type_="foreignkey"
        )
        batch_op.drop_constraint(
            op.f("ck_loan_rate_changes_rate_configuration_valid"), type_="check"
        )
        batch_op.drop_constraint(op.f("ck_loan_rate_changes_mode_valid"), type_="check")
        batch_op.alter_column("rate_bp", existing_type=sa.Integer(), nullable=False)
        batch_op.drop_column("margin_bp")
        batch_op.drop_column("reference_rate_id")
        batch_op.drop_column("mode")
        batch_op.create_check_constraint(op.f("ck_loan_rate_changes_rate_valid"), _OLD_RATE_VALID)
