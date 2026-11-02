"""Net-worth history (F5.3): month ends, date-dependent values, snapshots and ranges.

Pure: no I/O, database or clock access. Services pass "today" and the workspace time zone in
from the injected ``Clock``. Values are selected from the same ``ItemHistory`` that
``services.networth.current_values`` uses, so the summary, the automatic snapshot and the
backfill cannot drift apart.

An item counts up to its archive date: at month-end ``D`` it is excluded only when its
``archived_at`` falls on or before ``D`` in the workspace's time zone. A restored item has no
``archived_at`` and counts at every date. ``include_in_net_worth`` has no history in F5.3, so the
current setting applies to every date.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import cast
from uuid import UUID
from zoneinfo import ZoneInfo

from assetboard.domain.money import Money
from assetboard.domain.networth import Side, Unavailable, ValuationPoint, current_valuation
from assetboard.domain.networth_summary import NetWorthSummary

HISTORY_RANGES = ("1y", "3y", "5y", "all")
DEFAULT_HISTORY_RANGE = "1y"
SNAPSHOT_SOURCES = ("auto", "manual", "backfill")
# Lower wins when several sources exist for the same date.
SOURCE_PRIORITY = {"manual": 0, "auto": 1, "backfill": 2}
BREAKDOWN_VERSION = 1


@dataclass(frozen=True, slots=True)
class ItemHistory:
    """One item and all of its valuations, loaded once for many dates."""

    id: UUID
    side: Side
    category_id: UUID
    currency: str
    include_in_net_worth: bool
    archived_at: datetime | None
    valuations: tuple[ValuationPoint, ...]
    valuation_mode: str = "manual"
    link_type: str | None = None
    link_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ItemValue:
    """One item's effective value on an ``as_of`` date."""

    id: UUID
    side: Side
    category_id: UUID
    currency: str
    include_in_net_worth: bool
    archived_at: datetime | None
    archived: bool
    value_minor: int | None
    value_as_of: date | None
    unavailable: bool = False


@dataclass(frozen=True, slots=True)
class BreakdownCategory:
    category_id: UUID
    side: Side
    total_minor: int


@dataclass(frozen=True, slots=True)
class SnapshotBreakdown:
    """The versioned JSON stored on a snapshot; unknown keys are ignored."""

    categories: tuple[BreakdownCategory, ...]
    non_base: int
    no_value: int
    unavailable: int = 0


def month_end(day: date) -> date:
    """The last day of ``day``'s month."""
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def previous_month_end(day: date) -> date:
    """The last day of the month before ``day``'s month."""
    return day.replace(day=1) - timedelta(days=1)


def month_ends(start: date, end: date) -> tuple[date, ...]:
    """Every month-end from ``start``'s month through ``end``'s month, oldest first."""
    current = month_end(start)
    last = month_end(end)
    months: list[date] = []
    while current <= last:
        months.append(current)
        first_of_next = (
            date(current.year + 1, 1, 1)
            if current.month == 12
            else date(current.year, current.month + 1, 1)
        )
        current = month_end(first_of_next)
    return tuple(months)


def backfill_month_ends(earliest_valuation: date | None, today: date) -> tuple[date, ...]:
    """Month-ends from the earliest valuation's month through the previous month.

    No valuations, or a first valuation in the current month, means there is nothing to
    backfill yet.
    """
    if earliest_valuation is None:
        return ()
    first = month_end(earliest_valuation)
    last = previous_month_end(today)
    if first > last:
        return ()
    return month_ends(first, last)


