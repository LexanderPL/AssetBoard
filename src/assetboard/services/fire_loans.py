"""Live loan resolution for the FIRE projection (F8.12).

The service reads M7 loan schedules at calculation time. It never writes and never creates FIRE
events: the only persisted loan state is the small include/ignore choice. Every amount is an
integer minor unit in the plan currency. A broken loan becomes a visible excluded row, not a
zero and not a server error.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.repositories import InstallmentPlanRepository, LoanRepository
from assetboard.domain.fire import ProjectionLoan
from assetboard.domain.loans import (
    LoanValidationError,
    ScheduleError,
    ScheduleRow,
    summarize_schedule,
)
from assetboard.domain.money import Money
from assetboard.domain.transactions import MAX_AMOUNT_MINOR
from assetboard.services import loans
from assetboard.services.context import Context, Role, require_role
from assetboard.services.loan_schedule import (
    build_inputs_schedule,
    schedule_inputs_from_view,
)


class LoanPlanState(StrEnum):
    INCLUDED = "included"
    IGNORED = "ignored"
    ARCHIVED = "archived"
    PAID_OFF = "paid_off"
    NOT_DRAWN = "not_drawn"
    CURRENCY_MISMATCH = "currency_mismatch"
    SCHEDULE_ERROR = "schedule_error"
    NO_PAYMENT = "no_payment"
    AMOUNT_INVALID = "amount_invalid"
    INSTALLMENT_PLAN = "installment_plan"


ELIGIBLE_STATES = frozenset({LoanPlanState.INCLUDED, LoanPlanState.IGNORED})


@dataclass(frozen=True, slots=True)
class FireLoanRow:
    """One live loan as the FIRE page shows and uses it."""

    loan_id: UUID
    name: str
    currency: str
    state: LoanPlanState
    included: bool
    annual_payment: Money | None = None
    monthly_payment: Money | None = None
    payoff_date: date | None = None
    payoff_year: int | None = None
    current_balance: Money | None = None
    error_code: str | None = None
    interest_only: bool = False
    future_drawdown: bool = False

    @property
    def eligible(self) -> bool:
        return self.state in ELIGIBLE_STATES


@dataclass(frozen=True, slots=True)
class FireLoanData:
    """All loan rows plus the plan-level totals the projection and UI use."""

    currency: str
    as_of: date
    rows: tuple[FireLoanRow, ...]
    included_annual_total: Money
    ignored_annual_total: Money
    payoff_names_by_year: Mapping[int, tuple[str, ...]]

    @property
    def eligible_rows(self) -> tuple[FireLoanRow, ...]:
        return tuple(row for row in self.rows if row.eligible)

    @property
    def excluded_rows(self) -> tuple[FireLoanRow, ...]:
        return tuple(row for row in self.rows if not row.eligible)

    @property
    def active_annual_total(self) -> Money:
        return Money(
            self.included_annual_total.amount_minor + self.ignored_annual_total.amount_minor,
            self.currency,
        )

    @property
    def first_payoff_year(self) -> int | None:
        years = [row.payoff_year for row in self.eligible_rows if row.payoff_year is not None]
        return min(years) if years else None

    def projection_loans(self) -> tuple[ProjectionLoan, ...]:
        """The eligible rows as the pure projection's input; ignored rows stay for transparency."""
        result: list[ProjectionLoan] = []
        for row in self.eligible_rows:
            if row.annual_payment is None or row.payoff_year is None:
                continue
            result.append(
                ProjectionLoan(
                    annual_payment=row.annual_payment,
                    payoff_year=row.payoff_year,
                    included=row.included,
                )
            )
        return tuple(result)


def _money(minor: int, currency: str) -> Money:
    return Money(minor, currency)


def _next_row(rows: tuple[ScheduleRow, ...], today: date) -> ScheduleRow | None:
    return next((row for row in rows if row.date > today), None)


def _is_not_drawn(view: loans.LoanView, remaining_minor: int) -> bool:
    return view.drawdown_mode.value == "tranches" and not view.transfers and remaining_minor == 0


