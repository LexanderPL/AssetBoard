"""The F9.2 pure reports: the year grid, rolling category averages, year-over-year and plan
accuracy. Every amount is synthetic; the ledger series and the F10.3 category series are the
oracles for the grid totals."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from assetboard.domain.money import Money
from assetboard.domain.months import YearMonth
from assetboard.domain.reports import (
    CategorySeries,
    CategorySpend,
    LedgerEntry,
    PlannedMonth,
    ReportCategory,
    ReportStep,
    category_series,
    ledger_series,
    plan_accuracy,
    rolling_category_averages,
    year_grid,
    year_over_year,
)

FOOD = ReportCategory(uuid4(), "expense", "regular", "Food", "#16a34a")
RENT = ReportCategory(uuid4(), "expense", "regular", "Rent", "#2563eb")
SAVINGS = ReportCategory(uuid4(), "expense", "savings", "Savings", "#7c3aed")
SALARY = ReportCategory(uuid4(), "income", "regular", "Salary", "#059669")
WITHDRAWN = ReportCategory(uuid4(), "income", "savings", "Withdrawal", "#64748b")
TRANSFER = ReportCategory(uuid4(), "expense", "transfer", "Transfer", "#64748b")
CATEGORIES = {
    category.id: category for category in (FOOD, RENT, SAVINGS, SALARY, WITHDRAWN, TRANSFER)
}


def entry(category: ReportCategory, day: date, minor: int, currency: str = "EUR") -> LedgerEntry:
    return LedgerEntry(day, category.id, Money(minor, currency))


def monthly_series(
    amounts: list[int],
    *,
    start: YearMonth | None = None,
    category: ReportCategory = FOOD,
    currency: str = "EUR",
) -> CategorySeries:
    start = start or YearMonth(2026, 1)
    money = tuple(Money(amount, currency) for amount in amounts)
    periods = tuple(
        YearMonth(start.year + (start.month - 1 + index) // 12, (start.month - 1 + index) % 12 + 1)
        for index in range(len(amounts))
    )
    return CategorySeries(
        periods=periods,
        rows=(
            CategorySpend(
                category_id=category.id,
                name=category.name,
                color=category.color,
                amounts=money,
                total=Money(sum(amounts), currency),
                archived=category.archived,
            ),
        ),
        totals=money,
        total=Money(sum(amounts), currency),
    )


# Year grid


def test_the_year_grid_has_twelve_months_and_only_regular_expenses() -> None:
    grid = year_grid(
        [
            entry(FOOD, date(2026, 1, 5), 10_000),
            entry(FOOD, date(2026, 3, 5), 20_000),
            entry(SAVINGS, date(2026, 2, 5), 30_000),
            entry(SALARY, date(2026, 2, 6), 40_000),
            entry(WITHDRAWN, date(2026, 2, 7), 50_000),
            entry(TRANSFER, date(2026, 2, 8), 60_000),
            entry(FOOD, date(2025, 12, 31), 70_000),
            entry(FOOD, date(2026, 1, 6), 80_000, "USD"),
        ],
        CATEGORIES,
        year=2026,
        currency="EUR",
    )
    assert grid.periods == tuple(YearMonth(2026, month) for month in range(1, 13))
    assert [row.category_id for row in grid.rows] == [FOOD.id]
    assert [amount.amount_minor for amount in grid.rows[0].amounts] == [
        10_000,
        0,
        20_000,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
    ]
    assert grid.totals[0] == Money(10_000, "EUR")
    assert grid.totals[2] == Money(20_000, "EUR")
    assert grid.total == Money(30_000, "EUR")


def test_the_year_grid_can_include_savings_as_its_own_row() -> None:
    grid = year_grid(
        [
            entry(FOOD, date(2026, 1, 5), 10_000),
            entry(SAVINGS, date(2026, 1, 6), 30_000),
        ],
        CATEGORIES,
        year=2026,
        currency="EUR",
        include_savings=True,
    )
    assert {row.category_id for row in grid.rows} == {FOOD.id, SAVINGS.id}
    assert grid.totals[0] == Money(40_000, "EUR")


def test_a_selected_category_without_spending_keeps_its_zero_row() -> None:
    grid = year_grid(
        [entry(FOOD, date(2026, 1, 5), 10_000)],
        CATEGORIES,
        year=2026,
        currency="EUR",
        selected=(FOOD.id, RENT.id, SAVINGS.id, uuid4()),
    )
    assert [row.category_id for row in grid.rows] == [FOOD.id, RENT.id]
    rent = next(row for row in grid.rows if row.category_id == RENT.id)
    assert rent.total == Money(0, "EUR")
    assert all(amount.amount_minor == 0 for amount in rent.amounts)


def test_a_year_without_spending_still_has_the_twelve_month_columns() -> None:
    grid = year_grid([], CATEGORIES, year=2026, currency="EUR")
    assert len(grid.periods) == 12
    assert grid.rows == ()
    assert [total.amount_minor for total in grid.totals] == [0] * 12


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


@given(entries=entries_strategy(), include_savings=st.booleans())
def test_property_year_grid_totals_equal_the_ledger_spending(
    entries: list[LedgerEntry], include_savings: bool
) -> None:
    start, end = date(2026, 1, 1), date(2026, 12, 31)
    grid = year_grid(
        entries,
        CATEGORIES,
        year=2026,
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
    assert [total.amount_minor for total in grid.totals] == expected
    assert grid.total.amount_minor == sum(expected)
    assert sum(row.total.amount_minor for row in grid.rows) == grid.total.amount_minor
    for row in grid.rows:
        assert row.total.amount_minor == sum(amount.amount_minor for amount in row.amounts)


# Rolling category averages


def test_the_three_month_average_starts_at_the_third_month() -> None:
    series = monthly_series([100, 200, 300, 400])
    trends = rolling_category_averages(series, months=3)
    assert trends.periods == series.periods
    assert trends.months == 3
    assert [amount.amount_minor if amount else None for amount in trends.rows[0].averages] == [
        None,
        None,
        200,
        300,
    ]
    assert trends.rows[0].name == "Food" and trends.rows[0].color == "#16a34a"
    assert trends.has_values


def test_the_twelve_month_average_needs_a_full_window() -> None:
    series = monthly_series([120] * 13, start=YearMonth(2025, 1))
    trends = rolling_category_averages(series, months=12)
    values = [amount.amount_minor if amount else None for amount in trends.rows[0].averages]
    assert values[:11] == [None] * 11
    assert values[11] == 120
    assert values[12] == 120


def test_the_rolling_average_is_rounded_once_half_away_from_zero() -> None:
    series = monthly_series([33, 34, 34])
    trends = rolling_category_averages(series, months=3)
    assert trends.rows[0].averages[2] == Money(34, "EUR")  # 101 / 3 = 33.67
    exact = monthly_series([99, 100, 101])
    assert rolling_category_averages(exact, months=3).rows[0].averages[2] == Money(100, "EUR")


def test_a_yearly_series_has_no_monthly_rolling_averages() -> None:
    yearly = CategorySeries((2025, 2026), (), (), Money(0, "EUR"))
    trends = rolling_category_averages(yearly)
    assert trends.periods == () and trends.rows == () and not trends.has_values


def test_the_rolling_window_must_be_positive() -> None:
    with pytest.raises(ValueError):
        rolling_category_averages(monthly_series([1]), months=0)


@given(
    amounts=st.lists(st.integers(min_value=0, max_value=100_000), min_size=1, max_size=20),
    months=st.integers(min_value=1, max_value=12),
)
def test_property_each_rolling_average_is_the_window_sum_over_its_length(
    amounts: list[int], months: int
) -> None:
    trends = rolling_category_averages(monthly_series(amounts), months=months)
    for index, average in enumerate(trends.rows[0].averages):
        if index < months - 1:
            assert average is None
            continue
        window = amounts[index - months + 1 : index + 1]
        total = sum(window)
        quotient, remainder = divmod(total, months)
        if 2 * remainder >= months:
            quotient += 1
        assert average == Money(quotient, "EUR")


# Year over year


def yearly_series(
    current: int, previous: int, *, category: ReportCategory = FOOD
) -> CategorySeries:
    row = CategorySpend(
        category_id=category.id,
        name=category.name,
        color=category.color,
        amounts=(Money(previous, "EUR"), Money(current, "EUR")),
        total=Money(previous + current, "EUR"),
        archived=category.archived,
    )
    return CategorySeries(
        (2025, 2026), (row,), (Money(previous, "EUR"), Money(current, "EUR")), row.total
    )


def test_year_over_year_compares_a_category() -> None:
    yoy = year_over_year(yearly_series(1_200, 1_000), year=2026, category_id=FOOD.id)
    assert (yoy.current, yoy.previous, yoy.change) == (
        Money(1_200, "EUR"),
        Money(1_000, "EUR"),
        Money(200, "EUR"),
    )
    assert yoy.change_tenths == 200  # +20.0 %
    assert yoy.name == "Food" and yoy.color == "#16a34a" and not yoy.archived
    assert yoy.year == 2026 and yoy.previous_year == 2025


def test_year_over_year_total_uses_the_column_totals() -> None:
    yoy = year_over_year(yearly_series(1_200, 1_000), year=2026)
    assert yoy.category_id is None
    assert yoy.current == Money(1_200, "EUR") and yoy.previous == Money(1_000, "EUR")
    assert yoy.change_tenths == 200


def test_year_over_year_percent_is_none_when_the_previous_year_was_zero() -> None:
    yoy = year_over_year(yearly_series(500, 0), year=2026)
    assert yoy.previous == Money(0, "EUR") and yoy.change == Money(500, "EUR")
    assert yoy.change_tenths is None and not yoy.available


def test_year_over_year_handles_a_negative_change_and_rounding() -> None:
    yoy = year_over_year(yearly_series(1_000, 300), year=2026)
    assert yoy.change == Money(700, "EUR")
    assert yoy.change_tenths == 2_333  # +233.3 %
    down = year_over_year(yearly_series(800, 1_000), year=2026)
    assert down.change == Money(-200, "EUR")
    assert down.change_tenths == -200


def test_year_over_year_without_a_previous_year_counts_zero() -> None:
    series = CategorySeries((2026,), (), (Money(500, "EUR"),), Money(500, "EUR"))
    yoy = year_over_year(series, year=2026)
    assert yoy.previous == Money(0, "EUR") and yoy.change_tenths is None


def test_year_over_year_missing_category_counts_zero() -> None:
    yoy = year_over_year(yearly_series(1_000, 500), year=2026, category_id=RENT.id)
    assert yoy.current == Money(0, "EUR") and yoy.previous == Money(0, "EUR")
    assert yoy.change_tenths is None


# Planned vs actual accuracy


def accuracy_series() -> CategorySeries:
    report = category_series(
        [
            entry(FOOD, date(2026, 1, 5), 10_000),
            entry(FOOD, date(2026, 2, 5), 20_000),
            entry(FOOD, date(2026, 3, 5), 5_000),
            entry(RENT, date(2026, 1, 6), 30_000),
            entry(SAVINGS, date(2026, 1, 7), 40_000),
            entry(SALARY, date(2026, 1, 8), 50_000),
            entry(TRANSFER, date(2026, 1, 9), 60_000),
        ],
        CATEGORIES,
        start=date(2026, 1, 1),
        end=date(2026, 3, 31),
        step=ReportStep.MONTH,
        currency="EUR",
    )
    return report


def plans() -> list[PlannedMonth]:
    return [
        PlannedMonth(FOOD.id, YearMonth(2026, 1), Money(10_000, "EUR")),  # equal: not over
        PlannedMonth(FOOD.id, YearMonth(2026, 2), Money(15_000, "EUR")),  # over
        PlannedMonth(FOOD.id, YearMonth(2026, 3), Money(10_000, "EUR")),  # under
        PlannedMonth(RENT.id, YearMonth(2026, 1), Money(30_000, "EUR")),  # equal
        PlannedMonth(SAVINGS.id, YearMonth(2026, 1), Money(1_000, "EUR")),
        PlannedMonth(SALARY.id, YearMonth(2026, 1), Money(1_000, "EUR")),
        PlannedMonth(TRANSFER.id, YearMonth(2026, 1), Money(1_000, "EUR")),
        PlannedMonth(FOOD.id, YearMonth(2025, 12), Money(1_000, "EUR")),  # outside the range
    ]


def test_plan_accuracy_counts_planned_and_over_months() -> None:
    accuracy = plan_accuracy(accuracy_series(), plans(), CATEGORIES)
    assert [row.category_id for row in accuracy.rows] == [FOOD.id, RENT.id]
    food = accuracy.rows[0]
    assert (food.months_planned, food.months_over) == (3, 1)
    assert food.over_tenths == 333  # 33.3 %
    rent = accuracy.rows[1]
    assert (rent.months_planned, rent.months_over) == (1, 0)
    assert rent.over_tenths == 0
    assert (accuracy.months_planned, accuracy.months_over) == (4, 1)
    assert accuracy.over_tenths == 250  # 25.0 %
    assert not accuracy.empty


def test_plan_accuracy_excludes_categories_without_plans_and_non_expenses() -> None:
    accuracy = plan_accuracy(accuracy_series(), plans(), CATEGORIES)
    assert {row.category_id for row in accuracy.rows} == {FOOD.id, RENT.id}
    assert SAVINGS.id not in {row.category_id for row in accuracy.rows}
    assert SALARY.id not in {row.category_id for row in accuracy.rows}
    assert TRANSFER.id not in {row.category_id for row in accuracy.rows}


def test_plan_accuracy_ignores_other_currencies_and_unknown_categories_raise() -> None:
    accuracy = plan_accuracy(
        accuracy_series(),
        [PlannedMonth(FOOD.id, YearMonth(2026, 1), Money(1_000, "USD"))],
        CATEGORIES,
    )
    assert accuracy.empty
    with pytest.raises(ValueError):
        plan_accuracy(
            accuracy_series(),
            [PlannedMonth(uuid4(), YearMonth(2026, 1), Money(1_000, "EUR"))],
            CATEGORIES,
        )


def test_plan_accuracy_is_empty_without_plans() -> None:
    accuracy = plan_accuracy(accuracy_series(), [], CATEGORIES)
    assert accuracy.empty and accuracy.months_planned == 0
    assert accuracy.over_tenths is None


@given(
    amounts=st.lists(st.integers(min_value=0, max_value=100_000), min_size=1, max_size=8),
    planned=st.data(),
)
def test_property_accuracy_counts_every_strictly_over_month(
    amounts: list[int], planned: st.DataObject
) -> None:
    series = monthly_series(amounts)
    lines: list[PlannedMonth] = []
    expected_over = 0
    for index, actual in enumerate(amounts):
        if not planned.draw(st.booleans()):
            continue
        plan = planned.draw(st.integers(min_value=0, max_value=100_000))
        lines.append(PlannedMonth(FOOD.id, series.periods[index], Money(plan, "EUR")))  # type: ignore[arg-type]
        if actual > plan:
            expected_over += 1
    accuracy = plan_accuracy(series, lines, CATEGORIES)
    if not lines:
        assert accuracy.empty
        return
    row = accuracy.rows[0]
    assert row.months_planned == len(lines)
    assert row.months_over == expected_over
    assert row.months_over <= row.months_planned
    assert accuracy.months_planned == row.months_planned
    assert accuracy.months_over == row.months_over
