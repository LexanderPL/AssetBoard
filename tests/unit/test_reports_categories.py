"""The pure category report (F10.3): spending per regular expense category, top-8 + Other and the
category filter. Every amount is synthetic; the ledger series (F10.1) is the oracle for totals."""

from __future__ import annotations

from datetime import date
from uuid import UUID, uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from assetboard.domain.money import Money
from assetboard.domain.months import YearMonth
from assetboard.domain.reports import (
    TOP_CATEGORIES,
    CategorySeries,
    LedgerEntry,
    ReportCategory,
    ReportStep,
    category_series,
    ledger_and_category_series,
    ledger_series,
    select_categories,
    top_categories,
)

FOOD = ReportCategory(uuid4(), "expense", "regular", "Food", "#16a34a")
RENT = ReportCategory(uuid4(), "expense", "regular", "Rent", "#2563eb")
TRAVEL = ReportCategory(uuid4(), "expense", "regular", "Travel", "#f59e0b")
SAVINGS = ReportCategory(uuid4(), "expense", "savings", "Savings", "#7c3aed")
SALARY = ReportCategory(uuid4(), "income", "regular", "Salary", "#059669")
WITHDRAWN = ReportCategory(uuid4(), "income", "savings", "Withdrawal", "#64748b")
EXPENSE_TRANSFER = ReportCategory(uuid4(), "expense", "transfer", "Expense transfer", "#64748b")
INCOME_TRANSFER = ReportCategory(uuid4(), "income", "transfer", "Income transfer", "#64748b")
CATEGORIES = {
    category.id: category
    for category in (
        FOOD,
        RENT,
        TRAVEL,
        SAVINGS,
        SALARY,
        WITHDRAWN,
        EXPENSE_TRANSFER,
        INCOME_TRANSFER,
    )
}


def entry(category: ReportCategory, day: date, minor: int, currency: str = "EUR") -> LedgerEntry:
    return LedgerEntry(day, category.id, Money(minor, currency))


def series(
    entries: list[LedgerEntry],
    *,
    start: date = date(2026, 1, 1),
    end: date = date(2026, 3, 31),
    step: ReportStep = ReportStep.MONTH,
    include_savings: bool = False,
    selected: tuple[UUID, ...] | None = None,
) -> CategorySeries:
    return category_series(
        entries,
        CATEGORIES,
        start=start,
        end=end,
        step=step,
        currency="EUR",
        include_savings=include_savings,
        selected=selected,
    )


def test_only_regular_expenses_count_by_default() -> None:
    report = series(
        [
            entry(FOOD, date(2026, 1, 5), 100_000),
            entry(SAVINGS, date(2026, 1, 6), 200_000),
            entry(SALARY, date(2026, 1, 7), 300_000),
            entry(WITHDRAWN, date(2026, 1, 8), 400_000),
            entry(EXPENSE_TRANSFER, date(2026, 1, 9), 500_000),
            entry(INCOME_TRANSFER, date(2026, 1, 10), 600_000),
        ]
    )
    assert [row.category_id for row in report.rows] == [FOOD.id]
    assert report.rows[0].name == "Food" and report.rows[0].color == "#16a34a"
    assert report.rows[0].amounts[0] == Money(100_000, "EUR")
    assert report.totals[0] == Money(100_000, "EUR")
    assert report.total == Money(100_000, "EUR")


def test_the_savings_option_adds_expense_savings_as_their_own_rows() -> None:
    report = series(
        [
            entry(FOOD, date(2026, 1, 5), 100_000),
            entry(SAVINGS, date(2026, 1, 6), 200_000),
            entry(WITHDRAWN, date(2026, 1, 7), 400_000),
        ],
        include_savings=True,
    )
    assert {row.category_id for row in report.rows} == {FOOD.id, SAVINGS.id}
    assert report.totals[0] == Money(300_000, "EUR")
    assert report.total == Money(300_000, "EUR")


def test_periods_have_no_gaps_and_sum_the_entries() -> None:
    report = series(
        [
            entry(FOOD, date(2026, 1, 5), 10_000),
            entry(FOOD, date(2026, 3, 5), 5_000),
        ]
    )
    assert report.periods == (YearMonth(2026, 1), YearMonth(2026, 2), YearMonth(2026, 3))
    food = report.rows[0]
    assert [amount.amount_minor for amount in food.amounts] == [10_000, 0, 5_000]
    assert food.total == Money(15_000, "EUR")
    assert [total.amount_minor for total in report.totals] == [10_000, 0, 5_000]


def test_a_year_step_sums_the_calendar_years() -> None:
    report = series(
        [
            entry(FOOD, date(2024, 12, 31), 10_000),
            entry(FOOD, date(2025, 1, 1), 20_000),
        ],
        start=date(2024, 1, 1),
        end=date(2026, 12, 31),
        step=ReportStep.YEAR,
    )
    assert report.periods == (2024, 2025, 2026)
    assert [amount.amount_minor for amount in report.rows[0].amounts] == [10_000, 20_000, 0]


