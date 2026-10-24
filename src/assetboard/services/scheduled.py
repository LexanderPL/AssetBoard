"""Scheduled items (F3.1): rules, planned occurrences and the generator.

**Generation.** ``_generate`` makes the item's pending rows from today onward match the current
rule, holiday calendar and item state: stale pending rows are deleted and missing dates added,
so a country or holiday-data change never duplicates an occurrence. Posted and skipped rows,
past pending rows and every row in a closed month (F2.5) are never touched; nothing is created
in a closed month either. Items that are paused, archived or whose category is archived get no
new rows. Every caller holds the exclusive workspace lock (which also orders writes against
month close/reopen), so generation is serialised per workspace.

**History.** Only posted and skipped rows (and rows kept because their month is closed) are
history. Archiving or deleting removes every other pending row; deleting with no history left
removes the item itself.
"""

from __future__ import annotations

import logging
from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from uuid import UUID

import holidays as holiday_calendars
from sqlalchemy.orm import Session, sessionmaker

from assetboard.clock import Clock
from assetboard.config import Settings
from assetboard.db.engine import run_immediate
from assetboard.db.models.scheduled import ScheduledItem, ScheduledOccurrence
from assetboard.db.repositories.audit import AuditEventRepository
from assetboard.db.repositories.base import WorkspaceScope
from assetboard.db.repositories.category import CategoryRepository
from assetboard.db.repositories.month_state import MonthStateRepository
from assetboard.db.repositories.scheduled import (
    ScheduledItemRepository,
    ScheduledOccurrenceRepository,
)
from assetboard.db.repositories.workspace import CurrentWorkspaceRepository
from assetboard.domain.money import Money
from assetboard.domain.months import MAX_YEAR, YearMonth
from assetboard.domain.recurrence import (
    Frequency,
    MonthDay,
    RecurrenceRule,
    WeekendPolicy,
    occurrences,
)
from assetboard.domain.scheduled import AmountMode, AmountState, ItemAction, OccurrenceStatus
from assetboard.domain.transactions import MAX_AMOUNT_MINOR, TransactionDirection, clean_description
from assetboard.services.category_usage import CategoryUsage
from assetboard.services.context import Context, Role, require_role, require_role_now

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ScheduledInput:
    name: str
    direction: TransactionDirection
    category_id: UUID
    amount: Money
    rule: RecurrenceRule
    amount_mode: AmountMode = AmountMode.FIXED
    notes: str = ""


@dataclass(frozen=True, slots=True)
class ScheduledView:
    id: UUID
    values: ScheduledInput
    paused: bool
    archived: bool


def _rule(item: ScheduledItem) -> RecurrenceRule:
    choice = item.month_day
    return RecurrenceRule(
        start_date=item.start_date,
        end_date=item.end_date,
        max_count=item.max_count,
        freq=Frequency(item.freq),
        interval=item.interval,
        weekdays=tuple(item.weekdays),
        month_day=int(choice) if choice.lstrip("-").isdigit() else MonthDay(choice),
        month=item.month,
        weekend_policy=WeekendPolicy(item.weekend_policy),
    )


def _values(item: ScheduledItem) -> ScheduledInput:
    return ScheduledInput(
        item.name,
        TransactionDirection(item.direction),
        item.category_id,
        Money(item.amount_minor, item.currency),
        _rule(item),
        AmountMode(item.amount_mode),
        item.notes,
    )


def _holidays(
    country: str | None, rule: RecurrenceRule
) -> holiday_calendars.HolidayBase | frozenset[date]:
    if country is None or not rule.uses_working_days:
        return frozenset()
    try:
        return holiday_calendars.country_holidays(country, language="en")
    except NotImplementedError:
        raise ValueError("unsupported_holidays") from None


def holidays_supported(country: str | None) -> bool:
    """Whether working-day rules can be computed for ``country`` (no country: weekends only)."""
    if country is None:
        return True
    try:
        holiday_calendars.country_holidays(country)
    except NotImplementedError:
        return False
    return True


def _horizon(today: date, months: int) -> date:
    year, month = divmod(today.year * 12 + today.month - 1 + months, 12)
    if year > MAX_YEAR:
        return date(MAX_YEAR, 12, 31)
    return date(year, month + 1, min(today.day, monthrange(year, month + 1)[1]))


def preview(factory: sessionmaker[Session], ctx: Context, rule: RecurrenceRule) -> list[date]:
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        workspace = CurrentWorkspaceRepository(session, ctx).get()
        return occurrences(
            rule,
            ctx.clock.today(workspace.timezone),
            date(MAX_YEAR, 12, 31),
            _holidays(workspace.country_code, rule),
            limit=6,
        )


