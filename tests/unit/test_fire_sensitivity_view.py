"""Server-prepared sensitivity labels, tones and screen-reader sentences."""

from __future__ import annotations

from typing import Any

from assetboard.domain.fire import (
    DEFAULT_EXPENSE_FACTORS_BP,
    DEFAULT_RETURN_DELTAS_BP,
    ProjectionInputs,
    Rule,
    sensitivity,
)
from assetboard.domain.money import Money
from assetboard.web.fire_sensitivity import build_sensitivity_view


def _inputs(**overrides: object) -> ProjectionInputs:
    values: dict[str, Any] = {
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


def test_the_default_view_has_five_by_five_headers_rows_and_a_marked_base_cell() -> None:
    grid = sensitivity(_inputs(), DEFAULT_RETURN_DELTAS_BP, DEFAULT_EXPENSE_FACTORS_BP)
    view = build_sensitivity_view(grid, horizon_age=90, locale="en")
    assert len(view.headers) == 5
    assert len(view.rows) == 5
    assert [header.delta_label for header in view.headers] == [
        "-2 pp",
        "-1 pp",
        "0 pp",
        "1 pp",
        "2 pp",
    ]
    assert [header.rate_label for header in view.headers] == [
        "3%",
        "4%",
        "5%",
        "6%",
        "7%",
    ]
    assert view.rows[0].factor_label == "80%"
    assert [row.factor_delta_label for row in view.rows] == [
        "-20%",
        "-10%",
        "0%",
        "+10%",
        "+20%",
    ]
    base = view.rows[2].cells[2]
    assert base.is_base is True and base.tone == "base"
    assert base.age_label == str(grid.base_cell.fire_age)
    assert base.sr_text.startswith("Base case:")
    assert "title" in base.title.lower() or "Age" in base.title
    assert view.min_fire_age == grid.min_fire_age
    assert view.max_fire_age == grid.max_fire_age


def test_tones_compare_each_cell_with_the_saved_plan() -> None:
    grid = sensitivity(
        _inputs(start_portfolio=Money(20_000_000, "EUR")),
        DEFAULT_RETURN_DELTAS_BP,
        DEFAULT_EXPENSE_FACTORS_BP,
    )
    view = build_sensitivity_view(grid, horizon_age=90, locale="en")
    tones = {cell.tone for row in view.rows for cell in row.cells}
    assert {"better", "same", "worse", "base"}.issubset(tones)
    base_age = view.base_age
    assert base_age is not None
    for row in view.rows:
        for cell in row.cells:
            if cell.tone == "better":
                assert int(cell.age_label) < base_age
            elif cell.tone == "worse":
                assert int(cell.age_label) > base_age
            elif cell.tone == "same":
                assert int(cell.age_label) == base_age
            elif not cell.is_base:
                assert cell.age_label == "—"


def test_never_cells_show_a_dash_and_a_never_tone() -> None:
    grid = sensitivity(
        _inputs(
            inflation_bp=0,
            real_return_bp=0,
            income_growth_bp=0,
            stop_working_age=41,
            start_income=Money(0, "EUR"),
            start_expenses=Money(3_000_000, "EUR"),
            start_portfolio=Money(10_000_000, "EUR"),
        ),
        DEFAULT_RETURN_DELTAS_BP,
        DEFAULT_EXPENSE_FACTORS_BP,
    )
    view = build_sensitivity_view(grid, horizon_age=90, locale="en")
    assert all(cell.tone == "never" for row in view.rows for cell in row.cells)
    assert all(cell.age_label == "—" for row in view.rows for cell in row.cells)
    assert "Never before age 90" in view.rows[0].cells[0].title
    assert "No FIRE before age 90" in view.rows[0].cells[0].sr_text


def test_labels_follow_the_locale() -> None:
    grid = sensitivity(_inputs(), (0, 50), (10_000, 15_000))
    view = build_sensitivity_view(grid, horizon_age=90, locale="pl")
    assert view.headers[1].delta_label == "0,5 p.p."
    assert view.rows[1].factor_delta_label == "+50%"
    assert view.rows[0].factor_delta_label == "0%"
    assert view.headers[0].rate_label == "5%"
    assert view.rows[1].factor_label == "150%"