def test_entries_outside_the_range_and_in_another_currency_are_ignored() -> None:
    report = series(
        [
            entry(FOOD, date(2025, 12, 31), 1_000),
            entry(FOOD, date(2026, 1, 1), 2_000),
            entry(FOOD, date(2026, 1, 2), 4_000, "USD"),
            entry(FOOD, date(2026, 4, 1), 8_000),
        ]
    )
    assert report.rows[0].total == Money(2_000, "EUR")
    assert report.total == Money(2_000, "EUR")


def test_an_unknown_category_raises() -> None:
    with pytest.raises(ValueError):
        category_series(
            [LedgerEntry(date(2026, 1, 1), uuid4(), Money(1, "EUR"))],
            CATEGORIES,
            start=date(2026, 1, 1),
            end=date(2026, 1, 31),
            step=ReportStep.MONTH,
            currency="EUR",
        )


def test_an_empty_range_has_the_periods_and_no_rows() -> None:
    report = series([])
    assert report.periods == (YearMonth(2026, 1), YearMonth(2026, 2), YearMonth(2026, 3))
    assert report.rows == ()
    assert [total.amount_minor for total in report.totals] == [0, 0, 0]
    assert report.total == Money(0, "EUR")


def test_the_category_filter_shows_exactly_the_chosen_categories_including_zeros() -> None:
    report = series(
        [
            entry(FOOD, date(2026, 1, 5), 10_000),
            entry(TRAVEL, date(2026, 1, 6), 30_000),
        ],
        selected=(TRAVEL.id, FOOD.id, RENT.id, uuid4()),
    )
    assert [row.category_id for row in report.rows] == [TRAVEL.id, FOOD.id, RENT.id]
    assert RENT.id in {row.category_id for row in report.rows}
    rent = next(row for row in report.rows if row.category_id == RENT.id)
    assert rent.amounts == (Money(0, "EUR"), Money(0, "EUR"), Money(0, "EUR"))
    assert not any(row.other for row in report.rows)
    assert report.totals[0] == Money(40_000, "EUR")


def test_the_filter_ignores_ineligible_and_unknown_ids() -> None:
    report = series(
        [entry(FOOD, date(2026, 1, 5), 10_000)],
        selected=(SALARY.id, SAVINGS.id, uuid4()),
    )
    assert report.rows == ()


def test_an_empty_selection_is_not_a_filter() -> None:
    report = series([entry(FOOD, date(2026, 1, 5), 10_000)], selected=())
    assert [row.category_id for row in report.rows] == [FOOD.id]


def test_top_categories_keeps_eight_and_combines_the_rest_as_other() -> None:
    extra = [
        ReportCategory(uuid4(), "expense", "regular", f"Category {index:02d}", "#334155")
        for index in range(3, 11)
    ]
    known = {**CATEGORIES, **{category.id: category for category in extra}}
    entries = [
        entry(category, date(2026, 1, 5), index * 10_000)
        for index, category in enumerate((FOOD, RENT, TRAVEL, *extra), start=1)
    ]
    full = category_series(
        entries,
        known,
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
        step=ReportStep.MONTH,
        currency="EUR",
    )
    report = top_categories(full)
    assert len(report.rows) == TOP_CATEGORIES + 1
    assert [row.total.amount_minor for row in report.rows[:TOP_CATEGORIES]] == [
        110_000,
        100_000,
        90_000,
        80_000,
        70_000,
        60_000,
        50_000,
        40_000,
    ]
    other = report.rows[-1]
    assert other.other and other.category_id is None
    assert other.name == "" and other.color == ""
    assert other.total == Money(60_000, "EUR")  # the three smallest: 30k + 20k + 10k
    assert report.totals == full.totals
    assert report.total == full.total


def test_top_categories_leaves_eight_or_fewer_alone() -> None:
    report = series([entry(FOOD, date(2026, 1, 5), 10_000)])
    assert top_categories(report) == report


def test_top_categories_rejects_a_non_positive_limit() -> None:
    with pytest.raises(ValueError):
        top_categories(series([]), limit=0)


CATEGORY_STRATEGY = st.sampled_from(list(CATEGORIES.values()))
AMOUNT_STRATEGY = st.integers(min_value=0, max_value=10_000_000)
DAY_STRATEGY = st.dates(min_value=date(2024, 1, 1), max_value=date(2026, 12, 31))


@st.composite
def entries_strategy(draw: st.DrawFn, max_size: int = 60) -> list[LedgerEntry]:
    count = draw(st.integers(min_value=0, max_value=max_size))
    return [
        LedgerEntry(
            draw(DAY_STRATEGY),
            draw(CATEGORY_STRATEGY).id,
            Money(draw(AMOUNT_STRATEGY), draw(st.sampled_from(["EUR", "USD"]))),
        )
        for _ in range(count)
    ]


