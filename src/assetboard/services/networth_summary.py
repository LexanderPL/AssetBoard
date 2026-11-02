"""The net-worth summary (F5.2): reads current values and runs ``summarize_net_worth``.

Reads one snapshot of the workspace's items, their categories and valuations at the workspace's
today. The F5.1 list and this summary share ``services.networth.current_values``; F5.3 will call
it with a month-end date. No writes or audit events.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.repositories import CurrentWorkspaceRepository, NetWorthCategoryRepository
from assetboard.domain.networth import Side
from assetboard.domain.networth_summary import NetWorthSummary, SummaryItem, summarize_net_worth
from assetboard.services.context import Context, Role, require_role
from assetboard.services.networth import ItemView, current_values

__all__ = ["NetWorthSummaryData", "SummaryCategory", "networth_summary"]


@dataclass(frozen=True, slots=True)
class SummaryCategory:
    id: UUID
    side: Side
    name: str
    color: str


@dataclass(frozen=True, slots=True)
class NetWorthSummaryData:
    summary: NetWorthSummary
    categories: dict[UUID, SummaryCategory]
    non_base_items: tuple[ItemView, ...]
    no_value_items: tuple[ItemView, ...]
    excluded_items: tuple[ItemView, ...]
    unavailable_items: tuple[ItemView, ...]


def networth_summary(factory: sessionmaker[Session], ctx: Context) -> NetWorthSummaryData:
    """The summary at the workspace's today; every member may read it."""
    require_role(ctx, Role.VIEWER)
    as_of = ctx.clock.today(ctx.workspace.timezone)
    with factory() as session:
        base_currency = CurrentWorkspaceRepository(session, ctx).get().base_currency
        category_rows = NetWorthCategoryRepository(session, ctx).ordered()
        values = current_values(
            session,
            ctx,
            as_of=as_of,
            base_currency=base_currency,
            timezone=ctx.workspace.timezone,
            categories=category_rows,
        )
        categories = {
            category.id: SummaryCategory(
                id=category.id,
                side=Side(category.side),
                name=category.name,
                color=category.color,
            )
            for category in category_rows
        }
        order = {
            category.id: (category.sort_order, category.name_key, str(category.id))
            for category in category_rows
        }
        values.sort(key=lambda value: (order[value.category_id], value.name, str(value.id)))
    summary = summarize_net_worth(
        (
            SummaryItem(
                id=value.id,
                side=Side(value.side),
                category_id=value.category_id,
                value=value.current,
                include_in_net_worth=value.include_in_net_worth,
                archived=value.archived,
                unavailable=value.unavailable,
            )
            for value in values
        ),
        currency=base_currency,
    )
    by_id = {value.id: value for value in values}
    return NetWorthSummaryData(
        summary=summary,
        categories=categories,
        non_base_items=tuple(by_id[item_id] for item_id in summary.non_base_item_ids),
        no_value_items=tuple(by_id[item_id] for item_id in summary.no_value_item_ids),
        excluded_items=tuple(by_id[item_id] for item_id in summary.excluded_item_ids),
        unavailable_items=tuple(by_id[item_id] for item_id in summary.unavailable_item_ids),
    )
