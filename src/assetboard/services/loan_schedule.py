"""Loan schedule mapping (F7.3).

The service maps the stored loan configuration to the pure F7.2 engine types, builds the
configured schedule and a no-overpayment baseline, and returns the summary. There is no finance
math here: every row and total comes from ``domain.loans.build_schedule`` and
``summarize_schedule``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.domain.loans import (
    BalanceCheckpoint,
    CostPeriod,
    LoanScheduleSummary,
    LoanTerms,
    LoanValidationError,
    OneOffOverpayment,
    OverpaymentMode,
    RatePeriod,
    Schedule,
    ScheduleError,
    build_schedule,
    select_anchor,
    summarize_schedule,
)
from assetboard.services import loans
from assetboard.services.context import Context, Role, require_role


@dataclass(frozen=True, slots=True)
class LoanScheduleData:
    view: loans.LoanView
    schedule: Schedule | None
    baseline: Schedule | None
    summary: LoanScheduleSummary | None
    has_overpayments: bool
    error_code: str | None


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
    if view.rate_mode.value == "fixed":
        return [
            RatePeriod(effective_from=row.effective_from, annual_rate_bp=row.rate_bp)
            for row in view.rate_changes
        ]
    if view.margin_bp is None:
        return []
    return [
        RatePeriod(
            effective_from=row.effective_from,
            annual_rate_bp=row.value_bp + view.margin_bp,
        )
        for row in view.reference_rate_values
    ]


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


def loan_schedule(factory: sessionmaker[Session], ctx: Context, loan_id: UUID) -> LoanScheduleData:
    """The configured schedule, its no-overpayment baseline and the summary for one loan."""
    require_role(ctx, Role.VIEWER)
    view = loans.get_loan(factory, ctx, loan_id)
    today = ctx.clock.today(ctx.workspace.timezone)
    terms = _terms(view)
    has_overpayments = view.overpayment_mode is not OverpaymentMode.NONE or bool(view.overpayments)
    try:
        anchor = select_anchor(terms, _checkpoints(view), today)
        schedule = build_schedule(
            terms,
            _rate_timeline(view),
            _costs(view),
            _overpayments(view),
            anchor,
            today,
            view.overpayment_strategy,
            view.day_count,
        )
        if has_overpayments:
            baseline = build_schedule(
                replace(
                    terms,
                    overpayment_mode=OverpaymentMode.NONE,
                    overpayment_amount=None,
                ),
                _rate_timeline(view),
                _costs(view),
                [],
                anchor,
                today,
                view.overpayment_strategy,
                view.day_count,
            )
        else:
            baseline = schedule
        summary = summarize_schedule(schedule, today)
    except (ScheduleError, LoanValidationError) as exc:
        return LoanScheduleData(
            view=view,
            schedule=None,
            baseline=None,
            summary=None,
            has_overpayments=has_overpayments,
            error_code=exc.code,
        )
    return LoanScheduleData(
        view=view,
        schedule=schedule,
        baseline=baseline,
        summary=summary,
        has_overpayments=has_overpayments,
        error_code=None,
    )


__all__ = ["LoanScheduleData", "loan_schedule"]
