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
        ForeignKeyConstraint(
            ["workspace_id", "property_item_id"],
            ["networth_items.workspace_id", "networth_items.id"],
            name="fk_loans_workspace_property_item",
        ),
        CheckConstraint("length(name) > 0", name="name_not_empty"),
        CheckConstraint("length(currency) = 3", name="currency_length"),
        CheckConstraint(
            "kind IN ('annuity', 'equal_principal', 'installment_plan')",
            name="kind_valid",
        ),
        CheckConstraint("rate_mode IN ('fixed', 'variable')", name="rate_mode_valid"),
        CheckConstraint("day_count IN ('monthly', 'actual_365')", name="day_count_valid"),
        CheckConstraint(
            "drawdown_mode IN ('full_at_origination', 'tranches')",
            name="drawdown_mode_valid",
        ),
        CheckConstraint(
            "(kind = 'installment_plan' AND interest_only_months = 0) OR "
            "(kind != 'installment_plan' AND interest_only_months >= 0 AND "
            "interest_only_months < term_months)",
            name="interest_only_valid",
        ),
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
            "(kind = 'installment_plan' AND term_months BETWEEN 0 AND "
            f"{MAX_TERM_MONTHS}) OR (kind != 'installment_plan' AND "
            f"term_months BETWEEN 1 AND {MAX_TERM_MONTHS})",
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
        Index("ix_loans_workspace_property_item", "workspace_id", "property_item_id"),
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
    day_count: Mapped[str] = mapped_column(
        String(16), nullable=False, default="monthly", server_default="monthly"
    )
    drawdown_mode: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="full_at_origination",
        server_default="full_at_origination",
    )
    interest_only_months: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, server_default="0"
    )
    margin_bp: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    reference_rate_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    property_item_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    auto_record_payments: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=False, server_default=false()
    )
    installment_override_minor: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    overpayment_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    overpayment_amount_minor: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    overpayment_strategy: Mapped[str] = mapped_column(
        String(16), nullable=False, default="reduce_term"
    )
    archived_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


class LoanTransfer(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """One transfer of loan money to the target account (a drawdown, not a repayment)."""

    __tablename__ = "loan_transfers"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_loan_transfers_workspace_id_id"),
        ForeignKeyConstraint(
            ["workspace_id", "loan_id"],
            ["loans.workspace_id", "loans.id"],
            name="fk_loan_transfers_workspace_loan",
        ),
        CheckConstraint(
            f"amount_minor > 0 AND amount_minor <= {MAX_AMOUNT_MINOR}",
            name="amount_valid",
        ),
        CheckConstraint(f"length(note) <= {MAX_NOTE_LENGTH}", name="note_length"),
        _date_range("date"),
        Index("ix_loan_transfers_workspace_loan_date", "workspace_id", "loan_id", "date"),
    )

    loan_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    date: Mapped[date] = mapped_column(Date(), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    note: Mapped[str] = mapped_column(String(MAX_NOTE_LENGTH), nullable=False, default="")


class LoanRateChange(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """A rate segment start: a fixed rate or a reference rate plus margin.

    The latest effective row at or before a payment date decides the rate. Existing rows are
    fixed; a variable row stores the reference rate and margin instead of a static rate.
    """

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
        ForeignKeyConstraint(
            ["workspace_id", "reference_rate_id"],
            ["reference_rates.workspace_id", "reference_rates.id"],
            name="fk_loan_rate_changes_workspace_reference_rate",
        ),
        CheckConstraint("mode IN ('fixed', 'variable')", name="mode_valid"),
        CheckConstraint(
            "(mode = 'fixed' AND rate_bp IS NOT NULL AND "
            f"rate_bp BETWEEN 0 AND {MAX_LOAN_RATE_BP} AND "
            "reference_rate_id IS NULL AND margin_bp IS NULL) OR "
            "(mode = 'variable' AND rate_bp IS NULL AND reference_rate_id IS NOT NULL AND "
            f"margin_bp IS NOT NULL AND margin_bp BETWEEN 0 AND {MAX_LOAN_RATE_BP})",
            name="rate_configuration_valid",
        ),
        _date_range("effective_from"),
        Index(
            "ix_loan_rate_changes_workspace_loan_effective",
            "workspace_id",
            "loan_id",
            "effective_from",
        ),
        Index(
            "ix_loan_rate_changes_workspace_reference_rate",
            "workspace_id",
            "reference_rate_id",
        ),
    )

    loan_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date(), nullable=False)
    mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="fixed", server_default="fixed"
    )
    rate_bp: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    reference_rate_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)
    margin_bp: Mapped[int | None] = mapped_column(Integer(), nullable=True)


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
        UniqueConstraint(
            "workspace_id",
            "loan_id",
            "as_of",
            name="uq_loan_balance_checkpoints_workspace_loan_as_of",
        ),
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


