"""Actual loan payments and balance checkpoints (F7.5).

Payments are user records in the loan currency. A payment entered as one total is split against
the projected schedule row by the pure domain helper; an explicit breakdown is accepted instead.
Checkpoints re-anchor the schedule (F7.3) and show the variance against a projection from the
previous anchor. Writes lock the workspace and re-check the role; audit data never contains
amounts.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.engine import run_immediate
from assetboard.db.models.ledger import Transaction
from assetboard.db.models.loans import Loan, LoanBalanceCheckpoint, LoanPayment
from assetboard.db.repositories import (
    AuditEventRepository,
    CurrentWorkspaceRepository,
    LoanBalanceCheckpointRepository,
    LoanPaymentRepository,
    LoanRepository,
    NotFound,
    TransactionRepository,
)
from assetboard.domain.loans import (
    MAX_AMOUNT_MINOR,
    LoanPaymentKind,
    LoanPaymentSource,
    LoanPaymentSplit,
    LoanValidationError,
    Money,
    ScheduleAnchor,
    ScheduleError,
    clean_note,
    split_loan_payment,
    valid_date,
    validate_payment_breakdown,
)
from assetboard.services import loan_schedule, loans
from assetboard.services.context import Context, Role, require_role, require_role_now


@dataclass(frozen=True, slots=True)
class LoanPaymentView:
    id: UUID
    date: date
    kind: LoanPaymentKind
    total: Money
    principal: Money
    interest: Money
    costs: Money
    overpayment: Money
    note: str
    source: LoanPaymentSource
    transaction_id: UUID | None
    transaction_description: str | None
    transaction_date: date | None
    transaction_deleted: bool


@dataclass(frozen=True, slots=True)
class TransactionOption:
    id: UUID
    date: date
    amount: Money
    description: str


@dataclass(frozen=True, slots=True)
class CheckpointDetailView:
    id: UUID
    as_of: date
    remaining_principal: Money
    note: str
    starting: bool
    projected: Money | None
    variance: Money | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class LoanPaymentsPage:
    loan: loans.LoanView
    payments: tuple[LoanPaymentView, ...]
    checkpoints: tuple[CheckpointDetailView, ...]
    transactions: tuple[TransactionOption, ...]


def _write[T](factory: sessionmaker[Session], operation: Callable[[Session], T]) -> T:
    return run_immediate(factory, operation)


def _locked_currency(session: Session, ctx: Context) -> str:
    repository = CurrentWorkspaceRepository(session, ctx)
    repository.lock(exclusive=True)
    return repository.get().base_currency


def _audit(
    session: Session,
    ctx: Context,
    action: str,
    loan_id: UUID,
    data: dict[str, str | int | bool],
) -> None:
    AuditEventRepository(session, ctx).record(
        actor_user_id=ctx.user.id,
        action=action,
        entity_type="loan",
        entity_id=str(loan_id),
        ts=ctx.clock.now(),
        data=data,
    )


def _active_loan(session: Session, ctx: Context, loan_id: UUID) -> Loan:
    loan = LoanRepository(session, ctx).get_or_raise(loan_id)
    if loan.archived_at is not None:
        raise loans.LoanError("loan", "archived")
    return loan


def _require_money(value: Money, currency: str, field: str) -> int:
    if value.currency != currency:
        raise loans.LoanError(field, "invalid_currency")
    return value.amount_minor


def _payment_view(
    row: LoanPayment, currency: str, transaction: Transaction | None
) -> LoanPaymentView:
    return LoanPaymentView(
        id=row.id,
        date=row.date,
        kind=LoanPaymentKind(row.kind),
        total=Money(row.total_minor, currency),
        principal=Money(row.principal_minor, currency),
        interest=Money(row.interest_minor, currency),
        costs=Money(row.costs_minor, currency),
        overpayment=Money(row.overpayment_minor, currency),
        note=row.note,
        source=LoanPaymentSource(row.source),
        transaction_id=row.transaction_id,
        transaction_description=transaction.description if transaction is not None else None,
        transaction_date=transaction.date if transaction is not None else None,
        transaction_deleted=transaction.deleted_at is not None
        if transaction is not None
        else False,
    )


def list_loan_payments(
    factory: sessionmaker[Session], ctx: Context, loan_id: UUID
) -> tuple[LoanPaymentView, ...]:
    """Payments of one loan, newest first; viewers may read them."""
    require_role(ctx, Role.VIEWER)
    view = loans.get_loan(factory, ctx, loan_id)
    with factory() as session:
        rows = LoanPaymentRepository(session, ctx).for_loan(loan_id)
        repository = TransactionRepository(session, ctx)
        transaction_ids = {row.transaction_id for row in rows if row.transaction_id}
        by_id = {transaction.id: transaction for transaction in repository.by_ids(transaction_ids)}
        return tuple(
            _payment_view(
                row,
                view.currency,
                by_id.get(row.transaction_id) if row.transaction_id is not None else None,
            )
            for row in rows
        )


def _transaction_option(row: Transaction) -> TransactionOption:
    return TransactionOption(
        id=row.id,
        date=row.date,
        amount=Money(row.amount_minor, row.currency),
        description=row.description,
    )


def list_payment_transactions(
    factory: sessionmaker[Session], ctx: Context, loan_id: UUID
) -> tuple[TransactionOption, ...]:
    """Recent expense rows in the loan currency for the optional link select."""
    require_role(ctx, Role.VIEWER)
    view = loans.get_loan(factory, ctx, loan_id)
    with factory() as session:
        rows = TransactionRepository(session, ctx).recent_expenses(limit=50, currency=view.currency)
        return tuple(_transaction_option(row) for row in rows)


def _split_from_schedule(
    factory: sessionmaker[Session],
    ctx: Context,
    loan_id: UUID,
    day: date,
    kind: LoanPaymentKind,
    total: Money,
) -> LoanPaymentSplit:
    if kind is LoanPaymentKind.OVERPAYMENT:
        return split_loan_payment(total, kind, None)
    try:
        inputs = loan_schedule.load_loan_schedule_inputs(factory, ctx, loan_id)
        anchor = loan_schedule.anchor_for_date(inputs, day)
        schedule = loan_schedule.schedule_from_anchor(inputs, anchor, day)
    except (ScheduleError, LoanValidationError) as exc:
        raise loans.LoanError("date", exc.code) from None
    row = next((item for item in schedule.rows if item.date == day), None)
    if row is None:
        row = next((item for item in schedule.rows if item.date >= day), None)
    if row is None:
        raise loans.LoanError("date", "no_scheduled_payment")
    return split_loan_payment(total, kind, row)


def _explicit_split(
    total: Money,
    principal: Money | None,
    interest: Money | None,
    costs: Money | None,
    overpayment: Money | None,
) -> LoanPaymentSplit:
    values = (principal, interest, costs, overpayment)
    if any(value is None for value in values):
        raise loans.LoanError("total", "payment_breakdown_missing")
    return validate_payment_breakdown(
        total,
        principal,  # type: ignore[arg-type]
        interest,  # type: ignore[arg-type]
        costs,  # type: ignore[arg-type]
        overpayment,  # type: ignore[arg-type]
    )


def _validate_transaction(
    session: Session, ctx: Context, loan: Loan, total: Money, transaction_id: UUID | None
) -> None:
    if transaction_id is None:
        return
    transaction = TransactionRepository(session, ctx).get(transaction_id)
    if transaction is None:
        raise loans.LoanError("transaction_id", "transaction_not_found")
    if transaction.deleted_at is not None:
        raise loans.LoanError("transaction_id", "transaction_deleted")
    if transaction.direction != "expense":
        raise loans.LoanError("transaction_id", "transaction_not_expense")
    if transaction.currency != loan.currency:
        raise loans.LoanError("transaction_id", "transaction_currency")
    if transaction.amount_minor != total.amount_minor:
        raise loans.LoanError("transaction_id", "transaction_amount")


def _kind(value: LoanPaymentKind | str) -> LoanPaymentKind:
    try:
        return LoanPaymentKind(value)
    except ValueError:
        raise loans.LoanError("kind", "invalid_payment_kind") from None


def _total(total: Money, currency: str) -> Money:
    amount = _require_money(total, currency, "total")
    if amount <= 0 or amount > MAX_AMOUNT_MINOR:
        raise loans.LoanError("total", "invalid_payment_total")
    return total


def create_loan_payment(
    factory: sessionmaker[Session],
    ctx: Context,
    loan_id: UUID,
    *,
    date_value: date,
    kind: LoanPaymentKind | str,
    total: Money,
    principal: Money | None = None,
    interest: Money | None = None,
    costs: Money | None = None,
    overpayment: Money | None = None,
    note: str = "",
    transaction_id: UUID | None = None,
) -> UUID:
    """Record one actual payment; a single total is split against the projected row."""
    require_role(ctx, Role.EDITOR)
    view = loans.get_loan(factory, ctx, loan_id)
    valid_date(date_value, "date")
    wanted_kind = _kind(kind)
    wanted_total = _total(total, view.currency)

    def write(session: Session) -> UUID:
        require_role_now(session, ctx, Role.EDITOR)
        _locked_currency(session, ctx)
        loan = _active_loan(session, ctx, loan_id)
        if wanted_kind is LoanPaymentKind.OVERPAYMENT:
            split = split_loan_payment(wanted_total, wanted_kind, None)
        elif any(value is not None for value in (principal, interest, costs, overpayment)):
            split = _explicit_split(wanted_total, principal, interest, costs, overpayment)
        else:
            split = _split_from_schedule(
                factory, ctx, loan_id, date_value, wanted_kind, wanted_total
            )
        _validate_transaction(session, ctx, loan, wanted_total, transaction_id)
        payment = LoanPaymentRepository(session, ctx).create(
            loan_id=loan.id,
            date=date_value,
            kind=wanted_kind.value,
            total_minor=wanted_total.amount_minor,
            principal_minor=split.principal.amount_minor,
            interest_minor=split.interest.amount_minor,
            costs_minor=split.costs.amount_minor,
            overpayment_minor=split.overpayment.amount_minor,
            note=clean_note(note),
            source=LoanPaymentSource.MANUAL.value,
            transaction_id=transaction_id,
        )
        _audit(
            session,
            ctx,
            "loan_payment.created",
            loan.id,
            {
                "payment_id": str(payment.id),
                "kind": wanted_kind.value,
                "date": date_value.isoformat(),
            },
        )
        return payment.id

    return _write(factory, write)


def get_loan_payment(
    factory: sessionmaker[Session], ctx: Context, loan_id: UUID, payment_id: UUID
) -> LoanPaymentView:
    require_role(ctx, Role.VIEWER)
    view = loans.get_loan(factory, ctx, loan_id)
    with factory() as session:
        row = LoanPaymentRepository(session, ctx).get_for_loan(loan_id, payment_id)
        if row is None:
            raise NotFound
        transaction = (
            TransactionRepository(session, ctx).get(row.transaction_id)
            if row.transaction_id is not None
            else None
        )
        return _payment_view(row, view.currency, transaction)


def update_loan_payment(
    factory: sessionmaker[Session],
    ctx: Context,
    loan_id: UUID,
    payment_id: UUID,
    *,
    date_value: date,
    kind: LoanPaymentKind | str,
    total: Money,
    principal: Money | None = None,
    interest: Money | None = None,
    costs: Money | None = None,
    overpayment: Money | None = None,
    note: str = "",
    transaction_id: UUID | None = None,
) -> None:
    """Edit a payment; re-split when the total or kind changes without an explicit breakdown."""
    require_role(ctx, Role.EDITOR)
    view = loans.get_loan(factory, ctx, loan_id)
    valid_date(date_value, "date")
    wanted_kind = _kind(kind)
    wanted_total = _total(total, view.currency)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        _locked_currency(session, ctx)
        loan = _active_loan(session, ctx, loan_id)
        repository = LoanPaymentRepository(session, ctx)
        row = repository.get_for_loan(loan.id, payment_id)
        if row is None:
            raise NotFound
        if wanted_kind is LoanPaymentKind.OVERPAYMENT:
            split = split_loan_payment(wanted_total, wanted_kind, None)
        elif any(value is not None for value in (principal, interest, costs, overpayment)):
            split = _explicit_split(wanted_total, principal, interest, costs, overpayment)
        elif wanted_total.amount_minor != row.total_minor or wanted_kind.value != row.kind:
            split = _split_from_schedule(
                factory, ctx, loan_id, date_value, wanted_kind, wanted_total
            )
        else:
            split = LoanPaymentSplit(
                principal=Money(row.principal_minor, loan.currency),
                interest=Money(row.interest_minor, loan.currency),
                costs=Money(row.costs_minor, loan.currency),
                overpayment=Money(row.overpayment_minor, loan.currency),
            )
        _validate_transaction(session, ctx, loan, wanted_total, transaction_id)
        repository.update(
            row.id,
            date=date_value,
            kind=wanted_kind.value,
            total_minor=wanted_total.amount_minor,
            principal_minor=split.principal.amount_minor,
            interest_minor=split.interest.amount_minor,
            costs_minor=split.costs.amount_minor,
            overpayment_minor=split.overpayment.amount_minor,
            note=clean_note(note),
            transaction_id=transaction_id,
        )
        _audit(
            session,
            ctx,
            "loan_payment.updated",
            loan.id,
            {"payment_id": str(row.id), "kind": wanted_kind.value, "date": date_value.isoformat()},
        )

    _write(factory, write)


def delete_loan_payment(
    factory: sessionmaker[Session], ctx: Context, loan_id: UUID, payment_id: UUID
) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        _locked_currency(session, ctx)
        loan = _active_loan(session, ctx, loan_id)
        repository = LoanPaymentRepository(session, ctx)
        row = repository.get_for_loan(loan.id, payment_id)
        if row is None:
            raise NotFound
        repository.delete(row)
        _audit(
            session,
            ctx,
            "loan_payment.deleted",
            loan.id,
            {"payment_id": str(row.id), "kind": row.kind, "date": row.date.isoformat()},
        )

    _write(factory, write)


def _checkpoint_detail(
    row: LoanBalanceCheckpoint,
    checkpoints: list[LoanBalanceCheckpoint],
    view: loans.LoanView,
    inputs: loan_schedule.LoanScheduleInputs | None,
    load_error: str | None,
) -> CheckpointDetailView:
    previous = max(
        (item for item in checkpoints if item.as_of < row.as_of),
        key=lambda item: (item.as_of, item.id),
        default=None,
    )
    anchor: ScheduleAnchor | None = None
    if previous is not None:
        anchor = ScheduleAnchor(
            date=previous.as_of,
            balance=Money(previous.remaining_principal_minor, view.currency),
        )
    elif view.origination_date is not None and view.principal_original is not None:
        anchor = ScheduleAnchor(date=view.origination_date, balance=view.principal_original)
    if anchor is None:
        return CheckpointDetailView(
            id=row.id,
            as_of=row.as_of,
            remaining_principal=Money(row.remaining_principal_minor, view.currency),
            note=row.note,
            starting=True,
            projected=None,
            variance=None,
            error_code=None,
        )
    if inputs is None:
        return CheckpointDetailView(
            id=row.id,
            as_of=row.as_of,
            remaining_principal=Money(row.remaining_principal_minor, view.currency),
            note=row.note,
            starting=False,
            projected=None,
            variance=None,
            error_code=load_error or "schedule_unavailable",
        )
    try:
        schedule = loan_schedule.schedule_from_anchor(inputs, anchor, row.as_of)
    except (ScheduleError, LoanValidationError) as exc:
        return CheckpointDetailView(
            id=row.id,
            as_of=row.as_of,
            remaining_principal=Money(row.remaining_principal_minor, view.currency),
            note=row.note,
            starting=False,
            projected=None,
            variance=None,
            error_code=exc.code,
        )
    passed = [item for item in schedule.rows if item.date <= row.as_of]
    projected = passed[-1].closing if passed else anchor.balance
    balance = Money(row.remaining_principal_minor, view.currency)
    return CheckpointDetailView(
        id=row.id,
        as_of=row.as_of,
        remaining_principal=balance,
        note=row.note,
        starting=False,
        projected=projected,
        variance=Money(balance.amount_minor - projected.amount_minor, view.currency),
        error_code=None,
    )


def list_loan_checkpoints(
    factory: sessionmaker[Session], ctx: Context, loan_id: UUID
) -> tuple[CheckpointDetailView, ...]:
    """Checkpoints with their projected balance and signed variance; viewers may read."""
    require_role(ctx, Role.VIEWER)
    view = loans.get_loan(factory, ctx, loan_id)
    with factory() as session:
        rows = LoanBalanceCheckpointRepository(session, ctx).for_loan(loan_id)
    inputs: loan_schedule.LoanScheduleInputs | None = None
    load_error: str | None = None
    try:
        inputs = loan_schedule.load_loan_schedule_inputs(factory, ctx, loan_id)
    except (ScheduleError, LoanValidationError) as exc:
        load_error = exc.code
    return tuple(_checkpoint_detail(row, rows, view, inputs, load_error) for row in rows)


def _validate_checkpoint(
    view: loans.LoanView,
    day: date,
    balance: Money,
    today: date,
    *,
    exclude_id: UUID | None = None,
) -> int:
    valid_date(day, "as_of")
    if day > today:
        raise loans.LoanError("as_of", "checkpoint_future")
    if view.origination_date is not None and day < view.origination_date:
        raise loans.LoanError("as_of", "checkpoint_before_start")
    if view.entry_path == "current":
        earlier = [
            checkpoint.as_of for checkpoint in view.checkpoints if checkpoint.id != exclude_id
        ]
        if earlier and day < min(earlier):
            raise loans.LoanError("as_of", "checkpoint_before_start")
    amount = _require_money(balance, view.currency, "remaining_principal")
    if amount < 0 or amount > MAX_AMOUNT_MINOR:
        raise loans.LoanError("remaining_principal", "invalid_balance")
    return amount


def create_loan_checkpoint(
    factory: sessionmaker[Session],
    ctx: Context,
    loan_id: UUID,
    *,
    as_of: date,
    remaining_principal: Money,
    note: str = "",
) -> UUID:
    require_role(ctx, Role.EDITOR)
    view = loans.get_loan(factory, ctx, loan_id)
    today = ctx.clock.today(ctx.workspace.timezone)
    amount = _validate_checkpoint(view, as_of, remaining_principal, today)

    def write(session: Session) -> UUID:
        require_role_now(session, ctx, Role.EDITOR)
        _locked_currency(session, ctx)
        loan = _active_loan(session, ctx, loan_id)
        repository = LoanBalanceCheckpointRepository(session, ctx)
        if repository.exists_on(loan.id, as_of):
            raise loans.LoanError("as_of", "checkpoint_exists")
        checkpoint = repository.create(
            loan_id=loan.id,
            as_of=as_of,
            remaining_principal_minor=amount,
            note=clean_note(note),
        )
        _audit(
            session,
            ctx,
            "loan_checkpoint.created",
            loan.id,
            {"checkpoint_id": str(checkpoint.id), "as_of": as_of.isoformat()},
        )
        return checkpoint.id

    return _write(factory, write)


def update_loan_checkpoint(
    factory: sessionmaker[Session],
    ctx: Context,
    loan_id: UUID,
    checkpoint_id: UUID,
    *,
    as_of: date,
    remaining_principal: Money,
    note: str = "",
) -> None:
    require_role(ctx, Role.EDITOR)
    view = loans.get_loan(factory, ctx, loan_id)
    today = ctx.clock.today(ctx.workspace.timezone)
    amount = _validate_checkpoint(view, as_of, remaining_principal, today, exclude_id=checkpoint_id)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        _locked_currency(session, ctx)
        loan = _active_loan(session, ctx, loan_id)
        repository = LoanBalanceCheckpointRepository(session, ctx)
        row = repository.get_for_loan(loan.id, checkpoint_id)
        if row is None:
            raise NotFound
        if repository.exists_on(loan.id, as_of, exclude_id=row.id):
            raise loans.LoanError("as_of", "checkpoint_exists")
        repository.update(
            row.id,
            as_of=as_of,
            remaining_principal_minor=amount,
            note=clean_note(note),
        )
        _audit(
            session,
            ctx,
            "loan_checkpoint.updated",
            loan.id,
            {"checkpoint_id": str(row.id), "as_of": as_of.isoformat()},
        )

    _write(factory, write)


def delete_loan_checkpoint(
    factory: sessionmaker[Session], ctx: Context, loan_id: UUID, checkpoint_id: UUID
) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        _locked_currency(session, ctx)
        loan = _active_loan(session, ctx, loan_id)
        repository = LoanBalanceCheckpointRepository(session, ctx)
        row = repository.get_for_loan(loan.id, checkpoint_id)
        if row is None:
            raise NotFound
        rows = repository.for_loan(loan.id)
        earliest = min(rows, key=lambda item: (item.as_of, item.id))
        if loan.origination_date is None and earliest.id == row.id:
            raise loans.LoanError("checkpoint", "starting_checkpoint_required")
        repository.delete(row)
        _audit(
            session,
            ctx,
            "loan_checkpoint.deleted",
            loan.id,
            {"checkpoint_id": str(row.id), "as_of": row.as_of.isoformat()},
        )

    _write(factory, write)


def payments_page(factory: sessionmaker[Session], ctx: Context, loan_id: UUID) -> LoanPaymentsPage:
    require_role(ctx, Role.VIEWER)
    view = loans.get_loan(factory, ctx, loan_id)
    return LoanPaymentsPage(
        loan=view,
        payments=list_loan_payments(factory, ctx, loan_id),
        checkpoints=list_loan_checkpoints(factory, ctx, loan_id),
        transactions=list_payment_transactions(factory, ctx, loan_id),
    )


__all__ = [
    "CheckpointDetailView",
    "LoanPaymentView",
    "LoanPaymentsPage",
    "TransactionOption",
    "create_loan_checkpoint",
    "create_loan_payment",
    "delete_loan_checkpoint",
    "delete_loan_payment",
    "get_loan_payment",
    "list_loan_checkpoints",
    "list_loan_payments",
    "list_payment_transactions",
    "payments_page",
    "update_loan_checkpoint",
    "update_loan_payment",
]
