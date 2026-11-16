"""Net-worth history domain (F5.9): month ends, ranges, clipping, values and breakdowns.

The independent oracle for the date examples is the hand-written calendar below: month lengths
were checked against the Gregorian calendar (2024 is a leap year), not produced by the function
under test.
"""

from __future__ import annotations

import calendar
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext
from fractions import Fraction
from uuid import UUID, uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from assetboard.domain.money import Money
from assetboard.domain.networth import Side, Unavailable, ValuationPoint
from assetboard.domain.networth_history import (
    DEFAULT_HISTORY_RANGE,
    HISTORY_RANGES,
    BreakdownCategory,
    ItemHistory,
    ItemValue,
    breakdown_for,
    history_dates,
    history_start,
    linked_for_date,
    month_end,
    month_ends,
    normalize_history_range,
    previous_month_end,
    summarize_values,
    value_on,
    values_at,
)


def _point(as_of: date, value_minor: int = 100) -> ValuationPoint:
    return ValuationPoint(uuid4(), as_of, datetime(2026, 1, 1, tzinfo=UTC), value_minor)


def _item(
    *,
    side: Side = Side.ASSET,
    archived_at: datetime | None = None,
    include: bool = True,
    valuations: tuple[ValuationPoint, ...] = (),
    currency: str = "EUR",
    valuation_mode: str = "manual",
    link_type: str | None = None,
    link_id: UUID | None = None,
    estimate_between: bool = False,
    created_at: datetime | None = None,
) -> ItemHistory:
    return ItemHistory(
        id=uuid4(),
        side=side,
        category_id=uuid4(),
        currency=currency,
        include_in_net_worth=include,
        archived_at=archived_at,
        valuations=valuations,
        valuation_mode=valuation_mode,
        link_type=link_type,
        link_id=link_id,
        estimate_between=estimate_between,
        created_at=created_at,
    )


def _value(
    *,
    value_minor: int | None = 100,
    currency: str = "EUR",
    side: Side = Side.ASSET,
    include: bool = True,
    archived: bool = False,
    unavailable: bool = False,
    estimated: bool = False,
) -> ItemValue:
    return ItemValue(
        id=uuid4(),
        side=side,
        category_id=uuid4(),
        currency=currency,
        include_in_net_worth=include,
        archived_at=None,
        archived=archived,
        value_minor=value_minor,
        value_as_of=date(2026, 1, 31) if value_minor is not None else None,
        unavailable=unavailable,
        estimated=estimated,
    )


@pytest.mark.parametrize(
    ("day", "expected_end", "expected_previous"),
    [
        (date(2026, 1, 15), date(2026, 1, 31), date(2025, 12, 31)),
        (date(2026, 2, 1), date(2026, 2, 28), date(2026, 1, 31)),
        (date(2024, 2, 29), date(2024, 2, 29), date(2024, 1, 31)),
        (date(2026, 3, 1), date(2026, 3, 31), date(2026, 2, 28)),
        (date(2026, 12, 31), date(2026, 12, 31), date(2026, 11, 30)),
    ],
)
def test_month_end_and_previous_month_end(
    day: date, expected_end: date, expected_previous: date
) -> None:
    assert month_end(day) == expected_end
    assert previous_month_end(day) == expected_previous


def test_month_ends_are_inclusive_and_oldest_first() -> None:
    assert month_ends(date(2024, 1, 5), date(2024, 3, 1)) == (
        date(2024, 1, 31),
        date(2024, 2, 29),
        date(2024, 3, 31),
    )


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("1y", date(2025, 3, 1)),
        ("3y", date(2023, 3, 1)),
        ("5y", date(2021, 3, 1)),
        ("all", None),
        ("unknown", date(2025, 3, 1)),
    ],
)
def test_history_start(code: str, expected: date | None) -> None:
    assert normalize_history_range(code) == (code if code != "unknown" else DEFAULT_HISTORY_RANGE)
    assert history_start(date(2026, 3, 1), code) == expected


