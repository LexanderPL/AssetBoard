from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
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
)
from sqlalchemy.orm import Mapped, mapped_column

from assetboard.db.base import Base, IdMixin, TimestampMixin, WorkspaceScopedMixin
from assetboard.db.types import DecimalType, UtcDateTime
from assetboard.domain.transactions import MAX_AMOUNT_MINOR


class IncomeSource(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    __tablename__ = "income_sources"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_income_sources_workspace_id_id"),
        ForeignKeyConstraint(
            ["workspace_id", "category_id"],
            ["categories.workspace_id", "categories.id"],
            name="fk_income_sources_workspace_category",
        ),
        CheckConstraint("kind IN ('fixed', 'variable')", name="kind_valid"),
        CheckConstraint(
            f"fixed_minor IS NULL OR fixed_minor BETWEEN 1 AND {MAX_AMOUNT_MINOR}",
            name="fixed_amount_valid",
        ),
        CheckConstraint(
            f"rate_minor IS NULL OR rate_minor BETWEEN 1 AND {MAX_AMOUNT_MINOR}",
            name="rate_valid",
        ),
        CheckConstraint("pay_day BETWEEN 1 AND 31", name="pay_day_valid"),
        CheckConstraint("pay_lag_months BETWEEN 0 AND 24", name="pay_lag_valid"),
        CheckConstraint(
            "expected_mode IN ('working_days', 'fixed', 'average', 'manual')",
            name="expected_mode_valid",
        ),
        CheckConstraint("average_months BETWEEN 1 AND 24", name="average_months_valid"),
        CheckConstraint("active_to IS NULL OR active_to >= active_from", name="active_range_valid"),
        Index("ix_income_sources_workspace_active", "workspace_id", "active_from", "active_to"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    category_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    fixed_minor: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    rate_minor: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    unit_label: Mapped[str] = mapped_column(String(40), nullable=False, default="day")
    pay_day: Mapped[int] = mapped_column(Integer(), nullable=False)
    pay_lag_months: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    expected_mode: Mapped[str] = mapped_column(String(24), nullable=False, default="manual")
    expected_units: Mapped[Decimal | None] = mapped_column(DecimalType(), nullable=True)
    average_months: Mapped[int] = mapped_column(Integer(), nullable=False, default=3)
    active_from: Mapped[date] = mapped_column(Date(), nullable=False)
    active_to: Mapped[date | None] = mapped_column(Date(), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


class WorkLog(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    __tablename__ = "work_logs"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "income_source_id", "work_month", name="uq_work_logs_source_month"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "income_source_id"],
            ["income_sources.workspace_id", "income_sources.id"],
            name="fk_work_logs_workspace_source",
        ),
        CheckConstraint("units >= 0", name="units_valid"),
        Index("ix_work_logs_workspace_month", "workspace_id", "work_month"),
    )

    income_source_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    work_month: Mapped[date] = mapped_column(Date(), nullable=False)
    units: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    # F3.7: the month's units are the sum of its WorkDay rows; read-only in the month-level form.
    derived_from_days: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=False, server_default=false()
    )


MAX_WORK_DAY_UNITS = 24


class WorkDay(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """One day of the F3.7 work calendar (variable income sources only).

    ``units`` (0 < units <= ``MAX_WORK_DAY_UNITS``) is validated in the service: a SQL check on a
    ``DecimalType`` column would compare text on SQLite.
    """

    __tablename__ = "work_days"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "income_source_id", "work_date", name="uq_work_days_source_date"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "income_source_id"],
            ["income_sources.workspace_id", "income_sources.id"],
            name="fk_work_days_workspace_source",
            ondelete="CASCADE",
        ),
        Index(
            "ix_work_days_workspace_source_date", "workspace_id", "income_source_id", "work_date"
        ),
    )

    income_source_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    work_date: Mapped[date] = mapped_column(Date(), nullable=False)
    units: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)


class IncomeSourceTaxRule(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    __tablename__ = "income_source_tax_rules"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_income_source_tax_rules_workspace_id_id"),
        UniqueConstraint(
            "workspace_id",
            "income_source_id",
            "tax_rule_id",
            name="uq_income_source_tax_rule",
        ),
        UniqueConstraint(
            "workspace_id",
            "income_source_id",
            "sort_order",
            name="uq_income_source_tax_order",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "income_source_id"],
            ["income_sources.workspace_id", "income_sources.id"],
            name="fk_income_source_tax_workspace_source",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "tax_rule_id"],
            ["tax_rules.workspace_id", "tax_rules.id"],
            name="fk_income_source_tax_workspace_rule",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "payment_category_id"],
            ["categories.workspace_id", "categories.id"],
            name="fk_income_source_tax_workspace_category",
        ),
        CheckConstraint("sort_order >= 0", name="sort_order_valid"),
        CheckConstraint("generate_payment IN (0, 1)", name="generate_payment_valid"),
        CheckConstraint("due_day BETWEEN 1 AND 31", name="due_day_valid"),
        CheckConstraint("due_month_offset BETWEEN 0 AND 24", name="due_month_offset_valid"),
        Index("ix_income_source_tax_workspace_source", "workspace_id", "income_source_id"),
    )

    income_source_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    tax_rule_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer(), nullable=False)
    generate_payment: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)
    # Overrides of the tax rule's payment schedule (ADR-0022); NULL uses the rule's default.
    payment_category_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    due_day: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    due_month_offset: Mapped[int | None] = mapped_column(Integer(), nullable=True)
