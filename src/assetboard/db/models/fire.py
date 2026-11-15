"""The one FIRE plan of a workspace (F8.8).

A workspace has at most one live plan. The table keeps its v1 name ``fire_scenarios`` for
migration continuity: the chosen row is reshaped in place, the others are deleted after the user
chooses in the app.

While old scenarios await that choice, each old row stays readable through two temporary columns:
``is_live`` marks the chosen plan and ``legacy`` carries the old presentation data (name, start
year, default/archive flags, linked modes and exclusion category ids) as compact JSON. A partial
unique index on ``is_live`` enforces one live plan per workspace; a later cleanup revision drops
both temporary columns and replaces that index with a plain unique ``workspace_id``.

Amounts are non-negative integer minor units of the plan currency. Event amounts stay **signed**,
an explicit exception to overview §3.2: positive saves/adds/increases, negative
dissaves/withdraws/reduces.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from assetboard.db.base import Base, IdMixin, TimestampMixin, WorkspaceScopedMixin
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
    """The single FIRE plan of a workspace; legacy rows wait for the one-time chooser."""

    __tablename__ = "fire_scenarios"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_fire_scenarios_workspace_id_id"),
        CheckConstraint("length(currency) = 3", name="currency_length"),
        _year_range("birth_year"),
        CheckConstraint("horizon_age BETWEEN 1 AND 120", name="horizon_age_valid"),
        CheckConstraint("stop_working_age BETWEEN 0 AND 120", name="stop_working_age_valid"),
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
            f"income_minor IS NULL OR income_minor BETWEEN 0 AND {MAX_AMOUNT_MINOR}",
            name="income_amount_valid",
        ),
        CheckConstraint(
            f"expenses_minor IS NULL OR expenses_minor BETWEEN 0 AND {MAX_AMOUNT_MINOR}",
            name="expenses_amount_valid",
        ),
        CheckConstraint(
            f"portfolio_minor IS NULL OR portfolio_minor BETWEEN 0 AND {MAX_AMOUNT_MINOR}",
            name="portfolio_amount_valid",
        ),
        CheckConstraint(
            "NOT is_live OR ("
            "income_minor IS NOT NULL AND expenses_minor IS NOT NULL "
            "AND portfolio_minor IS NOT NULL)",
            name="live_complete",
        ),
        CheckConstraint("is_live OR legacy IS NOT NULL", name="live_or_legacy"),
        CheckConstraint("NOT (is_live AND legacy IS NOT NULL)", name="live_not_legacy"),
        # One chosen plan per workspace while old scenarios await the chooser.
        Index(
            "uq_fire_scenarios_live_workspace",
            "workspace_id",
            unique=True,
            sqlite_where=text("is_live"),
            postgresql_where=text("is_live"),
        ),
        Index(
            "uq_fire_scenarios_import_key",
            "workspace_id",
            "import_key",
            unique=True,
            sqlite_where=text("import_key IS NOT NULL"),
            postgresql_where=text("import_key IS NOT NULL"),
        ),
    )

    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    birth_year: Mapped[int] = mapped_column(Integer(), nullable=False)
    stop_working_age: Mapped[int] = mapped_column(Integer(), nullable=False)
    horizon_age: Mapped[int] = mapped_column(Integer(), nullable=False)
    inflation_bp: Mapped[int] = mapped_column(Integer(), nullable=False)
    real_return_bp: Mapped[int] = mapped_column(Integer(), nullable=False)
    income_growth_bp: Mapped[int] = mapped_column(Integer(), nullable=False)
    rule: Mapped[str] = mapped_column(String(32), nullable=False)
    swr_bp: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    income_minor: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    expenses_minor: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    portfolio_minor: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    needs_review: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=False, server_default=false()
    )
    # Temporary migration columns; removed by the cleanup revision once every workspace chose.
    is_live: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=False, server_default=false()
    )
    legacy: Mapped[str | None] = mapped_column(Text(), nullable=True)
    import_key: Mapped[str | None] = mapped_column(String(67), nullable=True)


class FireEvent(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """A cash flow, a permanent series change or a saving override in one plan year."""

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
            "kind IN ('cash', 'expense_change', 'income_change', 'saving_override')",
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
        Index(
            "uq_fire_events_saving_override",
            "workspace_id",
            "scenario_id",
            "year",
            unique=True,
            sqlite_where=text("kind = 'saving_override'"),
            postgresql_where=text("kind = 'saving_override'"),
        ),
        Index(
            "uq_fire_events_import_key",
            "workspace_id",
            "import_key",
            unique=True,
            sqlite_where=text("import_key IS NOT NULL"),
            postgresql_where=text("import_key IS NOT NULL"),
        ),
    )

    scenario_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    year: Mapped[int] = mapped_column(Integer(), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    label: Mapped[str] = mapped_column(String(MAX_LABEL_LENGTH), nullable=False)
    import_key: Mapped[str | None] = mapped_column(String(67), nullable=True)


class FireLoanSetting(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """A saved non-default FIRE choice for one loan; absence means included.

    Only ignored loans are written as rows (``included=false``). A row survives archiving the
    loan so restoring the loan restores the choice. The plan and loan foreign keys are composite
    so a setting can never point at another workspace's plan or loan.
    """

    __tablename__ = "fire_loan_settings"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "scenario_id", "loan_id", name="uq_fire_loan_settings_plan_loan"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "scenario_id"],
            ["fire_scenarios.workspace_id", "fire_scenarios.id"],
            name="fk_fire_loan_settings_workspace_scenario",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "loan_id"],
            ["loans.workspace_id", "loans.id"],
            name="fk_fire_loan_settings_workspace_loan",
            ondelete="RESTRICT",
        ),
        Index("ix_fire_loan_settings_workspace_scenario", "workspace_id", "scenario_id"),
    )

    scenario_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    loan_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    included: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=False, server_default=false()
    )