def _resolve_loan(
    view: loans.LoanView,
    *,
    plan_currency: str,
    today: date,
    included: bool,
) -> FireLoanRow:
    """Resolve one schedule loan without touching the database."""
    base = FireLoanRow(
        loan_id=view.id,
        name=view.name,
        currency=view.currency,
        state=LoanPlanState.INCLUDED if included else LoanPlanState.IGNORED,
        included=included,
    )
    if view.archived:
        return replace(base, state=LoanPlanState.ARCHIVED, included=False)
    if view.currency != plan_currency:
        return replace(base, state=LoanPlanState.CURRENCY_MISMATCH, included=False)
    try:
        inputs = schedule_inputs_from_view(view, today)
        actual = build_inputs_schedule(inputs)
        summary = summarize_schedule(actual, today)
    except (ScheduleError, LoanValidationError, loans.LoanError) as exc:
        return replace(
            base,
            state=LoanPlanState.SCHEDULE_ERROR,
            included=False,
            error_code=exc.code,
        )

    remaining = summary.remaining_principal
    if _is_not_drawn(view, remaining.amount_minor):
        return replace(
            base,
            state=LoanPlanState.NOT_DRAWN,
            included=False,
            current_balance=remaining,
            payoff_date=summary.payoff_date,
            payoff_year=summary.payoff_date.year if summary.payoff_date else None,
        )
    if summary.payoff_date is None or remaining.amount_minor == 0:
        return replace(
            base,
            state=LoanPlanState.PAID_OFF,
            included=False,
            current_balance=remaining,
            payoff_date=summary.payoff_date,
            payoff_year=summary.payoff_date.year if summary.payoff_date else None,
        )

    recurring_overpayments = [row for row in inputs.overpayments if row.date <= today]
    recurring = (
        actual
        if len(recurring_overpayments) == len(inputs.overpayments)
        else build_inputs_schedule(inputs, overpayments=recurring_overpayments)
    )
    next_payment = _next_row(recurring.rows, today)
    if next_payment is None:
        return replace(
            base,
            state=LoanPlanState.NO_PAYMENT,
            included=False,
            current_balance=remaining,
            payoff_date=summary.payoff_date,
            payoff_year=summary.payoff_date.year,
        )
    monthly_minor = (
        next_payment.interest.amount_minor
        + next_payment.principal.amount_minor
        + next_payment.costs.amount_minor
        + next_payment.overpayment.amount_minor
    )
    annual_minor = monthly_minor * 12
    if monthly_minor <= 0 or annual_minor > MAX_AMOUNT_MINOR:
        return replace(
            base,
            state=LoanPlanState.AMOUNT_INVALID,
            included=False,
            current_balance=remaining,
            payoff_date=summary.payoff_date,
            payoff_year=summary.payoff_date.year,
        )
    return FireLoanRow(
        loan_id=view.id,
        name=view.name,
        currency=view.currency,
        state=LoanPlanState.IGNORED if not included else LoanPlanState.INCLUDED,
        included=included,
        annual_payment=_money(annual_minor, plan_currency),
        monthly_payment=_money(monthly_minor, plan_currency),
        payoff_date=summary.payoff_date,
        payoff_year=summary.payoff_date.year,
        current_balance=remaining,
        interest_only=next_payment.principal.amount_minor == 0,
        future_drawdown=any(row.date > today for row in view.transfers)
        and view.drawdown_mode.value == "tranches",
    )


def _installment_rows(session: Session, ctx: Context) -> list[FireLoanRow]:
    plans = InstallmentPlanRepository(session, ctx).ordered()
    if not plans:
        return []
    loan_rows = LoanRepository(session, ctx).by_ids([plan.loan_id for plan in plans])
    by_id = {row.id: row for row in loan_rows}
    result: list[FireLoanRow] = []
    for plan in plans:
        loan = by_id.get(plan.loan_id)
        if loan is None:
            continue
        result.append(
            FireLoanRow(
                loan_id=loan.id,
                name=loan.name,
                currency=loan.currency,
                state=LoanPlanState.INSTALLMENT_PLAN,
                included=False,
                monthly_payment=Money(plan.installment_minor, loan.currency),
                payoff_date=plan.end_date,
                payoff_year=plan.end_date.year,
            )
        )
    return result


def load_fire_loans(
    factory: sessionmaker[Session],
    ctx: Context,
    *,
    currency: str,
    ignored_loan_ids: frozenset[UUID] = frozenset(),
    included_overrides: Mapping[UUID, bool] | None = None,
    today: date | None = None,
) -> FireLoanData:
    """Resolve every schedule loan in one bounded set of queries; never writes.

    ``ignored_loan_ids`` comes from the saved plan (or is empty during setup).
    ``included_overrides`` is the preview's posted map; an absent id keeps its saved/default
    state. The caller is responsible for loading both in the same transaction as the plan; this
    function only reads loans.
    """
    require_role(ctx, Role.VIEWER)
    as_of = today or ctx.clock.today(ctx.workspace.timezone)
    overrides = included_overrides or {}
    with factory() as session:
        views = loans.load_loan_views_in_session(session, ctx, today=as_of, include_archived=True)
        rows: list[FireLoanRow] = []
        for view in views:
            included = overrides.get(view.id, view.id not in ignored_loan_ids)
            rows.append(
                _resolve_loan(
                    view,
                    plan_currency=currency,
                    today=as_of,
                    included=included,
                )
            )
        rows.extend(_installment_rows(session, ctx))
    rows.sort(key=lambda row: (not row.eligible, row.name.casefold(), str(row.loan_id)))
    included = [row for row in rows if row.eligible and row.included]
    ignored = [row for row in rows if row.eligible and not row.included]
    included_total = Money(
        sum(row.annual_payment.amount_minor for row in included if row.annual_payment is not None),
        currency,
    )
    ignored_total = Money(
        sum(row.annual_payment.amount_minor for row in ignored if row.annual_payment is not None),
        currency,
    )
    payoff_names: dict[int, list[str]] = {}
    for row in included:
        if row.payoff_year is not None:
            payoff_names.setdefault(row.payoff_year, []).append(row.name)
    return FireLoanData(
        currency=currency,
        as_of=as_of,
        rows=tuple(rows),
        included_annual_total=included_total,
        ignored_annual_total=ignored_total,
        payoff_names_by_year={year: tuple(names) for year, names in sorted(payoff_names.items())},
    )


__all__ = [
    "ELIGIBLE_STATES",
    "FireLoanData",
    "FireLoanRow",
    "LoanPlanState",
    "load_fire_loans",
]
