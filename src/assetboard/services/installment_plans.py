"""Installment plans (F7.7): fixed-count liabilities without interest."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session, sessionmaker

from assetboard.config import DEFAULT_RECURRENCE_HORIZON_MONTHS
from assetboard.db.engine import run_immediate
from assetboard.db.models.ledger import Category
from assetboard.db.models.loans import InstallmentPlan, Loan
from assetboard.db.models.scheduled import ScheduledOccurrence
from assetboard.db.repositories import (
    AuditEventRepository,
    CategoryRepository,
    CurrentWorkspaceRepository,
    InstallmentPlanRepository,
    LoanRepository,
    NotFound,
    ScheduledItemRepository,
    ScheduledOccurrenceRepository,
    TransactionRepository,
)
from assetboard.db.repositories.base import WorkspaceScope
from assetboard.domain.loans import (
    MAX_TERM_MONTHS,
    LoanKind,
    Money,
    clean_loan_name,
    validate_payment_day,
)
from assetboard.domain.transactions import MAX_AMOUNT_MINOR
from assetboard.services import loans
from assetboard.services.context import Context, Role, require_role, require_role_now


@dataclass(frozen=True, slots=True)
class InstallmentPlanView:
    loan: loans.LoanView
    plan_id: UUID
    name: str
    currency: str
    installment: Money
    remaining_count: int
    total_remaining: Money
    end_date: date
    next_due: date | None
    category_id: UUID
    category_name: str
    closed: bool
    paid_count: int
    original_count: int
    percent_repaid_bp: int
    due_occurrence: date | None


@dataclass(frozen=True, slots=True)
class InstallmentPlansPage:
    active: tuple[InstallmentPlanView, ...]
    closed: tuple[InstallmentPlanView, ...]


def _write[T](factory: sessionmaker[Session], operation: Callable[[Session], T]) -> T:
    return run_immediate(factory, operation)


def _month_end(year: int, month: int) -> int:
    import calendar

    return calendar.monthrange(year, month)[1]


def _add_months(day: date, months: int, payment_day: int) -> date:
    index = day.year * 12 + (day.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(payment_day, _month_end(year, month)))


def next_due_on_or_after(day: date, payment_day: int) -> date:
    candidate = date(day.year, day.month, min(payment_day, _month_end(day.year, day.month)))
    return candidate if candidate >= day else _add_months(day, 1, payment_day)


def next_due_after(day: date, payment_day: int) -> date:
    return _add_months(day, 1, payment_day)


def _end_date(first_due: date, count: int, payment_day: int) -> date:
    return _add_months(first_due, max(0, count - 1), payment_day)


def _validate_count(value: int) -> int:
    if type(value) is not int or not 0 <= value <= MAX_TERM_MONTHS:
        raise loans.LoanError("remaining_count", "invalid_term")
    return value


def _validate_installment(value: Money | None, currency: str) -> Money:
    if value is None or value.currency != currency:
        raise loans.LoanError("installment", "invalid_currency")
    if value.amount_minor <= 0 or value.amount_minor > MAX_AMOUNT_MINOR:
        raise loans.LoanError("installment", "invalid_amount")
    return value


def _category(session: Session, ctx: Context, category_id: UUID) -> Category:
    category = CategoryRepository(session, ctx).get_or_raise(category_id)
    if category.kind != "expense" or category.archived_at is not None:
        raise loans.LoanError("category_id", "invalid_category")
    return category


def _occurrences_for_loan(
    session: Session, ctx: Context, loan_id: UUID
) -> list[ScheduledOccurrence]:
    items = ScheduledItemRepository(session, ctx).for_loan(loan_id)
    return ScheduledOccurrenceRepository(session, ctx).for_items([item.id for item in items])


def _progress(paid_count: int, remaining_count: int) -> tuple[int, int]:
    original = paid_count + remaining_count
    if original <= 0:
        return 0, 0
    percent = (paid_count * 20_000 + original) // (2 * original)
    return original, min(10_000, percent)


def _plan_view(
    session: Session,
    ctx: Context,
    loan: Loan,
    plan: InstallmentPlan,
    today: date,
) -> InstallmentPlanView:
    category = CategoryRepository(session, ctx).get_or_raise(plan.category_id)
    closed = plan.closed_at is not None or loan.archived_at is not None
    count = plan.remaining_count
    total = 0 if closed else plan.installment_minor * count
    next_due = None
    if not closed and count > 0:
        next_due = next_due_on_or_after(today, loan.payment_day)
    occurrences = _occurrences_for_loan(session, ctx, loan.id)
    paid = sum(1 for row in occurrences if row.status == "posted" and row.direction == "expense")
    original, percent = _progress(paid, count)
    due = [
        row.due_date
        for row in occurrences
        if row.status == "pending" and row.direction == "expense" and row.due_date <= today
    ]
    due_occurrence = min(due) if due and not closed else None
    return InstallmentPlanView(
        loan=_view(session, ctx, loan, today),
        plan_id=plan.id,
        name=loan.name,
        currency=loan.currency,
        installment=Money(plan.installment_minor, loan.currency),
        remaining_count=count,
        total_remaining=Money(total, loan.currency),
        end_date=plan.end_date,
        next_due=next_due,
        category_id=category.id,
        category_name=category.name,
        closed=closed,
        paid_count=paid,
        original_count=original,
        percent_repaid_bp=percent,
        due_occurrence=due_occurrence,
    )


def _view(session: Session, ctx: Context, loan: Loan, today: date) -> loans.LoanView:
    return loans.load_loan_view_in_session(session, ctx, loan.id, today)


def list_installment_plans(factory: sessionmaker[Session], ctx: Context) -> InstallmentPlansPage:
    require_role(ctx, Role.VIEWER)
    today = ctx.clock.today(ctx.workspace.timezone)
    with factory() as session:
        plans = InstallmentPlanRepository(session, ctx).ordered()
        loan_repository = LoanRepository(session, ctx)
        views: list[InstallmentPlanView] = []
        for plan in plans:
            loan = loan_repository.get(plan.loan_id)
            if loan is None:
                continue
            views.append(_plan_view(session, ctx, loan, plan, today))
    active = tuple(row for row in views if not row.closed)
    closed = tuple(row for row in views if row.closed)
    return InstallmentPlansPage(active=active, closed=closed)


def get_installment_plan(
    factory: sessionmaker[Session], ctx: Context, loan_id: UUID
) -> InstallmentPlanView:
    require_role(ctx, Role.VIEWER)
    today = ctx.clock.today(ctx.workspace.timezone)
    with factory() as session:
        loan = LoanRepository(session, ctx).get_or_raise(loan_id)
        if loan.kind != LoanKind.INSTALLMENT_PLAN.value:
            raise NotFound
        plan = InstallmentPlanRepository(session, ctx).for_loan(loan.id)
        if plan is None:
            raise NotFound
        return _plan_view(session, ctx, loan, plan, today)


def post_next_installment(factory: sessionmaker[Session], ctx: Context, loan_id: UUID) -> UUID:
    """Post the earliest pending occurrence due today or earlier, through the normal service."""
    require_role(ctx, Role.EDITOR)
    today = ctx.clock.today(ctx.workspace.timezone)
    with factory() as session:
        loan = LoanRepository(session, ctx).get_or_raise(loan_id)
        if loan.kind != LoanKind.INSTALLMENT_PLAN.value:
            raise NotFound
        plan = InstallmentPlanRepository(session, ctx).for_loan(loan.id)
        if plan is None:
            raise NotFound
        if plan.closed_at is not None or loan.archived_at is not None or plan.remaining_count <= 0:
            raise loans.LoanError("loan", "loan_paid_off")
        due = [
            row
            for row in _occurrences_for_loan(session, ctx, loan.id)
            if row.status == "pending" and row.direction == "expense" and row.due_date <= today
        ]
        if not due:
            raise loans.LoanError("loan", "no_due_installment")
        target = min(due, key=lambda row: (row.due_date, row.id))
        occurrence_id = target.id
        amount_minor = target.expected_minor
    from assetboard.services import occurrences as occurrence_service

    return occurrence_service.post_occurrence(
        factory, ctx, occurrence_id, amount_minor=amount_minor, day=today
    )


def plan_remaining_minor_at_in_session(
    session: Session, ctx: Context, loan_id: UUID, as_of: date
) -> int | None:
    """The plan's remaining principal at ``as_of`` from its posted occurrences.

    ``remaining_count`` is the current count, so the original count is reconstructed as
    ``remaining_count + posted_count``. A plan created after ``as_of`` is 0; a plan closed or
    archived on or before ``as_of`` is 0; otherwise posted occurrences whose linked transaction
    date (or due date when unlinked) is on or before ``as_of`` are subtracted.
    """
    plan = InstallmentPlanRepository(session, ctx).for_loan(loan_id)
    if plan is None:
        return None
    loan = LoanRepository(session, ctx).get(loan_id)
    if loan is None:
        return None
    local = ZoneInfo(ctx.workspace.timezone)
    if as_of < plan.created_at.astimezone(local).date():
        return 0
    closed = plan.closed_at.astimezone(local).date() if plan.closed_at is not None else None
    archived = loan.archived_at.astimezone(local).date() if loan.archived_at is not None else None
    if (closed is not None and closed <= as_of) or (archived is not None and archived <= as_of):
        return 0
    occurrences = _occurrences_for_loan(session, ctx, loan.id)
    posted_dates: list[date] = []
    transactions = TransactionRepository(session, ctx)
    for row in occurrences:
        if row.status != "posted" or row.direction != "expense":
            continue
        transaction = (
            transactions.get(row.transaction_id) if row.transaction_id is not None else None
        )
        # The count is decremented when the occurrence is posted, so the linked transaction's
        # date is the right boundary for an early or late post; fall back to the due date.
        posted_dates.append(
            transaction.date
            if transaction is not None and transaction.deleted_at is None
            else row.due_date
        )
    original = plan.remaining_count + len(posted_dates)
    posted_by_as_of = sum(1 for day in posted_dates if day <= as_of)
    return plan.installment_minor * max(0, original - posted_by_as_of)


def plan_installment_in_session(session: Session, ctx: WorkspaceScope, loan_id: UUID) -> int | None:
    plan = InstallmentPlanRepository(session, ctx).for_loan(loan_id)
    if plan is None or plan.closed_at is not None:
        return None
    return plan.installment_minor


def plan_due_dates_in_session(
    session: Session, ctx: Context, loan: Loan, today: date
) -> tuple[date, date]:
    plan = InstallmentPlanRepository(session, ctx).for_loan(loan.id)
    if plan is None or plan.closed_at is not None or plan.remaining_count <= 0:
        raise loans.LoanError("loan", "loan_paid_off")
    # An overdue pending occurrence is the next due date even when today's nominal date is
    # already past the plan's end; posting it must not roll the whole transaction back.
    pending = [
        row.due_date
        for row in _occurrences_for_loan(session, ctx, loan.id)
        if row.status == "pending" and row.direction == "expense"
    ]
    if pending:
        return min(pending), plan.end_date
    next_due = next_due_on_or_after(today, loan.payment_day)
    if plan.end_date < next_due:
        raise loans.LoanError("loan", "loan_paid_off")
    return next_due, plan.end_date


def create_installment_plan(
    factory: sessionmaker[Session],
    ctx: Context,
    *,
    name: str,
    installment: Money | None,
    remaining_count: int,
    payment_day: int,
    category_id: UUID,
    horizon_months: int = DEFAULT_RECURRENCE_HORIZON_MONTHS,
) -> UUID:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> UUID:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        clean_name = clean_loan_name(name)
        currency = CurrentWorkspaceRepository(session, ctx).get().base_currency
        amount = _validate_installment(installment, currency)
        count = _validate_count(remaining_count)
        if count < 1:
            raise loans.LoanError("remaining_count", "invalid_term")
        validate_payment_day(payment_day)
        category = _category(session, ctx, category_id)
        today = ctx.clock.today(ctx.workspace.timezone)
        next_due = next_due_on_or_after(today, payment_day)
        end_date = _end_date(next_due, count, payment_day)
        loan = LoanRepository(session, ctx).create(
            name=clean_name,
            kind=LoanKind.INSTALLMENT_PLAN.value,
            currency=currency,
            principal_original_minor=amount.amount_minor * count,
            origination_date=None,
            first_payment_date=next_due,
            term_months=count,
            payment_day=payment_day,
            rate_mode="fixed",
            day_count="monthly",
            margin_bp=None,
            reference_rate_id=None,
            installment_override_minor=None,
            overpayment_mode="none",
            overpayment_amount_minor=None,
            overpayment_strategy="reduce_term",
            archived_at=None,
        )
        plan = InstallmentPlanRepository(session, ctx).create(
            loan_id=loan.id,
            category_id=category.id,
            installment_minor=amount.amount_minor,
            remaining_count=count,
            end_date=end_date,
            closed_at=None,
        )
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="installment_plan.created",
            entity_type="loan",
            entity_id=str(loan.id),
            ts=ctx.clock.now(),
            data={"plan_id": str(plan.id), "name": clean_name},
        )
        from assetboard.services.loan_scheduled import create_loan_schedule_item_in_session

        create_loan_schedule_item_in_session(session, ctx, loan.id, category.id, horizon_months)
        return loan.id

    return _write(factory, write)


def update_installment_plan(
    factory: sessionmaker[Session],
    ctx: Context,
    loan_id: UUID,
    *,
    name: str,
    installment: Money | None,
    remaining_count: int,
    payment_day: int,
    category_id: UUID,
    horizon_months: int = DEFAULT_RECURRENCE_HORIZON_MONTHS,
) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        loan = LoanRepository(session, ctx).get_or_raise(loan_id)
        if loan.kind != LoanKind.INSTALLMENT_PLAN.value:
            raise NotFound
        plan = InstallmentPlanRepository(session, ctx).for_loan(loan.id)
        if plan is None:
            raise NotFound
        clean_name = clean_loan_name(name)
        amount = _validate_installment(installment, loan.currency)
        count = _validate_count(remaining_count)
        validate_payment_day(payment_day)
        category = _category(session, ctx, category_id)
        today = ctx.clock.today(ctx.workspace.timezone)
        next_due = next_due_on_or_after(today, payment_day)
        end_date = _end_date(next_due, count, payment_day) if count else plan.end_date
        LoanRepository(session, ctx).update(
            loan.id,
            name=clean_name,
            first_payment_date=next_due,
            term_months=count,
            payment_day=payment_day,
            principal_original_minor=amount.amount_minor * count if count else None,
            archived_at=None if count else ctx.clock.now(),
        )
        InstallmentPlanRepository(session, ctx).update(
            plan.id,
            category_id=category.id,
            installment_minor=amount.amount_minor,
            remaining_count=count,
            end_date=end_date,
            closed_at=ctx.clock.now() if count == 0 else None,
        )
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="installment_plan.updated",
            entity_type="loan",
            entity_id=str(loan.id),
            ts=ctx.clock.now(),
            data={"plan_id": str(plan.id), "name": clean_name},
        )
        from assetboard.services.loan_scheduled import (
            create_loan_schedule_item_in_session,
            disable_loan_schedule_item_in_session,
        )

        if count == 0:
            LoanRepository(session, ctx).update(loan.id, archived_at=ctx.clock.now())
            disable_loan_schedule_item_in_session(session, ctx, loan.id)
        else:
            create_loan_schedule_item_in_session(session, ctx, loan.id, category.id, horizon_months)

    _write(factory, write)


def apply_posted_occurrence(
    session: Session,
    ctx: Context,
    loan: Loan,
    posted_on: date,
    *,
    actor_user_id: UUID | None,
    now: datetime,
) -> None:
    """Decrement a plan after one occurrence is posted."""
    if loan.kind != LoanKind.INSTALLMENT_PLAN.value:
        return
    plan = InstallmentPlanRepository(session, ctx).for_loan(loan.id)
    if plan is None or plan.closed_at is not None or plan.remaining_count <= 0:
        return
    count = plan.remaining_count - 1
    next_due = next_due_after(posted_on, loan.payment_day)
    end_date = _end_date(next_due, count, loan.payment_day) if count > 0 else plan.end_date
    InstallmentPlanRepository(session, ctx).update(
        plan.id,
        remaining_count=count,
        end_date=end_date,
        closed_at=now if count == 0 else None,
    )
    LoanRepository(session, ctx).update(
        loan.id,
        term_months=count,
        principal_original_minor=plan.installment_minor * count if count else None,
        first_payment_date=next_due,
        archived_at=now if count == 0 else None,
    )
    AuditEventRepository(session, ctx).record(
        actor_user_id=actor_user_id,
        actor_type="system" if actor_user_id is None else "user",
        action="installment_plan.payment_posted",
        entity_type="loan",
        entity_id=str(loan.id),
        ts=now,
        data={"plan_id": str(plan.id), "posted_on": posted_on.isoformat()},
    )
    from assetboard.services.loan_scheduled import (
        create_loan_schedule_item_in_session,
        disable_loan_schedule_item_in_session,
    )

    if count == 0:
        disable_loan_schedule_item_in_session(session, ctx, loan.id)
    else:
        create_loan_schedule_item_in_session(
            session, ctx, loan.id, plan.category_id, DEFAULT_RECURRENCE_HORIZON_MONTHS
        )


def _reactivate_loan_item(session: Session, ctx: Context, loan_id: UUID) -> None:
    """Reuse an archived loan item before regenerating, so its old rows are not duplicated."""
    items = ScheduledItemRepository(session, ctx).for_loan(loan_id)
    for item in reversed(items):
        if item.archived_at is not None:
            ScheduledItemRepository(session, ctx).update(item.id, archived_at=None, paused_at=None)
            return


def revert_posted_occurrence(
    session: Session,
    ctx: Context,
    loan: Loan,
    posted_on: date,
    *,
    actor_user_id: UUID | None,
    now: datetime,
) -> None:
    """Reopen a plan after a posted occurrence returns to pending.

    A manually archived plan (closed with a positive remaining count) is left alone; undoing
    one of its old installments must not unarchive it. A paid-off plan (count zero) reopens.
    """
    plan = InstallmentPlanRepository(session, ctx).for_loan(loan.id)
    if plan is None:
        return
    count = plan.remaining_count + 1
    pending = [
        row.due_date
        for row in _occurrences_for_loan(session, ctx, loan.id)
        if row.status == "pending" and row.direction == "expense"
    ]
    next_due = min(pending) if pending else next_due_on_or_after(posted_on, loan.payment_day)
    end_date = _end_date(next_due, count, loan.payment_day)
    InstallmentPlanRepository(session, ctx).update(
        plan.id,
        remaining_count=count,
        end_date=end_date,
        closed_at=None,
    )
    LoanRepository(session, ctx).update(
        loan.id,
        term_months=count,
        principal_original_minor=plan.installment_minor * count,
        first_payment_date=next_due,
        archived_at=None,
    )
    AuditEventRepository(session, ctx).record(
        actor_user_id=actor_user_id,
        actor_type="system" if actor_user_id is None else "user",
        action="installment_plan.payment_reverted",
        entity_type="loan",
        entity_id=str(loan.id),
        ts=now,
        data={"plan_id": str(plan.id), "posted_on": posted_on.isoformat()},
    )
    from assetboard.services.loan_scheduled import create_loan_schedule_item_in_session

    _reactivate_loan_item(session, ctx, loan.id)
    create_loan_schedule_item_in_session(
        session, ctx, loan.id, plan.category_id, DEFAULT_RECURRENCE_HORIZON_MONTHS
    )


def archive_installment_plan(factory: sessionmaker[Session], ctx: Context, loan_id: UUID) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        loan = LoanRepository(session, ctx).get_or_raise(loan_id)
        if loan.kind != LoanKind.INSTALLMENT_PLAN.value or loan.archived_at is not None:
            return
        plan = InstallmentPlanRepository(session, ctx).for_loan(loan.id)
        if plan is None:
            raise NotFound
        now = ctx.clock.now()
        LoanRepository(session, ctx).update(loan.id, archived_at=now)
        InstallmentPlanRepository(session, ctx).update(plan.id, closed_at=now)
        from assetboard.services.loan_scheduled import disable_loan_schedule_item_in_session

        disable_loan_schedule_item_in_session(session, ctx, loan.id)
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="installment_plan.archived",
            entity_type="loan",
            entity_id=str(loan.id),
            ts=now,
            data={"plan_id": str(plan.id)},
        )

    _write(factory, write)


def restore_installment_plan(factory: sessionmaker[Session], ctx: Context, loan_id: UUID) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        loan = LoanRepository(session, ctx).get_or_raise(loan_id)
        plan = InstallmentPlanRepository(session, ctx).for_loan(loan.id)
        if loan.kind != LoanKind.INSTALLMENT_PLAN.value or plan is None:
            raise NotFound
        if plan.remaining_count <= 0:
            raise loans.LoanError("remaining_count", "installment_plan_closed")
        today = ctx.clock.today(ctx.workspace.timezone)
        next_due = next_due_on_or_after(today, loan.payment_day)
        end_date = _end_date(next_due, plan.remaining_count, loan.payment_day)
        LoanRepository(session, ctx).update(
            loan.id,
            archived_at=None,
            first_payment_date=next_due,
            term_months=plan.remaining_count,
            principal_original_minor=plan.installment_minor * plan.remaining_count,
        )
        InstallmentPlanRepository(session, ctx).update(plan.id, closed_at=None, end_date=end_date)
        from assetboard.services.loan_scheduled import create_loan_schedule_item_in_session

        _reactivate_loan_item(session, ctx, loan.id)
        create_loan_schedule_item_in_session(
            session,
            ctx,
            loan.id,
            plan.category_id,
            DEFAULT_RECURRENCE_HORIZON_MONTHS,
        )
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="installment_plan.restored",
            entity_type="loan",
            entity_id=str(loan.id),
            ts=ctx.clock.now(),
            data={"plan_id": str(plan.id)},
        )

    _write(factory, write)


__all__ = [
    "InstallmentPlanView",
    "InstallmentPlansPage",
    "apply_posted_occurrence",
    "archive_installment_plan",
    "create_installment_plan",
    "get_installment_plan",
    "list_installment_plans",
    "next_due_after",
    "next_due_on_or_after",
    "plan_due_dates_in_session",
    "plan_installment_in_session",
    "plan_remaining_minor_at_in_session",
    "revert_posted_occurrence",
    "post_next_installment",
    "restore_installment_plan",
    "update_installment_plan",
]
