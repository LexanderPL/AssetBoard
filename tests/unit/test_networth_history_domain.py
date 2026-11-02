"""Net-worth history domain (F5.3): month ends, ranges, source priority and dated values.

The independent oracle for the date examples is the hand-written calendar below: month lengths
were checked against the Gregorian calendar (2024 is a leap year), not produced by the function
under test.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from assetboard.domain.money import Money
from assetboard.domain.networth import Side, Unavailable, ValuationPoint
from assetboard.domain.networth_history import (
    DEFAULT_HISTORY_RANGE,
    BreakdownCategory,
    ItemHistory,
    backfill_month_ends,
    history_start,
    month_end,
    month_ends,
    normalize_history_range,
    parse_breakdown,
    preferred_source,
    previous_month_end,
    snapshot_breakdown,
    source_rank,
    values_at,
)
from assetboard.domain.networth_summary import SummaryItem, summarize_net_worth


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


def test_backfill_starts_at_the_earliest_valuations_month_end() -> None:
    assert backfill_month_ends(date(2025, 12, 15), date(2026, 3, 1)) == (
        date(2025, 12, 31),
        date(2026, 1, 31),
        date(2026, 2, 28),
    )
    assert backfill_month_ends(None, date(2026, 3, 1)) == ()
    assert backfill_month_ends(date(2026, 3, 2), date(2026, 3, 1)) == ()


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


def test_source_priority_is_manual_then_auto_then_backfill() -> None:
    assert preferred_source(["backfill"]) == "backfill"
    assert preferred_source(["backfill", "auto"]) == "auto"
    assert preferred_source(["auto", "manual", "backfill"]) == "manual"
    assert preferred_source([]) is None
    assert [source_rank(source) for source in ("manual", "auto", "backfill")] == [0, 1, 2]


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


def test_breakdown_round_trip_is_versioned_and_ignores_unknown_keys() -> None:
    category_id = uuid4()
    other_id = uuid4()
    summary = summarize_net_worth(
        [
            SummaryItem(category_id, Side.ASSET, category_id, Money(1200, "EUR")),
            SummaryItem(other_id, Side.LIABILITY, other_id, Money(200, "EUR")),
            SummaryItem(uuid4(), Side.ASSET, uuid4(), Money(100, "USD")),
            SummaryItem(uuid4(), Side.ASSET, uuid4(), None),
        ],
        currency="EUR",
    )
    raw = snapshot_breakdown(summary)
    assert raw["v"] == 1
    assert raw["not_counted"] == {"non_base": 1, "no_value": 1, "unavailable": 0}
    parsed = parse_breakdown(raw)
    assert parsed.categories == (
        BreakdownCategory(category_id, Side.ASSET, 1200),
        BreakdownCategory(other_id, Side.LIABILITY, 200),
    )
    future = {"v": 99, **raw, "unavailable": 1, "categories": raw["categories"]}
    assert parse_breakdown(future).categories == parsed.categories
    assert parse_breakdown(None).categories == ()
    assert parse_breakdown({"categories": [{"category_id": "nope"}]}).categories == ()


@given(st.dates(min_value=date(1900, 1, 1), max_value=date(2199, 12, 31)))
def test_property_every_month_end_is_the_last_day_and_ranges_are_inclusive(day: date) -> None:
    end = month_end(day)
    assert end.month == day.month and end.year == day.year
    assert month_ends(day, day) == (end,)
    assert month_end(end) == end
