"""FIRE planner scenarios (F8.1).

One scenario is a named set of planning assumptions and two editors: explicit yearly net-saving
overrides (``fire_contributions``) and one-off cash or permanent expense/income changes
(``fire_events``). The currency is snapshotted from the workspace base currency; child rows
store only minor units.

Contribution and event amounts are **signed** minor units, an explicit exception to overview
§3.2 (ledger rows are stored positive and get their direction from the entity). Here the sign
is the meaning: positive saves/adds/increases, negative dissaves/withdraws/reduces.

Scenarios are archived, never hard-deleted. A partial unique index keeps active names unique
ignoring case; another keeps at most one active default per workspace.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from assetboard.db.base import Base, IdMixin, TimestampMixin, WorkspaceScopedMixin
from assetboard.db.types import UtcDateTime
from assetboard.domain.fire import (
    MAX_LABEL_LENGTH,
    MAX_YEAR,
    MIN_YEAR,
    RATE_HARD_MAX_BP,
    RATE_HARD_MIN_BP,
    SWR_HARD_MAX_BP,
    SWR_HARD_MIN_BP,
)
from assetboard.domain.transactions import MAX_AMOUNT_MINOR


def _year_range(name: str) -> CheckConstraint:
    return CheckConstraint(
        f"{name} BETWEEN {MIN_YEAR} AND {MAX_YEAR}",
        name=f"{name}_in_range",
    )


class FireScenario(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """A named FIRE plan; ``start_year`` is fixed at creation and shown read-only."""

    __tablename__ = "fire_scenarios"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_fire_scenarios_workspace_id_id"),
        CheckConstraint("length(name) > 0", name="name_not_empty"),
        CheckConstraint("length(currency) = 3", name="currency_length"),
        _year_range("birth_year"),
        _year_range("start_year"),
        CheckConstraint("birth_year <= start_year", name="birth_not_after_start"),
        CheckConstraint(
            "horizon_age BETWEEN 1 AND 120",
            name="horizon_age_valid",
        ),
        CheckConstraint(
            "stop_working_age BETWEEN 0 AND 120",
            name="stop_working_age_valid",
        ),
        CheckConstraint(
            "horizon_age > start_year - birth_year",
            name="horizon_after_current_age",
        ),
        CheckConstraint(
            "stop_working_age >= start_year - birth_year",
            name="stop_not_before_current_age",
        ),
        CheckConstraint(
            f"inflation_bp BETWEEN {RATE_HARD_MIN_BP} AND {RATE_HARD_MAX_BP}",
            name="inflation_valid",
        ),
        CheckConstraint(
            f"real_return_bp BETWEEN {RATE_HARD_MIN_BP} AND {RATE_HARD_MAX_BP}",
            name="real_return_valid",
        ),
        CheckConstraint(
            f"income_growth_bp BETWEEN {RATE_HARD_MIN_BP} AND {RATE_HARD_MAX_BP}",
            name="income_growth_valid",
        ),
        CheckConstraint(
            "rule IN ('returns_cover_expenses', 'swr')",
            name="rule_valid",
        ),
        CheckConstraint(
            "(rule = 'swr' AND swr_bp IS NOT NULL AND "
            f"swr_bp BETWEEN {SWR_HARD_MIN_BP} AND {SWR_HARD_MAX_BP}) OR "
            "(rule = 'returns_cover_expenses' AND swr_bp IS NULL)",
            name="swr_valid",
        ),
        CheckConstraint(
            "income_mode IN ('manual', 'trailing_12m')",
            name="income_mode_valid",
        ),
        CheckConstraint(
            "expenses_mode IN ('manual', 'trailing_12m')",
            name="expenses_mode_valid",
        ),
        CheckConstraint(
            "portfolio_mode IN ('manual', 'linked_investable')",
            name="portfolio_mode_valid",
        ),
        CheckConstraint(
            "(income_mode = 'manual' AND income_manual_minor IS NOT NULL AND "
            f"income_manual_minor BETWEEN 0 AND {MAX_AMOUNT_MINOR}) OR "
            "(income_mode != 'manual' AND income_manual_minor IS NULL)",
            name="income_manual_valid",
        ),
        CheckConstraint(
            "(expenses_mode = 'manual' AND expenses_manual_minor IS NOT NULL AND "
            f"expenses_manual_minor BETWEEN 0 AND {MAX_AMOUNT_MINOR}) OR "
            "(expenses_mode != 'manual' AND expenses_manual_minor IS NULL)",
            name="expenses_manual_valid",
        ),
        CheckConstraint(
            "(portfolio_mode = 'manual' AND portfolio_manual_minor IS NOT NULL AND "
            f"portfolio_manual_minor BETWEEN 0 AND {MAX_AMOUNT_MINOR}) OR "
            "(portfolio_mode != 'manual' AND portfolio_manual_minor IS NULL)",
            name="portfolio_manual_valid",
        ),
        CheckConstraint(
            "NOT (is_default AND archived_at IS NOT NULL)",
            name="default_not_archived",
        ),
        # Active scenario names are unique ignoring case; archived names may be reused.
        Index(
            "uq_fire_scenarios_active_name_key",
            "workspace_id",
            "name_key",
            unique=True,
            sqlite_where=text("archived_at IS NULL"),
            postgresql_where=text("archived_at IS NULL"),
        ),
        # At most one active default per workspace (the service promotes one).
        Index(
            "uq_fire_scenarios_active_default",
            "workspace_id",
            unique=True,
            sqlite_where=text("is_default AND archived_at IS NULL"),
            postgresql_where=text("is_default AND archived_at IS NULL"),
        ),
        Index("ix_fire_scenarios_workspace_name", "workspace_id", "name"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    name_key: Mapped[str] = mapped_column(String(255), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    birth_year: Mapped[int] = mapped_column(Integer(), nullable=False)
    start_year: Mapped[int] = mapped_column(Integer(), nullable=False)
    inflation_bp: Mapped[int] = mapped_column(Integer(), nullable=False)
    real_return_bp: Mapped[int] = mapped_column(Integer(), nullable=False)
    income_growth_bp: Mapped[int] = mapped_column(Integer(), nullable=False)
    stop_working_age: Mapped[int] = mapped_column(Integer(), nullable=False)
    horizon_age: Mapped[int] = mapped_column(Integer(), nullable=False)
    rule: Mapped[str] = mapped_column(String(32), nullable=False)
    swr_bp: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    income_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    income_manual_minor: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    expenses_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    expenses_manual_minor: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    portfolio_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    portfolio_manual_minor: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    is_default: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=False, server_default=false()
    )
    archived_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


class FireContribution(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """One explicit net-saving override for a plan year (signed minor units)."""

    __tablename__ = "fire_contributions"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "scenario_id",
            "year",
            name="uq_fire_contributions_workspace_scenario_year",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "scenario_id"],
            ["fire_scenarios.workspace_id", "fire_scenarios.id"],
            name="fk_fire_contributions_workspace_scenario",
            ondelete="CASCADE",
        ),
        _year_range("year"),
        CheckConstraint(
            f"net_amount_minor BETWEEN -{MAX_AMOUNT_MINOR} AND {MAX_AMOUNT_MINOR}",
            name="net_amount_valid",
        ),
    )

    scenario_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    year: Mapped[int] = mapped_column(Integer(), nullable=False)
    net_amount_minor: Mapped[int] = mapped_column(BigInteger(), nullable=False)


class FireEvent(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """A one-off cash flow or a permanent annual expense/income change (signed minor units)."""

    __tablename__ = "fire_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "scenario_id"],
            ["fire_scenarios.workspace_id", "fire_scenarios.id"],
            name="fk_fire_events_workspace_scenario",
            ondelete="CASCADE",
        ),
        _year_range("year"),
        CheckConstraint(
            "kind IN ('cash', 'expense_change', 'income_change')",
            name="kind_valid",
        ),
        CheckConstraint(
            f"amount_minor BETWEEN -{MAX_AMOUNT_MINOR} AND {MAX_AMOUNT_MINOR}",
            name="amount_valid",
        ),
        CheckConstraint(
            f"length(label) > 0 AND length(label) <= {MAX_LABEL_LENGTH}",
            name="label_valid",
        ),
        Index("ix_fire_events_workspace_scenario_year", "workspace_id", "scenario_id", "year"),
    )

    scenario_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    year: Mapped[int] = mapped_column(Integer(), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    label: Mapped[str] = mapped_column(String(MAX_LABEL_LENGTH), nullable=False)