@st.composite
def range_strategy(draw: st.DrawFn) -> tuple[date, date]:
    first = draw(st.integers(min_value=2024 * 12, max_value=2026 * 12 + 11))
    last = draw(st.integers(min_value=first, max_value=2026 * 12 + 11))
    start = YearMonth(first // 12, first % 12 + 1)
    end = YearMonth(last // 12, last % 12 + 1)
    return start.first_day, end.last_day


@given(entries=entries_strategy(), bounds=range_strategy(), include_savings=st.booleans())
def test_property_column_totals_equal_the_ledger_spending(
    entries: list[LedgerEntry], bounds: tuple[date, date], include_savings: bool
) -> None:
    start, end = bounds
    report = category_series(
        entries,
        CATEGORIES,
        start=start,
        end=end,
        step=ReportStep.MONTH,
        currency="EUR",
        include_savings=include_savings,
    )
    ledger = ledger_series(
        entries, CATEGORIES, start=start, end=end, step=ReportStep.MONTH, currency="EUR"
    )
    expected = [
        period.spent.amount_minor + (period.saved.amount_minor if include_savings else 0)
        for period in ledger
    ]
    assert [total.amount_minor for total in report.totals] == expected
    assert report.total.amount_minor == sum(expected)
    for row in report.rows:
        assert row.total.amount_minor == sum(amount.amount_minor for amount in row.amounts)


@given(entries=entries_strategy(), limit=st.integers(min_value=1, max_value=6))
def test_property_top_categories_preserves_every_total(
    entries: list[LedgerEntry], limit: int
) -> None:
    full = category_series(
        entries,
        CATEGORIES,
        start=date(2024, 1, 1),
        end=date(2026, 12, 31),
        step=ReportStep.YEAR,
        currency="EUR",
        include_savings=True,
    )
    report = top_categories(full, limit=limit)
    assert report.totals == full.totals
    assert report.total == full.total
    assert len(report.rows) <= limit + 1
    if report.rows and report.rows[-1].other:
        assert len(report.rows) == limit + 1


@given(entries=entries_strategy())
def test_property_a_selection_never_contains_an_other_row(
    entries: list[LedgerEntry],
) -> None:
    chosen = (FOOD.id, RENT.id, SAVINGS.id, SALARY.id)
    report = category_series(
        entries,
        CATEGORIES,
        start=date(2024, 1, 1),
        end=date(2026, 12, 31),
        step=ReportStep.MONTH,
        currency="EUR",
        selected=chosen,
    )
    assert {row.category_id for row in report.rows} <= set(chosen)
    assert not any(row.other for row in report.rows)


def test_the_combined_pass_matches_the_individual_functions() -> None:
    entries = [
        entry(FOOD, date(2026, 1, 5), 10_000),
        entry(TRAVEL, date(2026, 2, 5), 30_000),
        entry(SAVINGS, date(2026, 3, 5), 50_000),
        entry(SALARY, date(2026, 3, 6), 300_000),
        entry(EXPENSE_TRANSFER, date(2026, 3, 7), 90_000),
    ]
    for include_savings in (False, True):
        ledger, categories = ledger_and_category_series(
            entries,
            CATEGORIES,
            start=date(2026, 1, 1),
            end=date(2026, 3, 31),
            step=ReportStep.MONTH,
            currency="EUR",
            include_savings=include_savings,
        )
        assert ledger == ledger_series(
            entries,
            CATEGORIES,
            start=date(2026, 1, 1),
            end=date(2026, 3, 31),
            step=ReportStep.MONTH,
            currency="EUR",
        )
        assert categories == category_series(
            entries,
            CATEGORIES,
            start=date(2026, 1, 1),
            end=date(2026, 3, 31),
            step=ReportStep.MONTH,
            currency="EUR",
            include_savings=include_savings,
        )


def test_selecting_categories_reuses_the_full_series_without_a_second_pass() -> None:
    entries = [
        entry(FOOD, date(2026, 1, 5), 10_000),
        entry(TRAVEL, date(2026, 1, 6), 30_000),
    ]
    full = series(entries)
    narrowed = select_categories(
        full, CATEGORIES, include_savings=False, selected=(TRAVEL.id, FOOD.id, RENT.id)
    )
    assert narrowed == series(entries, selected=(TRAVEL.id, FOOD.id, RENT.id))
    assert select_categories(full, CATEGORIES, selected=()) is full


@given(entries=entries_strategy(), include_savings=st.booleans())
def test_property_the_combined_pass_matches_the_individual_series(
    entries: list[LedgerEntry], include_savings: bool
) -> None:
    start, end = date(2024, 1, 1), date(2026, 12, 31)
    ledger, categories = ledger_and_category_series(
        entries,
        CATEGORIES,
        start=start,
        end=end,
        step=ReportStep.YEAR,
        currency="EUR",
        include_savings=include_savings,
    )
    assert ledger == ledger_series(
        entries, CATEGORIES, start=start, end=end, step=ReportStep.YEAR, currency="EUR"
    )
    assert categories == category_series(
        entries,
        CATEGORIES,
        start=start,
        end=end,
        step=ReportStep.YEAR,
        currency="EUR",
        include_savings=include_savings,
    )
