"""provider settings, errors and backfill markers

Revision ID: 0041_f64_price_sources
Revises: 0040_f63_prices_fx
Create Date: 2026-11-08

F6.4. Provider settings are opt-in per workspace; errors are operational rows pruned by the
daily refresh. ``price_points.provider_key`` records provenance, and
``instruments.provider_backfilled_at`` makes the first history backfill idempotent.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import UtcDateTime

revision = "0041_f64_price_sources"
down_revision = "0040_f63_prices_fx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "price_source_configs",
        sa.Column("provider_key", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("settings_encrypted", sa.Text(), nullable=True),
        sa.Column("last_success_at", UtcDateTime(), nullable=True),
        sa.Column("last_error_at", UtcDateTime(), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "length(provider_key) > 0", name=op.f("ck_price_source_configs_provider_key_not_empty")
        ),
        sa.CheckConstraint(
            "last_error_code IS NULL OR length(last_error_code) > 0",
            name=op.f("ck_price_source_configs_last_error_code_not_empty"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_price_source_configs_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_price_source_configs")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_price_source_configs_workspace_id_id"),
        sa.UniqueConstraint(
            "workspace_id",
            "provider_key",
            name="uq_price_source_configs_workspace_provider",
        ),
    )
    with op.batch_alter_table("price_source_configs", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_price_source_configs_workspace_id"),
            ["workspace_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_price_source_configs_workspace_enabled",
            ["workspace_id", "enabled"],
            unique=False,
        )

    op.create_table(
        "provider_errors",
        sa.Column("provider_key", sa.String(length=32), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=True),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", UtcDateTime(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "length(provider_key) > 0", name=op.f("ck_provider_errors_provider_key_not_empty")
        ),
        sa.CheckConstraint(
            "scope IN ('latest', 'history', 'fx', 'lookup')",
            name=op.f("ck_provider_errors_scope_valid"),
        ),
        sa.CheckConstraint("length(code) > 0", name=op.f("ck_provider_errors_code_not_empty")),
        sa.CheckConstraint("attempts >= 1", name=op.f("ck_provider_errors_attempts_valid")),
        sa.ForeignKeyConstraint(
            ["workspace_id", "instrument_id"],
            ["instruments.workspace_id", "instruments.id"],
            name="fk_provider_errors_workspace_instrument",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_provider_errors_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_errors")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_provider_errors_workspace_id_id"),
    )
    with op.batch_alter_table("provider_errors", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_provider_errors_workspace_id"),
            ["workspace_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_provider_errors_workspace_provider",
            ["workspace_id", "provider_key", "occurred_at"],
            unique=False,
        )
        batch_op.create_index(
            "ix_provider_errors_workspace_instrument",
            ["workspace_id", "instrument_id", "occurred_at"],
            unique=False,
        )

    with op.batch_alter_table("price_points", schema=None) as batch_op:
        batch_op.add_column(sa.Column("provider_key", sa.String(length=32), nullable=True))

    with op.batch_alter_table("instruments", schema=None) as batch_op:
        batch_op.add_column(sa.Column("provider_backfilled_at", UtcDateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("instruments", schema=None) as batch_op:
        batch_op.drop_column("provider_backfilled_at")
    with op.batch_alter_table("price_points", schema=None) as batch_op:
        batch_op.drop_column("provider_key")
    op.drop_table("provider_errors")
    op.drop_table("price_source_configs")
