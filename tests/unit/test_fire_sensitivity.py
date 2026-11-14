"""The pure F8.5 sensitivity grid: axes, scaling, base cell and validation."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from assetboard.domain import fire
from assetboard.domain.fire import (
    DEFAULT_EXPENSE_FACTORS_BP,
    DEFAULT_RETURN_DELTAS_BP,
    MAX_AMOUNT_MINOR,
    ProjectionInputs,
    Rule,
    SensitivityError,
    project,
    sensitivity,
)
from assetboard.domain.money import Money


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


def _never_inputs() -> ProjectionInputs:
    return _inputs(
        inflation_bp=0,
        real_return_bp=0,
        income_growth_bp=0,
        stop_working_age=41,
        start_income=Money(0, "EUR"),
        start_expenses=Money(3_000_000, "EUR"),
        start_portfolio=Money(10_000_000, "EUR"),
    )


def test_default_grid_shape_order_and_effective_returns() -> None:
    inputs = _inputs()
    grid = sensitivity(inputs, DEFAULT_RETURN_DELTAS_BP, DEFAULT_EXPENSE_FACTORS_BP)
    assert grid.return_deltas_bp == DEFAULT_RETURN_DELTAS_BP
    assert grid.expense_factors_bp == DEFAULT_EXPENSE_FACTORS_BP
    assert len(grid.rows) == 5
    assert all(len(row.cells) == 5 for row in grid.rows)
    assert [row.expense_factor_bp for row in grid.rows] == list(DEFAULT_EXPENSE_FACTORS_BP)
    assert [cell.effective_return_bp for cell in grid.rows[0].cells] == [
        300,
        400,
        500,
        600,
        700,
    ]
    base = grid.base_cell
    assert (base.return_delta_bp, base.expense_factor_bp, base.is_base) == (0, 10_000, True)
    expected = project(inputs)
    assert (base.fire_age, base.fire_year) == (expected.fire_age, expected.fire_year)


def test_the_base_cell_equals_the_saved_projection_for_each_rule() -> None:
    reached = project(_inputs())
    never = project(_never_inputs())
    withdrawal = project(_inputs(rule=Rule.SWR, swr_bp=400))
    for inputs, expected in (
        (_inputs(), reached),
        (_never_inputs(), never),
        (_inputs(rule=Rule.SWR, swr_bp=400), withdrawal),
    ):
        grid = sensitivity(inputs, DEFAULT_RETURN_DELTAS_BP, DEFAULT_EXPENSE_FACTORS_BP)
        assert grid.base_cell.fire_age == expected.fire_age
        assert grid.base_cell.fire_year == expected.fire_year


def test_only_the_start_expenses_and_return_change_with_exact_half_up_scaling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _inputs(
        contributions=(fire.ContributionOverride(year=2027, amount=Money(1_200_000, "EUR")),),
        events=(
            fire.ProjectionEvent(
                year=2028,
                kind=fire.EventKind.EXPENSE_CHANGE,
                amount=Money(-500_000, "EUR"),
            ),
        ),
        start_expenses=Money(1, "EUR"),
    )
    captured: list[ProjectionInputs] = []
    real_project = fire.project

    def recording(value: ProjectionInputs) -> fire.Projection:
        captured.append(value)
        return real_project(value)

    monkeypatch.setattr(fire, "project", recording)
    sensitivity(inputs, (0, -100), (10_000, 15_000))
    assert len(captured) == 4
    by_factor = {(item.real_return_bp, item.start_expenses.amount_minor) for item in captured}
    assert (500, 1) in by_factor
    assert (400, 1) in by_factor
    assert (500, 2) in by_factor  # 1 * 1.5 = 1.5, half up to 2
    assert (400, 2) in by_factor
    for item in captured:
        assert item.start_income == inputs.start_income
        assert item.start_portfolio == inputs.start_portfolio
        assert item.inflation_bp == inputs.inflation_bp
        assert item.income_growth_bp == inputs.income_growth_bp
        assert item.contributions == inputs.contributions
        assert item.events == inputs.events


def test_never_cells_keep_none_and_the_age_range_covers_reached_cells_only() -> None:
    grid = sensitivity(_never_inputs(), DEFAULT_RETURN_DELTAS_BP, DEFAULT_EXPENSE_FACTORS_BP)
    assert all(
        cell.fire_age is None and cell.fire_year is None for row in grid.rows for cell in row.cells
    )
    assert grid.min_fire_age is None
    assert grid.max_fire_age is None

    reached = sensitivity(_inputs(), DEFAULT_RETURN_DELTAS_BP, DEFAULT_EXPENSE_FACTORS_BP)
    ages = [
        cell.fire_age for row in reached.rows for cell in row.cells if cell.fire_age is not None
    ]
    assert ages
    assert reached.min_fire_age == min(ages)
    assert reached.max_fire_age == max(ages)


def test_custom_axes_keep_their_own_order() -> None:
    grid = sensitivity(_inputs(), (100, -100, 0), (12_000, 10_000))
    assert grid.return_deltas_bp == (100, -100, 0)
    assert [cell.return_delta_bp for cell in grid.rows[0].cells] == [100, -100, 0]
    assert [row.expense_factor_bp for row in grid.rows] == [12_000, 10_000]
    assert grid.base_cell is grid.rows[1].cells[2]


def test_sensitivity_is_deterministic_and_does_not_mutate_its_inputs() -> None:
    inputs = _inputs()
    before = replace(inputs)
    first = sensitivity(inputs, DEFAULT_RETURN_DELTAS_BP, DEFAULT_EXPENSE_FACTORS_BP)
    second = sensitivity(inputs, DEFAULT_RETURN_DELTAS_BP, DEFAULT_EXPENSE_FACTORS_BP)
    assert first == second
    assert inputs == before
    assert all(row.cells for row in first.rows)


@pytest.mark.parametrize(
    ("deltas", "factors", "field", "code"),
    [
        ((), DEFAULT_EXPENSE_FACTORS_BP, "return_deltas_bp", "empty_axis"),
        (DEFAULT_RETURN_DELTAS_BP, (), "expense_factors_bp", "empty_axis"),
        (
            DEFAULT_RETURN_DELTAS_BP + (0,),
            DEFAULT_EXPENSE_FACTORS_BP,
            "return_deltas_bp",
            "duplicate_value",
        ),
        (
            DEFAULT_RETURN_DELTAS_BP,
            DEFAULT_EXPENSE_FACTORS_BP + (10_000,),
            "expense_factors_bp",
            "duplicate_value",
        ),
        ((0.0, 100), DEFAULT_EXPENSE_FACTORS_BP, "return_deltas_bp", "invalid_value"),
        (None, DEFAULT_EXPENSE_FACTORS_BP, "return_deltas_bp", "invalid_value"),
        ((100, 200), DEFAULT_EXPENSE_FACTORS_BP, "return_deltas_bp", "missing_base"),
        (DEFAULT_RETURN_DELTAS_BP, (9000, 11_000), "return_deltas_bp", "missing_base"),
        (tuple(range(10)), DEFAULT_EXPENSE_FACTORS_BP, "return_deltas_bp", "too_many_values"),
    ],
)
def test_axis_validation(deltas: object, factors: object, field: str, code: str) -> None:
    with pytest.raises(SensitivityError) as exc:
        sensitivity(_inputs(), deltas, factors)  # type: ignore[arg-type]
    assert (exc.value.field, exc.value.code) == (field, code)


def test_effective_returns_must_stay_inside_the_hard_rate_bounds() -> None:
    with pytest.raises(SensitivityError) as exc:
        sensitivity(
            _inputs(real_return_bp=4_900), DEFAULT_RETURN_DELTAS_BP, DEFAULT_EXPENSE_FACTORS_BP
        )
    assert (exc.value.field, exc.value.code) == ("return_deltas_bp", "return_out_of_range")
    with pytest.raises(SensitivityError) as low:
        sensitivity(
            _inputs(real_return_bp=-1_900), DEFAULT_RETURN_DELTAS_BP, DEFAULT_EXPENSE_FACTORS_BP
        )
    assert (low.value.field, low.value.code) == ("return_deltas_bp", "return_out_of_range")


def test_expense_factors_are_bounded_and_scaled_expenses_must_fit() -> None:
    with pytest.raises(SensitivityError) as exc:
        sensitivity(_inputs(), (0, 100, 10_000), (0, 10_000, 100_001))
    assert (exc.value.field, exc.value.code) == (
        "expense_factors_bp",
        "expense_factor_out_of_range",
    )
    with pytest.raises(SensitivityError) as large:
        sensitivity(
            _inputs(start_expenses=Money(MAX_AMOUNT_MINOR, "EUR")),
            (0, 100),
            (10_000, 12_000),
        )
    assert (large.value.field, large.value.code) == ("expense_factors_bp", "expense_too_large")


def test_an_invalid_plan_keeps_the_projection_error() -> None:
    with pytest.raises(fire.FireProjectionError):
        sensitivity(
            _inputs(start_expenses=Money(1, "USD")),
            DEFAULT_RETURN_DELTAS_BP,
            DEFAULT_EXPENSE_FACTORS_BP,
        )
