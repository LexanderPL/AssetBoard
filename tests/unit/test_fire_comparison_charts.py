"""The F8.5 comparison overlay: union years, gaps, bases, labels, tooltips and a11y points."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from assetboard.domain.fire import Projection, ProjectionInputs, Rule, project
from assetboard.domain.money import Money
from assetboard.services.fire_comparison import FireComparison, FireComparisonRow
from assetboard.services.fire_scenarios import FireScenarioView
from assetboard.web.fire_charts import COMPARISON_COLORS, build_fire_comparison_chart


def _scenario(
    name: str,
    *,
    start_year: int = 2026,
    birth_year: int = 1985,
    horizon_age: int = 90,
    archived: bool = False,
    linked: bool = False,
) -> FireScenarioView:
    return FireScenarioView(
        id=uuid4(),
        name=name,
        currency="EUR",
        birth_year=birth_year,
        start_year=start_year,
        age_at_start=start_year - birth_year,
        inflation_bp=250,
        real_return_bp=500,
        income_growth_bp=300,
        stop_working_age=55,
        horizon_age=horizon_age,
        rule=Rule.RETURNS_COVER_EXPENSES.value,
        swr_bp=None,
        income_mode="trailing_12m" if linked else "manual",
        income_manual=None if linked else Money(5_000_000, "EUR"),
        expenses_mode="manual",
        expenses_manual=Money(3_000_000, "EUR"),
        portfolio_mode="manual",
        portfolio_manual=Money(100_000_000, "EUR"),
        is_default=False,
        archived_at=datetime(2026, 1, 1, tzinfo=UTC) if archived else None,
        contribution_count=0,
        event_count=0,
    )


def _projection(
    *,
    start_year: int = 2026,
    birth_year: int = 1985,
    horizon_age: int = 90,
    stop_working_age: int = 55,
) -> Projection:
    return project(
        ProjectionInputs(
            currency="EUR",
            birth_year=birth_year,
            start_year=start_year,
            horizon_age=horizon_age,
            stop_working_age=stop_working_age,
            inflation_bp=250,
            real_return_bp=500,
            income_growth_bp=300,
            rule=Rule.RETURNS_COVER_EXPENSES,
            swr_bp=None,
            start_income=Money(5_000_000, "EUR"),
            start_expenses=Money(3_000_000, "EUR"),
            start_portfolio=Money(100_000_000, "EUR"),
        )
    )


def _comparison(*rows: FireComparisonRow, mixed: bool = False) -> FireComparison:
    return FireComparison(
        rows=rows,
        selected_ids=tuple(row.scenario.id for row in rows),
        mixed_start_years=mixed,
    )


def _list(data: dict[str, object], key: str) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], data[key])


def test_the_overlay_uses_the_union_of_calendar_years_with_gaps() -> None:
    first = _scenario("First", start_year=2025, birth_year=1985, horizon_age=45)
    second = _scenario("Second", start_year=2026, birth_year=1985, horizon_age=47)
    first_projection = _projection(start_year=2025, birth_year=1985, horizon_age=45)
    second_projection = _projection(start_year=2026, birth_year=1985, horizon_age=47)
    chart = build_fire_comparison_chart(
        _comparison(
            FireComparisonRow(first, first_projection),
            FireComparisonRow(second, second_projection),
        ),
        "nominal",
        "en",
    )
    assert chart.data is not None
    assert chart.data["labels"] == [str(year) for year in range(2025, 2033)]
    series = _list(chart.data, "series")
    assert [item["name"] for item in series] == ["First", "Second"]
    assert series[0]["values"] == [
        first_projection.rows[index].net_worth.amount_minor
        for index in range(len(first_projection.rows))
    ] + [None] * (8 - len(first_projection.rows))
    assert series[1]["values"][0] is None
    assert chart.points[0].values[0] != "—"
    assert chart.points[0].values[1] == "—"
    assert chart.series[0].color == COMPARISON_COLORS[0]
    assert chart.series[1].color == COMPARISON_COLORS[1]
    assert chart.data["y_labels"]


def test_real_and_nominal_bases_change_only_the_plotted_values() -> None:
    scenario = _scenario("Plan")
    projection = _projection()
    comparison = _comparison(FireComparisonRow(scenario, projection))
    nominal = build_fire_comparison_chart(comparison, "nominal", "en")
    real = build_fire_comparison_chart(comparison, "real", "en")
    assert nominal.basis == "nominal" and real.basis == "real"
    assert nominal.data is not None and real.data is not None
    nominal_values = _list(nominal.data, "series")[0]["values"]
    real_values = _list(real.data, "series")[0]["values"]
    assert nominal_values == [row.net_worth.amount_minor for row in projection.rows]
    assert real_values == [row.real_net_worth.amount_minor for row in projection.rows]
    assert nominal_values != real_values


def test_archived_and_duplicate_names_are_disambiguated() -> None:
    first = _scenario("Plan")
    first_archived = _scenario("Plan", archived=True)
    second_archived = _scenario("Plan", archived=True)
    comparison = _comparison(
        FireComparisonRow(first, _projection()),
        FireComparisonRow(first_archived, _projection(horizon_age=89)),
        FireComparisonRow(second_archived, _projection(horizon_age=88)),
    )
    chart = build_fire_comparison_chart(comparison, "nominal", "en")
    assert chart.data is not None
    assert [item["name"] for item in _list(chart.data, "series")] == [
        "Plan",
        "Plan (archived)",
        "Plan (archived) (2)",
    ]


def test_fire_years_get_tooltips_for_each_series() -> None:
    first = _scenario("First")
    second = _scenario("Second")
    first_projection = _projection()
    second_projection = _projection(horizon_age=91)
    assert first_projection.fire_year is not None and first_projection.fire_age is not None
    chart = build_fire_comparison_chart(
        _comparison(
            FireComparisonRow(first, first_projection),
            FireComparisonRow(second, second_projection),
        ),
        "nominal",
        "en",
    )
    assert chart.data is not None
    tooltips = cast(list[list[str]], chart.data["tooltips"])
    index = first_projection.fire_year - first_projection.inputs.start_year
    assert f"First: FIRE at age {first_projection.fire_age}" in tooltips[index]
    assert all(not lines or all("FIRE at age" in line for line in lines) for lines in tooltips)


def test_accessible_points_carry_formatted_values_for_every_series() -> None:
    first = _scenario("First")
    second = _scenario("Second", archived=True)
    chart = build_fire_comparison_chart(
        _comparison(
            FireComparisonRow(first, _projection()),
            FireComparisonRow(second, _projection(horizon_age=90)),
        ),
        "real",
        "pl",
    )
    assert chart.points
    assert len(chart.points[0].values) == 2
    assert chart.points[0].year == "2026"
    assert chart.mixed_start_years is False
    assert chart.points[0].values[0].endswith("€")


def test_blocked_rows_are_not_plotted_and_no_projection_means_no_chart() -> None:
    scenario = _scenario("Blocked", linked=True)
    blocked = build_fire_comparison_chart(
        _comparison(FireComparisonRow(scenario, None, blocks=())),
        "real",
        "en",
    )
    assert blocked.data is None
    assert blocked.series == () and blocked.points == ()
    assert blocked.basis == "real"

    plotted = build_fire_comparison_chart(
        _comparison(
            FireComparisonRow(_scenario("Blocked"), None),
            FireComparisonRow(_scenario("Plotted"), _projection()),
            mixed=True,
        ),
        "nominal",
        "en",
    )
    assert plotted.data is not None
    assert [item["name"] for item in _list(plotted.data, "series")] == ["Plotted"]
    assert plotted.mixed_start_years is True
    assert plotted.series[0].color == COMPARISON_COLORS[0]
