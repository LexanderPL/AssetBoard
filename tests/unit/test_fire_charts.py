"""Server-prepared F8.3 chart data: basis, markers and accessible plotted values."""

from __future__ import annotations

from typing import Any, cast

from assetboard.domain.fire import (
    ProjectionInputs,
    Rule,
    project,
)
from assetboard.domain.money import Money
from assetboard.i18n import gettext
from assetboard.i18n.numbers import format_money
from assetboard.web.fire_charts import build_fire_charts


def _list(data: dict[str, object], key: str) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], data[key])


def _inputs(**overrides: object) -> ProjectionInputs:
    values: dict[str, object] = {
        "currency": "EUR",
        "birth_year": 1985,
        "start_year": 2026,
        "horizon_age": 90,
        "stop_working_age": 55,
        "inflation_bp": 250,
        "real_return_bp": 500,
        "income_growth_bp": 300,
        "rule": Rule.RETURNS_COVER_EXPENSES,
        "swr_bp": None,
        "start_income": Money(5_000_000, "EUR"),
        "start_expenses": Money(3_000_000, "EUR"),
        "start_portfolio": Money(100_000_000, "EUR"),
    }
    values.update(overrides)
    return ProjectionInputs(**values)  # type: ignore[arg-type]


def test_the_net_worth_chart_follows_the_nominal_basis() -> None:
    projection = project(_inputs())
    charts = build_fire_charts(projection, "nominal", "en")
    assert charts.basis == "nominal"
    assert charts.net_worth is not None
    data = charts.net_worth
    assert data["labels"] == [str(row.year) for row in projection.rows]
    series = _list(data, "series")
    assert series[0]["name"] == "Net worth"
    assert series[0]["values"] == [row.net_worth.amount_minor for row in projection.rows]
    assert charts.net_worth_points[0].net_worth == format_money(projection.rows[0].net_worth, "en")
    assert charts.net_worth_points[0].year == str(projection.rows[0].year)


def test_the_real_basis_uses_real_values_for_the_net_worth_chart_only() -> None:
    projection = project(_inputs(inflation_bp=250))
    charts = build_fire_charts(projection, "real", "en")
    assert charts.basis == "real"
    assert charts.net_worth is not None
    series = _list(charts.net_worth, "series")
    assert series[0]["name"] == "Net worth in today's money"
    assert series[0]["values"] == [row.real_net_worth.amount_minor for row in projection.rows]
    assert charts.expenses_gain is not None
    expense_series = _list(charts.expenses_gain, "series")
    assert expense_series[0]["values"] == [row.expenses.amount_minor for row in projection.rows]
    assert expense_series[1]["values"] == [row.gain.amount_minor for row in projection.rows]


def test_fire_income_stop_and_depletion_get_markers() -> None:
    projection = project(_inputs())
    assert projection.fire_index is not None
    charts = build_fire_charts(projection, "nominal", "en")
    assert charts.net_worth is not None
    labels = [marker["label"] for marker in _list(charts.net_worth, "markers")]
    assert "FIRE" in labels
    assert "Income stops" in labels
    mark_points = _list(charts.net_worth, "mark_points")
    assert mark_points[0]["index"] == projection.fire_index
    assert mark_points[0]["value"] == projection.rows[projection.fire_index].net_worth.amount_minor
    assert charts.expenses_gain is not None
    assert charts.expenses_gain["markers"] == [
        {
            "index": projection.fire_index,
            "label": "FIRE",
            "text": f"FIRE at age {projection.fire_age} ({projection.fire_year})",
        }
    ]


def test_a_never_fire_plan_marks_no_fire_point_and_reports_depletion() -> None:
    projection = project(
        _inputs(
            real_return_bp=0,
            inflation_bp=0,
            income_growth_bp=0,
            stop_working_age=41,
            start_income=Money(0, "EUR"),
            start_expenses=Money(3_000_000, "EUR"),
            start_portfolio=Money(10_000_000, "EUR"),
        )
    )
    assert projection.fire_index is None
    assert projection.depletion_age is not None
    charts = build_fire_charts(projection, "nominal", "en")
    assert charts.fire_index is None
    assert charts.fire_year is None
    assert charts.fire_age is None
    assert charts.net_worth is not None
    assert charts.net_worth["mark_points"] == []
    labels = [marker["label"] for marker in _list(charts.net_worth, "markers")]
    assert "FIRE" not in labels
    assert "Depleted" in labels
    assert charts.depletion_age == projection.depletion_age


def test_chart_labels_and_tooltips_use_the_ui_locale() -> None:
    projection = project(_inputs(rule=Rule.SWR, swr_bp=400))
    charts = build_fire_charts(projection, "nominal", "pl")
    assert charts.net_worth is not None
    series = _list(charts.net_worth, "series")
    assert series[0]["name"] == gettext("Net worth", "pl")
    markers = _list(charts.net_worth, "markers")
    assert markers[0]["label"] == gettext("FIRE", "pl")
    assert charts.expenses_gain is not None
    expense_series = _list(charts.expenses_gain, "series")
    assert expense_series[0]["name"] == gettext("Expenses", "pl")


def test_no_projection_means_no_chart_data() -> None:
    charts = build_fire_charts(None, "nominal", "en")
    assert charts.net_worth is None
    assert charts.expenses_gain is None
    assert charts.net_worth_points == ()
    assert charts.fire_index is None