def test_history_start_handles_29_february() -> None:
    assert history_start(date(2024, 2, 29), "1y") == date(2023, 2, 28)
    assert history_start(date(2028, 2, 29), "3y") == date(2025, 2, 28)


def test_history_dates_fixed_ranges_include_every_completed_month_end_and_today() -> None:
    today = date(2026, 11, 16)
    earliest = date(2020, 1, 1)  # before every window: no clipping
    expected = {
        "1y": (date(2025, 11, 30), 13),
        "3y": (date(2023, 11, 30), 37),
        "5y": (date(2021, 11, 30), 61),
    }
    for code, (first, count) in expected.items():
        dates = history_dates(today, code, earliest=earliest)
        assert len(dates) == count
        assert (dates[0], dates[-1]) == (first, today)
        assert all(day == month_end(day) for day in dates[:-1])


def test_history_dates_fixed_ranges_clip_to_the_earliest_recorded_value() -> None:
    dates = history_dates(date(2026, 11, 16), "1y", earliest=date(2026, 9, 15))
    assert dates == (date(2026, 9, 30), date(2026, 10, 31), date(2026, 11, 16))


def test_history_dates_all_starts_at_the_earliest_recorded_value() -> None:
    dates = history_dates(date(2026, 11, 16), "all", earliest=date(2024, 1, 15))
    assert dates[0] == date(2024, 1, 31)
    assert dates[-1] == date(2026, 11, 16)
    # January 2024 through October 2026 is 34 completed month-ends.
    assert len(dates) == 35


def test_history_dates_no_data_returns_no_points() -> None:
    assert history_dates(date(2026, 11, 16), "1y", earliest=None) == ()
    assert history_dates(date(2026, 11, 16), "all", earliest=None) == ()


@pytest.mark.parametrize(
    ("today", "expected_last_month_end"),
    [
        (date(2026, 11, 30), date(2026, 10, 31)),
        (date(2026, 12, 31), date(2026, 11, 30)),
        (date(2026, 1, 1), date(2025, 12, 31)),
    ],
)
def test_history_dates_never_duplicates_a_month_end_today(
    today: date, expected_last_month_end: date
) -> None:
    dates = history_dates(today, "1y", earliest=today - timedelta(days=400))
    assert dates[-2:] == (expected_last_month_end, today)
    assert len(set(dates)) == len(dates)


def test_history_dates_clips_inside_the_current_month_to_today_only() -> None:
    assert history_dates(date(2026, 11, 16), "1y", earliest=date(2026, 11, 10)) == (
        date(2026, 11, 16),
    )


def test_history_dates_normalises_unknown_ranges_and_handles_leap_day_starts() -> None:
    expected = history_dates(date(2024, 2, 29), "1y", earliest=date(2020, 1, 1))
    assert history_dates(date(2024, 2, 29), "unknown", earliest=date(2020, 1, 1)) == expected
    assert expected[0] == date(2023, 2, 28)
    assert expected[-1] == date(2024, 2, 29)


@given(
    today=st.dates(min_value=date(2000, 1, 1), max_value=date(2099, 12, 31)),
    age=st.integers(min_value=0, max_value=2000),
    code=st.sampled_from(HISTORY_RANGES),
)
def test_property_dates_are_ordered_month_ends_with_today_last(
    today: date, age: int, code: str
) -> None:
    earliest = max(date(1900, 1, 1), today - timedelta(days=age))
    dates = history_dates(today, code, earliest=earliest)
    assert dates[-1] == today
    assert dates == tuple(sorted(set(dates)))
    assert all(day <= today for day in dates)
    assert all(day == month_end(day) for day in dates[:-1])
    start = history_start(today, code)
    lower = earliest if start is None else max(start, earliest)
    assert dates[0] >= lower