def list_items(factory: sessionmaker[Session], ctx: Context) -> list[ScheduledView]:
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        return [
            ScheduledView(
                row.id, _values(row), row.paused_at is not None, row.archived_at is not None
            )
            for row in ScheduledItemRepository(session, ctx).ordered()
        ]


def get_item(factory: sessionmaker[Session], ctx: Context, item_id: UUID) -> ScheduledInput:
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        return _values(ScheduledItemRepository(session, ctx).get_or_raise(item_id))


def _open(day: date, closed: frozenset[YearMonth]) -> bool:
    return YearMonth.from_date(day) not in closed


def _remove_pending(
    session: Session, scope: WorkspaceScope, item_id: UUID, *, since: date | None
) -> list[ScheduledOccurrence]:
    """Delete the item's pending rows due on/after ``since`` (all when ``None``), except in
    closed months. Returns the rows that remain."""
    closed = MonthStateRepository(session, scope).closed_months()
    repository = ScheduledOccurrenceRepository(session, scope)
    remaining: list[ScheduledOccurrence] = []
    for row in repository.for_item(item_id):
        if (
            row.status == OccurrenceStatus.PENDING
            and (since is None or row.due_date >= since)
            and _open(row.due_date, closed)
        ):
            repository.delete(row)
        else:
            remaining.append(row)
    return remaining


def _generate(
    session: Session,
    scope: WorkspaceScope,
    clock: Clock,
    item: ScheduledItem,
    horizon_months: int,
    *,
    include_past: bool = False,
) -> int:
    """Reconcile the item's rows with its rule (module docstring); returns rows created."""
    workspace = CurrentWorkspaceRepository(session, scope).get()
    today = clock.today(workspace.timezone)
    closed = MonthStateRepository(session, scope).closed_months()
    category = CategoryRepository(session, scope).get_or_raise(item.category_id)
    wanted: set[date] = set()
    if item.paused_at is None and item.archived_at is None and category.archived_at is None:
        rule = _rule(item)
        wanted = {
            day
            for day in occurrences(
                rule,
                date(1900, 1, 1) if include_past else today,
                _horizon(today, horizon_months),
                _holidays(workspace.country_code, rule),
            )
            if _open(day, closed)
        }
    repository = ScheduledOccurrenceRepository(session, scope)
    kept: set[date] = set()
    for row in repository.for_item(item.id):
        stale = (
            row.status == OccurrenceStatus.PENDING
            and row.due_date >= today
            and _open(row.due_date, closed)
            and row.due_date not in wanted
        )
        if stale:
            repository.delete(row)
        else:
            kept.add(row.due_date)
    missing = sorted(wanted - kept)
    for day in missing:
        repository.create(
            scheduled_item_id=item.id,
            direction=item.direction,
            category_id=item.category_id,
            due_date=day,
            expected_minor=item.amount_minor,
            currency=item.currency,
            amount_state=AmountState.CONFIRMED
            if item.amount_mode == AmountMode.FIXED
            else AmountState.ESTIMATED,
            status=OccurrenceStatus.PENDING,
            label=item.name,
        )
    return len(missing)


def _generate_items(
    session: Session,
    scope: WorkspaceScope,
    clock: Clock,
    horizon_months: int,
    items: list[ScheduledItem],
) -> int:
    """Generate each item in its own savepoint: one failing item does not stop the others."""
    created = 0
    for item in items:
        item_id = item.id
        try:
            with session.begin_nested():
                created += _generate(session, scope, clock, item, horizon_months)
        except Exception as exc:
            # Identifiers and the exception type only in the message (no amounts), as the
            # app's unhandled-error log does.
            logger.error(
                "recurring_generation_failed workspace=%s item=%s exception_type=%s",
                scope.workspace.id,
                item_id,
                type(exc).__name__,
                exc_info=True,
            )
    return created


def generate_workspace(
    factory: sessionmaker[Session],
    scope: WorkspaceScope,
    clock: Clock,
    settings: Settings,
) -> int:
    """Trusted scheduler entry point; it has an explicit tenant scope, not an impersonated user."""

    def write(session: Session) -> int:
        CurrentWorkspaceRepository(session, scope).lock(exclusive=True)
        items = ScheduledItemRepository(session, scope).ordered(active_only=True)
        return _generate_items(session, scope, clock, settings.recurrence_horizon_months, items)

    return run_immediate(factory, write)


def has_working_day_items(session: Session, scope: WorkspaceScope) -> bool:
    """Whether a non-archived item (paused ones too: they can resume) uses working days."""
    return any(
        item.archived_at is None and _rule(item).uses_working_days
        for item in ScheduledItemRepository(session, scope).ordered()
    )


