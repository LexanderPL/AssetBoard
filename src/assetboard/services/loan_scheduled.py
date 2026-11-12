"""Loan-driven scheduled payments (F7.6)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.config import Settings
from assetboard.db.engine import run_immediate
from assetboard.db.models.loans import Loan
from assetboard.db.models.scheduled import ScheduledItem
from assetboard.db.repositories import (
    AuditEventRepository,
    CategoryRepository,
    CurrentWorkspaceRepository,
    LoanRepository,
    MonthStateRepository,
    ScheduledItemRepository,
    ScheduledOccurrenceRepository,
)
from assetboard.domain.loans import LoanValidationError, Money, ScheduleError
from assetboard.domain.months import YearMonth
from assetboard.domain.recurrence import Frequency, RecurrenceRule, WeekendPolicy
from assetboard.domain.scheduled import AmountMode, OccurrenceStatus
from assetboard.domain.transactions import TransactionDirection
from assetboard.services import loan_schedule, loans, scheduled
from assetboard.services.context import Context, Role, require_role, require_role_now


@dataclass(frozen=True, slots=True)
class LoanScheduledView:
    item_id: UUID
    name: str
    category_id: UUID
    category_name: str
    next_due: date | None
    payoff_date: date | None
    pending_count: int


def _write[T](factory: sessionmaker[Session], operation: Callable[[Session], T]) -> T:
    return run_immediate(factory, operation)


def _active_item(session: Session, ctx: Context, loan_id: UUID) -> ScheduledItem | None:
    for item in ScheduledItemRepository(session, ctx).ordered(active_only=True):
        if item.loan_id == loan_id:
            return item
    return None


def loan_schedule_item_view(
    factory: sessionmaker[Session], ctx: Context, loan_id: UUID
) -> LoanScheduledView | None:
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        LoanRepository(session, ctx).get_or_raise(loan_id)
        item = _active_item(session, ctx, loan_id)
        if item is None:
            return None
        category = CategoryRepository(session, ctx).get_or_raise(item.category_id)
        today = ctx.clock.today(ctx.workspace.timezone)
        rows = ScheduledOccurrenceRepository(session, ctx).for_item(item.id)
        upcoming = [
            row.due_date
            for row in rows
            if row.status == OccurrenceStatus.PENDING and row.due_date >= today
        ]
        return LoanScheduledView(
            item_id=item.id,
            name=item.name,
            category_id=item.category_id,
            category_name=category.name,
            next_due=min(upcoming) if upcoming else None,
            payoff_date=item.end_date,
            pending_count=sum(1 for row in rows if row.status == OccurrenceStatus.PENDING),
        )


def _item_dates(session: Session, ctx: Context, loan: Loan, today: date) -> tuple[date, date]:
    from assetboard.services.installment_plans import plan_due_dates_in_session

    if getattr(loan, "kind", None) == "installment_plan":
        return plan_due_dates_in_session(session, ctx, loan, today)
    try:
        inputs = loan_schedule.load_loan_schedule_inputs_in_session(
            session,
            ctx,
            loan.id,
            today,
        )
        schedule = loan_schedule.build_inputs_schedule(inputs)
    except (ScheduleError, LoanValidationError) as exc:
        raise loans.LoanError("loan", exc.code) from None
    future = [row for row in schedule.rows if row.date > today]
    if not future:
        raise loans.LoanError("loan", "loan_paid_off")
    return future[0].date, schedule.rows[-1].date


def create_loan_schedule_item_in_session(
    session: Session,
    ctx: Context,
    loan_id: UUID,
    category_id: UUID,
    horizon_months: int,
) -> UUID:
    """Create or refresh the one active loan-linked scheduled item."""
    require_role_now(session, ctx, Role.EDITOR)
    loan = LoanRepository(session, ctx).get_or_raise(loan_id)
    if loan.archived_at is not None:
        raise loans.LoanError("loan", "archived")
    category = CategoryRepository(session, ctx).get_or_raise(category_id)
    if category.archived_at is not None or category.kind != "expense":
        raise loans.LoanError("category_id", "invalid_category")
    today = ctx.clock.today(ctx.workspace.timezone)
    next_due, payoff = _item_dates(session, ctx, loan, today)
    existing = _active_item(session, ctx, loan_id)
    values = scheduled.ScheduledInput(
        name=loan.name,
        direction=TransactionDirection.EXPENSE,
        category_id=category.id,
        amount=Money(0, loan.currency),
        rule=RecurrenceRule(
            start_date=next_due,
            end_date=payoff,
            freq=Frequency.MONTHLY,
            interval=1,
            weekdays=(),
            month_day=loan.payment_day,
            month=1,
            weekend_policy=WeekendPolicy.NONE,
        ),
        amount_mode=AmountMode.FROM_LOAN,
        loan_id=loan.id,
    )
    return scheduled.save_item_in_session(
        session,
        ctx,
        values,
        _settings(horizon_months),
        item_id=existing.id if existing is not None else None,
        require_kind="expense",
    )


def _settings(horizon_months: int) -> Settings:
    """The generator only needs the horizon; avoid a settings dependency in write services."""
    return Settings(_env_file=None, recurrence_horizon_months=horizon_months)


def create_loan_schedule_item(
    factory: sessionmaker[Session],
    ctx: Context,
    loan_id: UUID,
    category_id: UUID,
    settings: Settings,
) -> UUID:
    """Create or refresh the one active loan-linked scheduled item."""
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> UUID:
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        return create_loan_schedule_item_in_session(
            session,
            ctx,
            loan_id,
            category_id,
            settings.recurrence_horizon_months,
        )

    return _write(factory, write)


def disable_loan_schedule_item_in_session(session: Session, ctx: Context, loan_id: UUID) -> None:
    """Archive the loan-linked item and remove only its pending future occurrences."""
    require_role_now(session, ctx, Role.EDITOR)
    LoanRepository(session, ctx).get_or_raise(loan_id)
    item = _active_item(session, ctx, loan_id)
    if item is None:
        return
    today = ctx.clock.today(ctx.workspace.timezone)
    occurrences = ScheduledOccurrenceRepository(session, ctx)
    closed = MonthStateRepository(session, ctx).closed_months()
    for row in occurrences.for_item(item.id):
        if (
            row.status == OccurrenceStatus.PENDING
            and row.due_date >= today
            and YearMonth.from_date(row.due_date) not in closed
        ):
            occurrences.delete(row)
    ScheduledItemRepository(session, ctx).update(item.id, archived_at=ctx.clock.now())
    AuditEventRepository(session, ctx).record(
        actor_user_id=ctx.user.id,
        action="scheduled.archive",
        entity_type="scheduled_item",
        entity_id=str(item.id),
        ts=ctx.clock.now(),
    )


def disable_loan_schedule_item(factory: sessionmaker[Session], ctx: Context, loan_id: UUID) -> None:
    """Archive the loan-linked item and remove only its pending future occurrences."""
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        disable_loan_schedule_item_in_session(session, ctx, loan_id)

    _write(factory, write)


def refresh_loan_occurrences(session: Session, ctx: Context, loan_id: UUID) -> int:
    """Refresh the loan-linked item inside a loan write transaction."""
    return scheduled.refresh_loan_item_in_session(session, ctx, loan_id)


__all__ = [
    "LoanScheduledView",
    "create_loan_schedule_item",
    "create_loan_schedule_item_in_session",
    "disable_loan_schedule_item",
    "disable_loan_schedule_item_in_session",
    "loan_schedule_item_view",
    "refresh_loan_occurrences",
]
