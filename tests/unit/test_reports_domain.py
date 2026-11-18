"""The pure ledger report series (F10.1): treatment rules, no gaps, exact integer totals."""

from __future__ import annotations

from datetime import date
from uuid import UUID, uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from assetboard.domain.money import Money
from assetboard.domain.months import YearMonth
from assetboard.domain.reports import (
    LedgerEntry,
    PeriodTotals,
    ReportCategory,
    ReportStep,
    ledger_series,
    period_keys,
    totals_of,
)

EARNED = ReportCategory(uuid4(), "income", "regular")
WITHDRAWN = ReportCategory(uuid4(), "income", "savings")
SPENT = ReportCategory(uuid4(), "expense", "regular")
SAVED = ReportCategory(uuid4(), "expense", "savings")
EXPENSE_TRANSFER = ReportCategory(uuid4(), "expense", "transfer")
INCOME_TRANSFER = ReportCategory(uuid4(), "income", "transfer")
CATEGORIES = {
    category.id: category
    for category in (
        EARNED,
        WITHDRAWN,
        SPENT,
        SAVED,
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
) -> list[PeriodTotals]:
    return ledger_series(entries, CATEGORIES, start=start, end=end, step=step, currency="EUR")


def test_the_four_treatments_and_transfers() -> None:
    amounts = series(
        [
            entry(EARNED, date(2026, 1, 5), 500_000),
            entry(WITHDRAWN, date(2026, 1, 6), 7_000),
            entry(SPENT, date(2026, 1, 7), 120_000),
            entry(SAVED, date(2026, 1, 8), 30_000),
            entry(EXPENSE_TRANSFER, date(2026, 1, 9), 99_000),
            entry(INCOME_TRANSFER, date(2026, 1, 10), 88_000),
        ]
    )
    january = amounts[0]
    assert (january.earned, january.spent, january.saved, january.net) == (
        Money(500_000, "EUR"),
        Money(120_000, "EUR"),
        Money(30_000, "EUR"),
        Money(350_000, "EUR"),
    )
    assert amounts[1].empty and amounts[2].empty


def test_periods_have_no_gaps_and_are_in_calendar_order() -> None:
    amounts = series([], start=date(2025, 11, 20), end=date(2026, 2, 3))
    assert [period.period for period in amounts] == [
        YearMonth(2025, 11),
        YearMonth(2025, 12),
        YearMonth(2026, 1),
        YearMonth(2026, 2),
    ]
    assert all(period.empty and period.net.amount_minor == 0 for period in amounts)


def test_a_year_step_sums_the_calendar_years() -> None:
    entries = [
        entry(EARNED, date(2024, 12, 31), 10_000),
        entry(SPENT, date(2025, 1, 1), 2_000),
        entry(SAVED, date(2025, 12, 31), 3_000),
        entry(EARNED, date(2026, 1, 1), 5_000),
    ]
    amounts = series(entries, start=date(2024, 1, 1), end=date(2026, 12, 31), step=ReportStep.YEAR)
    assert [
        (period.period, period.earned.amount_minor, period.spent.amount_minor) for period in amounts
    ] == [
        (2024, 10_000, 0),
        (2025, 0, 2_000),
        (2026, 5_000, 0),
    ]
    assert amounts[1].saved == Money(3_000, "EUR")
    monthly = series(entries, start=date(2024, 1, 1), end=date(2026, 12, 31), step=ReportStep.MONTH)
    for index, year in enumerate((2024, 2025, 2026)):
        months = [period for period in monthly if period.period.year == year]  # type: ignore[union-attr]
        assert amounts[index].earned.amount_minor == sum(
            period.earned.amount_minor for period in months
        )
        assert amounts[index].spent.amount_minor == sum(
            period.spent.amount_minor for period in months
        )
        assert amounts[index].saved.amount_minor == sum(
            period.saved.amount_minor for period in months
        )


def test_entries_outside_the_range_and_in_another_currency_are_ignored() -> None:
    amounts = series(
        [
            entry(EARNED, date(2025, 12, 31), 1_000),
            entry(EARNED, date(2026, 1, 1), 2_000),
            entry(EARNED, date(2026, 1, 2), 4_000, "USD"),
            entry(EARNED, date(2026, 4, 1), 8_000),
        ]
    )
    assert amounts[0].earned == Money(2_000, "EUR")
    assert [period.earned.amount_minor for period in amounts] == [2_000, 0, 0]


def test_an_unknown_category_raises() -> None:
    with pytest.raises(ValueError):
        ledger_series(
            [LedgerEntry(date(2026, 1, 1), uuid4(), Money(1, "EUR"))],
            CATEGORIES,
            start=date(2026, 1, 1),
            end=date(2026, 1, 31),
            step=ReportStep.MONTH,
            currency="EUR",
        )


def test_totals_are_exact_sums_of_the_periods() -> None:
    amounts = series(
        [
            entry(EARNED, date(2026, 1, 5), 500_000),
            entry(SPENT, date(2026, 2, 5), 120_000),
            entry(SAVED, date(2026, 3, 5), 30_000),
        ]
    )
    total = totals_of(amounts, currency="EUR")
    assert (total.earned, total.spent, total.saved, total.net) == (
        Money(500_000, "EUR"),
        Money(120_000, "EUR"),
        Money(30_000, "EUR"),
        Money(350_000, "EUR"),
    )


def test_period_keys_are_empty_for_a_reversed_range() -> None:
    assert period_keys(date(2026, 2, 1), date(2026, 1, 1), ReportStep.MONTH) == ()
    assert series([], start=date(2026, 2, 1), end=date(2026, 1, 1)) == []


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


@given(entries=entries_strategy(), bounds=range_strategy())
def test_property_totals_equal_the_counted_entries(
    entries: list[LedgerEntry], bounds: tuple[date, date]
) -> None:
    start, end = bounds
    amounts = ledger_series(
        entries, CATEGORIES, start=start, end=end, step=ReportStep.MONTH, currency="EUR"
    )
    expected = {"earned": 0, "spent": 0, "saved": 0}
    for row in entries:
        if not start <= row.day <= end or row.amount.currency != "EUR":
            continue
        category = CATEGORIES[row.category_id]
        if (category.kind, category.treatment) == ("income", "regular"):
            expected["earned"] += row.amount.amount_minor
        elif (category.kind, category.treatment) == ("expense", "regular"):
            expected["spent"] += row.amount.amount_minor
        elif (category.kind, category.treatment) == ("expense", "savings"):
            expected["saved"] += row.amount.amount_minor
    assert sum(row.earned.amount_minor for row in amounts) == expected["earned"]
    assert sum(row.spent.amount_minor for row in amounts) == expected["spent"]
    assert sum(row.saved.amount_minor for row in amounts) == expected["saved"]
    for row in amounts:
        assert row.net.amount_minor == (
            row.earned.amount_minor - row.spent.amount_minor - row.saved.amount_minor
        )


@given(entries=entries_strategy(), bounds=range_strategy())
def test_property_every_period_appears_once(
    entries: list[LedgerEntry], bounds: tuple[date, date]
) -> None:
    start, end = bounds
    amounts = ledger_series(
        entries, CATEGORIES, start=start, end=end, step=ReportStep.MONTH, currency="EUR"
    )
    periods = [row.period for row in amounts]
    assert len(periods) == len(set(periods))
    assert periods == list(period_keys(start, end, ReportStep.MONTH))
    assert all(isinstance(period, YearMonth) for period in periods)


@given(entries=entries_strategy())
def test_property_transfers_and_withdrawals_never_change_a_figure(
    entries: list[LedgerEntry],
) -> None:
    start, end = date(2024, 1, 1), date(2026, 12, 31)
    base = ledger_series(
        entries, CATEGORIES, start=start, end=end, step=ReportStep.MONTH, currency="EUR"
    )
    extra = [
        entry(EXPENSE_TRANSFER, date(2025, 5, 5), 999_999),
        entry(INCOME_TRANSFER, date(2025, 5, 6), 888_888),
        entry(WITHDRAWN, date(2025, 5, 7), 777_777),
    ]
    with_extra = ledger_series(
        [*entries, *extra], CATEGORIES, start=start, end=end, step=ReportStep.MONTH, currency="EUR"
    )
    assert with_extra == base


def test_series_is_stable_for_duplicate_categories_of_the_same_kind() -> None:
    other_expense = ReportCategory(uuid4(), "expense", "regular")
    known = {**CATEGORIES, other_expense.id: other_expense}
    amounts = ledger_series(
        [
            entry(SPENT, date(2026, 1, 1), 1_000),
            entry(other_expense, date(2026, 1, 1), 2_000),
        ],
        known,
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
        step=ReportStep.MONTH,
        currency="EUR",
    )
    assert amounts[0].spent == Money(3_000, "EUR")


def test_uuid_keys_are_not_confused_with_other_categories() -> None:
    # Guards against accidentally keying the report by name or position instead of id.
    known: dict[UUID, ReportCategory] = {SPENT.id: SPENT}
    with pytest.raises(ValueError):
        ledger_series(
            [entry(SAVED, date(2026, 1, 1), 1_000)],
            known,
            start=date(2026, 1, 1),
            end=date(2026, 1, 31),
            step=ReportStep.MONTH,
            currency="EUR",
        )
