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
    PeriodTotals,
    PlannedMonth,
    ReportCategory,
    ReportStep,
    category_series,
    ledger_series,
    plan_accuracy,
    rolling_category_averages,
    year_grid,
    year_over_year_table,
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


def totals(year: int, month: int, earned: int, spent: int, saved: int = 0) -> PeriodTotals:
    return PeriodTotals(
        YearMonth(year, month),
        Money(earned, "EUR"),
        Money(spent, "EUR"),
        Money(saved, "EUR"),
        Money(earned - spent - saved, "EUR"),
    )


def test_year_over_year_table_has_one_row_per_year_and_separate_changes() -> None:
    periods = [totals(2025, month, 1_000, 500) for month in range(1, 13)]
    periods += [totals(2026, 1, 1_500, 700), totals(2026, 2, 1_500, 700)]
    table = year_over_year_table(
        periods,
        start_year=2025,
        end_year=2026,
        today=date(2026, 2, 14),
        currency="EUR",
    )
    assert [row.year for row in table.rows] == [2025, 2026]
    first, current = table.rows
    assert first.earned == Money(12_000, "EUR")
    assert first.spent == Money(6_000, "EUR")
    assert first.has_previous is False
    assert first.earned_change == Money(0, "EUR")
    assert first.spent_change == Money(0, "EUR")
    assert first.saved_change == Money(0, "EUR")
    assert first.earned_change_tenths is None and first.spent_change_tenths is None

    # 2026 is compared with the full 2025 year, not only January-February.
    assert current.earned == Money(3_000, "EUR")
    assert current.spent == Money(1_400, "EUR")
    assert current.earned_change == Money(-9_000, "EUR")
    assert current.spent_change == Money(-4_600, "EUR")
    assert current.earned_change_tenths == -750  # -75.0 %
    assert current.spent_change_tenths == -767  # -76.7 %
    assert current.has_previous is True
    assert table.earned == Money(15_000, "EUR")
    assert table.spent == Money(7_400, "EUR")


def test_year_over_year_first_range_year_keeps_changes_when_a_prior_year_exists() -> None:
    table = year_over_year_table(
        [
            totals(2025, 1, 1_000, 500, 200),
            totals(2026, 1, 1_500, 700, 400),
        ],
        start_year=2026,
        end_year=2026,
        today=date(2026, 10, 14),
        currency="EUR",
    )
    row = table.rows[0]
    assert row.has_previous is True
    assert row.earned_change == Money(500, "EUR")
    assert row.spent_change == Money(200, "EUR")
    assert row.saved_change == Money(200, "EUR")
    assert row.earned_change_tenths == 500  # +50.0 %
    assert row.spent_change_tenths == 400  # +40.0 %
    assert row.saved_change_tenths == 1_000  # +100.0 %


def test_year_over_year_handles_a_zero_previous_year_and_rounding() -> None:
    periods = [totals(2026, 1, 1_000, 300)]
    table = year_over_year_table(
        periods,
        start_year=2026,
        end_year=2026,
        today=date(2026, 10, 14),
        currency="EUR",
    )
    row = table.rows[0]
    assert row.has_previous is False
    assert row.earned_change == Money(0, "EUR")
    assert row.spent_change == Money(0, "EUR")
    assert row.earned_change_tenths is None and row.spent_change_tenths is None

    previous = [totals(2025, 1, 300, 1_000)]
    down = year_over_year_table(
        previous + periods,
        start_year=2026,
        end_year=2026,
        today=date(2026, 10, 14),
        currency="EUR",
    ).rows[0]
    assert down.has_previous is True
    assert down.earned_change_tenths == 2_333  # +233.3 %
    assert down.spent_change_tenths == -700  # -70.0 %


def test_year_over_year_prior_year_with_zero_income_keeps_expense_change() -> None:
    row = year_over_year_table(
        [
            totals(2025, 1, 0, 500, 200),
            totals(2026, 1, 1_000, 700, 400),
        ],
        start_year=2026,
        end_year=2026,
        today=date(2026, 10, 14),
        currency="EUR",
    ).rows[0]
    assert row.has_previous is True
    assert row.earned_change == Money(1_000, "EUR")
    assert row.earned_change_tenths is None
    assert row.spent_change == Money(200, "EUR")
    assert row.spent_change_tenths == 400
    assert row.saved_change == Money(200, "EUR")
    assert row.saved_change_tenths == 1_000


def test_year_over_year_does_not_clip_the_current_or_previous_year() -> None:
    # Both years carry all twelve months. A June "today" must not cut either year at June.
    periods = [totals(2025, month, 1_000, 500, 100) for month in range(1, 13)] + [
        totals(2026, month, 1_000, 500, 100) for month in range(1, 13)
    ]
    row = year_over_year_table(
        periods,
        start_year=2026,
        end_year=2026,
        today=date(2026, 6, 15),
        currency="EUR",
    ).rows[0]
    assert (row.earned, row.spent, row.saved) == (
        Money(12_000, "EUR"),
        Money(6_000, "EUR"),
        Money(1_200, "EUR"),
    )
    assert row.earned_change == Money(0, "EUR")
    assert row.spent_change == Money(0, "EUR")
    assert row.saved_change == Money(0, "EUR")
    assert row.earned_change_tenths == 0
    assert row.spent_change_tenths == 0
    assert row.saved_change_tenths == 0


