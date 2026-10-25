"""Donut slices (F2.7): table-driven cases and property tests (slices add up to the block's
actual total, no savings or transfer slice, sorted, at most ``max_slices``)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from assetboard.domain.charts import Slice, category_breakdown, share_tenths
from assetboard.domain.money import Money
from assetboard.domain.summary import Entry, PlanLine, SummaryCategory, summarize_month


def _eur(minor: int) -> Money:
    return Money(minor, "EUR")


def _category(kind: str = "expense", treatment: str = "regular") -> SummaryCategory:
    return SummaryCategory(uuid4(), kind, treatment)


@pytest.mark.parametrize(
    ("part", "whole", "expected"),
    [
        (1, 3, 333),  # 33.33...% -> 33.3%
        (2, 3, 667),  # 66.66...% -> 66.7%
        (1, 8, 125),  # exactly 12.5%
        (1, 2000, 1),  # 0.05% rounds half up to 0.1%
        (1, 2001, 0),  # just under 0.05%
        (5, 5, 1000),
        (0, 5, 0),
        (3, 0, 0),  # empty total
    ],
)
def test_share_tenths(part: int, whole: int, expected: int) -> None:
    assert share_tenths(part, whole) == expected


def test_expense_slices_sorted_by_amount_with_shares() -> None:
    food, rent, fun = _category(), _category(), _category()
    summary = summarize_month(
        [
            Entry(food.id, _eur(20_00)),
            Entry(rent.id, _eur(60_00)),
            Entry(fun.id, _eur(10_00)),
            Entry(fun.id, _eur(10_00)),
        ],
        [],
        [food, rent, fun],
        currency="EUR",
    )
    assert category_breakdown(summary, "expense") == (
        Slice(rent.id, _eur(60_00), 600),
        Slice(food.id, _eur(20_00), 200),
        Slice(fun.id, _eur(20_00), 200),  # equal amounts keep the category order
    )


def test_savings_transfers_and_zero_actuals_have_no_slice() -> None:
    food = _category()
    planned_only = _category()
    savings = _category(treatment="savings")
    transfer = _category(treatment="transfer")
    salary = _category("income")
    withdrawn = _category("income", "savings")
    summary = summarize_month(
        [
            Entry(food.id, _eur(5_00)),
            Entry(savings.id, _eur(100_00)),
            Entry(transfer.id, _eur(50_00)),
            Entry(salary.id, _eur(1000_00)),
            Entry(withdrawn.id, _eur(30_00)),
        ],
        [PlanLine(planned_only.id, _eur(40_00))],
        [food, planned_only, savings, transfer, salary, withdrawn],
        currency="EUR",
    )
    assert category_breakdown(summary, "expense") == (Slice(food.id, _eur(5_00), 1000),)
    assert category_breakdown(summary, "income") == (Slice(salary.id, _eur(1000_00), 1000),)


def test_empty_month_has_no_slices() -> None:
    summary = summarize_month([], [], [_category(), _category("income")], currency="EUR")
    assert category_breakdown(summary, "expense") == ()
    assert category_breakdown(summary, "income") == ()


def test_small_slices_are_grouped_into_other() -> None:
    cats = [_category() for _ in range(5)]
    amounts = [50_00, 40_00, 5_00, 3_00, 2_00]
    summary = summarize_month(
        [Entry(c.id, _eur(a)) for c, a in zip(cats, amounts, strict=True)],
        [],
        cats,
        currency="EUR",
    )
    assert category_breakdown(summary, "expense", max_slices=3) == (
        Slice(cats[0].id, _eur(50_00), 500),
        Slice(cats[1].id, _eur(40_00), 400),
        Slice(None, _eur(10_00), 100, grouped=3),
    )
    # Exactly max_slices categories: nothing is grouped.
    assert len(category_breakdown(summary, "expense", max_slices=5)) == 5


@pytest.mark.parametrize("bad", [0, 1])
def test_max_slices_must_allow_other(bad: int) -> None:
    summary = summarize_month([], [], [], currency="EUR")
    with pytest.raises(ValueError, match="max_slices"):
        category_breakdown(summary, "expense", max_slices=bad)


def test_only_expense_and_income_are_charted() -> None:
    summary = summarize_month([], [], [], currency="EUR")
    with pytest.raises(ValueError, match="charted"):
        category_breakdown(summary, "saved")  # type: ignore[arg-type]


_KINDS = st.sampled_from(
    [
        ("expense", "regular"),
        ("expense", "savings"),
        ("expense", "transfer"),
        ("income", "regular"),
        ("income", "savings"),
        ("income", "transfer"),
    ]
)


@st.composite
def _months(draw: st.DrawFn):
    categories = [SummaryCategory(uuid4(), *draw(_KINDS)) for _ in range(draw(st.integers(0, 14)))]
    entries = []
    if categories:
        for _ in range(draw(st.integers(0, 40))):
            category = draw(st.sampled_from(categories))
            entries.append(Entry(category.id, _eur(draw(st.integers(1, 10**9)))))
    return categories, summarize_month(entries, [], categories, currency="EUR")


@given(_months(), st.sampled_from(["expense", "income"]), st.integers(2, 12))
def test_slices_add_up_to_the_block_total(month, block, max_slices) -> None:
    categories, summary = month
    slices = category_breakdown(summary, block, max_slices=max_slices)
    assert sum(s.amount.amount_minor for s in slices) == summary.block(block).actual.amount_minor
    assert len(slices) <= max_slices
    assert all(s.amount.amount_minor > 0 for s in slices)
    assert all(0 <= s.share_tenths <= 1000 for s in slices)
    named = [s for s in slices if s.category_id is not None]
    amounts = [s.amount.amount_minor for s in named]
    assert amounts == sorted(amounts, reverse=True)
    # No savings or transfer slice: every named slice is a regular category of the block's kind.
    by_id = {c.id: c for c in categories}
    for s in named:
        assert by_id[s.category_id].kind == block
        assert by_id[s.category_id].treatment == "regular"