def regenerate_working_day_items(
    session: Session, scope: WorkspaceScope, clock: Clock, horizon_months: int
) -> int:
    """After a workspace country change, inside the caller's exclusive-locked transaction."""
    items = [
        item
        for item in ScheduledItemRepository(session, scope).ordered(active_only=True)
        if _rule(item).uses_working_days
    ]
    return _generate_items(session, scope, clock, horizon_months, items)


def save_item(
    factory: sessionmaker[Session],
    ctx: Context,
    values: ScheduledInput,
    settings: Settings,
    *,
    item_id: UUID | None = None,
    include_past: bool = False,
) -> UUID:
    require_role(ctx, Role.EDITOR)
    name = clean_description(values.name)
    notes = clean_description(values.notes)
    if not name or not 0 < values.amount.amount_minor <= MAX_AMOUNT_MINOR:
        raise ValueError("invalid_item")

    def write(session: Session) -> UUID:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        workspace = CurrentWorkspaceRepository(session, ctx).get()
        if values.amount.currency != workspace.base_currency:
            raise ValueError("invalid_currency")
        category = CategoryRepository(session, ctx).get_or_raise(values.category_id)
        if category.kind != values.direction or category.archived_at is not None:
            raise ValueError("invalid_category")
        repository = ScheduledItemRepository(session, ctx)
        rule = values.rule
        fields = {
            "name": name,
            "notes": notes,
            "direction": values.direction,
            "category_id": values.category_id,
            "amount_minor": values.amount.amount_minor,
            "currency": values.amount.currency,
            "amount_mode": values.amount_mode,
            "start_date": rule.start_date,
            "end_date": rule.end_date,
            "max_count": rule.max_count,
            "freq": rule.freq,
            "interval": rule.interval,
            "weekdays": list(rule.weekdays),
            "month_day": str(rule.month_day),
            "month": rule.month,
            "weekend_policy": rule.weekend_policy,
        }
        if item_id is None:
            item = repository.create(**fields)
        else:
            item = repository.get_or_raise(item_id)
            if item.archived_at is not None:
                raise ValueError("archived_item")
            item = repository.update(item_id, **fields)
            # Refresh amounts/labels of future pending rows; closed months stay as they are.
            _remove_pending(session, ctx, item.id, since=ctx.clock.today(workspace.timezone))
        _generate(
            session,
            ctx,
            ctx.clock,
            item,
            settings.recurrence_horizon_months,
            include_past=include_past if item_id is None else False,
        )
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="scheduled.updated" if item_id else "scheduled.created",
            entity_type="scheduled_item",
            entity_id=str(item.id),
            ts=ctx.clock.now(),
        )
        return item.id

    return run_immediate(factory, write)


def change_state(
    factory: sessionmaker[Session],
    ctx: Context,
    item_id: UUID,
    action: ItemAction,
    settings: Settings,
) -> None:
    require_role(ctx, Role.EDITOR)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.EDITOR)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
        repository = ScheduledItemRepository(session, ctx)
        item = repository.get_or_raise(item_id)
        if item.archived_at is not None:
            raise ValueError("archived_item")
        workspace = CurrentWorkspaceRepository(session, ctx).get()
        today = ctx.clock.today(workspace.timezone)
        audit_action = f"scheduled.{action}"
        if action == ItemAction.RESUME:
            item = repository.update(item.id, paused_at=None)
            _generate(session, ctx, ctx.clock, item, settings.recurrence_horizon_months)
        elif action == ItemAction.PAUSE:
            _remove_pending(session, ctx, item.id, since=today)
            repository.update(item.id, paused_at=ctx.clock.now())
        else:
            # Past pending rows mean nothing once the item is gone; only history remains.
            remaining = _remove_pending(session, ctx, item.id, since=None)
            if action == ItemAction.DELETE and not remaining:
                repository.delete(item.id)
            else:
                if action == ItemAction.DELETE:
                    audit_action = "scheduled.archive"
                repository.update(item.id, archived_at=ctx.clock.now())
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action=audit_action,
            entity_type="scheduled_item",
            entity_id=str(item_id),
            ts=ctx.clock.now(),
        )

    run_immediate(factory, write)


def category_usage(session: Session, ctx: Context) -> dict[UUID, CategoryUsage]:
    counts: dict[UUID, int] = {}
    for repository in (
        ScheduledItemRepository(session, ctx),
        ScheduledOccurrenceRepository(session, ctx),
    ):
        for key, count in repository.category_counts().items():
            counts[key] = counts.get(key, 0) + count
    return {key: CategoryUsage(references=count) for key, count in counts.items()}


def workspace_has_scheduled_amounts(session: Session, ctx: Context) -> bool:
    return ScheduledItemRepository(session, ctx).any_exists()