def test_year_over_year_savings_only_year_is_not_empty() -> None:
    table = year_over_year_table(
        [totals(2025, 1, 0, 0, 1_000), totals(2026, 1, 0, 0, 1_500)],
        start_year=2026,
        end_year=2026,
        today=date(2026, 10, 14),
        currency="EUR",
    )
    assert not table.empty
    row = table.rows[0]
    assert row.has_previous is True
    assert (row.earned, row.spent, row.saved) == (
        Money(0, "EUR"),
        Money(0, "EUR"),
        Money(1_500, "EUR"),
    )
    assert row.earned_change == Money(0, "EUR")
    assert row.earned_change_tenths is None
    assert row.saved_change == Money(500, "EUR")
    assert row.saved_change_tenths == 500


def test_year_over_year_totals_include_savings() -> None:
    table = year_over_year_table(
        [
            totals(2024, 1, 1_000, 500, 100),
            totals(2025, 1, 2_000, 600, 200),
            totals(2026, 1, 3_000, 700, 300),
        ],
        start_year=2024,
        end_year=2026,
        today=date(2026, 10, 14),
        currency="EUR",
    )
    assert [row.saved for row in table.rows] == [
        Money(100, "EUR"),
        Money(200, "EUR"),
        Money(300, "EUR"),
    ]
    assert table.saved == Money(600, "EUR")


def test_year_over_year_table_omits_future_years_and_keeps_empty_years() -> None:
    periods = [totals(2024, 1, 500, 100), totals(2026, 1, 800, 200)]
    table = year_over_year_table(
        periods,
        start_year=2024,
        end_year=2027,
        today=date(2026, 10, 14),
        currency="EUR",
    )
    assert [row.year for row in table.rows] == [2024, 2025, 2026]
    empty = table.rows[1]
    assert empty.earned == Money(0, "EUR") and empty.spent == Money(0, "EUR")


def test_year_over_year_table_is_not_empty_when_only_the_previous_year_has_data() -> None:
    table = year_over_year_table(
        [totals(2025, 1, 1_000, 500)],
        start_year=2026,
        end_year=2026,
        today=date(2026, 10, 14),
        currency="EUR",
    )
    assert not table.empty
    row = table.rows[0]
    assert row.has_previous is True
    assert row.earned_change == Money(-1_000, "EUR")
    assert row.spent_change == Money(-500, "EUR")


def test_year_over_year_table_is_not_empty_when_the_previous_year_has_only_savings() -> None:
    table = year_over_year_table(
        [totals(2025, 1, 0, 0, 1_000)],
        start_year=2026,
        end_year=2026,
        today=date(2026, 10, 14),
        currency="EUR",
    )
    assert table.rows and not table.empty
    assert table.rows[0].has_previous is True


def test_year_over_year_table_is_empty_when_every_figure_is_zero() -> None:
    table = year_over_year_table(
        [totals(2026, 1, 0, 0)],
        start_year=2026,
        end_year=2026,
        today=date(2026, 10, 14),
        currency="EUR",
    )
    assert table.rows and table.empty


def test_year_over_year_table_requires_monthly_periods() -> None:
    yearly = PeriodTotals(
        2026, Money(1_000, "EUR"), Money(500, "EUR"), Money(0, "EUR"), Money(500, "EUR")
    )
    with pytest.raises(ValueError, match="monthly"):
        year_over_year_table(
            [yearly],
            start_year=2026,
            end_year=2026,
            today=date(2026, 10, 14),
            currency="EUR",
        )


