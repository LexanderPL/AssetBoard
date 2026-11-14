"""Property tests for the pure FIRE projection engine (F8.2)."""

from __future__ import annotations

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
    ContributionOverride,
    EventKind,
    ProjectionEvent,
    ProjectionInputs,
    Rule,
    project,
)
from assetboard.domain.money import Money


def _half_up(value: Fraction) -> int:
    if value >= 0:
        return floor(value + Fraction(1, 2))
    return ceil(value - Fraction(1, 2))


def _deflate(value: int, inflation_bp: int, years: int) -> int:
    return _half_up(Fraction(value) * Fraction(10_000, 10_000 + inflation_bp) ** years)


@st.composite
def _projection_inputs(
    draw: st.DrawFn,
    *,
    zero_rates: bool = False,
    with_contributions: bool = True,
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

    contributions: tuple[ContributionOverride, ...] = ()
    if with_contributions:
        years = draw(
            st.lists(
                st.integers(min_value=start_year + 1, max_value=last_year),
                max_size=4,
                unique=True,
            )
        )
        contributions = tuple(
            ContributionOverride(
                year, Money(draw(st.integers(-1_000_000_000, 1_000_000_000)), "EUR")
            )
            for year in years
        )
    events: tuple[ProjectionEvent, ...] = ()
    if with_events:
        rows = draw(
            st.lists(
                st.tuples(
                    st.integers(min_value=start_year, max_value=last_year),
                    st.sampled_from(list(EventKind)),
                    st.integers(-1_000_000_000, 1_000_000_000),
                ),
                max_size=4,
            )
        )
        events = tuple(
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
        contributions=contributions,
        events=events,
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
@given(inputs=_projection_inputs(with_contributions=False, with_events=False))
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
def test_real_values_are_the_nominal_line_deflated_by_inflation(
    inputs: ProjectionInputs,
) -> None:
    result = project(inputs)
    for offset, row in enumerate(result.rows):
        for nominal, real in (
            (row.income, row.real_income),
            (row.expenses, row.real_expenses),
            (row.net, row.real_net),
            (row.gain, row.real_gain),
            (row.cash, row.real_cash),
            (row.net_worth, row.real_net_worth),
        ):
            assert real.amount_minor == _deflate(
                nominal.amount_minor, inputs.inflation_bp, offset
            ), (row.year, nominal, real)