class LoanScenario(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """A saved what-if for one loan: overpayment, strategy, rate shift and lump sum."""

    __tablename__ = "loan_scenarios"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_loan_scenarios_workspace_id_id"),
        UniqueConstraint(
            "workspace_id",
            "loan_id",
            "name_key",
            name="uq_loan_scenarios_workspace_loan_name_key",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "loan_id"],
            ["loans.workspace_id", "loans.id"],
            name="fk_loan_scenarios_workspace_loan",
        ),
        CheckConstraint("length(name) > 0", name="name_not_empty"),
        CheckConstraint(
            "overpayment_mode IS NULL OR "
            "overpayment_mode IN ('none', 'fixed_extra', 'target_total')",
            name="overpayment_mode_valid",
        ),
        CheckConstraint(
            "overpayment_strategy IS NULL OR "
            "overpayment_strategy IN ('reduce_term', 'reduce_installment')",
            name="overpayment_strategy_valid",
        ),
        CheckConstraint(
            "(overpayment_mode IS NULL AND overpayment_amount_minor IS NULL) OR "
            "(overpayment_mode = 'none' AND overpayment_amount_minor IS NULL) OR "
            "(overpayment_mode IN ('fixed_extra', 'target_total') AND "
            "overpayment_amount_minor IS NOT NULL AND overpayment_amount_minor > 0 AND "
            f"overpayment_amount_minor <= {MAX_AMOUNT_MINOR})",
            name="overpayment_amount_valid",
        ),
        CheckConstraint(
            "(rate_shift_bp IS NULL AND rate_shift_from IS NULL) OR "
            "(rate_shift_bp IS NOT NULL AND rate_shift_bp != 0 AND "
            f"rate_shift_bp BETWEEN -{MAX_LOAN_RATE_BP} AND {MAX_LOAN_RATE_BP} AND "
            "rate_shift_from IS NOT NULL)",
            name="rate_shift_valid",
        ),
        CheckConstraint(
            "(one_off_date IS NULL AND one_off_amount_minor IS NULL) OR "
            "(one_off_date IS NOT NULL AND one_off_amount_minor IS NOT NULL AND "
            f"one_off_amount_minor > 0 AND one_off_amount_minor <= {MAX_AMOUNT_MINOR})",
            name="one_off_valid",
        ),
        _date_range("rate_shift_from"),
        _date_range("one_off_date"),
        Index("ix_loan_scenarios_workspace_loan_name", "workspace_id", "loan_id", "name"),
    )

    loan_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    name: Mapped[str] = mapped_column(String(MAX_LOAN_NAME_LENGTH), nullable=False)
    name_key: Mapped[str] = mapped_column(String(255), nullable=False)
    overpayment_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    overpayment_amount_minor: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    overpayment_strategy: Mapped[str | None] = mapped_column(String(16), nullable=True)
    rate_shift_bp: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    rate_shift_from: Mapped[date | None] = mapped_column(Date(), nullable=True)
    one_off_date: Mapped[date | None] = mapped_column(Date(), nullable=True)
    one_off_amount_minor: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)


class LoanPayment(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """An actual installment or overpayment, optionally linked to a ledger transaction."""

    __tablename__ = "loan_payments"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_loan_payments_workspace_id_id"),
        ForeignKeyConstraint(
            ["workspace_id", "loan_id"],
            ["loans.workspace_id", "loans.id"],
            name="fk_loan_payments_workspace_loan",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "transaction_id"],
            ["transactions.workspace_id", "transactions.id"],
            name="fk_loan_payments_workspace_transaction",
        ),
        CheckConstraint("kind IN ('installment', 'overpayment')", name="kind_valid"),
        CheckConstraint("source IN ('manual', 'upcoming')", name="source_valid"),
        CheckConstraint(
            f"total_minor > 0 AND total_minor <= {MAX_AMOUNT_MINOR}",
            name="total_valid",
        ),
        CheckConstraint(
            "principal_minor >= 0 AND principal_minor <= "
            f"{MAX_AMOUNT_MINOR} AND interest_minor >= 0 AND interest_minor <= "
            f"{MAX_AMOUNT_MINOR} AND costs_minor >= 0 AND costs_minor <= "
            f"{MAX_AMOUNT_MINOR} AND overpayment_minor >= 0 AND overpayment_minor <= "
            f"{MAX_AMOUNT_MINOR}",
            name="components_valid",
        ),
        CheckConstraint(
            "total_minor = principal_minor + interest_minor + costs_minor + overpayment_minor",
            name="total_matches_components",
        ),
        CheckConstraint(f"length(note) <= {MAX_NOTE_LENGTH}", name="note_length"),
        _date_range("date"),
        Index("ix_loan_payments_workspace_loan_date", "workspace_id", "loan_id", "date"),
        Index("ix_loan_payments_workspace_transaction", "workspace_id", "transaction_id"),
    )

    loan_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    date: Mapped[date] = mapped_column(Date(), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    total_minor: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    principal_minor: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    interest_minor: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    costs_minor: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    overpayment_minor: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    note: Mapped[str] = mapped_column(String(MAX_NOTE_LENGTH), nullable=False, default="")
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="manual", server_default="manual"
    )
    transaction_id: Mapped[UUID | None] = mapped_column(Uuid(), nullable=True)


class InstallmentPlan(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """A simple fixed-installment plan (F7.7); the linked Loan supplies the common header."""

    __tablename__ = "installment_plans"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_installment_plans_workspace_id_id"),
        UniqueConstraint(
            "workspace_id",
            "loan_id",
            name="uq_installment_plans_workspace_loan",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "loan_id"],
            ["loans.workspace_id", "loans.id"],
            name="fk_installment_plans_workspace_loan",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "category_id"],
            ["categories.workspace_id", "categories.id"],
            name="fk_installment_plans_workspace_category",
        ),
        CheckConstraint(
            f"installment_minor > 0 AND installment_minor <= {MAX_AMOUNT_MINOR}",
            name="installment_valid",
        ),
        CheckConstraint(
            f"remaining_count >= 0 AND remaining_count <= {MAX_TERM_MONTHS}",
            name="remaining_count_valid",
        ),
        _date_range("end_date"),
        Index("ix_installment_plans_workspace_loan", "workspace_id", "loan_id"),
    )

    loan_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    category_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    installment_minor: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    remaining_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    end_date: Mapped[date] = mapped_column(Date(), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
