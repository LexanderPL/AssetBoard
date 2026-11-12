"""Loan configuration models (F7.1).

Amounts are positive minor units in ``Loan.currency``, which is the workspace base currency.
Rates are annual integer basis points except a cost component's percent, which is a monthly
percent of the opening balance. Every child reference is composite so a row can never point at
another workspace's loan or reference rate.
"""

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

from assetboard.db.base import Base, IdMixin, TimestampMixin, WorkspaceScopedMixin
from assetboard.db.types import UtcDateTime
from assetboard.domain.loans import (
    MAX_LOAN_NAME_LENGTH,
    MAX_LOAN_RATE_BP,
    MAX_NOTE_LENGTH,
    MAX_TERM_MONTHS,
)
from assetboard.domain.transactions import MAX_AMOUNT_MINOR


def _date_range(name: str) -> CheckConstraint:
    return CheckConstraint(
        f"{name} >= '1900-01-01' AND {name} <= '2199-12-31'",
        name=f"{name}_in_range",
    )


class ReferenceRate(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """A user-defined reference-rate series (WIBOR, WIRON, EURIBOR…); the app hardcodes none."""

    __tablename__ = "reference_rates"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_reference_rates_workspace_id_id"),
        UniqueConstraint("workspace_id", "name_key", name="uq_reference_rates_workspace_name_key"),
        CheckConstraint("length(name) > 0", name="name_not_empty"),
        CheckConstraint(f"length(notes) <= {MAX_NOTE_LENGTH}", name="notes_length"),
        Index("ix_reference_rates_workspace_name", "workspace_id", "name"),
    )

    name: Mapped[str] = mapped_column(String(MAX_LOAN_NAME_LENGTH), nullable=False)
    # NFKC/casefold can lengthen a name; 255 keeps the B-tree key within PostgreSQL's limit.
    name_key: Mapped[str] = mapped_column(String(255), nullable=False)
    notes: Mapped[str] = mapped_column(String(MAX_NOTE_LENGTH), nullable=False, default="")


class ReferenceRateValue(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """One value of a reference-rate series, effective from a date (annual basis points)."""

    __tablename__ = "reference_rate_values"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "reference_rate_id",
            "effective_from",
            name="uq_reference_rate_values_workspace_rate_effective",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "reference_rate_id"],
            ["reference_rates.workspace_id", "reference_rates.id"],
            name="fk_reference_rate_values_workspace_rate",
        ),
        CheckConstraint(
            f"value_bp BETWEEN 0 AND {MAX_LOAN_RATE_BP}",
            name="value_valid",
        ),
        _date_range("effective_from"),
        Index(
            "ix_reference_rate_values_workspace_rate_effective",
            "workspace_id",
            "reference_rate_id",
            "effective_from",
        ),
    )

    reference_rate_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date(), nullable=False)
    value_bp: Mapped[int] = mapped_column(Integer(), nullable=False)


class Loan(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """One configured loan (F7.1). Terms only; no schedule is stored."""

    __tablename__ = "loans"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_loans_workspace_id_id"),
        ForeignKeyConstraint(
            ["workspace_id", "reference_rate_id"],
            ["reference_rates.workspace_id", "reference_rates.id"],
            name="fk_loans_workspace_reference_rate",
        ),
        CheckConstraint("length(name) > 0", name="name_not_empty"),
        CheckConstraint("length(currency) = 3", name="currency_length"),
        CheckConstraint("kind IN ('annuity', 'equal_principal')", name="kind_valid"),
        CheckConstraint("rate_mode IN ('fixed', 'variable')", name="rate_mode_valid"),
        CheckConstraint(
            "overpayment_mode IN ('none', 'fixed_extra', 'target_total')",
            name="overpayment_mode_valid",
        ),
        CheckConstraint(
            "overpayment_strategy IN ('reduce_term', 'reduce_installment')",
            name="overpayment_strategy_valid",
        ),
        CheckConstraint("payment_day BETWEEN 1 AND 31", name="payment_day_valid"),
        CheckConstraint(
            f"term_months BETWEEN 1 AND {MAX_TERM_MONTHS}",
            name="term_valid",
        ),
        CheckConstraint(
            "principal_original_minor IS NULL OR "
            f"(principal_original_minor > 0 AND principal_original_minor <= {MAX_AMOUNT_MINOR})",
            name="principal_original_valid",
        ),
        CheckConstraint(
            "installment_override_minor IS NULL OR "
            "(installment_override_minor > 0 AND "
            f"installment_override_minor <= {MAX_AMOUNT_MINOR})",
            name="installment_override_valid",
        ),
        CheckConstraint(
            "(overpayment_mode = 'none' AND overpayment_amount_minor IS NULL) OR "
            "(overpayment_mode IN ('fixed_extra', 'target_total') AND "
            f"overpayment_amount_minor > 0 AND overpayment_amount_minor <= {MAX_AMOUNT_MINOR})",
            name="overpayment_amount_valid",
        ),
        CheckConstraint(
            "overpayment_mode != 'target_total' OR installment_override_minor IS NULL OR "
            "overpayment_amount_minor >= installment_override_minor",
            name="target_not_below_override",
        ),
        CheckConstraint(
            "(rate_mode = 'fixed' AND reference_rate_id IS NULL AND margin_bp IS NULL) OR "
            "(rate_mode = 'variable' AND reference_rate_id IS NOT NULL AND margin_bp IS NOT NULL "
            f"AND margin_bp BETWEEN 0 AND {MAX_LOAN_RATE_BP})",
            name="rate_configuration_valid",
        ),
        CheckConstraint(
            "kind = 'annuity' OR installment_override_minor IS NULL",
            name="override_annuity_only",
        ),
        CheckConstraint(
            "origination_date IS NULL OR first_payment_date > origination_date",
            name="first_payment_after_origination",
        ),
        _date_range("origination_date"),
        _date_range("first_payment_date"),
        Index("ix_loans_workspace_archived", "workspace_id", "archived_at"),
        Index("ix_loans_workspace_name", "workspace_id", "name"),
    )

    name: Mapped[str] = mapped_column(String(MAX_LOAN_NAME_LENGTH), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # All money columns are in this currency (the workspace base currency at creation).
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    principal_original_minor: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    origination_date: Mapped[date | None] = mapped_column(Date(), nullable=True)
    first_payment_date: Mapped[date] = mapped_column(Date(), nullable=False)
    term_months: Mapped[int] = mapped_column(Integer(), nullable=False)
    payment_day: Mapped[int] = mapped_column(Integer(), nullable=False)
    rate_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    margin_bp: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    reference_rate_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    installment_override_minor: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    overpayment_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    overpayment_amount_minor: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    overpayment_strategy: Mapped[str] = mapped_column(
        String(16), nullable=False, default="reduce_term"
    )
    archived_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


class LoanRateChange(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """The complete fixed-rate timeline of a fixed loan; the latest effective row applies."""

    __tablename__ = "loan_rate_changes"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "loan_id",
            "effective_from",
            name="uq_loan_rate_changes_workspace_loan_effective",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "loan_id"],
            ["loans.workspace_id", "loans.id"],
            name="fk_loan_rate_changes_workspace_loan",
        ),
        CheckConstraint(
            f"rate_bp BETWEEN 0 AND {MAX_LOAN_RATE_BP}",
            name="rate_valid",
        ),
        _date_range("effective_from"),
        Index(
            "ix_loan_rate_changes_workspace_loan_effective",
            "workspace_id",
            "loan_id",
            "effective_from",
        ),
    )

    loan_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date(), nullable=False)
    rate_bp: Mapped[int] = mapped_column(Integer(), nullable=False)