def _years_before(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:  # 29 February in a non-leap year
        return date(day.year - years, 2, 28)


def normalize_history_range(value: str) -> str:
    """A supported range code, falling back to the documented default."""
    return value if value in HISTORY_RANGES else DEFAULT_HISTORY_RANGE


def history_start(today: date, range_code: str) -> date | None:
    """The first date included by ``range_code``; ``None`` means every stored snapshot."""
    years = {"1y": 1, "3y": 3, "5y": 5}.get(normalize_history_range(range_code))
    return None if years is None else _years_before(today, years)


def source_rank(source: str) -> int:
    return SOURCE_PRIORITY[source]


def preferred_source(sources: Iterable[str]) -> str | None:
    """The source shown when several snapshots share a date: manual > auto > backfill."""
    return min(sources, key=source_rank, default=None)


def archived_on(archived_at: datetime | None, timezone: str) -> date | None:
    """The workspace-local date on which an item was archived."""
    if archived_at is None:
        return None
    return archived_at.astimezone(ZoneInfo(timezone)).date()


def values_at(
    items: Sequence[ItemHistory],
    as_of: date,
    *,
    timezone: str,
    linked: Mapping[UUID, Money | Unavailable] | None = None,
) -> tuple[ItemValue, ...]:
    """Each item's effective value and archive state on ``as_of``, in input order.

    A manual valuation dated after ``as_of`` is ignored. Linked items use the service-resolved
    value passed in ``linked`` (keyed by item id); a missing or ``Unavailable`` resolution means
    the value is unavailable, never zero. An item is archived from its workspace-local archive
    date onwards; that date itself is already excluded (the owner's decision).
    """
    values: list[ItemValue] = []
    for item in items:
        archive_day = archived_on(item.archived_at, timezone)
        archived = archive_day is not None and archive_day <= as_of
        if item.valuation_mode == "linked":
            resolution = (linked or {}).get(item.id)
            available = isinstance(resolution, Money)
            values.append(
                ItemValue(
                    id=item.id,
                    side=item.side,
                    category_id=item.category_id,
                    currency=item.currency,
                    include_in_net_worth=item.include_in_net_worth,
                    archived_at=item.archived_at,
                    archived=archived,
                    value_minor=resolution.amount_minor if available else None,
                    value_as_of=as_of if available else None,
                    unavailable=not available,
                )
            )
            continue
        current = current_valuation(item.valuations, as_of)
        values.append(
            ItemValue(
                id=item.id,
                side=item.side,
                category_id=item.category_id,
                currency=item.currency,
                include_in_net_worth=item.include_in_net_worth,
                archived_at=item.archived_at,
                archived=archived,
                value_minor=current.value_minor if current is not None else None,
                value_as_of=current.as_of if current is not None else None,
            )
        )
    return tuple(values)


def snapshot_breakdown(summary: NetWorthSummary) -> dict[str, object]:
    """The versioned object stored in a snapshot's ``breakdown`` column."""
    categories = [
        {
            "category_id": str(category.category_id),
            "side": category.side.value,
            "total_minor": category.total.amount_minor,
        }
        for category in (*summary.asset_categories, *summary.liability_categories)
    ]
    return {
        "v": BREAKDOWN_VERSION,
        "categories": categories,
        "not_counted": {
            "non_base": len(summary.non_base_item_ids),
            "no_value": len(summary.no_value_item_ids),
            "unavailable": len(summary.unavailable_item_ids),
        },
    }


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return cast(Mapping[str, object], value)


def _optional_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def parse_breakdown(raw: object) -> SnapshotBreakdown:
    """Read a stored breakdown, ignoring unknown keys and malformed entries."""
    root = _mapping(raw)
    categories: list[BreakdownCategory] = []
    rows = root.get("categories")
    if isinstance(rows, list):
        for entry in cast(list[object], rows):
            row = _mapping(entry)
            category_id = row.get("category_id")
            side = row.get("side")
            total = row.get("total_minor")
            if not isinstance(category_id, str) or side not in {s.value for s in Side}:
                continue
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                continue
            try:
                parsed_id = UUID(category_id)
            except ValueError:
                continue
            categories.append(BreakdownCategory(parsed_id, Side(str(side)), total))
    counts = _mapping(root.get("not_counted"))
    return SnapshotBreakdown(
        tuple(categories),
        _optional_count(counts.get("non_base")),
        _optional_count(counts.get("no_value")),
        _optional_count(counts.get("unavailable")),
    )
