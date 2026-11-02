"""The net-worth summary (F5.2): assets, liabilities, net worth and category shares.

Pure: no I/O, no clock. Values are integer minor units in one currency, the workspace's base
currency. The caller resolves each item's current value (F5.1/F5.3); this module only adds and
groups. Items are counted in this order:

1. an archived item is ignored;
2. ``include_in_net_worth=false`` is the user's choice, so the item is reported as excluded;
3. an item with no effective value is reported separately;
4. an item in another currency is reported separately (no conversion before M6);
5. everything else is counted.

Items under an archived category still count; only the item's own archive removes it. Category
shares are of their own side's total, in tenths of a percent, rounded once half up by the same
helper as the month summary/donuts. When a side has no counted value, its category shares are
``None`` instead of dividing by zero.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from assetboard.domain.charts import share_tenths
from assetboard.domain.money import Money
from assetboard.domain.networth import Side


@dataclass(frozen=True, slots=True)
class SummaryItem:
    """One item's current value, already selected as of the requested date."""

    id: UUID
    side: Side
    category_id: UUID
    value: Money | None
    include_in_net_worth: bool = True
    archived: bool = False


@dataclass(frozen=True, slots=True)
class CategoryTotal:
    category_id: UUID
    side: Side
    total: Money
    share_tenths: int | None  # None when the side total is zero


@dataclass(frozen=True, slots=True)
class NetWorthSummary:
    currency: str
    assets: Money
    liabilities: Money
    net: Money
    asset_categories: tuple[CategoryTotal, ...]
    liability_categories: tuple[CategoryTotal, ...]
    non_base_item_ids: tuple[UUID, ...]
    no_value_item_ids: tuple[UUID, ...]
    excluded_item_ids: tuple[UUID, ...]

    def categories(self, side: Side) -> tuple[CategoryTotal, ...]:
        return self.asset_categories if side is Side.ASSET else self.liability_categories


def summarize_net_worth(items: Iterable[SummaryItem], *, currency: str) -> NetWorthSummary:
    """The net-worth summary of already-resolved item values (see the module docstring)."""
    totals = {Side.ASSET: 0, Side.LIABILITY: 0}
    category_totals: dict[tuple[Side, UUID], int] = {}
    category_order: dict[tuple[Side, UUID], int] = {}
    non_base: list[UUID] = []
    no_value: list[UUID] = []
    excluded: list[UUID] = []

    for position, item in enumerate(items):
        if item.archived:
            continue
        if not item.include_in_net_worth:
            excluded.append(item.id)
            continue
        if item.value is None:
            no_value.append(item.id)
            continue
        if item.value.currency != currency:
            non_base.append(item.id)
            continue
        if item.value.amount_minor < 0:
            raise ValueError("net-worth values cannot be negative")
        key = (item.side, item.category_id)
        category_order.setdefault(key, position)
        totals[item.side] += item.value.amount_minor
        category_totals[key] = category_totals.get(key, 0) + item.value.amount_minor

    def category_rows(side: Side) -> tuple[CategoryTotal, ...]:
        side_total = totals[side]
        rows = [
            (category_id, amount, order)
            for (row_side, category_id), amount in category_totals.items()
            if row_side is side and amount != 0
            for order in (category_order[(row_side, category_id)],)
        ]
        rows.sort(key=lambda row: (-row[1], row[2]))
        return tuple(
            CategoryTotal(
                category_id=category_id,
                side=side,
                total=Money(amount, currency),
                share_tenths=(share_tenths(amount, side_total) if side_total > 0 else None),
            )
            for category_id, amount, _order in rows
        )

    assets = Money(totals[Side.ASSET], currency)
    liabilities = Money(totals[Side.LIABILITY], currency)
    return NetWorthSummary(
        currency=currency,
        assets=assets,
        liabilities=liabilities,
        net=assets - liabilities,
        asset_categories=category_rows(Side.ASSET),
        liability_categories=category_rows(Side.LIABILITY),
        non_base_item_ids=tuple(non_base),
        no_value_item_ids=tuple(no_value),
        excluded_item_ids=tuple(excluded),
    )
