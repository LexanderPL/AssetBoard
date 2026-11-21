"""The remembered Reports filters: cookie value parsing and serialization (reports polish 2)."""

from __future__ import annotations

from uuid import UUID, uuid4

from assetboard.domain.reports import ReportStep
from assetboard.services.reports import ReportFilters
from assetboard.web.report_filters import (
    MAX_REPORT_FILTER_COOKIE_LENGTH,
    parse_report_filter_cookie,
    serialize_report_filters,
)


def _filters(*, category_ids: tuple[UUID, ...] = (), savings: bool = False) -> ReportFilters:
    return ReportFilters(
        start_year=2024,
        end_year=2026,
        step=ReportStep.YEAR,
        currency="EUR",
        from_text="2024",
        to_text="2026",
        category_ids=category_ids,
        savings=savings,
        year=2025,
        trend_months=12,
    )


def test_the_resolved_filters_round_trip_through_the_cookie() -> None:
    first, second = uuid4(), uuid4()
    serialized = serialize_report_filters(_filters(category_ids=(first, second), savings=True))
    assert serialized.startswith("from=2024&to=2026&step=year&currency=EUR")
    assert f"category={first}" in serialized and f"category={second}" in serialized
    assert "savings=1" in serialized
    assert serialized.endswith("year=2025&trend=12")

    source = parse_report_filter_cookie(serialized)
    assert source.explicit is False
    assert (source.from_year, source.to_year) == ("2024", "2026")
    assert source.step == "year" and source.currency == "EUR"
    assert source.category_ids == (str(first), str(second))
    assert source.savings == "1"
    assert (source.year, source.trend) == ("2025", "12")


def test_defaults_omit_savings_and_categories() -> None:
    serialized = serialize_report_filters(_filters())
    assert "savings=" not in serialized
    assert "category=" not in serialized
    source = parse_report_filter_cookie(serialized)
    assert source.category_ids == () and source.savings == ""


def test_unknown_keys_are_ignored_and_the_last_duplicate_wins() -> None:
    source = parse_report_filter_cookie(
        "from=2024&surprise=1&step=month&step=year&currency=EUR&category=junk&category=2"
    )
    assert source.from_year == "2024"
    assert source.step == "year"
    assert source.currency == "EUR"
    assert source.category_ids == ("junk", "2")


def test_missing_empty_and_oversized_values_mean_defaults() -> None:
    empty = parse_report_filter_cookie("")
    assert (empty.from_year, empty.to_year, empty.category_ids, empty.explicit) == (
        "",
        "",
        (),
        False,
    )
    assert parse_report_filter_cookie("   ") == empty
    too_long = "from=2024&" + "x=" + "a" * MAX_REPORT_FILTER_COOKIE_LENGTH
    oversized = parse_report_filter_cookie(too_long)
    assert (oversized.from_year, oversized.year, oversized.explicit) == ("", "", False)


def test_too_many_fields_fall_back_instead_of_raising() -> None:
    raw = "&".join(f"category={uuid4()}" for _ in range(300))
    source = parse_report_filter_cookie(raw)
    assert source.category_ids == ()


def test_serialization_keeps_the_category_order() -> None:
    ids: tuple[UUID, ...] = tuple(uuid4() for _ in range(3))
    source = parse_report_filter_cookie(serialize_report_filters(_filters(category_ids=ids)))
    assert source.category_ids == tuple(str(category_id) for category_id in ids)
