from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
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

from assetboard.db.base import (
    AppendOnly,
    Base,
    IdMixin,
    TimestampMixin,
    WorkspaceScopedMixin,
)
from assetboard.db.types import UtcDateTime
from assetboard.domain.percent_or_fixed import MAX_RATE_BP
from assetboard.domain.transactions import MAX_AMOUNT_MINOR


class TaxRule(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    __tablename__ = "tax_rules"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_tax_rules_workspace_id_id"),
        ForeignKeyConstraint(
            ["workspace_id", "payment_category_id"],
            ["categories.workspace_id", "categories.id"],
            name="fk_tax_rules_workspace_payment_category",
        ),
        CheckConstraint("kind IN ('added', 'self_paid', 'payer_withheld')", name="kind_valid"),
        CheckConstraint("base IN ('net', 'gross')", name="base_valid"),
        CheckConstraint("kind != 'added' OR base = 'net'", name="added_base_valid"),
        CheckConstraint("payment_day BETWEEN 1 AND 31", name="payment_day_valid"),
        CheckConstraint(
            "payment_timing IN ('next_month', 'same_month', 'after_quarter')",
            name="payment_timing_valid",
        ),
        Index("ix_tax_rules_workspace_name", "workspace_id", "name"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # ADR-0022: who pays the tax to the tax office (added | self_paid | payer_withheld).
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    base: Mapped[str] = mapped_column(String(16), nullable=False)
    # Default payment schedule; an income source's attachment may override day and category.
    payment_day: Mapped[int] = mapped_column(Integer(), nullable=False, default=10)
    payment_timing: Mapped[str] = mapped_column(String(16), nullable=False, default="next_month")
    payment_category_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    notes: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    archived_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


class TaxRuleVersion(IdMixin, TimestampMixin, WorkspaceScopedMixin, AppendOnly, Base):
    __tablename__ = "tax_rule_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "tax_rule_id"],
            ["tax_rules.workspace_id", "tax_rules.id"],
            name="fk_tax_rule_versions_workspace_rule",
        ),
        UniqueConstraint(
            "workspace_id",
            "tax_rule_id",
            "effective_from",
            name="uq_tax_rule_versions_workspace_rule_effective",
        ),
        CheckConstraint("mode IN ('percent', 'fixed')", name="mode_valid"),
        CheckConstraint(
            f"(mode = 'percent' AND rate_bp IS NOT NULL AND rate_bp BETWEEN 0 AND {MAX_RATE_BP} "
            "AND fixed_minor IS NULL) OR "
            f"(mode = 'fixed' AND rate_bp IS NULL AND fixed_minor BETWEEN 0 AND "
            f"{MAX_AMOUNT_MINOR})",
            name="value_valid",
        ),
        Index("ix_tax_rule_versions_workspace_effective", "workspace_id", "effective_from"),
    )

    tax_rule_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date(), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    rate_bp: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    fixed_minor: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
