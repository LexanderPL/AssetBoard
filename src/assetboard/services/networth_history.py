"""Derived net-worth history (F5.9): one point per completed month-end plus today.

Nothing is stored: each point is computed from the item valuation history, the linked
resolvers, prices/FX and ``estimate_between`` on every read. Completed month-ends use the F5.6
backfill path (``values_at(..., backfill=True, today=today)``); today is the live value and is
never estimated. Stored ``NetWorthSnapshot`` rows are legacy archive data and are never read
for display.

The service loads items, valuations, categories and every linked resolution once. The number of
SQL statements therefore does not grow with the selected range.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.repositories import CurrentWorkspaceRepository, NetWorthCategoryRepository
from assetboard.domain.money import Money
from assetboard.domain.networth import Side
from assetboard.domain.networth_history import (
    DEFAULT_HISTORY_RANGE,
    ItemHistory,
    SnapshotBreakdown,
    breakdown_for,
    history_dates,
    linked_for_date,
    normalize_history_range,
    summarize_values,
    values_at,
)
from assetboard.services.context import Context, Role, require_role
from assetboard.services.networth import load_item_histories
from assetboard.services.networth_links import registry

__all__ = [
    "HistoryCategory",
    "HistoryPoint",
    "NetWorthHistoryData",
    "networth_history",
]


@dataclass(frozen=True, slots=True)
class HistoryCategory:
    id: UUID
    side: Side
    name: str
    color: str


@dataclass(frozen=True, slots=True)
class HistoryPoint:
    as_of: date
    assets: Money
    liabilities: Money
    net: Money
    breakdown: SnapshotBreakdown
    live: bool


@dataclass(frozen=True, slots=True)
class NetWorthHistoryData:
    base_currency: str
    range_code: str
    today: date
    points: tuple[HistoryPoint, ...]
    categories: dict[UUID, HistoryCategory]
    has_values: bool
    has_recorded_values: bool

    @property
    def has_chart(self) -> bool:
        return self.has_values and bool(self.points)


def _earliest_recorded(
    histories: tuple[ItemHistory, ...], *, today: date, timezone: str
) -> date | None:
    """The first date with something to derive, in the workspace time zone.

    A manual item contributes its earliest valuation on or before today. A linked item
    contributes its creation date, clamped to today; the resolver protocol has no earlier start.
    """
    zone = ZoneInfo(timezone)
    candidates: list[date] = []
    for item in histories:
        if item.valuation_mode == "linked":
            if item.created_at is not None:
                candidates.append(min(item.created_at.astimezone(zone).date(), today))
            continue
        dates = [point.as_of for point in item.valuations if point.as_of <= today]
        if dates:
            candidates.append(min(dates))
    return min(candidates, default=None)


def networth_history(
    factory: sessionmaker[Session],
    ctx: Context,
    range_code: str = DEFAULT_HISTORY_RANGE,
) -> NetWorthHistoryData:
    """The derived trend for one range; every member may read it."""
    require_role(ctx, Role.VIEWER)
    code = normalize_history_range(range_code)
    today = ctx.clock.today(ctx.workspace.timezone)
    with factory() as session:
        base_currency = CurrentWorkspaceRepository(session, ctx).get().base_currency
        category_rows = NetWorthCategoryRepository(session, ctx).ordered()
        categories = {
            category.id: HistoryCategory(
                id=category.id,
                side=Side(category.side),
                name=category.name,
                color=category.color,
            )
            for category in category_rows
        }
        histories = load_item_histories(session, ctx, categories=category_rows)
        earliest = _earliest_recorded(histories, today=today, timezone=ctx.workspace.timezone)
        dates = history_dates(today, code, earliest=earliest)
        if not dates:
            return NetWorthHistoryData(
                base_currency=base_currency,
                range_code=code,
                today=today,
                points=(),
                categories=categories,
                has_values=False,
                has_recorded_values=False,
            )
        resolved = registry.resolve_many(session, ctx, histories, dates)
        points: list[HistoryPoint] = []
        has_values = False
        has_recorded_values = False
        for day in dates:
            values = values_at(
                histories,
                day,
                timezone=ctx.workspace.timezone,
                linked=linked_for_date(resolved, histories, day),
                backfill=day < today,
                today=today,
            )
            summary, estimated = summarize_values(values, currency=base_currency)
            points.append(
                HistoryPoint(
                    as_of=day,
                    assets=summary.assets,
                    liabilities=summary.liabilities,
                    net=summary.net,
                    breakdown=breakdown_for(summary, estimated=estimated),
                    live=day == today,
                )
            )
            has_recorded_values = has_recorded_values or any(
                value.value_minor is not None for value in values
            )
            has_values = has_values or any(
                value.value_minor is not None
                and not value.archived
                and value.include_in_net_worth
                and value.currency == base_currency
                for value in values
            )
    return NetWorthHistoryData(
        base_currency=base_currency,
        range_code=code,
        today=today,
        points=tuple(points),
        categories=categories,
        has_values=has_values,
        has_recorded_values=has_recorded_values,
    )
