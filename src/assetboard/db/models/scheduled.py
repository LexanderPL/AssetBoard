from __future__ import annotations

from datetime import date, datetime
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
)
from sqlalchemy.orm import Mapped, mapped_column

from assetboard.db.base import Base, IdMixin, TimestampMixin, WorkspaceScopedMixin
from assetboard.db.types import UtcDateTime
from assetboard.domain.notifications import MAX_LEAD_DAYS
from assetboard.domain.transactions import MAX_AMOUNT_MINOR


class ScheduledItem(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    __tablename__ = "scheduled_items"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_scheduled_items_workspace_id_id"),
        ForeignKeyConstraint(
            ["workspace_id", "category_id"],
            ["categories.workspace_id", "categories.id"],
            name="fk_scheduled_items_workspace_category",
        ),
        CheckConstraint("direction IN ('expense', 'income')", name="direction_valid"),
        CheckConstraint("amount_mode IN ('fixed', 'estimate')", name="amount_mode_valid"),
        CheckConstraint(
            f"amount_minor > 0 AND amount_minor <= {MAX_AMOUNT_MINOR}", name="amount_valid"
        ),
        CheckConstraint("freq IN ('once', 'weekly', 'monthly', 'yearly')", name="freq_valid"),
        CheckConstraint("interval >= 1", name="interval_valid"),
        CheckConstraint("max_count IS NULL OR max_count >= 1", name="max_count_valid"),
        CheckConstraint("end_date IS NULL OR end_date >= start_date", name="end_date_valid"),
        CheckConstraint(
            f"reminder_lead_days IS NULL OR reminder_lead_days BETWEEN 0 AND {MAX_LEAD_DAYS}",
            name="reminder_lead_days_valid",
        ),
        CheckConstraint(
            "weekend_policy IN ('none', 'previous_working_day', 'next_working_day')",
            name="weekend_policy_valid",
        ),
        Index("ix_scheduled_items_workspace_category", "workspace_id", "category_id"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    category_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    amount_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    start_date: Mapped[date] = mapped_column(Date(), nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date(), nullable=True)
    max_count: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    freq: Mapped[str] = mapped_column(String(16), nullable=False)
    interval: Mapped[int] = mapped_column(Integer(), nullable=False)
    weekdays: Mapped[list[int]] = mapped_column(JSON(), nullable=False)
    month_day: Mapped[str] = mapped_column(String(24), nullable=False)
    month: Mapped[int] = mapped_column(Integer(), nullable=False)
    weekend_policy: Mapped[str] = mapped_column(String(24), nullable=False)
    notes: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    auto_post: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    # Days before the due date to send a reminder (F3.6); None: the channel's default.
    reminder_lead_days: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    paused_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


class ScheduledOccurrence(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    __tablename__ = "scheduled_occurrences"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_scheduled_occurrences_workspace_id_id"),
        UniqueConstraint(
            "scheduled_item_id",
            "nominal_date",
            name="uq_scheduled_occurrences_item_nominal",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "scheduled_item_id"],
            ["scheduled_items.workspace_id", "scheduled_items.id"],
            name="fk_scheduled_occurrences_workspace_item",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "income_source_id"],
            ["income_sources.workspace_id", "income_sources.id"],
            name="fk_scheduled_occurrences_workspace_income_source",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "parent_occurrence_id"],
            ["scheduled_occurrences.workspace_id", "scheduled_occurrences.id"],
            name="fk_scheduled_occurrences_workspace_parent",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "income_source_tax_rule_id"],
            ["income_source_tax_rules.workspace_id", "income_source_tax_rules.id"],
            name="fk_scheduled_occurrences_workspace_tax_attachment",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "category_id"],
            ["categories.workspace_id", "categories.id"],
            name="fk_scheduled_occurrences_workspace_category",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "transaction_id"],
            ["transactions.workspace_id", "transactions.id"],
            name="fk_scheduled_occurrences_workspace_transaction",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "previous_transaction_id"],
            ["transactions.workspace_id", "transactions.id"],
            name="fk_scheduled_occurrences_workspace_previous_transaction",
        ),
        CheckConstraint("direction IN ('expense', 'income')", name="direction_valid"),
        CheckConstraint("amount_state IN ('estimated', 'confirmed')", name="amount_state_valid"),
        CheckConstraint("status IN ('pending', 'posted', 'skipped')", name="status_valid"),
        CheckConstraint(
            "generated_by IN ('rule', 'manual', 'income', 'tax')", name="generated_by_valid"
        ),
        CheckConstraint(
            "(generated_by IN ('rule', 'manual') AND scheduled_item_id IS NOT NULL "
            "AND income_source_id IS NULL) OR "
            "(generated_by IN ('income', 'tax') AND scheduled_item_id IS NULL)",
            name="source_reference_valid",
        ),
        CheckConstraint(
            f"expected_minor >= 0 AND expected_minor <= {MAX_AMOUNT_MINOR}", name="amount_valid"
        ),
        Index("ix_scheduled_occurrences_workspace_due", "workspace_id", "due_date"),
        Index("ix_scheduled_occurrences_workspace_category", "workspace_id", "category_id"),
        UniqueConstraint(
            "workspace_id",
            "income_source_id",
            "work_month",
            name="uq_scheduled_occurrences_income_work_month",
        ),
        UniqueConstraint(
            "workspace_id",
            "parent_occurrence_id",
            "income_source_tax_rule_id",
            name="uq_scheduled_occurrences_parent_tax_attachment",
        ),
    )

    scheduled_item_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    income_source_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    work_month: Mapped[date | None] = mapped_column(Date(), nullable=True)
    parent_occurrence_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    income_source_tax_rule_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    category_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    nominal_date: Mapped[date] = mapped_column(Date(), nullable=False)
    due_date: Mapped[date] = mapped_column(Date(), nullable=False)
    expected_minor: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount_state: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    generated_by: Mapped[str] = mapped_column(String(16), nullable=False, default="rule")
    transaction_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    # The transaction this occurrence was last posted as, kept after it returned to pending
    # (undo or ledger delete). Set means "posted before": auto-post leaves the row alone, and a
    # restore of that transaction may re-link it.
    previous_transaction_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    rescheduled_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    amount_overridden: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
