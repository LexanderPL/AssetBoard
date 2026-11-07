"""Investment models (F6.1): asset classes, instruments and allocation weights.

Asset classes and instruments are archived, never deleted, so their history stays valid.
Allocation weights are integer basis points; the service enforces that each instrument's active
rows total 10 000. Every table is workspace-scoped and every reference to another workspace-owned
table is composite (docs/architecture.md).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from assetboard.db.base import Base, IdMixin, TimestampMixin, WorkspaceScopedMixin
from assetboard.db.types import UtcDateTime
from assetboard.domain.instruments import (
    ALLOCATION_TOTAL_BP,
    MAX_ASSET_CLASS_NAME_LENGTH,
    MAX_INSTRUMENT_NAME_LENGTH,
    MAX_SYMBOL_LENGTH,
    MAX_UNIT_LABEL_LENGTH,
)


class AssetClass(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """A portfolio grouping such as Stocks, Bonds or Real estate (F6.1).

    ``name_key`` is the case-insensitive identity; names are unique per workspace among
    non-archived classes. ``investable`` feeds the FIRE planner and the net-worth helper;
    archiving keeps the class on instruments that already use it.
    """

    __tablename__ = "asset_classes"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_asset_classes_workspace_id_id"),
        CheckConstraint("length(name) > 0", name="name_not_empty"),
        CheckConstraint("length(color) = 7", name="color_length"),
        CheckConstraint("sort_order >= 0", name="sort_order_valid"),
        Index(
            "uq_asset_classes_active_name",
            "workspace_id",
            "name_key",
            unique=True,
            sqlite_where=text("archived_at IS NULL"),
            postgresql_where=text("archived_at IS NULL"),
        ),
        Index("ix_asset_classes_workspace_order", "workspace_id", "sort_order"),
    )

    name: Mapped[str] = mapped_column(String(MAX_ASSET_CLASS_NAME_LENGTH), nullable=False)
    name_key: Mapped[str] = mapped_column(String(255), nullable=False)
    investable: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=True, server_default=true()
    )
    color: Mapped[str] = mapped_column(String(7), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer(), nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


class Instrument(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """A security, metal, currency, property or other investment (F6.1).

    ``quote_currency`` is the currency of the unit price and of the instrument's fees.
    ``unit_label`` is the human label of one unit ("share", "oz", the currency code for a
    currency instrument). ``valuation_source`` is provider, manual or bond_model; F6.1 writes
    manual except for bond_model instruments, whose terms arrive in F6.5.
    """

    __tablename__ = "instruments"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_instruments_workspace_id_id"),
        UniqueConstraint(
            "workspace_id",
            "id",
            "quote_currency",
            name="uq_instruments_workspace_id_id_quote_currency",
        ),
        CheckConstraint("length(name) > 0", name="name_not_empty"),
        CheckConstraint("length(quote_currency) = 3", name="quote_currency_length"),
        CheckConstraint("length(unit_label) > 0", name="unit_label_not_empty"),
        CheckConstraint(
            "type IN ('equity', 'etf', 'bond_market', 'bond_model', 'metal', 'crypto', "
            "'currency', 'real_estate', 'other')",
            name="type_valid",
        ),
        CheckConstraint(
            "valuation_source IN ('provider', 'manual', 'bond_model')",
            name="valuation_source_valid",
        ),
        CheckConstraint(
            "risk_tier_override IS NULL OR risk_tier_override IN ('safe', 'growth', 'speculative')",
            name="risk_tier_override_valid",
        ),
        Index(
            "uq_instruments_active_name",
            "workspace_id",
            "name_key",
            unique=True,
            sqlite_where=text("archived_at IS NULL"),
            postgresql_where=text("archived_at IS NULL"),
        ),
        Index("ix_instruments_workspace_type", "workspace_id", "type"),
    )

    name: Mapped[str] = mapped_column(String(MAX_INSTRUMENT_NAME_LENGTH), nullable=False)
    name_key: Mapped[str] = mapped_column(String(255), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(MAX_SYMBOL_LENGTH), nullable=True)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    quote_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    unit_label: Mapped[str] = mapped_column(String(MAX_UNIT_LABEL_LENGTH), nullable=False)
    valuation_source: Mapped[str] = mapped_column(String(16), nullable=False)
    provider_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    risk_tier_override: Mapped[str | None] = mapped_column(String(16), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


class InstrumentAllocation(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """How much of one instrument's value belongs to one asset class (F6.1).

    Weights are basis points and the service requires each instrument's rows to total
    ``ALLOCATION_TOTAL_BP``. Rows are replaced as a set when the allocation editor is saved.
    """

    __tablename__ = "instrument_allocations"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_instrument_allocations_workspace_id_id"),
        UniqueConstraint(
            "workspace_id",
            "instrument_id",
            "asset_class_id",
            name="uq_instrument_allocations_workspace_instrument_class",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "instrument_id"],
            ["instruments.workspace_id", "instruments.id"],
            name="fk_instrument_allocations_workspace_instrument",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "asset_class_id"],
            ["asset_classes.workspace_id", "asset_classes.id"],
            name="fk_instrument_allocations_workspace_asset_class",
        ),
        CheckConstraint(
            f"weight_bp > 0 AND weight_bp <= {ALLOCATION_TOTAL_BP}", name="weight_valid"
        ),
        Index(
            "ix_instrument_allocations_workspace_instrument",
            "workspace_id",
            "instrument_id",
        ),
        Index(
            "ix_instrument_allocations_workspace_asset_class",
            "workspace_id",
            "asset_class_id",
        ),
    )

    instrument_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    asset_class_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    weight_bp: Mapped[int] = mapped_column(Integer(), nullable=False)
