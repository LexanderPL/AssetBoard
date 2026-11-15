"""Property tests for the pure FIRE projection engine (F8.2)."""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from math import ceil, floor

from hypothesis import given, settings
from hypothesis import strategies as st

from assetboard.domain.fire import (
    MAX_HORIZON_AGE,
    RATE_HARD_MAX_BP,
    RATE_HARD_MIN_BP,
    SWR_HARD_MAX_BP,
    SWR_HARD_MIN_BP,
    EventKind,
    ProjectionEvent,
    ProjectionInputs,
    ProjectionLoan,
    Rule,
    project,
)
from assetboard.domain.money import Money


def _half_up(value: Fraction) -> int:
    if value >= 0:
        return floor(value + Fraction(1, 2))
    return ceil(value - Fraction(1, 2))


@st.composite
def _projection_inputs(
    draw: st.DrawFn,
    *,
    zero_rates: bool = False,
    with_overrides: bool = True,
    with_events: bool = True,
) -> ProjectionInputs:
    start_year = draw(st.integers(min_value=2000, max_value=2090))
    age = draw(st.integers(min_value=18, max_value=69))
    birth_year = start_year - age
    horizon_age = age + draw(st.integers(min_value=1, max_value=MAX_HORIZON_AGE - age))
    stop_working_age = age + draw(st.integers(min_value=0, max_value=50))
    last_year = birth_year + horizon_age

    if zero_rates:
        inflation_bp = 0
        real_return_bp = 0
        income_growth_bp = 0
        rule = Rule.RETURNS_COVER_EXPENSES
        swr_bp = None
    else:
        inflation_bp = draw(st.integers(RATE_HARD_MIN_BP, RATE_HARD_MAX_BP))
        real_return_bp = draw(st.integers(RATE_HARD_MIN_BP, RATE_HARD_MAX_BP))
        income_growth_bp = draw(st.integers(RATE_HARD_MIN_BP, RATE_HARD_MAX_BP))
        rule = draw(st.sampled_from([Rule.RETURNS_COVER_EXPENSES, Rule.SWR]))
        swr_bp = draw(st.integers(SWR_HARD_MIN_BP, SWR_HARD_MAX_BP)) if rule is Rule.SWR else None

    events: list[ProjectionEvent] = []
    if with_overrides:
        years = draw(
            st.lists(
                st.integers(min_value=start_year + 1, max_value=last_year),
                max_size=4,
                unique=True,
            )
        )
        events.extend(
            ProjectionEvent(
                year,
                EventKind.SAVING_OVERRIDE,
                Money(draw(st.integers(-1_000_000_000, 1_000_000_000)), "EUR"),
            )
            for year in years
        )
    if with_events:
        rows = draw(
            st.lists(
                st.tuples(
                    st.integers(min_value=start_year, max_value=last_year),
                    st.sampled_from(
                        [
                            EventKind.CASH,
                            EventKind.INCOME_CHANGE,
                            EventKind.EXPENSE_CHANGE,
                        ]
                    ),
                    st.integers(-1_000_000_000, 1_000_000_000),
                ),
                max_size=4,
            )
        )
        events.extend(
            ProjectionEvent(year, kind, Money(amount, "EUR")) for year, kind, amount in rows
        )

    return ProjectionInputs(
        currency="EUR",
        birth_year=birth_year,
        start_year=start_year,
        horizon_age=horizon_age,
        stop_working_age=stop_working_age,
        inflation_bp=inflation_bp,
        real_return_bp=real_return_bp,
        income_growth_bp=income_growth_bp,
        rule=rule,
        swr_bp=swr_bp,
        start_income=Money(draw(st.integers(0, 1_000_000_000)), "EUR"),
        start_expenses=Money(draw(st.integers(0, 1_000_000_000)), "EUR"),
        start_portfolio=Money(draw(st.integers(0, 1_000_000_000)), "EUR"),
        events=tuple(events),
    )


@settings(max_examples=50, deadline=None)
@given(inputs=_projection_inputs(zero_rates=True, with_events=False))
def test_zero_return_and_inflation_equals_start_plus_cumulative_savings(
    inputs: ProjectionInputs,
) -> None:
    result = project(inputs)
    start = inputs.start_portfolio.amount_minor
    # The t=0 net column is the start value, not a saving, so it is not counted twice.
    assert result.rows[0].net_worth.amount_minor == start
    running = start
    for row in result.rows[1:]:
        assert row.gain.amount_minor == 0
        running += row.net.amount_minor
        assert row.net_worth.amount_minor == running


@settings(max_examples=50, deadline=None)
@given(inputs=_projection_inputs(with_overrides=False, with_events=False))
def test_without_events_or_overrides_the_parity_recurrence_holds(
    inputs: ProjectionInputs,
) -> None:
    result = project(inputs)
    first = result.rows[0]
    gain_bp = inputs.inflation_bp + inputs.real_return_bp
    assert first.net.amount_minor == inputs.start_portfolio.amount_minor
    assert first.gain.amount_minor == _half_up(
        Fraction(inputs.start_portfolio.amount_minor * gain_bp, 10_000)
    )
    assert first.net_worth == inputs.start_portfolio + first.gain
    previous = first
    for row in result.rows[1:]:
        assert row.net == row.income - row.expenses
        assert row.gain.amount_minor == _half_up(
            Fraction(previous.net_worth.amount_minor * gain_bp, 10_000)
        )
        assert row.net_worth == previous.net_worth + row.net + row.gain
        previous = row


@settings(max_examples=50, deadline=None)
@given(inputs=_projection_inputs())
def test_nominal_fire_metrics_follow_the_fire_row(inputs: ProjectionInputs) -> None:
    result = project(inputs)
    if result.fire_index is None:
        assert result.fire_number is None
        assert result.plan_progress_bp is None
        return
    fire_row = result.rows[result.fire_index]
    rate_bp = (
        inputs.swr_bp if inputs.rule is Rule.SWR else inputs.inflation_bp + inputs.real_return_bp
    )
    if rate_bp is not None and rate_bp > 0:
        assert result.fire_number == Money(
            _half_up(Fraction(fire_row.expenses.amount_minor * 10_000, rate_bp)), "EUR"
        )
    else:
        assert result.fire_number is None
    if fire_row.net_worth.amount_minor > 0:
        expected_progress = _half_up(
            Fraction(
                inputs.start_portfolio.amount_minor * 10_000,
                fire_row.net_worth.amount_minor,
            )
        )
        assert result.plan_progress_bp == expected_progress
        assert expected_progress >= 0
    else:
        assert result.plan_progress_bp is None


@settings(max_examples=50, deadline=None)
@given(inputs=_projection_inputs(with_overrides=False, with_events=False))
def test_an_included_loan_never_increases_expenses_and_drops_at_payoff(
    inputs: ProjectionInputs,
) -> None:
    annual = 1_000_000
    payoff_year = inputs.start_year + 2
    loaned = replace(
        inputs,
        loans=(ProjectionLoan(Money(annual, "EUR"), payoff_year, True),),
    )
    before = project(inputs)
    after = project(loaned)
    for plain, with_loan in zip(before.rows, after.rows, strict=True):
        if with_loan.year < payoff_year:
            assert with_loan.expenses == plain.expenses
            assert with_loan.loan_payment == Money(annual, "EUR")
        else:
            assert with_loan.expenses.amount_minor == plain.expenses.amount_minor - annual
            assert with_loan.loan_payment == Money(0, "EUR")
