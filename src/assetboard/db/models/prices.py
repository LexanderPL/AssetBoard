"""Price points and FX rates (F6.3).

``price_points`` stores manual and provider observations only. Transaction prices are derived
from live investment transactions at read time, so they are never rows here. ``fx_rates``
stores the base currency's value in another currency: ``rate`` is **base-currency units per one
quote-currency unit**. ``source`` is ``manual`` or a provider key (``nbp``, ``ecb``, ...).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from assetboard.db.base import Base, IdMixin, TimestampMixin, WorkspaceScopedMixin
from assetboard.db.types import DecimalType


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
