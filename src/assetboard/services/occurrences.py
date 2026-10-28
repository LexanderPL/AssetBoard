"""Upcoming occurrences and their posting workflow (F3.2)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.clock import Clock
from assetboard.db.engine import run_immediate
from assetboard.db.models.scheduled import ScheduledItem, ScheduledOccurrence
from assetboard.db.repositories import (
    AuditEventRepository,
    CategoryRepository,
    CurrentWorkspaceRepository,
    MonthStateRepository,
    NotFound,
    ScheduledItemRepository,
    ScheduledOccurrenceRepository,
    TransactionRepository,
)
from assetboard.db.repositories.base import WorkspaceScope
from assetboard.domain.money import Money
from assetboard.domain.months import MAX_YEAR, MIN_YEAR, YearMonth
from assetboard.domain.recurrence import Frequency
from assetboard.domain.scheduled import AmountState, OccurrenceStatus
from assetboard.domain.transactions import (
    MAX_AMOUNT_MINOR,
    UNDO_WINDOW,
    Source,
    clean_description,
    description_key,
)
from assetboard.services.context import Context, Role, require_role, require_role_now
from assetboard.services.months import ensure_date_open

logger = logging.getLogger(__name__)


class OccurrenceError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class UndoExpired(OccurrenceError):
    def __init__(self) -> None:
        super().__init__("undo_expired")


@dataclass(frozen=True, slots=True)
class UpcomingOccurrence:
    id: UUID
    scheduled_item_id: UUID | None
    income_source_id: UUID | None
    label: str
    direction: str
    category_id: UUID
    category_name: str
    nominal_date: date
    due_date: date
    expected: Money
    amount_state: str
    auto_post: bool
    one_time: bool
    rescheduled: bool


@dataclass(frozen=True, slots=True)
class UpcomingWeek:
    monday: date
    occurrences: tuple[UpcomingOccurrence, ...]


@dataclass(frozen=True, slots=True)
class PostedTransaction:
    occurrence_id: UUID
    transaction_id: UUID
    created_at: datetime


def _upcoming_view(
    occurrence: ScheduledOccurrence, item: ScheduledItem | None, category_name: str
) -> UpcomingOccurrence:
    return UpcomingOccurrence(
        id=occurrence.id,
        scheduled_item_id=item.id if item is not None else None,
        income_source_id=occurrence.income_source_id,
        label=occurrence.label,
        direction=occurrence.direction,
        category_id=occurrence.category_id,
        category_name=category_name,
        nominal_date=occurrence.nominal_date,
        due_date=occurrence.due_date,
        expected=Money(occurrence.expected_minor, occurrence.currency),
        amount_state=occurrence.amount_state,
        auto_post=item.auto_post if item is not None else False,
        one_time=item.freq == Frequency.ONCE if item is not None else False,
        rescheduled=occurrence.rescheduled_at is not None,
    )


def upcoming(
    factory: sessionmaker[Session],
    ctx: Context,
    *,
    days: int,
    direction: str | None = None,
    category_id: UUID | None = None,
) -> list[UpcomingWeek]:
    require_role(ctx, Role.VIEWER)
    if days < 1:
        raise ValueError("invalid_days")
    today = ctx.clock.today(ctx.workspace.timezone)
    through = min(today + timedelta(days=days), date(MAX_YEAR, 12, 31))
    grouped: dict[date, list[UpcomingOccurrence]] = {}
    with factory() as session:
        rows = ScheduledOccurrenceRepository(session, ctx).upcoming(
            through, direction=direction, category_id=category_id
        )
        for occurrence, item, category_name in rows:
            monday = occurrence.due_date - timedelta(days=occurrence.due_date.weekday())
            grouped.setdefault(monday, []).append(_upcoming_view(occurrence, item, category_name))
    return [UpcomingWeek(monday, tuple(grouped[monday])) for monday in sorted(grouped)]


def pending_for_month(
    factory: sessionmaker[Session], ctx: Context, month: YearMonth
) -> tuple[UpcomingOccurrence, ...]:
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        rows = ScheduledOccurrenceRepository(session, ctx).pending_for_month(
            month.first_day, month.last_day
        )
    return tuple(_upcoming_view(occurrence, item, name) for occurrence, item, name in rows)


def recently_posted(
    factory: sessionmaker[Session], ctx: Context, occurrence_id: UUID
) -> PostedTransaction | None:
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        occurrence = ScheduledOccurrenceRepository(session, ctx).get(occurrence_id)
        if (
            occurrence is None
            or occurrence.status != OccurrenceStatus.POSTED
            or occurrence.transaction_id is None
        ):
            return None
        transaction = TransactionRepository(session, ctx).get(occurrence.transaction_id)
        if (
            transaction is None
            or transaction.deleted_at is not None
            or transaction.source != Source.RECURRING
            or ctx.clock.now() - transaction.created_at > UNDO_WINDOW
        ):
            return None
        return PostedTransaction(occurrence.id, transaction.id, transaction.created_at)


def _ensure_open(session: Session, scope: WorkspaceScope, day: date) -> None:
    month = YearMonth.from_date(day)
    state = MonthStateRepository(session, scope).for_month(month)
    if state is not None and state.closed_at is not None:
        raise OccurrenceError("month_closed")


def _valid_day(day: date) -> None:
    if not MIN_YEAR <= day.year <= MAX_YEAR:
        raise OccurrenceError("invalid_date")


def _post_in_session(
    session: Session,
    scope: WorkspaceScope,
    clock: Clock,
    occurrence_id: UUID,
    *,
    amount_minor: int,
    day: date,
    source: Source,
    actor_user_id: UUID | None,
) -> UUID:
    repository = ScheduledOccurrenceRepository(session, scope)
    occurrence = repository.get_for_update(occurrence_id)
    if occurrence is None:
        raise NotFound
    if occurrence.status == OccurrenceStatus.POSTED and occurrence.transaction_id is not None:
        return occurrence.transaction_id
    if occurrence.status != OccurrenceStatus.PENDING:
        raise OccurrenceError("not_pending")
    if not 0 < amount_minor <= MAX_AMOUNT_MINOR:
        raise OccurrenceError("invalid_amount")
    _valid_day(day)
    _ensure_open(session, scope, occurrence.due_date)
    _ensure_open(session, scope, day)
    item = (
        ScheduledItemRepository(session, scope).get_or_raise(occurrence.scheduled_item_id)
        if occurrence.scheduled_item_id is not None
        else None
    )
    category = CategoryRepository(session, scope).get_or_raise(occurrence.category_id)
    if category.archived_at is not None or category.kind != occurrence.direction:
        raise OccurrenceError("category_unavailable")
    description = clean_description(occurrence.label or (item.name if item else ""))
    transaction = TransactionRepository(session, scope).create(
        direction=occurrence.direction,
        day=day,
        amount_minor=amount_minor,
        currency=occurrence.currency,
        category_id=occurrence.category_id,
        description=description,
        description_key=description_key(description),
        source=source.value,
        created_by_user_id=actor_user_id,
    )
    repository.update(
        occurrence.id,
        status=OccurrenceStatus.POSTED,
        transaction_id=transaction.id,
    )
    now = clock.now()
    audit = AuditEventRepository(session, scope)
    audit.record(
        actor_user_id=actor_user_id,
        actor_type="system" if actor_user_id is None else "user",
        action="transaction.created",
        entity_type="transaction",
        entity_id=str(transaction.id),
        ts=now,
        data={
            "date": transaction.date.isoformat(),
            "direction": transaction.direction,
            "category": category.name,
            "amount": str(Money(transaction.amount_minor, transaction.currency).to_decimal()),
            "currency": transaction.currency,
            "source": source.value,
        },
    )
    audit.record(
        actor_user_id=actor_user_id,
        actor_type="system" if actor_user_id is None else "user",
        action="scheduled_occurrence.posted",
        entity_type="scheduled_occurrence",
        entity_id=str(occurrence.id),
        ts=now,
        data={"transaction_id": str(transaction.id), "source": source.value},
    )
    return transaction.id


def post_occurrence(
    factory: sessionmaker[Session],
    ctx: Context,
    occurrence_id: UUID,
    *,
    amount_minor: int,
    day: date,
) -> UUID:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> UUID:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=False)
        return _post_in_session(
            session,
            ctx,
            ctx.clock,
            occurrence_id,
            amount_minor=amount_minor,
            day=day,
            source=Source.RECURRING,
            actor_user_id=ctx.user.id,
        )

    return run_immediate(factory, write)


def skip_occurrence(factory: sessionmaker[Session], ctx: Context, occurrence_id: UUID) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=False)
        repository = ScheduledOccurrenceRepository(session, ctx)
        occurrence = repository.get_for_update(occurrence_id)
        if occurrence is None:
            raise NotFound
        if occurrence.status == OccurrenceStatus.SKIPPED:
            return
        if occurrence.status != OccurrenceStatus.PENDING:
            raise OccurrenceError("not_pending")
        _ensure_open(session, ctx, occurrence.due_date)
        for child in repository.children(occurrence.id):
            if child.status == OccurrenceStatus.PENDING:
                repository.delete(child)
        repository.update(occurrence.id, status=OccurrenceStatus.SKIPPED)
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="scheduled_occurrence.skipped",
            entity_type="scheduled_occurrence",
            entity_id=str(occurrence.id),
            ts=ctx.clock.now(),
        )

    run_immediate(factory, write)


def reschedule_occurrence(
    factory: sessionmaker[Session], ctx: Context, occurrence_id: UUID, day: date
) -> None:
    require_role(ctx, Role.EDITOR)
    _valid_day(day)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=False)
        repository = ScheduledOccurrenceRepository(session, ctx)
        occurrence = repository.get_for_update(occurrence_id)
        if occurrence is None:
            raise NotFound
        if occurrence.status != OccurrenceStatus.PENDING:
            raise OccurrenceError("not_pending")
        _ensure_open(session, ctx, day)
        previous_date = occurrence.due_date
        repository.update(
            occurrence.id,
            due_date=day,
            rescheduled_at=ctx.clock.now(),
        )
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="scheduled_occurrence.rescheduled",
            entity_type="scheduled_occurrence",
            entity_id=str(occurrence.id),
            ts=ctx.clock.now(),
            data={"previous_date": previous_date.isoformat(), "due_date": day.isoformat()},
        )

    run_immediate(factory, write)


def edit_occurrence_amount(
    factory: sessionmaker[Session],
    ctx: Context,
    occurrence_id: UUID,
    amount_minor: int,
) -> None:
    require_role(ctx, Role.EDITOR)
    if not 0 < amount_minor <= MAX_AMOUNT_MINOR:
        raise OccurrenceError("invalid_amount")

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=False)
        repository = ScheduledOccurrenceRepository(session, ctx)
        occurrence = repository.get_for_update(occurrence_id)
        if occurrence is None:
            raise NotFound
        if occurrence.status != OccurrenceStatus.PENDING:
            raise OccurrenceError("not_pending")
        _ensure_open(session, ctx, occurrence.due_date)
        previous_minor = occurrence.expected_minor
        repository.update(
            occurrence.id,
            expected_minor=amount_minor,
            amount_state=AmountState.CONFIRMED,
            amount_overridden=True,
        )
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="scheduled_occurrence.amount_edited",
            entity_type="scheduled_occurrence",
            entity_id=str(occurrence.id),
            ts=ctx.clock.now(),
            data={
                "amount": str(Money(amount_minor, occurrence.currency).to_decimal()),
                "previous_amount": str(Money(previous_minor, occurrence.currency).to_decimal()),
                "currency": occurrence.currency,
            },
        )

    run_immediate(factory, write)


def undo_post(factory: sessionmaker[Session], ctx: Context, occurrence_id: UUID) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=False)
        occurrence_repository = ScheduledOccurrenceRepository(session, ctx)
        occurrence = occurrence_repository.get_for_update(occurrence_id)
        if occurrence is None:
            raise NotFound
        if occurrence.status != OccurrenceStatus.POSTED or occurrence.transaction_id is None:
            raise OccurrenceError("not_posted")
        transactions = TransactionRepository(session, ctx)
        transaction = transactions.get_for_update(occurrence.transaction_id)
        if transaction is None or transaction.deleted_at is not None:
            raise OccurrenceError("not_posted")
        if transaction.source != Source.RECURRING:
            raise OccurrenceError("not_posted")
        if ctx.clock.now() - transaction.created_at > UNDO_WINDOW:
            raise UndoExpired
        _ensure_open(session, ctx, transaction.date)
        _ensure_open(session, ctx, occurrence.due_date)
        category = CategoryRepository(session, ctx).get_or_raise(transaction.category_id)
        transactions.update(transaction.id, deleted_at=ctx.clock.now())
        occurrence_repository.update(
            occurrence.id,
            status=OccurrenceStatus.PENDING,
            transaction_id=None,
            previous_transaction_id=transaction.id,
        )
        audit = AuditEventRepository(session, ctx)
        audit.record(
            actor_user_id=ctx.user.id,
            action="transaction.deleted",
            entity_type="transaction",
            entity_id=str(transaction.id),
            ts=ctx.clock.now(),
            data={
                "date": transaction.date.isoformat(),
                "direction": transaction.direction,
                "category": category.name,
                "amount": str(Money(transaction.amount_minor, transaction.currency).to_decimal()),
                "currency": transaction.currency,
                "source": transaction.source,
            },
        )
        audit.record(
            actor_user_id=ctx.user.id,
            action="scheduled_occurrence.post_undone",
            entity_type="scheduled_occurrence",
            entity_id=str(occurrence.id),
            ts=ctx.clock.now(),
            data={"transaction_id": str(transaction.id)},
        )

    run_immediate(factory, write)


def unlink_deleted_transaction(session: Session, ctx: Context, transaction_id: UUID) -> None:
    """Return the occurrence posted as ``transaction_id`` to pending; the ledger soft-deletes it.

    Runs inside the ledger delete's write transaction, after its role check and month lock and
    before the row changes. The occurrence keeps its nominal and due date and any amount
    override, and remembers the transaction (``previous_transaction_id``) so auto-post never
    posts it again and a restore can re-link it. Its due month must be open (``MonthClosed``),
    as for an undo of "mark paid".
    """
    repository = ScheduledOccurrenceRepository(session, ctx)
    for occurrence in repository.linked_to_transaction_for_update(transaction_id):
        ensure_date_open(session, ctx, occurrence.due_date)
        repository.update(
            occurrence.id,
            status=OccurrenceStatus.PENDING,
            transaction_id=None,
            previous_transaction_id=transaction_id,
        )
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="scheduled_occurrence.unposted",
            entity_type="scheduled_occurrence",
            entity_id=str(occurrence.id),
            ts=ctx.clock.now(),
            data={"transaction_id": str(transaction_id)},
        )


def relink_restored_transaction(session: Session, ctx: Context, transaction_id: UUID) -> None:
    """Re-link a restored transaction to the occurrence it was posted from.

    Only when that occurrence is still pending, unlinked and in an open month; otherwise the
    restored row stays a plain transaction. Runs inside the ledger restore's write transaction.
    """
    repository = ScheduledOccurrenceRepository(session, ctx)
    for occurrence in repository.linked_to_transaction_for_update(transaction_id, previous=True):
        if occurrence.status != OccurrenceStatus.PENDING or occurrence.transaction_id is not None:
            continue
        state = MonthStateRepository(session, ctx).for_month(
            YearMonth.from_date(occurrence.due_date)
        )
        if state is not None and state.closed_at is not None:
            continue
        repository.update(
            occurrence.id, status=OccurrenceStatus.POSTED, transaction_id=transaction_id
        )
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="scheduled_occurrence.relinked",
            entity_type="scheduled_occurrence",
            entity_id=str(occurrence.id),
            ts=ctx.clock.now(),
            data={"transaction_id": str(transaction_id)},
        )
        return


def auto_post_workspace(factory: sessionmaker[Session], scope: WorkspaceScope, clock: Clock) -> int:
    """Post due auto-post items in one workspace; safe to rerun after a partial failure."""

    def write(session: Session) -> int:
        CurrentWorkspaceRepository(session, scope).lock(exclusive=False)
        workspace = CurrentWorkspaceRepository(session, scope).get()
        today = clock.today(workspace.timezone)
        occurrences = ScheduledOccurrenceRepository(session, scope)
        candidates = occurrences.upcoming(today)
        posted = 0
        for occurrence, item, _category_name in candidates:
            if (
                item is None
                or not item.auto_post
                or item.paused_at is not None
                or item.archived_at is not None
            ):
                continue
            if occurrence.previous_transaction_id is not None:
                # Posted before and returned to pending (undo or ledger delete): the user
                # chose to take it off the ledger, so only they post it again.
                continue
            state = MonthStateRepository(session, scope).for_month(
                YearMonth.from_date(occurrence.due_date)
            )
            if state is not None and state.closed_at is not None:
                continue
            try:
                with session.begin_nested():
                    _post_in_session(
                        session,
                        scope,
                        clock,
                        occurrence.id,
                        amount_minor=occurrence.expected_minor,
                        day=occurrence.due_date,
                        source=Source.RECURRING_AUTO,
                        actor_user_id=None,
                    )
                    posted += 1
            except Exception as exc:
                logger.error(
                    "recurring_auto_post_failed workspace=%s occurrence=%s exception_type=%s",
                    scope.workspace.id,
                    occurrence.id,
                    type(exc).__name__,
                    exc_info=True,
                )
        return posted

    return run_immediate(factory, write)
