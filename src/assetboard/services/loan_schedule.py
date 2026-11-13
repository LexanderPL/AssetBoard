"""Loan schedule mapping (F7.3/F7.4).

The service maps the stored loan configuration to the pure F7.2 engine types, builds the
configured schedule and a no-overpayment baseline, and returns the summary. There is no finance
math here: every row and total comes from ``domain.loans.build_schedule`` and
``summarize_schedule``. F7.4 reuses the loaded inputs for what-if scenarios.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.repositories import LoanPaymentRepository
from assetboard.domain.loans import (
    BalanceCheckpoint,
    CostPeriod,
    DayCount,
    LoanPaymentKind,
    LoanScheduleSummary,
    LoanTerms,
    LoanValidationError,
    OneOffOverpayment,
    OverpaymentMode,
    OverpaymentStrategy,
    RatePeriod,
    Schedule,
    ScheduleAnchor,
    ScheduleError,
    build_schedule,
    select_anchor,
    summarize_schedule,
)
from assetboard.domain.money import Money
from assetboard.services import loans
from assetboard.services.context import Context, Role, require_role


@dataclass(frozen=True, slots=True)
class SchedulePaymentActual:
    id: UUID
    date: date
    kind: LoanPaymentKind
    total: Money
    principal: Money
    interest: Money
    costs: Money
    overpayment: Money
    note: str


@dataclass(frozen=True, slots=True)
class ScheduleActual:
    principal: Money
    interest: Money
    costs: Money
    overpayment: Money
    total: Money
    count: int


@dataclass(frozen=True, slots=True)
class LoanScheduleData:
    view: loans.LoanView
    schedule: Schedule | None
    baseline: Schedule | None
    summary: LoanScheduleSummary | None
    has_overpayments: bool
    error_code: str | None
    actual_payments: tuple[SchedulePaymentActual, ...] = ()
    actual_by_date: Mapping[date, ScheduleActual] = field(
        default_factory=dict[date, ScheduleActual]
    )


@dataclass(frozen=True, slots=True)
class LoanScheduleInputs:
    """Everything the pure engine needs for one loan, loaded once."""

    view: loans.LoanView
    terms: LoanTerms
    rate_timeline: list[RatePeriod]
    costs: list[CostPeriod]
    overpayments: list[OneOffOverpayment]
    anchor: ScheduleAnchor
    as_of: date
    day_count: DayCount
    strategy: OverpaymentStrategy


def _terms(view: loans.LoanView) -> LoanTerms:
    return LoanTerms(
        kind=view.kind,
        currency=view.currency,
        principal_original=view.principal_original,
        origination_date=view.origination_date,
        first_payment_date=view.first_payment_date,
        term_months=view.term_months,
        payment_day=view.payment_day,
        installment_override=view.installment_override,
        overpayment_mode=view.overpayment_mode,
        overpayment_amount=view.overpayment_amount,
    )


def _rate_timeline(view: loans.LoanView) -> list[RatePeriod]:
    return loans.rate_timeline(view)


def _costs(view: loans.LoanView) -> list[CostPeriod]:
    return [
        CostPeriod(
            value=row.value,
            effective_from=row.effective_from,
            effective_to=row.effective_to,
        )
        for row in view.cost_components
    ]


def _overpayments(view: loans.LoanView) -> list[OneOffOverpayment]:
    return [OneOffOverpayment(date=row.date, amount=row.amount) for row in view.overpayments]


def _checkpoints(view: loans.LoanView) -> list[BalanceCheckpoint]:
    return [
        BalanceCheckpoint(as_of=row.as_of, remaining_principal=row.remaining_principal)
        for row in view.checkpoints
    ]


def schedule_inputs_from_view(view: loans.LoanView, as_of: date) -> LoanScheduleInputs:
    terms = _terms(view)
    anchor = select_anchor(terms, _checkpoints(view), as_of)
    return LoanScheduleInputs(
        view=view,
        terms=terms,
        rate_timeline=_rate_timeline(view),
        costs=_costs(view),
        overpayments=_overpayments(view),
        anchor=anchor,
        as_of=as_of,
        day_count=view.day_count,
        strategy=view.overpayment_strategy,
    )


def load_loan_schedule_inputs(
    factory: sessionmaker[Session], ctx: Context, loan_id: UUID
) -> LoanScheduleInputs:
    """Load and map one loan for the engine; domain errors propagate with their code."""
    require_role(ctx, Role.VIEWER)
    view = loans.get_loan(factory, ctx, loan_id)
    return schedule_inputs_from_view(view, ctx.clock.today(ctx.workspace.timezone))


def load_loan_schedule_inputs_in_session(
    session: Session, ctx: Context, loan_id: UUID, as_of: date
) -> LoanScheduleInputs:
    """The same mapping inside the caller's transaction, anchored at ``as_of``."""
    require_role(ctx, Role.VIEWER)
    today = ctx.clock.today(ctx.workspace.timezone)
    view = loans.load_loan_view_in_session(session, ctx, loan_id, today)
    return schedule_inputs_from_view(view, as_of)


