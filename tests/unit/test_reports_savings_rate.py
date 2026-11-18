"""The report savings rate (F10.2): F2.4's golden month, yearly sums, rolling windows, gaps
and rounding properties. Every amount is synthetic."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from assetboard.domain.money import Money
from assetboard.domain.reports import (
    LedgerEntry,
    PeriodTotals,
    ReportCategory,
    ReportStep,
    ledger_series,
    rolling_savings_rates,
    savings_rate,
)
from assetboard.i18n.numbers import format_rate_tenths

EARNED = ReportCategory(uuid4(), "income", "regular")
SPENT = ReportCategory(uuid4(), "expense", "regular")
CATEGORIES = {EARNED.id: EARNED, SPENT.id: SPENT}


def entry(category: ReportCategory, day: date, minor: int) -> LedgerEntry:
    return LedgerEntry(day, category.id, Money(minor, "EUR"))


def series(
    entries: list[LedgerEntry],
    *,
    start: date = date(2026, 1, 1),
    end: date = date(2026, 12, 31),
    step: ReportStep = ReportStep.MONTH,
) -> list[PeriodTotals]:
    return ledger_series(entries, CATEGORIES, start=start, end=end, step=step, currency="EUR")


def test_the_f2_4_golden_month_is_minus_43_3_percent() -> None:
    # The F2.4 golden example: earned 3 400.00, spent 550.00 + 4 323.00 = 4 873.00.
    monthly = series(
        [
            entry(EARNED, date(2026, 1, 5), 340_000),
            entry(SPENT, date(2026, 1, 7), 55_000 + 432_300),
        ],
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
    )
    rate = monthly[0].savings_rate
    assert rate.bp == -4332  # -43.32 %
    assert rate.tenths == -433
    assert format_rate_tenths(-433, "en") == "-43.3%"


def test_a_yearly_rate_uses_the_year_sums_not_the_monthly_average() -> None:
    entries = [
        entry(EARNED, date(2026, 1, 10), 400_000),
        entry(SPENT, date(2026, 1, 11), 200_000),
        entry(EARNED, date(2026, 2, 10), 100_000),
        entry(SPENT, date(2026, 2, 11), 100_000),
    ]
    monthly = series(entries, start=date(2026, 1, 1), end=date(2026, 2, 28))
    assert [period.savings_rate.tenths for period in monthly] == [500, 0]
    yearly = series(entries, start=date(2026, 1, 1), end=date(2026, 12, 31), step=ReportStep.YEAR)
    assert len(yearly) == 1
    assert (yearly[0].earned.amount_minor, yearly[0].spent.amount_minor) == (500_000, 300_000)
    # (5 000.00 - 3 000.00) / 5 000.00 = 40.0 %, not the 25.0 % average of 50 % and 0 %.
    assert yearly[0].savings_rate.bp == 4_000
    assert yearly[0].savings_rate.tenths == 400
    assert format_rate_tenths(yearly[0].savings_rate.tenths or 0, "en") == "40.0%"


@pytest.mark.parametrize(
    ("earned", "spent", "bp", "tenths"),
    [
        (0, 0, None, None),
        (0, 1_000, None, None),
        (0, -1_000, None, None),
        (1_000, 0, 10_000, 1_000),
        (1_000, 1_000, 0, 0),
        (1_000, 2_000, -10_000, -1_000),
        (3_400_000, 4_873_000, -4_332, -433),
        (2_000, 2_001, -5, -1),  # -0.05 %: -0.5 tenths rounds away from zero
        (2_000, 1_999, 5, 1),  # +0.05 %
        # 12.3495 %: basis points round to 1 235, but the display must be 12.3 %, not 12.4 %.
        (200_000, 175_301, 1_235, 123),
        (200_000, 224_699, -1_235, -123),
    ],
)
def test_rates_are_rounded_once_from_the_exact_ratio(
    earned: int, spent: int, bp: int | None, tenths: int | None
) -> None:
    rate = savings_rate(earned, spent)
    assert (rate.bp, rate.tenths) == (bp, tenths)
    assert rate.available is (bp is not None)


def test_a_period_without_earnings_has_a_gap_not_a_zero() -> None:
    monthly = series(
        [
            entry(SPENT, date(2026, 1, 10), 10_000),
            entry(EARNED, date(2026, 2, 10), 20_000),
            entry(SPENT, date(2026, 2, 11), 10_000),
        ],
        start=date(2026, 1, 1),
        end=date(2026, 2, 28),
    )
    assert monthly[0].savings_rate.bp is None
    assert monthly[0].savings_rate.tenths is None
    assert monthly[1].savings_rate.tenths == 500


def test_the_rolling_rate_uses_the_window_sums_not_averaged_rates() -> None:
    # January earns 9 000.00 and spends 9 000.00 (0 %); February earns 1 000.00 and spends
    # nothing (100 %); March to December are empty. The 12-month window from January therefore
    # earns 10 000.00, spends 9 000.00 and has a 10.0 % rate, not the 50 % average of the two
    # rated months and not 8.3 % (the average including zeros). Month 13 drops January.
    entries = [
        entry(EARNED, date(2025, 1, 10), 900_000),
        entry(SPENT, date(2025, 1, 11), 900_000),
        entry(EARNED, date(2025, 2, 10), 100_000),
        entry(EARNED, date(2026, 1, 10), 100_000),
    ]
    monthly = series(entries, start=date(2025, 1, 1), end=date(2026, 1, 31))
    assert [period.savings_rate.tenths for period in monthly[:3]] == [0, 1_000, None]
    rolling = rolling_savings_rates(monthly)
    assert len(rolling) == 13
    assert rolling[:11] == (None,) * 11
    assert rolling[11] is not None and rolling[11].tenths == 100  # 10.0 %
    assert rolling[12] is not None and rolling[12].tenths == 1_000  # February to January
    with pytest.raises(ValueError):
        rolling_savings_rates(monthly, months=0)


@given(
    st.integers(min_value=1, max_value=10**12),
    st.integers(min_value=-(10**12), max_value=10**12),
)
def test_a_rate_is_within_half_a_unit_of_the_exact_ratio(earned: int, spent: int) -> None:
    rate = savings_rate(earned, spent)
    kept = earned - spent
    assert rate.bp is not None and rate.tenths is not None
    for rounded, scale in ((rate.bp, 10_000), (rate.tenths, 1_000)):
        assert abs(rounded * earned - kept * scale) * 2 <= earned


@given(
    st.integers(min_value=1, max_value=10**9),
    st.integers(min_value=-(10**9), max_value=10**9),
    st.integers(min_value=0, max_value=10**9),
)
def test_more_spending_never_raises_the_rate(earned: int, spent: int, extra: int) -> None:
    lower = savings_rate(earned, spent)
    higher = savings_rate(earned, spent + extra)
    assert lower.bp is not None and higher.bp is not None
    assert lower.tenths is not None and higher.tenths is not None
    assert higher.bp <= lower.bp
    assert higher.tenths <= lower.tenths


PAIR = st.tuples(
    st.integers(min_value=0, max_value=10**7), st.integers(min_value=0, max_value=10**7)
)


@given(st.lists(PAIR, min_size=1, max_size=12))
def test_a_yearly_rate_is_the_rate_of_the_year_sums(months: list[tuple[int, int]]) -> None:
    entries = []
    for index, (earned, spent) in enumerate(months, start=1):
        if earned:
            entries.append(entry(EARNED, date(2026, index, 5), earned))
        if spent:
            entries.append(entry(SPENT, date(2026, index, 6), spent))
    monthly = series(entries)
    yearly = series(entries, step=ReportStep.YEAR)
    expected = savings_rate(
        sum(period.earned.amount_minor for period in monthly),
        sum(period.spent.amount_minor for period in monthly),
    )
    assert yearly[0].savings_rate == expected


@given(st.lists(PAIR, min_size=12, max_size=24))
def test_every_rolling_point_is_the_rate_of_its_window_sums(
    months: list[tuple[int, int]],
) -> None:
    entries = []
    for index, (earned, spent) in enumerate(months, start=1):
        year, month = divmod(index - 1, 12)
        day = date(2025 + year, month + 1, 5)
        if earned:
            entries.append(entry(EARNED, day, earned))
        if spent:
            entries.append(entry(SPENT, day, spent))
    monthly = series(
        entries,
        start=date(2025, 1, 1),
        end=date(2025 + (len(months) - 1) // 12, 12, 31),
    )
    rolling = rolling_savings_rates(monthly)
    for index, rate in enumerate(rolling):
        if index < 11:
            assert rate is None
            continue
        window = monthly[index - 11 : index + 1]
        expected = savings_rate(
            sum(period.earned.amount_minor for period in window),
            sum(period.spent.amount_minor for period in window),
        )
        assert rate == expected
