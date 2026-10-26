"""Differential tests: ``domain.recurrence`` against ``dateutil.rrule`` as an independent oracle.

The oracle expresses every F3.1 rule in RFC 5545 terms: weekly rules with ``wkst=MO``, monthly
and yearly day clamping as "the last existing day among 28..day" (``bysetpos=-1``), last day as
``bymonthday=-1`` and first/last working day (weekends only) as weekdays with ``bysetpos=±1``.
``count`` and ``until`` are the rule's maximum count and end date.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from dateutil import rrule as rr
from hypothesis import given, settings
from hypothesis import strategies as st

from assetboard.domain.recurrence import (
    Frequency,
    MonthDay,
    RecurrenceRule,
    WeekendPolicy,
    occurrences,
    shift,
)

WINDOW_YEARS = 6
_RR_FREQ = {Frequency.WEEKLY: rr.WEEKLY, Frequency.MONTHLY: rr.MONTHLY, Frequency.YEARLY: rr.YEARLY}
_WEEKDAYS = (rr.MO, rr.TU, rr.WE, rr.TH, rr.FR, rr.SA, rr.SU)


def _oracle(rule: RecurrenceRule, first: date, last: date) -> list[date]:
    if rule.freq == Frequency.ONCE:
        # One time: a single daily occurrence on the start date; the repeat fields do not apply.
        only = rr.rrule(rr.DAILY, dtstart=rule.start_date, count=1)[0].date()
        return [only] if first <= only <= last else []
    options: dict[str, object] = {
        "dtstart": datetime.combine(rule.start_date, datetime.min.time()),
        "interval": rule.interval,
        "wkst": rr.MO,
    }
    if rule.max_count is not None:
        options["count"] = rule.max_count
    if rule.end_date is not None:
        options["until"] = datetime.combine(rule.end_date, datetime.min.time())
    if rule.freq == Frequency.WEEKLY:
        options["byweekday"] = [_WEEKDAYS[day] for day in rule.weekdays]
    elif rule.month_day == -1:
        options["bymonthday"] = -1
    elif isinstance(rule.month_day, MonthDay):
        options["byweekday"] = _WEEKDAYS[:5]
        options["bysetpos"] = 1 if rule.month_day == MonthDay.FIRST_WORKING else -1
    else:
        day = rule.month_day
        options["bymonthday"] = list(range(28, day + 1)) if day >= 28 else [day]
        options["bysetpos"] = -1
    if rule.freq == Frequency.YEARLY:
        options["bymonth"] = rule.month
    result: list[date] = []
    for moment in rr.rrule(_RR_FREQ[rule.freq], **options):  # type: ignore[arg-type]
        day = moment.date()
        if day > last:
            break
        if day >= first:
            result.append(day)
    return result


@st.composite
def rules(draw: st.DrawFn) -> RecurrenceRule:
    freq = draw(st.sampled_from(list(Frequency)))
    start = draw(st.dates(date(2020, 1, 1), date(2030, 12, 31)))
    month = draw(st.integers(1, 12))
    if freq == Frequency.WEEKLY:
        weekdays = tuple(draw(st.sets(st.integers(0, 6), min_size=1, max_size=7)))
        month_day: int | MonthDay = 1
    else:
        weekdays = ()
        choices: list[int | MonthDay] = [*range(1, 32), -1]
        if freq == Frequency.MONTHLY:
            choices += list(MonthDay)
        else:
            longest = 29 if month == 2 else 30 if month in (4, 6, 9, 11) else 31
            choices = list(range(1, longest + 1))
        month_day = draw(st.sampled_from(choices))
    limit = draw(st.sampled_from(["none", "count", "until"]))
    return RecurrenceRule(
        start,
        freq,
        interval=draw(st.integers(1, 5)),
        weekdays=weekdays,
        month_day=month_day,
        month=month,
        max_count=draw(st.integers(1, 40)) if limit == "count" else None,
        end_date=(
            start + timedelta(days=draw(st.integers(0, 4 * 366))) if limit == "until" else None
        ),
    )


@settings(max_examples=300, deadline=None)
@given(rule=rules(), offset=st.integers(-60, 400))
def test_occurrences_match_dateutil_rrule(rule: RecurrenceRule, offset: int) -> None:
    first = rule.start_date + timedelta(days=offset)
    last = first + timedelta(days=366 * WINDOW_YEARS)
    assert occurrences(rule, first, last, frozenset()) == _oracle(rule, first, last)


def test_oracle_examples_cover_clamping_and_week_start() -> None:
    weekly = RecurrenceRule(date(2026, 3, 4), Frequency.WEEKLY, interval=2, weekdays=(0, 4))
    yearly = RecurrenceRule(date(2023, 1, 1), Frequency.YEARLY, month=2, month_day=29)
    monthly = RecurrenceRule(date(2024, 1, 31), month_day=31, max_count=4)
    for rule in (weekly, yearly, monthly):
        first, last = rule.start_date, rule.start_date + timedelta(days=3 * 366)
        assert occurrences(rule, first, last, frozenset()) == _oracle(rule, first, last)
    first = date(2024, 1, 1)
    assert _oracle(monthly, first, date(2024, 12, 31))[1] == date(2024, 2, 29)


holiday_sets = st.frozensets(st.dates(date(2025, 12, 1), date(2027, 2, 28)), max_size=60)


@given(
    days=st.lists(st.dates(date(2026, 1, 1), date(2026, 12, 31)), min_size=2, max_size=2),
    policy=st.sampled_from(list(WeekendPolicy)),
    holidays=holiday_sets,
)
def test_working_day_shift_is_monotone(days, policy, holidays) -> None:
    earlier, later = sorted(days)
    assert shift(earlier, policy, holidays) <= shift(later, policy, holidays)
    moved = shift(earlier, policy, holidays)
    if policy != WeekendPolicy.NONE:
        assert moved.weekday() < 5 and moved not in holidays


@given(
    rule=rules(),
    policy=st.sampled_from(list(WeekendPolicy)),
    holidays=holiday_sets,
    count=st.integers(1, 30),
)
def test_shifted_and_deduplicated_dates_never_exceed_max_count(
    rule: RecurrenceRule, policy: WeekendPolicy, holidays: frozenset[date], count: int
) -> None:
    limited = RecurrenceRule(
        rule.start_date,
        rule.freq,
        interval=rule.interval,
        weekdays=rule.weekdays,
        month_day=rule.month_day,
        month=rule.month,
        end_date=rule.end_date,
        max_count=count,
        weekend_policy=policy,
    )
    first = rule.start_date - timedelta(days=30)
    actual = occurrences(limited, first, first + timedelta(days=366 * 4), holidays)
    assert len(actual) <= count
    assert actual == sorted(set(actual))
