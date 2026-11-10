"""Demo mode flags and reset state (F9.11b)

Revision ID: 0050_demo_mode
Revises: 0049_auto_link_portfolio
Create Date: 2026-11-10

Adds the demo flags to users and workspaces (at most one of each, enforced by a partial unique
index), the last-reset state to instance settings, and ``demo`` as a session authentication
method. The downgrade deletes demo sessions first, because the old CHECK constraint does not
allow them; the demo user and workspace rows themselves stay (their flag columns are dropped
with the rest of the schema downgrade).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import UtcDateTime

revision = "0050_demo_mode"
down_revision = "0049_auto_link_portfolio"
branch_labels = None
depends_on = None


def _replace_check(expression: str) -> None:
    with op.batch_alter_table("user_sessions", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_user_sessions_auth_method_valid"), type_="check")
        batch_op.create_check_constraint(op.f("ck_user_sessions_auth_method_valid"), expression)


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("is_demo", sa.Boolean(), server_default=sa.false(), nullable=False)
        )
    op.create_index(
        "uq_users_is_demo",
        "users",
        ["is_demo"],
        unique=True,
        sqlite_where=sa.text("is_demo = 1"),
        postgresql_where=sa.text("is_demo"),
    )

    with op.batch_alter_table("workspaces", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("is_demo", sa.Boolean(), server_default=sa.false(), nullable=False)
        )
    op.create_index(
        "uq_workspaces_is_demo",
        "workspaces",
        ["is_demo"],
        unique=True,
        sqlite_where=sa.text("is_demo = 1"),
        postgresql_where=sa.text("is_demo"),
    )

    with op.batch_alter_table("instance_settings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("demo_last_reset_at", UtcDateTime(), nullable=True))
        batch_op.add_column(
            sa.Column("demo_last_reset_error", sa.String(length=200), nullable=True)
        )
        batch_op.add_column(sa.Column("demo_dataset_version", sa.Integer(), nullable=True))

    _replace_check("auth_method IN ('password', 'oidc', 'proxy', 'demo')")


def downgrade() -> None:
    op.execute("DELETE FROM user_sessions WHERE auth_method = 'demo'")
    _replace_check("auth_method IN ('password', 'oidc', 'proxy')")

    with op.batch_alter_table("instance_settings", schema=None) as batch_op:
        batch_op.drop_column("demo_dataset_version")
        batch_op.drop_column("demo_last_reset_error")
        batch_op.drop_column("demo_last_reset_at")

    with op.batch_alter_table("workspaces", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_workspaces_is_demo",
            sqlite_where=sa.text("is_demo = 1"),
            postgresql_where=sa.text("is_demo"),
        )
        batch_op.drop_column("is_demo")

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_users_is_demo",
            sqlite_where=sa.text("is_demo = 1"),
            postgresql_where=sa.text("is_demo"),
        )
        batch_op.drop_column("is_demo")