@given(
    previous=st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=100_000),
            st.integers(min_value=0, max_value=100_000),
            st.integers(min_value=0, max_value=100_000),
        ),
        min_size=1,
        max_size=12,
    ),
    current=st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=100_000),
            st.integers(min_value=0, max_value=100_000),
            st.integers(min_value=0, max_value=100_000),
        ),
        min_size=1,
        max_size=12,
    ),
)
def test_property_year_over_year_rows_sum_full_years_and_flag_prior_data(
    previous: list[tuple[int, int, int]], current: list[tuple[int, int, int]]
) -> None:
    periods = [
        totals(2025, index + 1, earned, spent, saved)
        for index, (earned, spent, saved) in enumerate(previous)
    ] + [
        totals(2026, index + 1, earned, spent, saved)
        for index, (earned, spent, saved) in enumerate(current)
    ]
    row = year_over_year_table(
        periods,
        start_year=2026,
        end_year=2026,
        today=date(2026, 12, 31),
        currency="EUR",
    ).rows[0]
    prev_earned = sum(values[0] for values in previous)
    prev_spent = sum(values[1] for values in previous)
    prev_saved = sum(values[2] for values in previous)
    cur_earned = sum(values[0] for values in current)
    cur_spent = sum(values[1] for values in current)
    cur_saved = sum(values[2] for values in current)
    assert row.earned == Money(cur_earned, "EUR")
    assert row.spent == Money(cur_spent, "EUR")
    assert row.saved == Money(cur_saved, "EUR")
    has_previous = prev_earned != 0 or prev_spent != 0 or prev_saved != 0
    assert row.has_previous is has_previous
    if has_previous:
        assert row.earned_change == Money(cur_earned - prev_earned, "EUR")
        assert row.spent_change == Money(cur_spent - prev_spent, "EUR")
        assert row.saved_change == Money(cur_saved - prev_saved, "EUR")
        assert (row.earned_change_tenths is None) is (prev_earned == 0)
        assert (row.spent_change_tenths is None) is (prev_spent == 0)
        assert (row.saved_change_tenths is None) is (prev_saved == 0)
    else:
        assert row.earned_change == Money(0, "EUR")
        assert row.spent_change == Money(0, "EUR")
        assert row.saved_change == Money(0, "EUR")
        assert row.earned_change_tenths is None
        assert row.spent_change_tenths is None
        assert row.saved_change_tenths is None


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
    # Deviation 10 000 against a plan of 65 000 is 15.4 %; the score is 84.6 %.
    assert accuracy.planned == Money(65_000, "EUR")
    assert accuracy.deviation == Money(10_000, "EUR")
    assert accuracy.score_tenths == 846
    assert not accuracy.empty


def test_plan_accuracy_scores_100_when_every_month_matches() -> None:
    series = monthly_series([10_000, 20_000])
    lines = [
        PlannedMonth(FOOD.id, series.periods[0], Money(10_000, "EUR")),  # type: ignore[arg-type]
        PlannedMonth(FOOD.id, series.periods[1], Money(20_000, "EUR")),  # type: ignore[arg-type]
    ]
    accuracy = plan_accuracy(series, lines, CATEGORIES)
    assert accuracy.deviation == Money(0, "EUR")
    assert accuracy.score_tenths == 1_000


def test_plan_accuracy_score_floors_at_zero_when_deviation_exceeds_the_plan() -> None:
    series = monthly_series([30_000])
    accuracy = plan_accuracy(
        series,
        [PlannedMonth(FOOD.id, series.periods[0], Money(10_000, "EUR"))],  # type: ignore[arg-type]
        CATEGORIES,
    )
    assert accuracy.deviation == Money(20_000, "EUR")
    assert accuracy.score_tenths == 0


def test_plan_accuracy_score_rounds_the_deviation_once() -> None:
    series = monthly_series([301])
    accuracy = plan_accuracy(
        series,
        [PlannedMonth(FOOD.id, series.periods[0], Money(300, "EUR"))],  # type: ignore[arg-type]
        CATEGORIES,
    )
    # 1 / 300 = 0.333 % -> 0.3 % (3 tenths), so the score is 99.7 %.
    assert accuracy.score_tenths == 997


def test_plan_accuracy_score_is_none_with_a_zero_planned_total() -> None:
    series = monthly_series([500])
    accuracy = plan_accuracy(
        series,
        [PlannedMonth(FOOD.id, series.periods[0], Money(0, "EUR"))],  # type: ignore[arg-type]
        CATEGORIES,
    )
    assert not accuracy.empty
    assert accuracy.planned == Money(0, "EUR")
    assert accuracy.deviation == Money(500, "EUR")
    assert accuracy.score_tenths is None


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
    assert accuracy.planned == Money(0, "EUR")
    assert accuracy.deviation == Money(0, "EUR")
    assert accuracy.score_tenths is None


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


def _score(plans: list[int], actuals: list[int]) -> int:
    series = monthly_series(actuals)
    lines = [
        PlannedMonth(FOOD.id, series.periods[index], Money(plan, "EUR"))  # type: ignore[arg-type]
        for index, plan in enumerate(plans)
    ]
    score = plan_accuracy(series, lines, CATEGORIES).score_tenths
    assert score is not None
    return score


@given(
    plans=st.lists(st.integers(min_value=1, max_value=100_000), min_size=1, max_size=6),
    actuals=st.lists(st.integers(min_value=0, max_value=200_000), min_size=6, max_size=6),
)
def test_property_planning_score_is_bounded_and_improves_as_deviations_shrink(
    plans: list[int], actuals: list[int]
) -> None:
    actuals = actuals[: len(plans)]
    score = _score(plans, actuals)
    assert 0 <= score <= 1_000
    nearer = [plan + (actual - plan) // 2 for plan, actual in zip(plans, actuals, strict=True)]
    assert _score(plans, nearer) >= score
    assert _score(plans, plans) == 1_000