def test_linked_for_date_returns_only_linked_items() -> None:
    first = _item(valuation_mode="linked", link_type="portfolio_asset_class", link_id=uuid4())
    second = _item(valuation_mode="linked", link_type="portfolio_asset_class", link_id=uuid4())
    manual = _item()
    resolved = {
        (first.id, date(2026, 1, 31)): Money(500, "EUR"),
        (second.id, date(2026, 1, 31)): Unavailable(),
    }
    linked = linked_for_date(resolved, (first, second, manual), date(2026, 1, 31))
    assert linked == {first.id: Money(500, "EUR"), second.id: Unavailable()}
    missing = linked_for_date({}, (first,), date(2026, 2, 28))
    assert missing == {first.id: Unavailable()}


def test_summarize_values_counts_only_included_base_estimates() -> None:
    values = (
        _value(value_minor=10_000, estimated=True),
        _value(value_minor=5_000, include=False, estimated=True),
        _value(value_minor=None),
        _value(value_minor=7_000, archived=True, estimated=True),
        _value(value_minor=2_000, currency="USD", estimated=True),
        _value(value_minor=None, unavailable=True),
    )
    summary, estimated = summarize_values(values, currency="EUR")
    assert summary.assets == Money(10_000, "EUR")
    assert summary.liabilities == Money(0, "EUR")
    assert summary.net == Money(10_000, "EUR")
    assert estimated == 1
    assert summary.non_base_item_ids == (values[4].id,)
    assert summary.no_value_item_ids == (values[2].id,)
    assert summary.excluded_item_ids == (values[1].id,)
    assert summary.unavailable_item_ids == (values[5].id,)


def test_breakdown_for_matches_the_category_and_not_counted_shape() -> None:
    category_id = uuid4()
    other_id = uuid4()
    values = (
        _value(value_minor=1_200, estimated=True),
        _value(value_minor=200, side=Side.LIABILITY),
        _value(value_minor=100, currency="USD"),
        _value(value_minor=None),
        _value(value_minor=None, unavailable=True),
    )
    # Give the first two values their own categories so the breakdown has two rows.
    values = (
        ItemValue(
            id=values[0].id,
            side=Side.ASSET,
            category_id=category_id,
            currency="EUR",
            include_in_net_worth=True,
            archived_at=None,
            archived=False,
            value_minor=1_200,
            value_as_of=date(2026, 1, 31),
            estimated=True,
        ),
        ItemValue(
            id=values[1].id,
            side=Side.LIABILITY,
            category_id=other_id,
            currency="EUR",
            include_in_net_worth=True,
            archived_at=None,
            archived=False,
            value_minor=200,
            value_as_of=date(2026, 1, 31),
        ),
        *values[2:],
    )
    summary, estimated = summarize_values(values, currency="EUR")
    breakdown = breakdown_for(summary, estimated=estimated)
    assert breakdown.categories == (
        BreakdownCategory(category_id, Side.ASSET, 1_200),
        BreakdownCategory(other_id, Side.LIABILITY, 200),
    )
    assert (breakdown.non_base, breakdown.no_value, breakdown.unavailable, breakdown.estimated) == (
        1,
        1,
        1,
        1,
    )


def test_values_at_ignores_a_valuation_after_the_date() -> None:
    item = _item(
        valuations=(
            _point(date(2026, 1, 1), 100),
            _point(date(2026, 3, 1), 200),
        )
    )
    january = values_at([item], date(2026, 1, 31), timezone="UTC")[0]
    march = values_at([item], date(2026, 3, 1), timezone="UTC")[0]
    assert (january.value_minor, january.value_as_of) == (100, date(2026, 1, 1))
    assert (march.value_minor, march.value_as_of) == (200, date(2026, 3, 1))


