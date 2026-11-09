"""Net-worth domain rules (F5.1): names, seeds, valuation order and bulk lines."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from assetboard.domain.networth import (
    ASSET_SEED,
    ASSET_SEED_V1,
    ASSET_SEED_V2,
    ASSET_SEEDS,
    LIABILITY_SEED,
    MAX_BULK_BYTES,
    MAX_BULK_LINES,
    BulkTooLarge,
    InvalidItemName,
    Side,
    TooManyBulkLines,
    ValuationPoint,
    check_bulk_limits,
    clean_item_name,
    clean_notes,
    current_valuation,
    duplicate_name_lines,
    history_points,
    ordered_valuations,
    seed_for,
    sparkline_polyline,
    split_bulk_lines,
)
from assetboard.i18n import gettext

TODAY = date(2026, 11, 2)


def _valuation(
    as_of: date, value_minor: int, *, created_at: datetime | None = None
) -> ValuationPoint:
    return ValuationPoint(
        id=uuid4(),
        as_of=as_of,
        created_at=created_at or datetime(2026, 1, 1, tzinfo=UTC),
        value_minor=value_minor,
    )


def _points(text: str) -> list[tuple[Decimal, Decimal]]:
    points: list[tuple[Decimal, Decimal]] = []
    for point in text.split():
        x, y = point.split(",")
        points.append((Decimal(x), Decimal(y)))
    return points


def test_seed_sets_have_the_specified_names_and_sides() -> None:
    # F5.7: Real estate first; the rest as before, with their colours unchanged.
    assert [seed.name for seed in ASSET_SEED] == [
        "Real estate",
        "Vehicles",
        "Electronics",
        "Appliances",
        "Furniture",
        "Jewelry & watches",
        "Art & collectibles",
        "Tools",
        "Other",
    ]
    assert [seed.name for seed in LIABILITY_SEED] == ["Installments", "Mortgage", "Other"]
    assert {seed.side for seed in ASSET_SEED} == {Side.ASSET}
    assert {seed.side for seed in LIABILITY_SEED} == {Side.LIABILITY}
    assert all(seed.color.startswith("#") and len(seed.color) == 7 for seed in ASSET_SEED)
    assert len({seed.color for seed in ASSET_SEED}) == len(ASSET_SEED)
    assert ASSET_SEED[1:] == ASSET_SEED_V2
    assert seed_for(Side.ASSET) == ASSET_SEED and seed_for(Side.LIABILITY) == LIABILITY_SEED


def test_every_shipped_asset_seed_is_kept_for_restore() -> None:
    assert ASSET_SEEDS == (ASSET_SEED_V1, ASSET_SEED_V2, ASSET_SEED)
    assert "Real estate" not in {seed.name for seed in (*ASSET_SEED_V1, *ASSET_SEED_V2)}


@pytest.mark.parametrize(
    ("locale", "first", "names"),
    [("en", "Real estate", 9), ("pl", "Nieruchomości", 9)],
)
def test_the_seed_translates_with_real_estate_first(locale: str, first: str, names: int) -> None:
    translated = [gettext(seed.name, locale) for seed in ASSET_SEED]
    assert translated[0] == first
    assert len(set(translated)) == names
    if locale == "pl":
        assert all(name != seed.name for name, seed in zip(translated, ASSET_SEED, strict=True))


def test_item_names_and_notes_follow_the_shared_text_rules() -> None:
    assert clean_item_name("  Vintage   camera  ") == "Vintage camera"
    assert clean_notes("  Serial 123  ") == "Serial 123"
    assert clean_notes("") == ""
    with pytest.raises(InvalidItemName):
        clean_item_name("   ")
    with pytest.raises(InvalidItemName):
        clean_item_name("x" * 201)
    with pytest.raises(InvalidItemName):
        clean_item_name("Bad\u202ename")


def test_current_value_orders_by_as_of_then_entry_order_or_id() -> None:
    old = _valuation(date(2026, 1, 1), 100)
    same_date_earlier = _valuation(
        date(2026, 2, 1), 200, created_at=datetime(2026, 2, 1, 10, tzinfo=UTC)
    )
    same_date_later = _valuation(
        date(2026, 2, 1), 300, created_at=datetime(2026, 2, 1, 11, tzinfo=UTC)
    )
    future = _valuation(date(2026, 12, 1), 999)
    assert ordered_valuations([future, same_date_later, old, same_date_earlier]) == [
        old,
        same_date_earlier,
        same_date_later,
        future,
    ]
    assert current_valuation([old, same_date_earlier, same_date_later, future], TODAY) is (
        same_date_later
    )
    assert current_valuation([future], TODAY) is None
    assert current_valuation([], TODAY) is None


def test_history_points_keep_the_last_entry_per_date_and_skip_future_rows() -> None:
    first = _valuation(date(2026, 1, 1), 100)
    same_date = _valuation(date(2026, 1, 1), 150, created_at=datetime(2026, 1, 2, tzinfo=UTC))
    later = _valuation(date(2026, 3, 1), 300)
    future = _valuation(date(2026, 12, 1), 999)
    assert [(p.as_of, p.value_minor) for p in history_points(
        [later, same_date, future, first], TODAY
    )] == [(date(2026, 1, 1), 150), (date(2026, 3, 1), 300)]  # fmt: skip


def test_sparkline_handles_empty_single_and_flat_series() -> None:
    assert sparkline_polyline([]) == ""
    single = _points(sparkline_polyline([100]))
    assert len(single) == 2
    assert single[0][1] == single[1][1] == Decimal("13.50")
    flat = _points(sparkline_polyline([5, 5, 5]))
    assert {y for _x, y in flat} == {Decimal("13.50")}


def test_sparkline_accepts_decimal_prices() -> None:
    points = _points(sparkline_polyline([Decimal("100.25"), Decimal("101"), Decimal("99.5")]))
    assert len(points) == 3
    assert points[0][0] < points[1][0] < points[2][0]
    assert all(Decimal(0) <= y <= Decimal(28) for _x, y in points)


def test_sparkline_points_stay_inside_the_viewbox() -> None:
    points = _points(sparkline_polyline([0, 1000, 10, 500]))
    assert [x for x, _y in points] == sorted(x for x, _y in points)
    assert all(Decimal(0) <= x <= Decimal(120) for x, _y in points)
    assert all(Decimal(0) <= y <= Decimal(28) for _x, y in points)


@given(st.lists(st.integers(min_value=0, max_value=10**9), min_size=1, max_size=30))
def test_sparkline_property_x_is_monotonic_and_y_stays_in_the_box(values: list[int]) -> None:
    points = _points(sparkline_polyline(values))
    assert points == sorted(points, key=lambda point: point[0])
    assert all(Decimal(0) <= point[0] <= Decimal(120) for point in points)
    assert all(Decimal(0) <= point[1] <= Decimal(28) for point in points)


def test_bulk_limits_are_checked_before_line_parsing() -> None:
    assert check_bulk_limits("\n".join("a; 1; b" for _ in range(MAX_BULK_LINES))) == 500
    with pytest.raises(TooManyBulkLines):
        check_bulk_limits("\n".join("a; 1; b" for _ in range(MAX_BULK_LINES + 1)))
    with pytest.raises(BulkTooLarge):
        check_bulk_limits("x" * (MAX_BULK_BYTES + 1))


def test_bulk_lines_are_trimmed_and_blank_lines_are_ignored() -> None:
    text = "\ufeff\r\n  Bike ; 1 200,50 ; Vehicles \r\n\r\nCamera;300;Electronics\r\n"
    lines, problems = split_bulk_lines(text)
    assert problems == ()
    assert [(line.number, line.name, line.value, line.category) for line in lines] == [
        (2, "Bike", "1 200,50", "Vehicles"),
        (4, "Camera", "300", "Electronics"),
    ]


def test_bulk_line_errors_are_reported_per_line() -> None:
    text = "\n".join(
        (
            "Only a name",
            ";;",
            "; 12; Vehicles",
            "Car; ; Vehicles",
            "Car; 12; ",
            "Bad\u202ename; 12; Vehicles",
            "Car; 12; Vehicles",
        )
    )
    lines, problems = split_bulk_lines(text)
    assert [line.number for line in lines] == [7]
    assert [(problem.number, problem.code) for problem in problems] == [
        (1, "wrong_format"),
        (2, "name_required"),
        (3, "name_required"),
        (4, "value_required"),
        (5, "category_required"),
        (6, "invalid_name"),
    ]


def test_duplicate_bulk_names_are_flagged_case_insensitively() -> None:
    lines, _problems = split_bulk_lines("Car; 1; Vehicles\ncar; 2; Vehicles\nBike; 3; Other")
    assert duplicate_name_lines(lines) == frozenset({2})


def test_valuation_order_is_stable_for_identical_timestamps_and_dates() -> None:
    created = datetime(2026, 1, 1, tzinfo=UTC)
    first = _valuation(date(2026, 1, 1), 1, created_at=created)
    second = _valuation(date(2026, 1, 1), 2, created_at=created)
    ordered = ordered_valuations([second, first])
    assert ordered == sorted([first, second], key=lambda value: str(value.id))
    assert current_valuation(ordered, TODAY) in {first, second}


def test_a_date_after_today_is_not_current_but_stays_in_order() -> None:
    yesterday = _valuation(TODAY - timedelta(days=1), 10)
    tomorrow = _valuation(TODAY + timedelta(days=1), 20)
    assert current_valuation([yesterday, tomorrow], TODAY) is yesterday
    assert ordered_valuations([tomorrow, yesterday]) == [yesterday, tomorrow]