class LoanCostComponent(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """A monthly cost: a percent of the opening balance or a fixed amount, with inclusive dates."""

    __tablename__ = "loan_cost_components"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "loan_id"],
            ["loans.workspace_id", "loans.id"],
            name="fk_loan_cost_components_workspace_loan",
        ),
        CheckConstraint("length(name) > 0", name="name_not_empty"),
        CheckConstraint("mode IN ('percent', 'fixed')", name="mode_valid"),
        CheckConstraint("basis = 'opening_balance'", name="basis_valid"),
        CheckConstraint(
            "(mode = 'percent' AND rate_bp IS NOT NULL AND "
            f"rate_bp BETWEEN 0 AND {MAX_LOAN_RATE_BP} AND fixed_minor IS NULL) OR "
            "(mode = 'fixed' AND rate_bp IS NULL AND fixed_minor IS NOT NULL AND "
            f"fixed_minor > 0 AND fixed_minor <= {MAX_AMOUNT_MINOR})",
            name="value_valid",
        ),
        _date_range("effective_from"),
        _date_range("effective_to"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="effective_range_valid",
        ),
        Index(
            "ix_loan_cost_components_workspace_loan_effective",
            "workspace_id",
            "loan_id",
            "effective_from",
        ),
    )

    loan_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    name: Mapped[str] = mapped_column(String(MAX_LOAN_NAME_LENGTH), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    rate_bp: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    fixed_minor: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    basis: Mapped[str] = mapped_column(String(32), nullable=False, default="opening_balance")
    effective_from: Mapped[date] = mapped_column(Date(), nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date(), nullable=True)


class LoanOverpayment(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """A planned one-off overpayment; actual payments are recorded in F7.5."""

    __tablename__ = "loan_overpayments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "loan_id"],
            ["loans.workspace_id", "loans.id"],
            name="fk_loan_overpayments_workspace_loan",
        ),
        CheckConstraint(
            f"amount_minor > 0 AND amount_minor <= {MAX_AMOUNT_MINOR}",
            name="amount_valid",
        ),
        CheckConstraint(f"length(note) <= {MAX_NOTE_LENGTH}", name="note_length"),
        _date_range("date"),
        Index("ix_loan_overpayments_workspace_loan_date", "workspace_id", "loan_id", "date"),
    )

    loan_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    date: Mapped[date] = mapped_column(Date(), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    note: Mapped[str] = mapped_column(String(MAX_NOTE_LENGTH), nullable=False, default="")


class LoanBalanceCheckpoint(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """A bank-confirmed remaining balance at a date; F7.1 creates only the starting balance."""

    __tablename__ = "loan_balance_checkpoints"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "loan_id"],
            ["loans.workspace_id", "loans.id"],
            name="fk_loan_balance_checkpoints_workspace_loan",
        ),
        CheckConstraint(
            f"remaining_principal_minor BETWEEN 0 AND {MAX_AMOUNT_MINOR}",
            name="amount_valid",
        ),
        CheckConstraint(f"length(note) <= {MAX_NOTE_LENGTH}", name="note_length"),
        _date_range("as_of"),
        Index(
            "ix_loan_balance_checkpoints_workspace_loan_as_of",
            "workspace_id",
            "loan_id",
            "as_of",
        ),
    )

    loan_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    as_of: Mapped[date] = mapped_column(Date(), nullable=False)
    remaining_principal_minor: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    note: Mapped[str] = mapped_column(String(MAX_NOTE_LENGTH), nullable=False, default="")
