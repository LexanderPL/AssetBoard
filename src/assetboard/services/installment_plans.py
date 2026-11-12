"""Installment plans (F7.7): fixed-count liabilities without interest."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.config import DEFAULT_RECURRENCE_HORIZON_MONTHS
from assetboard.db.engine import run_immediate
from assetboard.db.models.ledger import Category
from assetboard.db.models.loans import InstallmentPlan, Loan
from assetboard.db.repositories import (
    AuditEventRepository,
    CategoryRepository,
    CurrentWorkspaceRepository,
    InstallmentPlanRepository,
    LoanRepository,
    NotFound,
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


def plan_remaining_minor_in_session(
    session: Session, ctx: WorkspaceScope, loan_id: UUID
) -> int | None:
    plan = InstallmentPlanRepository(session, ctx).for_loan(loan_id)
    if plan is None:
        return None
    loan = LoanRepository(session, ctx).get(loan_id)
    if loan is None or plan.closed_at is not None or loan.archived_at is not None:
        return 0
    return plan.installment_minor * plan.remaining_count


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


def apply_posted_occurrence(session: Session, ctx: Context, loan: Loan, posted_on: date) -> None:
    """Decrement a plan after one occurrence is posted; idempotent by the posted row."""
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
        closed_at=ctx.clock.now() if count == 0 else None,
    )
    LoanRepository(session, ctx).update(
        loan.id,
        term_months=count,
        principal_original_minor=plan.installment_minor * count if count else None,
        first_payment_date=next_due,
        archived_at=ctx.clock.now() if count == 0 else None,
    )
    AuditEventRepository(session, ctx).record(
        actor_user_id=ctx.user.id,
        action="installment_plan.payment_posted",
        entity_type="loan",
        entity_id=str(loan.id),
        ts=ctx.clock.now(),
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
    "plan_remaining_minor_in_session",
    "restore_installment_plan",
    "update_installment_plan",
]
