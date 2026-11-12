from __future__ import annotations

from datetime import date

import pytest

from assetboard.domain.loans import (
    LoanValidationError,
    RatePeriod,
    ScheduleError,
    shift_rate_timeline,
)

TIMELINE = [
    RatePeriod(date(2026, 1, 1), 500),
    RatePeriod(date(2027, 1, 1), 600),
]


def _rates(periods: list[RatePeriod]) -> list[tuple[date, int]]:
    return [(period.effective_from, period.annual_rate_bp) for period in periods]


def test_shift_before_the_first_period_shifts_everything() -> None:
    shifted = shift_rate_timeline(TIMELINE, 150, date(2025, 6, 1))
    assert _rates(shifted) == [
        (date(2026, 1, 1), 650),
        (date(2027, 1, 1), 750),
    ]


def test_shift_exactly_on_a_period_shifts_that_period_and_later_ones() -> None:
    shifted = shift_rate_timeline(TIMELINE, -100, date(2027, 1, 1))
    assert _rates(shifted) == [
        (date(2026, 1, 1), 500),
        (date(2027, 1, 1), 500),
    ]


def test_shift_between_periods_inserts_the_date_with_the_rate_then_in_force() -> None:
    shifted = shift_rate_timeline(TIMELINE, 150, date(2026, 7, 1))
    assert _rates(shifted) == [
        (date(2026, 1, 1), 500),
        (date(2026, 7, 1), 650),
        (date(2027, 1, 1), 750),
    ]


def test_shift_after_the_last_period_appends_one() -> None:
    shifted = shift_rate_timeline(TIMELINE, 150, date(2028, 3, 1))
    assert _rates(shifted) == [
        (date(2026, 1, 1), 500),
        (date(2027, 1, 1), 600),
        (date(2028, 3, 1), 750),
    ]


def test_shift_on_an_exact_period_does_not_duplicate_it() -> None:
    shifted = shift_rate_timeline(
        [RatePeriod(date(2026, 1, 1), 500), RatePeriod(date(2026, 6, 1), 600)],
        100,
        date(2026, 6, 1),
    )
    assert _rates(shifted) == [
        (date(2026, 1, 1), 500),
        (date(2026, 6, 1), 700),
    ]


def test_negative_and_zero_shifts() -> None:
    assert _rates(shift_rate_timeline(TIMELINE, -400, date(2026, 1, 1)))[0] == (
        date(2026, 1, 1),
        100,
    )
    assert _rates(shift_rate_timeline(TIMELINE, 0, date(2026, 7, 1))) == _rates(TIMELINE)


@pytest.mark.parametrize("shift", [10_000, -600])
def test_a_shift_outside_the_valid_rate_range_is_refused(shift: int) -> None:
    with pytest.raises(LoanValidationError) as raised:
        shift_rate_timeline(TIMELINE, shift, date(2026, 7, 1))
    assert raised.value.code == "invalid_rate_shift"


def test_a_shift_larger_than_the_rate_range_is_refused_even_if_a_rate_would_stay_valid() -> None:
    with pytest.raises(LoanValidationError) as raised:
        shift_rate_timeline([RatePeriod(date(2026, 1, 1), 9000)], -15000, date(2026, 2, 1))
    assert raised.value.code == "invalid_rate_shift"


def test_a_missing_timeline_is_an_error() -> None:
    with pytest.raises(ScheduleError) as raised:
        shift_rate_timeline([], 100, date(2026, 7, 1))
    assert raised.value.code == "missing_rate"


def test_duplicate_and_invalid_dates_are_refused() -> None:
    with pytest.raises(ScheduleError) as raised:
        shift_rate_timeline(
            [RatePeriod(date(2026, 1, 1), 500), RatePeriod(date(2026, 1, 1), 600)],
            100,
            date(2026, 7, 1),
        )
    assert raised.value.code == "duplicate_rate"

    with pytest.raises(LoanValidationError):
        shift_rate_timeline([RatePeriod(date(2026, 1, 1), 500)], 100, date(1700, 1, 1))


def test_a_non_integer_shift_is_refused() -> None:
    with pytest.raises(LoanValidationError) as raised:
        shift_rate_timeline(TIMELINE, 1.5, date(2026, 7, 1))  # type: ignore[arg-type]
    assert raised.value.code == "invalid_rate_shift"