def test_linked_values_come_from_the_service_resolution_not_manual_history() -> None:
    linked = _item(
        valuation_mode="linked",
        link_type="portfolio_asset_class",
        link_id=uuid4(),
        valuations=(_point(date(2026, 1, 1), 100),),
    )
    resolved = values_at(
        [linked],
        date(2026, 1, 31),
        timezone="UTC",
        linked={linked.id: Money(500, "EUR")},
    )[0]
    unavailable = values_at(
        [linked],
        date(2026, 2, 28),
        timezone="UTC",
        linked={linked.id: Unavailable()},
    )[0]
    missing = values_at([linked], date(2026, 3, 31), timezone="UTC")[0]
    assert (resolved.value_minor, resolved.value_as_of, resolved.unavailable) == (
        500,
        date(2026, 1, 31),
        False,
    )
    assert (unavailable.value_minor, unavailable.unavailable) == (None, True)
    assert (missing.value_minor, missing.unavailable) == (None, True)


def test_an_item_is_archived_from_its_workspace_local_archive_date() -> None:
    archived = datetime(2026, 6, 1, 0, 30, tzinfo=UTC)
    item = _item(archived_at=archived, valuations=(_point(date(2026, 1, 1), 100),))
    may_utc = values_at([item], date(2026, 5, 31), timezone="UTC")[0]
    may_utc_negative = values_at([item], date(2026, 5, 31), timezone="America/New_York")[0]
    june = values_at([item], date(2026, 6, 30), timezone="UTC")[0]
    # 2026-06-01T00:30Z is still 31 May in New York: the item is already archived there.
    assert not may_utc.archived
    assert may_utc_negative.archived
    assert june.archived


def test_a_restored_item_counts_at_every_date() -> None:
    item = _item(archived_at=None, valuations=(_point(date(2026, 1, 1), 100),))
    assert all(not value.archived for value in values_at([item], date(2026, 1, 31), timezone="UTC"))
    assert all(not value.archived for value in values_at([item], date(2026, 6, 30), timezone="UTC"))


@given(st.dates(min_value=date(1900, 1, 1), max_value=date(2199, 12, 31)))
def test_property_every_month_end_is_the_last_day_and_ranges_are_inclusive(day: date) -> None:
    end = month_end(day)
    assert end.month == day.month and end.year == day.year
    assert month_ends(day, day) == (end,)
    assert month_end(end) == end


# Hand checks from the 1,461-day span (2022-11-03 to 2026-11-03):
# 2022-11-30 is day 27: 10,000,000 - 4,000,000 * 27 / 1461 = 9,926,078.
# 2023-11-30 is day 392: 10,000,000 - 4,000,000 * 392 / 1461 = 8,926,762.
# 2026-10-31 is day 1458: 10,000,000 - 4,000,000 * 1458 / 1461 = 6,008,214.
ESTIMATE_GOLDEN = (
    (date(2022, 11, 30), 9926078),
    (date(2022, 12, 31), 9841205),
    (date(2023, 1, 31), 9756331),
    (date(2023, 2, 28), 9679671),
    (date(2023, 3, 31), 9594798),
    (date(2023, 4, 30), 9512663),
    (date(2023, 5, 31), 9427789),
    (date(2023, 6, 30), 9345654),
    (date(2023, 7, 31), 9260780),
    (date(2023, 8, 31), 9175907),
    (date(2023, 9, 30), 9093771),
    (date(2023, 10, 31), 9008898),
    (date(2023, 11, 30), 8926762),
    (date(2023, 12, 31), 8841889),
    (date(2024, 1, 31), 8757016),
    (date(2024, 2, 29), 8677618),
    (date(2024, 3, 31), 8592745),
    (date(2024, 4, 30), 8510609),
    (date(2024, 5, 31), 8425736),
    (date(2024, 6, 30), 8343600),
    (date(2024, 7, 31), 8258727),
    (date(2024, 8, 31), 8173854),
    (date(2024, 9, 30), 8091718),
    (date(2024, 10, 31), 8006845),
    (date(2024, 11, 30), 7924709),
    (date(2024, 12, 31), 7839836),
    (date(2025, 1, 31), 7754962),
    (date(2025, 2, 28), 7678303),
    (date(2025, 3, 31), 7593429),
    (date(2025, 4, 30), 7511294),
    (date(2025, 5, 31), 7426420),
    (date(2025, 6, 30), 7344285),
    (date(2025, 7, 31), 7259411),
    (date(2025, 8, 31), 7174538),
    (date(2025, 9, 30), 7092402),
    (date(2025, 10, 31), 7007529),
    (date(2025, 11, 30), 6925394),
    (date(2025, 12, 31), 6840520),
    (date(2026, 1, 31), 6755647),
    (date(2026, 2, 28), 6678987),
    (date(2026, 3, 31), 6594114),
    (date(2026, 4, 30), 6511978),
    (date(2026, 5, 31), 6427105),
    (date(2026, 6, 30), 6344969),
    (date(2026, 7, 31), 6260096),
    (date(2026, 8, 31), 6175222),
    (date(2026, 9, 30), 6093087),
    (date(2026, 10, 31), 6008214),
)


