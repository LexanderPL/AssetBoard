"""Chart data (F2.7): the slices of the Home and Month donuts (also net worth and portfolio).

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

from collections.abc import Iterable
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


@dataclass(frozen=True, slots=True)
class CategoryAmount:
    """One category's total for the shared donut slicer."""

    category_id: UUID
    amount: Money


def category_slices(
    amounts: Iterable[CategoryAmount],
    *,
    currency: str,
    max_slices: int | None = DEFAULT_MAX_SLICES,
    min_share_bp: int = 0,
) -> tuple[Slice, ...]:
    """The donut slices of category totals, in ``currency`` (see the module docstring).

    This is the shared core of the month, net-worth and portfolio donuts: positive amounts only,
    largest first with stable ties, and a tail grouped into one "Other" slice. Two rules pick the
    tail and may be combined:

    - ``min_share_bp``: every amount below this share of the total, in basis points, is grouped
      (``amount × 10 000 < min_share_bp × total``, compared in integers);
    - ``max_slices``: at most this many slices, "Other" included (``None``: no limit).

    Shares use ``share_tenths`` (rounded once, half up).
    """
    if max_slices is not None and max_slices < 2:
        raise ValueError("max_slices must be at least 2")
    if min_share_bp < 0:
        raise ValueError("min_share_bp must not be negative")
    rows = list(amounts)
    for row in rows:
        if row.amount.currency != currency:
            raise ValueError(f"amount in {row.amount.currency}, chart in {currency}")
    rows = [row for row in rows if row.amount.amount_minor > 0]
    # sorted() is stable: equal amounts keep the input (category) order.
    rows = sorted(rows, key=lambda row: -row.amount.amount_minor)
    total = sum(row.amount.amount_minor for row in rows)
    kept = [row for row in rows if row.amount.amount_minor * 10_000 >= min_share_bp * total]
    rest = rows[len(kept) :]  # sorted descending, so the small amounts are the tail
    if max_slices is not None and len(kept) + (1 if rest else 0) > max_slices:
        kept, rest = rows[: max_slices - 1], rows[max_slices - 1 :]
    slices = [
        Slice(
            row.category_id,
            Money(row.amount.amount_minor, currency),
            share_tenths(row.amount.amount_minor, total),
        )
        for row in kept
    ]
    if rest:
        other = sum(row.amount.amount_minor for row in rest)
        slices.append(Slice(None, Money(other, currency), share_tenths(other, total), len(rest)))
    return tuple(slices)


def category_breakdown(
    summary: MonthSummary, block: ChartBlock, *, max_slices: int = DEFAULT_MAX_SLICES
) -> tuple[Slice, ...]:
    """The donut slices of ``block`` (see the module docstring). ``max_slices`` is at least 2
    (one category and "Other")."""
    if block not in CHART_BLOCKS:
        raise ValueError(f"not a charted block: {block}")
    return category_slices(
        (CategoryAmount(row.category_id, row.actual) for row in summary.block(block).rows),
        currency=summary.currency,
        max_slices=max_slices,
    )
