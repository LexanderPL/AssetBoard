"""Net-worth models (F5.1/F5.3): categories, items, valuations and snapshots."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    false,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from assetboard.db.base import (
    AppendOnly,
    Base,
    IdMixin,
    TimestampMixin,
    WorkspaceScopedMixin,
)
from assetboard.db.types import UtcDateTime
from assetboard.domain.categories import MAX_CATEGORY_NAME_LENGTH, MAX_NAME_KEY_LENGTH
from assetboard.domain.networth import MAX_ITEM_NAME_LENGTH, MAX_NOTES_LENGTH
from assetboard.domain.transactions import MAX_AMOUNT_MINOR


class NetWorthCategory(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """A category for assets or liabilities (F5.1).

    ``name_key`` is the name's case-insensitive identity (``domain.categories.name_key``):
    names are unique per (workspace, side) among non-archived categories. The two unique
    constraints are the targets of the item's side-consistency foreign key.
    """

    __tablename__ = "networth_categories"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_networth_categories_workspace_id_id"),
        UniqueConstraint(
            "workspace_id",
            "id",
            "side",
            name="uq_networth_categories_workspace_id_id_side",
        ),
        CheckConstraint("side IN ('asset', 'liability')", name="side_valid"),
        CheckConstraint("length(color) = 7", name="color_length"),
        CheckConstraint("sort_order >= 0", name="sort_order_valid"),
        CheckConstraint("length(name) > 0", name="name_not_empty"),
        Index(
            "uq_networth_categories_active_name",
            "workspace_id",
            "side",
            "name_key",
            unique=True,
            sqlite_where=text("archived_at IS NULL"),
            postgresql_where=text("archived_at IS NULL"),
        ),
        Index("ix_networth_categories_workspace_side_order", "workspace_id", "side", "sort_order"),
        Index(
            "uq_networth_categories_import_key",
            "workspace_id",
            "import_key",
            unique=True,
            sqlite_where=text("import_key IS NOT NULL"),
            postgresql_where=text("import_key IS NOT NULL"),
        ),
        ForeignKeyConstraint(
            ["workspace_id", "import_batch_id"],
            ["import_batches.workspace_id", "import_batches.id"],
            name="fk_networth_categories_import_batch",
        ),
    )

    name: Mapped[str] = mapped_column(String(MAX_CATEGORY_NAME_LENGTH), nullable=False)
    name_key: Mapped[str] = mapped_column(String(MAX_NAME_KEY_LENGTH), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    color: Mapped[str] = mapped_column(String(7), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    import_key: Mapped[str | None] = mapped_column(String(67), nullable=True)
    import_batch_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


class NetWorthItem(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """An asset or liability (F5.1).

    ``side`` must equal the category's side (composite foreign key). ``currency`` is the
    item's source of truth; every valuation must use it (composite foreign key), so changing
    it after a valuation exists is refused by the service and the database. Amounts are
    positive in the item's currency: a liability's value is the amount owed, never negative.
    The link columns are reserved for F5.4; F5.1 writes only ``valuation_mode='manual'``.
    """

    __tablename__ = "networth_items"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_networth_items_workspace_id_id"),
        UniqueConstraint(
            "workspace_id",
            "id",
            "currency",
            name="uq_networth_items_workspace_id_id_currency",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "category_id", "side"],
            [
                "networth_categories.workspace_id",
                "networth_categories.id",
                "networth_categories.side",
            ],
            name="fk_networth_items_workspace_category_side",
        ),
        CheckConstraint("side IN ('asset', 'liability')", name="side_valid"),
        CheckConstraint("length(currency) = 3", name="currency_length"),
        CheckConstraint("valuation_mode IN ('manual', 'linked')", name="valuation_mode_valid"),
        CheckConstraint(
            "link_type IS NULL OR link_type IN "
            "('portfolio_asset_class', 'portfolio_holding', 'loan_balance')",
            name="link_type_valid",
        ),
        CheckConstraint(
            "(valuation_mode = 'manual' AND link_type IS NULL AND link_id IS NULL) OR "
            "(valuation_mode = 'linked' AND link_type IS NOT NULL AND link_id IS NOT NULL)",
            name="link_required_for_linked",
        ),
        CheckConstraint(
            "acquired_on IS NULL OR (acquired_on >= '1900-01-01' AND acquired_on <= '2199-12-31')",
            name="acquired_on_in_range",
        ),
        Index(
            "ix_networth_items_workspace_side_category",
            "workspace_id",
            "side",
            "category_id",
        ),
        Index("ix_networth_items_workspace_category", "workspace_id", "category_id"),
        Index(
            "uq_networth_items_active_link",
            "workspace_id",
            "link_type",
            "link_id",
            unique=True,
            sqlite_where=text("valuation_mode = 'linked' AND archived_at IS NULL"),
            postgresql_where=text("valuation_mode = 'linked' AND archived_at IS NULL"),
        ),
        Index(
            "uq_networth_items_import_key",
            "workspace_id",
            "import_key",
            unique=True,
            sqlite_where=text("import_key IS NOT NULL"),
            postgresql_where=text("import_key IS NOT NULL"),
        ),
        ForeignKeyConstraint(
            ["workspace_id", "import_batch_id"],
            ["import_batches.workspace_id", "import_batches.id"],
            name="fk_networth_items_import_batch",
        ),
    )

    side: Mapped[str] = mapped_column(String(16), nullable=False)
    category_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(MAX_ITEM_NAME_LENGTH), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    notes: Mapped[str] = mapped_column(String(MAX_NOTES_LENGTH), nullable=False, default="")
    acquired_on: Mapped[date | None] = mapped_column(Date(), nullable=True)
    valuation_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    link_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    link_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    import_key: Mapped[str | None] = mapped_column(String(67), nullable=True)
    import_batch_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    include_in_net_worth: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=True, server_default=true()
    )
    investable: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=False, server_default=false()
    )
    archived_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


class ItemValuation(IdMixin, TimestampMixin, WorkspaceScopedMixin, AppendOnly, Base):
    """One manual value of an item, appended and never changed or deleted (F5.1).

    The amount is positive minor units in the item's currency. ``as_of`` may be back-dated or
    future-dated; "current" is the latest row whose ``as_of <= today``. Several rows may share
    a date; the latest ``created_at``, then ``id``, wins. The composite foreign key keeps the
    currency equal to the item's currency.
    """

    __tablename__ = "item_valuations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "item_id", "currency"],
            ["networth_items.workspace_id", "networth_items.id", "networth_items.currency"],
            name="fk_item_valuations_workspace_item_currency",
        ),
        CheckConstraint(
            f"value_minor >= 0 AND value_minor <= {MAX_AMOUNT_MINOR}", name="value_valid"
        ),
        CheckConstraint("length(currency) = 3", name="currency_length"),
        CheckConstraint("as_of >= '1900-01-01' AND as_of <= '2199-12-31'", name="as_of_in_range"),
        Index(
            "ix_item_valuations_workspace_item_as_of",
            "workspace_id",
            "item_id",
            "as_of",
            "created_at",
        ),
        Index(
            "uq_item_valuations_import_key",
            "workspace_id",
            "import_key",
            unique=True,
            sqlite_where=text("import_key IS NOT NULL"),
            postgresql_where=text("import_key IS NOT NULL"),
        ),
        ForeignKeyConstraint(
            ["workspace_id", "import_batch_id"],
            ["import_batches.workspace_id", "import_batches.id"],
            name="fk_item_valuations_import_batch",
        ),
    )

    item_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    as_of: Mapped[date] = mapped_column(Date(), nullable=False)
    value_minor: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    note: Mapped[str] = mapped_column(String(MAX_NOTES_LENGTH), nullable=False, default="")
    import_key: Mapped[str | None] = mapped_column(String(67), nullable=True)
    import_batch_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class NetWorthSnapshot(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """One stored net-worth total (F5.3), never recalculated except by backfill.

    ``as_of`` is a month-end for automatic and backfilled snapshots and the workspace's today
    for a manual one. ``source`` is ``auto``, ``manual`` or ``backfill``; the unique constraint
    is the race guard. ``breakdown`` is a versioned JSON object read by the history page, which
    ignores unknown keys so later versions can add markers without a migration.
    """

    __tablename__ = "networth_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "id",
            name="uq_networth_snapshots_workspace_id_id",
        ),
        UniqueConstraint(
            "workspace_id",
            "as_of",
            "source",
            name="uq_networth_snapshots_workspace_as_of_source",
        ),
        CheckConstraint(
            "as_of >= '1900-01-01' AND as_of <= '2199-12-31'",
            name="as_of_in_range",
        ),
        CheckConstraint("assets_minor >= 0", name="assets_non_negative"),
        CheckConstraint("liabilities_minor >= 0", name="liabilities_non_negative"),
        CheckConstraint(
            "net_minor = assets_minor - liabilities_minor",
            name="net_matches_totals",
        ),
        CheckConstraint("length(currency) = 3", name="currency_length"),
        CheckConstraint(
            "source IN ('auto', 'manual', 'backfill')",
            name="source_valid",
        ),
        Index("ix_networth_snapshots_workspace_as_of", "workspace_id", "as_of"),
        Index(
            "uq_networth_snapshots_import_key",
            "workspace_id",
            "import_key",
            unique=True,
            sqlite_where=text("import_key IS NOT NULL"),
            postgresql_where=text("import_key IS NOT NULL"),
        ),
        ForeignKeyConstraint(
            ["workspace_id", "import_batch_id"],
            ["import_batches.workspace_id", "import_batches.id"],
            name="fk_networth_snapshots_import_batch",
        ),
    )

    as_of: Mapped[date] = mapped_column(Date(), nullable=False)
    assets_minor: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    liabilities_minor: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    net_minor: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    breakdown: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    import_key: Mapped[str | None] = mapped_column(String(67), nullable=True)
    import_batch_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