def _dated_point(day: date, value_minor: int, *, hour: int = 0) -> ValuationPoint:
    return ValuationPoint(uuid4(), day, datetime(2020, 1, 1, hour, tzinfo=UTC), value_minor)


def _fraction_value(v0: int, v1: int, d0: date, d1: date, day: date) -> int:
    exact = Fraction(
        v0 * (d1 - day).days + v1 * (day - d0).days,
        (d1 - d0).days,
    )
    with localcontext() as context:
        context.prec = 50
        return int(
            (Decimal(exact.numerator) / Decimal(exact.denominator)).quantize(
                Decimal(1), rounding=ROUND_HALF_UP
            )
        )


def test_estimate_between_golden_table_is_independent_and_exact() -> None:
    """100 000.00 on 2022-11-03 to 60 000.00 on 2026-11-03, every month-end."""
    points = (
        _dated_point(date(2022, 11, 3), 10_000_000),
        _dated_point(date(2026, 11, 3), 6_000_000),
    )
    assert [day for day, _value in ESTIMATE_GOLDEN] == [
        date(year, month, last)
        for year in (2022, 2023, 2024, 2025, 2026)
        for month in range(1, 13)
        for last in (calendar.monthrange(year, month)[1],)
        if date(2022, 11, 3) < date(year, month, last) < date(2026, 11, 3)
    ]
    for day, expected in ESTIMATE_GOLDEN:
        assert value_on(points, day, today=date(2026, 11, 3), estimate_between=True) == (
            expected,
            True,
        )


def test_estimate_between_endpoints_and_switch() -> None:
    points = (
        _dated_point(date(2026, 1, 1), 10000),
        _dated_point(date(2026, 3, 1), 20000),
    )
    assert value_on(points, date(2026, 2, 1), today=date(2026, 3, 15), estimate_between=True) == (
        15254,
        True,
    )
    assert value_on(points, date(2026, 1, 1), today=date(2026, 3, 15), estimate_between=True) == (
        10000,
        False,
    )
    assert value_on(points, date(2026, 3, 1), today=date(2026, 3, 15), estimate_between=True) == (
        20000,
        False,
    )
    assert value_on(points, date(2026, 2, 1), today=date(2026, 3, 15), estimate_between=False) == (
        10000,
        False,
    )


def test_estimate_between_uses_the_effective_end_value_on_a_corrected_day() -> None:
    points = (
        _dated_point(date(2026, 1, 1), 10000),
        _dated_point(date(2026, 3, 1), 20000, hour=8),
        _dated_point(date(2026, 3, 1), 30000, hour=9),
    )
    assert value_on(points, date(2026, 2, 1), today=date(2026, 3, 15), estimate_between=True) == (
        20508,
        True,
    )


