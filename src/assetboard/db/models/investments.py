"""Investment models (F6.1): asset classes, instruments and allocation weights.

Asset classes and instruments are archived, never deleted, so their history stays valid.
Allocation weights are integer basis points; the service enforces that each instrument's active
rows total 10 000. Every table is workspace-scoped and every reference to another workspace-owned
table is composite (docs/architecture.md).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
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
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from assetboard.db.base import (
    Base,
    IdMixin,
    SoftDeleteMixin,
    TimestampMixin,
    WorkspaceScopedMixin,
)
from assetboard.db.types import DecimalType, UtcDateTime
from assetboard.domain.bonds import MAX_SERIES_CODE_LENGTH
from assetboard.domain.holdings import MAX_NOTE_LENGTH
from assetboard.domain.instruments import (
    ALLOCATION_TOTAL_BP,
    MAX_ASSET_CLASS_NAME_LENGTH,
    MAX_INSTRUMENT_NAME_LENGTH,
    MAX_SYMBOL_LENGTH,
    MAX_UNIT_LABEL_LENGTH,
)
from assetboard.domain.transactions import MAX_AMOUNT_MINOR


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
        CheckConstraint(
            "risk_tier IS NULL OR risk_tier IN ('safe', 'growth', 'speculative')",
            name="risk_tier_valid",
        ),
        Index(
            "uq_asset_classes_active_name",
            "workspace_id",
            "name_key",
            unique=True,
            sqlite_where=text("archived_at IS NULL"),
            postgresql_where=text("archived_at IS NULL"),
        ),
        Index("ix_asset_classes_workspace_order", "workspace_id", "sort_order"),
        Index(
            "uq_asset_classes_import_key",
            "workspace_id",
            "import_key",
            unique=True,
            sqlite_where=text("import_key IS NOT NULL"),
            postgresql_where=text("import_key IS NOT NULL"),
        ),
        Index("ix_asset_classes_workspace_import_batch", "workspace_id", "import_batch_id"),
        ForeignKeyConstraint(
            ["workspace_id", "import_batch_id"],
            ["import_batches.workspace_id", "import_batches.id"],
            name="fk_asset_classes_import_batch",
        ),
    )

    name: Mapped[str] = mapped_column(String(MAX_ASSET_CLASS_NAME_LENGTH), nullable=False)
    name_key: Mapped[str] = mapped_column(String(255), nullable=False)
    investable: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=True, server_default=true()
    )
    color: Mapped[str] = mapped_column(String(7), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer(), nullable=False)
    risk_tier: Mapped[str | None] = mapped_column(String(16), nullable=True)
    import_key: Mapped[str | None] = mapped_column(String(67), nullable=True)
    import_batch_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


class RiskTargetPolicy(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """The workspace's target allocation and tolerance (F6.11).

    One row per workspace. ``safe_bp``/``growth_bp``/``speculative_bp`` are the fixed targets;
    age-based modes use ``speculative_bp`` as X and compute the safe/growth split from the
    viewer's profile, keeping the fixed values for when the mode is switched back.
    """

    __tablename__ = "risk_target_policies"
    __table_args__ = (
        UniqueConstraint("workspace_id", name="uq_risk_target_policies_workspace_id"),
        CheckConstraint("mode IN ('fixed', 'age_scaled', 'age_in_bonds')", name="mode_valid"),
        CheckConstraint(
            "safe_bp BETWEEN 0 AND 10000 AND growth_bp BETWEEN 0 AND 10000 "
            "AND speculative_bp BETWEEN 0 AND 10000",
            name="targets_valid",
        ),
        CheckConstraint(
            "mode != 'fixed' OR safe_bp + growth_bp + speculative_bp = 10000",
            name="fixed_targets_total",
        ),
        CheckConstraint("tolerance_bp BETWEEN 0 AND 10000", name="tolerance_valid"),
        Index(
            "ix_risk_target_policies_workspace_import_batch",
            "workspace_id",
            "import_batch_id",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "import_batch_id"],
            ["import_batches.workspace_id", "import_batches.id"],
            name="fk_risk_target_policies_import_batch",
        ),
    )

    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="fixed")
    safe_bp: Mapped[int] = mapped_column(Integer(), nullable=False, default=5000)
    growth_bp: Mapped[int] = mapped_column(Integer(), nullable=False, default=4000)
    speculative_bp: Mapped[int] = mapped_column(Integer(), nullable=False, default=1000)
    tolerance_bp: Mapped[int] = mapped_column(Integer(), nullable=False, default=500)
    import_batch_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


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
        Index(
            "uq_instruments_import_key",
            "workspace_id",
            "import_key",
            unique=True,
            sqlite_where=text("import_key IS NOT NULL"),
            postgresql_where=text("import_key IS NOT NULL"),
        ),
        Index("ix_instruments_workspace_import_batch", "workspace_id", "import_batch_id"),
        ForeignKeyConstraint(
            ["workspace_id", "import_batch_id"],
            ["import_batches.workspace_id", "import_batches.id"],
            name="fk_instruments_import_batch",
        ),
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
    provider_backfilled_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    risk_tier_override: Mapped[str | None] = mapped_column(String(16), nullable=True)
    import_key: Mapped[str | None] = mapped_column(String(67), nullable=True)
    import_batch_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
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
        Index(
            "uq_instrument_allocations_import_key",
            "workspace_id",
            "import_key",
            unique=True,
            sqlite_where=text("import_key IS NOT NULL"),
            postgresql_where=text("import_key IS NOT NULL"),
        ),
        Index(
            "ix_instrument_allocations_workspace_import_batch",
            "workspace_id",
            "import_batch_id",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "import_batch_id"],
            ["import_batches.workspace_id", "import_batches.id"],
            name="fk_instrument_allocations_import_batch",
        ),
    )

    instrument_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    asset_class_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    weight_bp: Mapped[int] = mapped_column(Integer(), nullable=False)
    import_key: Mapped[str | None] = mapped_column(String(67), nullable=True)
    import_batch_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class BondTerms(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """The editable terms of one bond-model instrument (F6.5).

    A bond-model instrument is valued from these terms instead of a market price. The face value
    and any fixed early-redemption fee are in the instrument's ``quote_currency``; no currency
    column is stored here, so changing the currency before transactions exist re-labels the
    terms. The rate schedule is an ordered JSON list of annual periods.
    """

    __tablename__ = "bond_terms"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_bond_terms_workspace_id_id"),
        UniqueConstraint(
            "workspace_id",
            "instrument_id",
            name="uq_bond_terms_workspace_instrument",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "instrument_id"],
            ["instruments.workspace_id", "instruments.id"],
            name="fk_bond_terms_workspace_instrument",
        ),
        CheckConstraint("face_value_minor > 0", name="face_value_valid"),
        CheckConstraint("tax_rate_bp BETWEEN 0 AND 10000", name="tax_rate_valid"),
        CheckConstraint(
            "interest_mode IN ('capitalized_annually', 'paid_out_annually', 'at_maturity_simple')",
            name="interest_mode_valid",
        ),
        CheckConstraint(
            "(early_redemption_fee_mode IS NULL "
            "AND early_redemption_fee_rate_bp IS NULL "
            "AND early_redemption_fee_minor IS NULL) OR "
            "(early_redemption_fee_mode = 'percent' "
            "AND early_redemption_fee_rate_bp IS NOT NULL "
            "AND early_redemption_fee_rate_bp BETWEEN 0 AND 10000 "
            "AND early_redemption_fee_minor IS NULL) OR "
            "(early_redemption_fee_mode = 'fixed' "
            "AND early_redemption_fee_rate_bp IS NULL "
            "AND early_redemption_fee_minor IS NOT NULL "
            "AND early_redemption_fee_minor >= 0)",
            name="early_redemption_fee_valid",
        ),
        Index(
            "uq_bond_terms_import_key",
            "workspace_id",
            "import_key",
            unique=True,
            sqlite_where=text("import_key IS NOT NULL"),
            postgresql_where=text("import_key IS NOT NULL"),
        ),
        Index("ix_bond_terms_workspace_import_batch", "workspace_id", "import_batch_id"),
        ForeignKeyConstraint(
            ["workspace_id", "import_batch_id"],
            ["import_batches.workspace_id", "import_batches.id"],
            name="fk_bond_terms_import_batch",
        ),
    )

    instrument_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    series_code: Mapped[str | None] = mapped_column(String(MAX_SERIES_CODE_LENGTH), nullable=True)
    face_value_minor: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    maturity_date: Mapped[date] = mapped_column(Date(), nullable=False)
    interest_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    rate_schedule: Mapped[list[dict[str, Any]]] = mapped_column(JSON(), nullable=False)
    tax_rate_bp: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, server_default="0"
    )
    early_redemption_fee_mode: Mapped[str | None] = mapped_column(String(8), nullable=True)
    early_redemption_fee_rate_bp: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    early_redemption_fee_minor: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    import_key: Mapped[str | None] = mapped_column(String(67), nullable=True)
    import_batch_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


# F6.2a: the three settlement columns are set all or none.
SETTLEMENT_CHECK = (
    "(settlement_currency IS NULL AND settlement_unit_price IS NULL "
    "AND settlement_fee_minor IS NULL) OR "
    "(settlement_currency IS NOT NULL AND settlement_unit_price IS NOT NULL "
    "AND settlement_fee_minor IS NOT NULL "
    "AND length(settlement_currency) = 3 "
    f"AND settlement_fee_minor >= 0 AND settlement_fee_minor <= {MAX_AMOUNT_MINOR})"
)


class InvestmentTransaction(IdMixin, TimestampMixin, SoftDeleteMixin, WorkspaceScopedMixin, Base):
    """A buy or sell of one instrument (F6.2).

    ``quantity`` and ``unit_price`` are ``Decimal`` values; ``unit_price`` and ``fee_minor``
    are in the instrument's ``quote_currency``. ``fx_to_base`` is the resolved rate to the
    workspace base currency (``1`` for a base-currency instrument). The composite foreign key
    keeps ``quote_currency`` equal to the instrument's currency; changing that currency is
    refused while rows exist. ``date`` is nullable only for undated imported legacy lots (F6.9).

    F6.2a: a row entered in the base currency also keeps ``settlement_currency`` (the base),
    ``settlement_unit_price`` and ``settlement_fee_minor`` as entered; all three are set or none.
    ``unit_price`` and ``fee_minor`` then hold the quote values derived with ``fx_to_base``.
    ``import_batch_id`` groups the rows of one bulk add; it is not a foreign key because bulk
    add records no ``import_batches`` row.
    """

    __tablename__ = "investment_transactions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_investment_transactions_workspace_id_id"),
        ForeignKeyConstraint(
            ["workspace_id", "instrument_id", "quote_currency"],
            [
                "instruments.workspace_id",
                "instruments.id",
                "instruments.quote_currency",
            ],
            name="fk_investment_transactions_workspace_instrument_currency",
        ),
        CheckConstraint(
            "type IN ('buy', 'sell', 'dividend', 'fee', 'split')",
            name="type_valid",
        ),
        CheckConstraint(
            "date IS NULL OR (date >= '1900-01-01' AND date <= '2199-12-31')",
            name="date_in_range",
        ),
        CheckConstraint("quantity >= 0", name="quantity_valid"),
        CheckConstraint("unit_price >= 0", name="unit_price_valid"),
        CheckConstraint(f"fee_minor >= 0 AND fee_minor <= {MAX_AMOUNT_MINOR}", name="fee_valid"),
        CheckConstraint("fx_to_base IS NULL OR fx_to_base > 0", name="fx_valid"),
        CheckConstraint("length(quote_currency) = 3", name="quote_currency_length"),
        CheckConstraint(f"length(note) <= {MAX_NOTE_LENGTH}", name="note_length_valid"),
        CheckConstraint(SETTLEMENT_CHECK, name="settlement_all_or_none"),
        Index(
            "ix_investment_transactions_workspace_import_batch",
            "workspace_id",
            "import_batch_id",
        ),
        Index(
            "ix_investment_transactions_workspace_instrument_date",
            "workspace_id",
            "instrument_id",
            "date",
            "created_at",
        ),
        Index(
            "ix_investment_transactions_workspace_date",
            "workspace_id",
            "date",
            "created_at",
        ),
        Index(
            "uq_investment_transactions_import_key",
            "workspace_id",
            "import_key",
            unique=True,
            sqlite_where=text("import_key IS NOT NULL"),
            postgresql_where=text("import_key IS NOT NULL"),
        ),
    )

    instrument_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    date: Mapped[date | None] = mapped_column(Date(), nullable=True)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    quote_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    fee_minor: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    fx_to_base: Mapped[Decimal | None] = mapped_column(DecimalType(), nullable=True)
    note: Mapped[str] = mapped_column(String(MAX_NOTE_LENGTH), nullable=False, default="")
    settlement_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    settlement_unit_price: Mapped[Decimal | None] = mapped_column(DecimalType(), nullable=True)
    settlement_fee_minor: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    import_batch_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    import_key: Mapped[str | None] = mapped_column(String(67), nullable=True)