def anchor_for_date(inputs: LoanScheduleInputs, day: date) -> ScheduleAnchor:
    """The latest checkpoint at or before ``day``, otherwise the loan's origination."""
    return select_anchor(inputs.terms, _checkpoints(inputs.view), day)


def _base_anchor(inputs: LoanScheduleInputs) -> ScheduleAnchor:
    if inputs.view.origination_date is not None and inputs.view.principal_original is not None:
        return ScheduleAnchor(
            date=inputs.view.origination_date,
            balance=inputs.view.principal_original,
        )
    checkpoints = _checkpoints(inputs.view)
    if not checkpoints:
        raise ScheduleError("no_anchor")
    earliest = min(
        checkpoints, key=lambda item: (item.as_of, item.remaining_principal.amount_minor)
    )
    return ScheduleAnchor(date=earliest.as_of, balance=earliest.remaining_principal)


def schedule_from_anchor(
    inputs: LoanScheduleInputs, anchor: ScheduleAnchor, as_of: date
) -> Schedule:
    """Build from ``anchor`` with the remaining term adjusted for rows already projected.

    The F7.2 engine takes the number of payments from its anchor. A checkpoint after the loan's
    base anchor needs the original term minus the rows the base schedule already put before it.
    """
    base = _base_anchor(inputs)
    remaining = inputs.terms.term_months
    if anchor.date > base.date:
        base_schedule = build_inputs_schedule(replace(inputs, anchor=base, as_of=as_of))
        elapsed = sum(1 for row in base_schedule.rows if row.date <= anchor.date)
        remaining = max(1, inputs.terms.term_months - elapsed)
    terms = (
        inputs.terms
        if remaining == inputs.terms.term_months
        else replace(inputs.terms, term_months=remaining)
    )
    return build_inputs_schedule(replace(inputs, anchor=anchor, terms=terms, as_of=as_of))


def build_inputs_schedule(
    inputs: LoanScheduleInputs,
    *,
    terms: LoanTerms | None = None,
    rates: list[RatePeriod] | None = None,
    overpayments: list[OneOffOverpayment] | None = None,
    strategy: OverpaymentStrategy | str | None = None,
) -> Schedule:
    """Build a schedule from loaded inputs, replacing only what a scenario changes."""
    return build_schedule(
        terms if terms is not None else inputs.terms,
        rates if rates is not None else inputs.rate_timeline,
        inputs.costs,
        overpayments if overpayments is not None else inputs.overpayments,
        inputs.anchor,
        inputs.as_of,
        strategy if strategy is not None else inputs.strategy,
        inputs.day_count,
    )


def list_figures(view: loans.LoanView, today: date) -> loans.LoanListFigures:
    """The loan-list figures for one loan; a broken schedule becomes an unavailable row."""
    try:
        inputs = schedule_inputs_from_view(view, today)
        schedule = build_inputs_schedule(inputs)
        summary = summarize_schedule(schedule, today)
    except (ScheduleError, LoanValidationError) as exc:
        return loans.LoanListFigures(
            currency=view.currency,
            remaining_principal=None,
            monthly_installment=None,
            percent_repaid_bp=None,
            next_payment_date=None,
            due_next_30_days=Money(0, view.currency),
            current_rate_bp=view.current_rate_bp,
            paid_off=False,
            schedule_error=exc.code,
        )
    window_end = today + timedelta(days=30)
    due = sum(
        row.installment_total.amount_minor
        for row in schedule.rows
        if today < row.date <= window_end
    )
    return loans.LoanListFigures(
        currency=view.currency,
        remaining_principal=summary.remaining_principal,
        monthly_installment=summary.next_installment or Money(0, view.currency),
        percent_repaid_bp=max(0, min(10_000, summary.percent_repaid_bp)),
        next_payment_date=summary.next_payment_date,
        due_next_30_days=Money(due, view.currency),
        current_rate_bp=view.current_rate_bp,
        paid_off=summary.remaining_principal.amount_minor == 0,
        schedule_error=None,
    )