def test_estimate_between_rising_zero_and_jpy_values() -> None:
    rising = (
        _dated_point(date(2026, 1, 1), 10000),
        _dated_point(date(2026, 3, 1), 20000),
    )
    assert value_on(rising, date(2026, 2, 1), today=date(2026, 3, 1), estimate_between=True) == (
        15254,
        True,
    )
    zero = (
        _dated_point(date(2026, 1, 1), 0),
        _dated_point(date(2026, 3, 1), 10000),
    )
    assert value_on(zero, date(2026, 2, 1), today=date(2026, 3, 1), estimate_between=True) == (
        5254,
        True,
    )
    falling_to_zero = (
        _dated_point(date(2026, 1, 1), 10000),
        _dated_point(date(2026, 3, 1), 0),
    )
    assert value_on(
        falling_to_zero, date(2026, 2, 1), today=date(2026, 3, 1), estimate_between=True
    ) == (4746, True)
    jpy = (
        _dated_point(date(2026, 1, 1), 100),
        _dated_point(date(2026, 3, 1), 200),
    )
    assert value_on(jpy, date(2026, 2, 1), today=date(2026, 3, 1), estimate_between=True) == (
        153,
        True,
    )  # no minor digits: the same integer formula


def test_estimate_between_has_no_value_before_the_first_and_carries_after_the_last() -> None:
    points = (
        _dated_point(date(2026, 1, 1), 10000),
        _dated_point(date(2026, 3, 1), 20000),
    )
    assert value_on(points, date(2025, 12, 1), today=date(2026, 3, 15), estimate_between=True) == (
        None,
        False,
    )
    assert value_on(points, date(2026, 4, 1), today=date(2026, 4, 15), estimate_between=True) == (
        20000,
        False,
    )


def test_estimate_between_never_uses_a_future_value_as_the_end_point() -> None:
    points = (
        _dated_point(date(2026, 1, 1), 10000),
        _dated_point(date(2026, 3, 1), 20000),
    )
    assert value_on(points, date(2026, 1, 31), today=date(2026, 2, 1), estimate_between=True) == (
        10000,
        False,
    )


def test_estimate_between_linked_items_ignore_the_switch() -> None:
    linked = _item(
        valuation_mode="linked",
        link_type="portfolio_asset_class",
        link_id=uuid4(),
        estimate_between=True,
        valuations=(
            _dated_point(date(2026, 1, 1), 10000),
            _dated_point(date(2026, 3, 1), 20000),
        ),
    )
    resolved = values_at(
        [linked],
        date(2026, 2, 1),
        timezone="UTC",
        linked={linked.id: Money(500, "EUR")},
        backfill=True,
        today=date(2026, 3, 1),
    )[0]
    assert (resolved.value_minor, resolved.estimated) == (500, False)


@given(
    v0=st.integers(min_value=0, max_value=10**8),
    v1=st.integers(min_value=0, max_value=10**8),
    span=st.integers(min_value=2, max_value=365),
    offset=st.integers(min_value=1, max_value=364),
)
def test_property_estimate_is_between_ends_monotonic_and_rounded_once(
    v0: int, v1: int, span: int, offset: int
) -> None:
    if offset >= span:
        return
    start = date(2020, 1, 1)
    end = start.fromordinal(start.toordinal() + span)
    day = start.fromordinal(start.toordinal() + offset)
    points = (_dated_point(start, v0), _dated_point(end, v1))
    value, estimated = value_on(points, day, today=end, estimate_between=True)
    assert estimated is True
    assert value is not None
    assert min(v0, v1) <= value <= max(v0, v1)
    assert value == _fraction_value(v0, v1, start, end, day)
    values: list[int] = []
    for index in range(span + 1):
        result = value_on(
            points,
            start.fromordinal(start.toordinal() + index),
            today=end,
            estimate_between=True,
        )[0]
        assert result is not None
        values.append(result)
    if v1 >= v0:
        assert values == sorted(values)
    else:
        assert values == sorted(values, reverse=True)
