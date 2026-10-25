"""Chart data (F2.7): the slices of the Home and Month donuts.

Pure: no I/O, no clock. A slice is one category's actual amount in one summary block, in integer
minor units, with its share of the block's total in tenths of a percent (rounded half up once per
slice, from the exact ratio; the shares need not add up to exactly 100.0%).

Only the ``expense`` block (expense/regular) and the ``income`` block (income/regular) are
charted: savings and transfers are in neither chart (owner, F2.7: the flow bar shows saving).
A category with no actual (or a net non-positive one) has no slice. Slices are sorted by amount,
largest first (ties keep the summary's category order). Beyond ``max_slices`` the smallest
categories are grouped into one "Other" slice (``category_id`` ``None``), so a ring stays
readable; the slices still add up to the block's total.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from assetboard.domain.money import Money
from assetboard.domain.summary import MonthSummary

ChartBlock = Literal["expense", "income"]
CHART_BLOCKS: tuple[ChartBlock, ...] = ("expense", "income")
DEFAULT_MAX_SLICES = 8
PER_MILLE = 1000  # shares are in tenths of a percent


@dataclass(frozen=True, slots=True)
class Slice:
    category_id: UUID | None  # None: the "Other" slice (several small categories)
    amount: Money
    share_tenths: int  # share of the chart's total, in tenths of a percent
    grouped: int = 1  # how many categories the slice holds (>1 only for "Other")


def share_tenths(part: int, whole: int) -> int:
    """``part / whole`` in tenths of a percent, rounded half up; 0 when ``whole`` is 0."""
    if whole <= 0 or part <= 0:
        return 0
    quotient, remainder = divmod(part * PER_MILLE, whole)
    return quotient + (1 if 2 * remainder >= whole else 0)


def category_breakdown(
    summary: MonthSummary, block: ChartBlock, *, max_slices: int = DEFAULT_MAX_SLICES
) -> tuple[Slice, ...]:
    """The donut slices of ``block`` (see the module docstring). ``max_slices`` is at least 2
    (one category and "Other")."""
    if block not in CHART_BLOCKS:
        raise ValueError(f"not a charted block: {block}")
    if max_slices < 2:
        raise ValueError("max_slices must be at least 2")
    rows = [r for r in summary.block(block).rows if r.actual.amount_minor > 0]
    # sorted() is stable: equal amounts keep the summary's category order.
    rows = sorted(rows, key=lambda r: -r.actual.amount_minor)
    total = sum(r.actual.amount_minor for r in rows)
    currency = summary.currency
    if len(rows) > max_slices:
        kept, rest = rows[: max_slices - 1], rows[max_slices - 1 :]
    else:
        kept, rest = rows, []
    slices = [
        Slice(
            r.category_id,
            Money(r.actual.amount_minor, currency),
            share_tenths(r.actual.amount_minor, total),
        )
        for r in kept
    ]
    if rest:
        other = sum(r.actual.amount_minor for r in rest)
        slices.append(Slice(None, Money(other, currency), share_tenths(other, total), len(rest)))
    return tuple(slices)