def _actual_payments(
    factory: sessionmaker[Session], ctx: Context, loan_id: UUID, currency: str
) -> tuple[SchedulePaymentActual, ...]:
    with factory() as session:
        rows = LoanPaymentRepository(session, ctx).for_loan(loan_id)
    return tuple(
        SchedulePaymentActual(
            id=row.id,
            date=row.date,
            kind=LoanPaymentKind(row.kind),
            total=Money(row.total_minor, currency),
            principal=Money(row.principal_minor, currency),
            interest=Money(row.interest_minor, currency),
            costs=Money(row.costs_minor, currency),
            overpayment=Money(row.overpayment_minor, currency),
            note=row.note,
        )
        for row in rows
    )


def _actual_by_date(
    actuals: tuple[SchedulePaymentActual, ...],
) -> dict[date, ScheduleActual]:
    sums: dict[date, ScheduleActual] = {}
    for payment in actuals:
        if payment.kind is not LoanPaymentKind.INSTALLMENT:
            continue
        previous = sums.get(payment.date)
        if previous is None:
            sums[payment.date] = ScheduleActual(
                principal=payment.principal,
                interest=payment.interest,
                costs=payment.costs,
                overpayment=payment.overpayment,
                total=payment.total,
                count=1,
            )
        else:
            sums[payment.date] = ScheduleActual(
                principal=Money(
                    previous.principal.amount_minor + payment.principal.amount_minor,
                    payment.total.currency,
                ),
                interest=Money(
                    previous.interest.amount_minor + payment.interest.amount_minor,
                    payment.total.currency,
                ),
                costs=Money(
                    previous.costs.amount_minor + payment.costs.amount_minor,
                    payment.total.currency,
                ),
                overpayment=Money(
                    previous.overpayment.amount_minor + payment.overpayment.amount_minor,
                    payment.total.currency,
                ),
                total=Money(
                    previous.total.amount_minor + payment.total.amount_minor,
                    payment.total.currency,
                ),
                count=previous.count + 1,
            )
    return sums


def loan_schedule(factory: sessionmaker[Session], ctx: Context, loan_id: UUID) -> LoanScheduleData:
    """The configured schedule, its no-overpayment baseline and the summary for one loan."""
    require_role(ctx, Role.VIEWER)
    try:
        inputs = load_loan_schedule_inputs(factory, ctx, loan_id)
        has_overpayments = inputs.view.overpayment_mode is not OverpaymentMode.NONE or bool(
            inputs.view.overpayments
        )
        schedule = build_inputs_schedule(inputs)
        if has_overpayments:
            baseline = build_inputs_schedule(
                inputs,
                terms=replace(
                    inputs.terms,
                    overpayment_mode=OverpaymentMode.NONE,
                    overpayment_amount=None,
                ),
                overpayments=[],
            )
        else:
            baseline = schedule
        summary = summarize_schedule(schedule, inputs.as_of)
        actual_payments = _actual_payments(factory, ctx, loan_id, inputs.view.currency)
        actual_by_date = _actual_by_date(actual_payments)
    except (ScheduleError, LoanValidationError) as exc:
        view = loans.get_loan(factory, ctx, loan_id)
        return LoanScheduleData(
            view=view,
            schedule=None,
            baseline=None,
            summary=None,
            has_overpayments=(
                view.overpayment_mode is not OverpaymentMode.NONE or bool(view.overpayments)
            ),
            error_code=exc.code,
        )
    return LoanScheduleData(
        view=inputs.view,
        schedule=schedule,
        baseline=baseline,
        summary=summary,
        has_overpayments=has_overpayments,
        error_code=None,
        actual_payments=actual_payments,
        actual_by_date=actual_by_date,
    )


__all__ = [
    "LoanScheduleData",
    "LoanScheduleInputs",
    "ScheduleActual",
    "SchedulePaymentActual",
    "anchor_for_date",
    "build_inputs_schedule",
    "list_figures",
    "load_loan_schedule_inputs",
    "load_loan_schedule_inputs_in_session",
    "loan_schedule",
    "schedule_from_anchor",
    "schedule_inputs_from_view",
]
