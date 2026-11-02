"""Stored net-worth snapshots and history (F5.3).

Automatic and manual snapshots are written once and never recalculated. Backfill deletes and
recreates only ``source=backfill`` rows. Every write runs in ``run_immediate``, takes the
workspace row lock and checks the source rules inside the transaction; the unique
``(workspace_id, as_of, source)`` constraint is the final guard when the monthly job and the
startup catch-up race.

Values come from ``domain.networth_history.values_at`` over rows loaded once, so a backfill is
one database read per table rather than one read per month.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from assetboard.clock import Clock
from assetboard.db.engine import run_immediate
from assetboard.db.repositories import (
    AuditEventRepository,
    CurrentWorkspaceRepository,
    ItemValuationRepository,
    NetWorthCategoryRepository,
    NetWorthItemRepository,
    NetWorthSnapshotRepository,
)
from assetboard.db.repositories.base import WorkspaceScope
from assetboard.domain.money import Money
from assetboard.domain.networth import Side
from assetboard.domain.networth_history import (
    ItemHistory,
    ItemValue,
    SnapshotBreakdown,
    backfill_month_ends,
    history_start,
    normalize_history_range,
    parse_breakdown,
    previous_month_end,
    snapshot_breakdown,
    source_rank,
    values_at,
)
from assetboard.domain.networth_summary import NetWorthSummary, SummaryItem, summarize_net_worth
from assetboard.services.context import Context, Role, require_role, require_role_now
from assetboard.services.networth import load_item_histories

__all__ = [
    "BackfillResult",
    "HistoryCategory",
    "HistorySnapshot",
    "NetWorthHistoryData",
    "NetWorthSnapshotError",
    "SnapshotDateInFuture",
    "backfill_snapshots",
    "networth_history",
    "take_auto_snapshot",
    "take_manual_snapshot",
]


class NetWorthSnapshotError(Exception):
    code = "invalid"


class SnapshotDateInFuture(NetWorthSnapshotError):
    code = "future_date"


@dataclass(frozen=True, slots=True)
class BackfillResult:
    count: int
    from_date: date | None
    to_date: date | None

    @property
    def empty(self) -> bool:
        return self.from_date is None


@dataclass(frozen=True, slots=True)
class HistoryCategory:
    id: UUID
    side: Side
    name: str
    color: str


@dataclass(frozen=True, slots=True)
class HistorySnapshot:
    id: UUID
    as_of: date
    source: str
    currency: str
    assets: Money
    liabilities: Money
    net: Money
    breakdown: SnapshotBreakdown
    charted: bool


@dataclass(frozen=True, slots=True)
class NetWorthHistoryData:
    base_currency: str
    range_code: str
    snapshots: tuple[HistorySnapshot, ...]
    categories: dict[UUID, HistoryCategory]

    @property
    def charted(self) -> tuple[HistorySnapshot, ...]:
        return tuple(point for point in self.snapshots if point.charted)

    @property
    def other_currency_count(self) -> int:
        return sum(1 for point in self.snapshots if not point.charted)


@dataclass(frozen=True, slots=True)
class _StoredSnapshot:
    id: UUID
    as_of: date
    source: str
    currency: str
    assets_minor: int
    liabilities_minor: int
    net_minor: int
    breakdown: object


def _summarize(
    values: tuple[ItemValue, ...], *, currency: str
) -> tuple[NetWorthSummary, dict[str, object]]:
    summary = summarize_net_worth(
        (
            SummaryItem(
                id=value.id,
                side=value.side,
                category_id=value.category_id,
                value=(
                    Money(value.value_minor, value.currency)
                    if value.value_minor is not None
                    else None
                ),
                include_in_net_worth=value.include_in_net_worth,
                archived=value.archived,
            )
            for value in values
        ),
        currency=currency,
    )
    return summary, snapshot_breakdown(summary)


def _summary_at(
    histories: tuple[ItemHistory, ...],
    *,
    as_of: date,
    timezone: str,
    currency: str,
) -> tuple[NetWorthSummary, dict[str, object]]:
    return _summarize(values_at(histories, as_of, timezone=timezone), currency=currency)


def _snapshot_uniqueness_error(error: IntegrityError) -> bool:
    message = str(error.orig).lower()
    return "networth_snapshots" in message and (
        "unique" in message or "uq_networth_snapshots" in message
    )


def _repository(session: Session, scope: WorkspaceScope) -> NetWorthSnapshotRepository:
    return NetWorthSnapshotRepository(session, scope)


def _currency(session: Session, scope: WorkspaceScope) -> str:
    return CurrentWorkspaceRepository(session, scope).get().base_currency


def _auto_write(
    session: Session,
    scope: WorkspaceScope,
    *,
    as_of: date,
    clock: Clock,
    timezone: str,
) -> bool:
    CurrentWorkspaceRepository(session, scope).lock(exclusive=True)
    # A workspace that does not use net worth never fills up with zero rows. Archived items
    # still count as items, so a workspace whose items are all archived keeps its snapshots.
    if not NetWorthItemRepository(session, scope).any_exists():
        return False
    repository = _repository(session, scope)
    existing = repository.for_date(as_of)
    if any(row.source in {"manual", "auto"} for row in existing):
        return False
    currency = _currency(session, scope)
    values = values_at(
        load_item_histories(session, scope),
        as_of,
        timezone=timezone,
    )
    # An item created after the month-end has no effective value there yet. Storing a zero
    # automatic row would start the chart with a misleading drop, and backfill could never
    # replace it because auto wins over backfill.
    if not any(value.value_minor is not None for value in values):
        return False
    for row in existing:
        if row.source == "backfill":
            repository.delete(row)
    summary, breakdown = _summarize(values, currency=currency)
    row = repository.create(
        as_of=as_of,
        assets_minor=summary.assets.amount_minor,
        liabilities_minor=summary.liabilities.amount_minor,
        currency=currency,
        breakdown=breakdown,
        source="auto",
    )
    AuditEventRepository(session, scope).record(
        actor_user_id=None,
        actor_type="system",
        action="networth_snapshot.created",
        entity_type="networth_snapshot",
        entity_id=str(row.id),
        ts=clock.now(),
        data={"as_of": as_of.isoformat()},
    )
    return True


def take_auto_snapshot(
    factory: sessionmaker[Session],
    scope: WorkspaceScope,
    clock: Clock,
    timezone: str,
) -> bool:
    """Store the previous month-end for one workspace; ``False`` when it was already taken."""
    as_of = previous_month_end(clock.today(timezone))
    try:
        return run_immediate(
            factory,
            lambda session: _auto_write(
                session, scope, as_of=as_of, clock=clock, timezone=timezone
            ),
        )
    except IntegrityError as exc:
        if _snapshot_uniqueness_error(exc):
            return False
        raise


def _manual_write(
    session: Session,
    ctx: Context,
    *,
    as_of: date,
    clock: Clock,
    timezone: str,
) -> tuple[date, bool]:
    require_role_now(session, ctx, Role.EDITOR)
    CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
    repository = _repository(session, ctx)
    existing = repository.for_date(as_of)
    replaced = any(row.source == "manual" for row in existing)
    for row in existing:
        if row.source in {"manual", "backfill"}:
            repository.delete(row)
    summary, breakdown = _summary_at(
        load_item_histories(session, ctx),
        as_of=as_of,
        timezone=timezone,
        currency=_currency(session, ctx),
    )
    row = repository.create(
        as_of=as_of,
        assets_minor=summary.assets.amount_minor,
        liabilities_minor=summary.liabilities.amount_minor,
        currency=summary.currency,
        breakdown=breakdown,
        source="manual",
    )
    AuditEventRepository(session, ctx).record(
        actor_user_id=ctx.user.id,
        action="networth_snapshot.taken",
        entity_type="networth_snapshot",
        entity_id=str(row.id),
        ts=clock.now(),
        data={"as_of": as_of.isoformat(), "replaced": replaced},
    )
    return as_of, replaced


def take_manual_snapshot(
    factory: sessionmaker[Session],
    ctx: Context,
    *,
    as_of: date | None = None,
) -> tuple[date, bool]:
    """Store today's manual snapshot, replacing an earlier manual one for the same date.

    ``as_of`` exists for service tests; the UI always uses the workspace's today. A future date
    is refused. Manual snapshots always store, even for an empty workspace.
    """
    require_role(ctx, Role.EDITOR)
    today = ctx.clock.today(ctx.workspace.timezone)
    target = as_of or today
    if target > today:
        raise SnapshotDateInFuture
    try:
        return run_immediate(
            factory,
            lambda session: _manual_write(
                session,
                ctx,
                as_of=target,
                clock=ctx.clock,
                timezone=ctx.workspace.timezone,
            ),
        )
    except IntegrityError as exc:
        if _snapshot_uniqueness_error(exc):
            # Another manual snapshot won a race for this date; the date is already taken.
            return target, True
        raise


def _backfill_write(session: Session, ctx: Context, *, today: date) -> BackfillResult:
    require_role_now(session, ctx, Role.EDITOR)
    CurrentWorkspaceRepository(session, ctx).lock(exclusive=True)
    dates = backfill_month_ends(ItemValuationRepository(session, ctx).earliest_as_of(), today)
    repository = _repository(session, ctx)
    rows = repository.for_range(None)
    blocking = {row.as_of for row in rows if row.source in {"manual", "auto"}}
    for row in rows:
        if row.source == "backfill":
            repository.delete(row)
    if not dates:
        return BackfillResult(0, None, None)
    currency = _currency(session, ctx)
    histories = load_item_histories(session, ctx)
    created = 0
    for as_of in dates:
        if as_of in blocking:
            continue
        summary, breakdown = _summary_at(
            histories,
            as_of=as_of,
            timezone=ctx.workspace.timezone,
            currency=currency,
        )
        repository.create(
            as_of=as_of,
            assets_minor=summary.assets.amount_minor,
            liabilities_minor=summary.liabilities.amount_minor,
            currency=currency,
            breakdown=breakdown,
            source="backfill",
        )
        created += 1
    AuditEventRepository(session, ctx).record(
        actor_user_id=ctx.user.id,
        action="networth_snapshot.backfilled",
        entity_type="networth_snapshot",
        entity_id=None,
        ts=ctx.clock.now(),
        data={
            "from": dates[0].isoformat(),
            "to": dates[-1].isoformat(),
            "count": created,
        },
    )
    return BackfillResult(created, dates[0], dates[-1])


def backfill_snapshots(factory: sessionmaker[Session], ctx: Context) -> BackfillResult:
    """Rebuild the backfill rows from valuation history; real snapshots are untouched."""
    require_role(ctx, Role.EDITOR)
    today = ctx.clock.today(ctx.workspace.timezone)
    return run_immediate(factory, lambda session: _backfill_write(session, ctx, today=today))


def networth_history(
    factory: sessionmaker[Session], ctx: Context, range_code: str
) -> NetWorthHistoryData:
    """The preferred snapshot per date in the requested range; every member may read it."""
    require_role(ctx, Role.VIEWER)
    code = normalize_history_range(range_code)
    today = ctx.clock.today(ctx.workspace.timezone)
    start = history_start(today, code)
    with factory() as session:
        base_currency = _currency(session, ctx)
        categories = {
            category.id: HistoryCategory(
                id=category.id,
                side=Side(category.side),
                name=category.name,
                color=category.color,
            )
            for category in NetWorthCategoryRepository(session, ctx).ordered()
        }
        rows = NetWorthSnapshotRepository(session, ctx).for_range(start)
        preferred: dict[date, _StoredSnapshot] = {}
        for row in rows:
            current = preferred.get(row.as_of)
            if current is None or source_rank(row.source) < source_rank(current.source):
                preferred[row.as_of] = _StoredSnapshot(
                    id=row.id,
                    as_of=row.as_of,
                    source=row.source,
                    currency=row.currency,
                    assets_minor=row.assets_minor,
                    liabilities_minor=row.liabilities_minor,
                    net_minor=row.net_minor,
                    breakdown=row.breakdown,
                )
    snapshots = tuple(
        HistorySnapshot(
            id=point.id,
            as_of=point.as_of,
            source=point.source,
            currency=point.currency,
            assets=Money(point.assets_minor, point.currency),
            liabilities=Money(point.liabilities_minor, point.currency),
            net=Money(point.net_minor, point.currency),
            breakdown=parse_breakdown(point.breakdown),
            charted=point.currency == base_currency,
        )
        for _as_of, point in sorted(preferred.items())
    )
    return NetWorthHistoryData(
        base_currency=base_currency,
        range_code=code,
        snapshots=snapshots,
        categories=categories,
    )
