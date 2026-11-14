"""Monotonicity properties for the F8.5 sensitivity grid (pure, hypothesis)."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from assetboard.domain.fire import (
    DEFAULT_EXPENSE_FACTORS_BP,
    DEFAULT_RETURN_DELTAS_BP,
    SWR_HARD_MAX_BP,
    SWR_HARD_MIN_BP,
    ProjectionInputs,
    Rule,
    sensitivity,
)
from assetboard.domain.money import Money

_INFINITY = 10**9


def _as_infinity(age: int | None) -> int:
    return _INFINITY if age is None else age


@st.composite
def _non_negative_inputs(draw: st.DrawFn) -> ProjectionInputs:
    """A plan whose net worth path stays non-negative.

    Income starts at twice the base expenses and grows at least as fast as inflation, so the
    net saving never turns negative; the starting portfolio is non-negative. That is the
    condition the return monotonicity caveat needs (a negative opening balance can delay FIRE
    when a higher return makes the balance more negative).
    """
    start_expenses = draw(st.integers(min_value=100_000, max_value=100_000_000))
    inflation_bp = draw(st.integers(min_value=-1_000, max_value=500))
    income_growth_bp = draw(st.integers(min_value=inflation_bp, max_value=1_000))
    real_return_bp = draw(st.integers(min_value=-1_800, max_value=4_800))
    rule = draw(st.sampled_from([Rule.RETURNS_COVER_EXPENSES, Rule.SWR]))
    swr_bp = (
        draw(st.integers(min_value=SWR_HARD_MIN_BP, max_value=SWR_HARD_MAX_BP))
        if rule is Rule.SWR
        else None
    )
    age = draw(st.integers(min_value=18, max_value=40))
    horizon_age = draw(st.integers(min_value=age + 1, max_value=90))
    return ProjectionInputs(
        currency="EUR",
        birth_year=2026 - age,
        start_year=2026,
        horizon_age=horizon_age,
        stop_working_age=horizon_age,
        inflation_bp=inflation_bp,
        real_return_bp=real_return_bp,
        income_growth_bp=income_growth_bp,
        rule=rule,
        swr_bp=swr_bp,
        start_income=Money(start_expenses * 2, "EUR"),
        start_expenses=Money(start_expenses, "EUR"),
        start_portfolio=Money(draw(st.integers(min_value=0, max_value=10_000_000_000)), "EUR"),
    )


@settings(max_examples=25, deadline=None)
@given(inputs=_non_negative_inputs())
def test_a_higher_real_return_never_delays_fire(inputs: ProjectionInputs) -> None:
    grid = sensitivity(inputs, DEFAULT_RETURN_DELTAS_BP, DEFAULT_EXPENSE_FACTORS_BP)
    for row in grid.rows:
        ages = [_as_infinity(cell.fire_age) for cell in row.cells]
        assert ages == sorted(ages, reverse=True), (row.expense_factor_bp, ages)


@settings(max_examples=25, deadline=None)
@given(inputs=_non_negative_inputs())
def test_higher_expenses_never_accelerate_fire(inputs: ProjectionInputs) -> None:
    grid = sensitivity(inputs, DEFAULT_RETURN_DELTAS_BP, DEFAULT_EXPENSE_FACTORS_BP)
    for column in range(len(grid.return_deltas_bp)):
        ages = [_as_infinity(row.cells[column].fire_age) for row in grid.rows]
        assert ages == sorted(ages), (grid.return_deltas_bp[column], ages)
