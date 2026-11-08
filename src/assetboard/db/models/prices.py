"""Price points and FX rates (F6.3).

``price_points`` stores manual and provider observations only. Transaction prices are derived
from live investment transactions at read time, so they are never rows here. ``fx_rates``
stores the base currency's value in another currency: ``rate`` is **base-currency units per one
quote-currency unit**. ``source`` is ``manual`` or a provider key (``nbp``, ``ecb``, ...).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from assetboard.db.base import Base, IdMixin, TimestampMixin, WorkspaceScopedMixin
from assetboard.db.types import DecimalType, UtcDateTime


class PricePoint(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """One manual or provider price in the instrument's quote currency."""

    __tablename__ = "price_points"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_price_points_workspace_id_id"),
        UniqueConstraint(
            "workspace_id",
            "instrument_id",
            "date",
            "source",
            name="uq_price_points_workspace_instrument_date_source",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "instrument_id"],
            ["instruments.workspace_id", "instruments.id"],
            name="fk_price_points_workspace_instrument",
        ),
        CheckConstraint("source IN ('manual', 'provider')", name="source_valid"),
        CheckConstraint("price >= 0", name="price_valid"),
        CheckConstraint("date >= '1900-01-01' AND date <= '2199-12-31'", name="date_in_range"),
        Index(
            "ix_price_points_workspace_instrument_date",
            "workspace_id",
            "instrument_id",
            "date",
        ),
    )

    instrument_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    date: Mapped[date] = mapped_column(Date(), nullable=False)
    price: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    provider_key: Mapped[str | None] = mapped_column(String(32), nullable=True)


class FxRate(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """One FX observation: base-currency units per one quote-currency unit."""

    __tablename__ = "fx_rates"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_fx_rates_workspace_id_id"),
        UniqueConstraint(
            "workspace_id",
            "base_currency",
            "quote_currency",
            "date",
            "source",
            name="uq_fx_rates_workspace_pair_date_source",
        ),
        CheckConstraint("length(base_currency) = 3", name="base_currency_length"),
        CheckConstraint("length(quote_currency) = 3", name="quote_currency_length"),
        CheckConstraint("rate > 0", name="rate_positive"),
        CheckConstraint("length(source) > 0", name="source_not_empty"),
        CheckConstraint("date >= '1900-01-01' AND date <= '2199-12-31'", name="date_in_range"),
        Index(
            "ix_fx_rates_workspace_pair_date",
            "workspace_id",
            "base_currency",
            "quote_currency",
            "date",
        ),
    )

    base_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    quote_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    date: Mapped[date] = mapped_column(Date(), nullable=False)
    rate: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)


class PriceSourceConfig(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """One workspace's opt-in settings for one provider (F6.4)."""

    __tablename__ = "price_source_configs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_price_source_configs_workspace_id_id"),
        UniqueConstraint(
            "workspace_id", "provider_key", name="uq_price_source_configs_workspace_provider"
        ),
        CheckConstraint("length(provider_key) > 0", name="provider_key_not_empty"),
        CheckConstraint(
            "last_error_code IS NULL OR length(last_error_code) > 0",
            name="last_error_code_not_empty",
        ),
        Index(
            "ix_price_source_configs_workspace_enabled",
            "workspace_id",
            "enabled",
        ),
    )

    provider_key: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    settings_encrypted: Mapped[str | None] = mapped_column(Text(), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ProviderError(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """One failed provider operation, kept for the instrument badge and the provider page.

    Rows are pruned by the daily refresh; this is an operational log, not financial history.
    """

    __tablename__ = "provider_errors"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_provider_errors_workspace_id_id"),
        ForeignKeyConstraint(
            ["workspace_id", "instrument_id"],
            ["instruments.workspace_id", "instruments.id"],
            name="fk_provider_errors_workspace_instrument",
        ),
        CheckConstraint("length(provider_key) > 0", name="provider_key_not_empty"),
        CheckConstraint("scope IN ('latest', 'history', 'fx', 'lookup')", name="scope_valid"),
        CheckConstraint("length(code) > 0", name="code_not_empty"),
        CheckConstraint("attempts >= 1", name="attempts_valid"),
        Index(
            "ix_provider_errors_workspace_provider",
            "workspace_id",
            "provider_key",
            "occurred_at",
        ),
        Index(
            "ix_provider_errors_workspace_instrument",
            "workspace_id",
            "instrument_id",
            "occurred_at",
        ),
    )

    provider_key: Mapped[str] = mapped_column(String(32), nullable=False)
    instrument_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer(), nullable=False, default=1)
