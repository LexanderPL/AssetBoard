"""Golden and edge tests for the pure FIRE projection engine (F8.2).

The oracle below is a literal transcription of the formulas in docs/specs/M8-fire-planner.md
using exact ``Fraction`` values and its own half-up helper. It deliberately does not import or
call any helper from the engine, so a rounding or recurrence mistake in the engine shows up as a
row-by-row mismatch.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from fractions import Fraction
from math import ceil, floor
from typing import Any, cast, get_type_hints

import pytest

from assetboard.domain.fire import (
    MAX_YEAR,
    ContributionOverride,
    EventKind,
    FireProjectionError,
    Projection,
    ProjectionEvent,
    ProjectionInputs,
    ProjectionRow,
    Rule,
    project,
)
from assetboard.domain.money import Money
from assetboard.domain.transactions import MAX_AMOUNT_MINOR


def _eur(minor: int) -> Money:
    return Money(minor, "EUR")


def _half_up(value: Fraction) -> int:
    """Round half away from zero, the projection's one rounding rule."""
    if value >= 0:
        return floor(value + Fraction(1, 2))
    return ceil(value - Fraction(1, 2))


def _deflate_oracle(value: int, inflation: Fraction, years: int) -> int:
    return _half_up(Fraction(value) / inflation**years)


def _oracle(inputs: ProjectionInputs) -> Projection:
    current_age = inputs.start_year - inputs.birth_year
    last_year = inputs.birth_year + inputs.horizon_age
    gain_bp = inputs.inflation_bp + inputs.real_return_bp
    inflation = Fraction(10_000 + inputs.inflation_bp, 10_000)
    growth = Fraction(10_000 + inputs.income_growth_bp, 10_000)

    overrides = {row.year: row.amount.amount_minor for row in inputs.contributions}
    cash: defaultdict[int, int] = defaultdict(int)
    income_changes: defaultdict[int, int] = defaultdict(int)
    expense_changes: defaultdict[int, int] = defaultdict(int)
    for event in inputs.events:
        amount = event.amount.amount_minor
        if event.kind is EventKind.CASH:
            cash[event.year] += amount
        elif event.kind is EventKind.INCOME_CHANGE:
            income_changes[event.year] += amount
        else:
            expense_changes[event.year] += amount

    income_exact = Fraction(inputs.start_income.amount_minor)
    expenses_exact = Fraction(inputs.start_expenses.amount_minor)
    rows: list[ProjectionRow] = []
    fire_index: int | None = None
    previous_net_worth = 0
    for offset, year in enumerate(range(inputs.start_year, last_year + 1)):
        age = year - inputs.birth_year
        if offset > 0:
            if age < inputs.stop_working_age:
                income_exact *= growth
            else:
                income_exact = Fraction(0)
            expenses_exact *= inflation

        income = _half_up(income_exact)
        for event_year, amount in income_changes.items():
            if event_year <= year:
                income += _half_up(Fraction(amount) * growth**offset)
        expenses = _half_up(expenses_exact)
        for event_year, amount in expense_changes.items():
            if event_year <= year:
                expenses += _half_up(Fraction(amount) * inflation**offset)

        if offset == 0:
            net = inputs.start_portfolio.amount_minor
            opening = inputs.start_portfolio.amount_minor
        else:
            net = overrides.get(year, income - expenses)
            opening = previous_net_worth
        cash_amount = cash[year]
        gain = _half_up(Fraction(opening * gain_bp, 10_000))
        net_worth = net + gain + cash_amount if offset == 0 else opening + net + gain + cash_amount

        is_fire = False
        if fire_index is None and opening >= 0:
            if inputs.rule is Rule.SWR:
                assert inputs.swr_bp is not None
                is_fire = opening * inputs.swr_bp >= expenses * 10_000
            else:
                is_fire = expenses < gain
            if is_fire:
                fire_index = offset

        rows.append(
            ProjectionRow(
                year=year,
                age=age,
                income=_eur(income),
                expenses=_eur(expenses),
                net=_eur(net),
                gain=_eur(gain),
                cash=_eur(cash_amount),
                net_worth=_eur(net_worth),
                real_income=_eur(_deflate_oracle(income, inflation, offset)),
                real_expenses=_eur(_deflate_oracle(expenses, inflation, offset)),
                real_net=_eur(_deflate_oracle(net, inflation, offset)),
                real_gain=_eur(_deflate_oracle(gain, inflation, offset)),
                real_cash=_eur(_deflate_oracle(cash_amount, inflation, offset)),
                real_net_worth=_eur(_deflate_oracle(net_worth, inflation, offset)),
                is_fire=is_fire,
                income_stopped=offset > 0 and age >= inputs.stop_working_age,
            )
        )
        previous_net_worth = net_worth

    fire_row = rows[fire_index] if fire_index is not None else None
    first_expenses = rows[0].expenses.amount_minor
    fire_number: Money | None
    if first_expenses == 0:
        fire_number = _eur(0)
    elif inputs.rule is Rule.SWR:
        assert inputs.swr_bp is not None
        fire_number = _eur(_half_up(Fraction(first_expenses * 10_000, inputs.swr_bp)))
    elif gain_bp > 0:
        fire_number = _eur(_half_up(Fraction(first_expenses * 10_000, gain_bp)))
    else:
        fire_number = None

    progress_sheet: int | None = None
    if fire_row is not None and fire_row.net_worth.amount_minor > 0:
        progress_sheet = _half_up(
            Fraction(rows[0].net_worth.amount_minor * 10_000, fire_row.net_worth.amount_minor)
        )
    progress_today: int | None = None
    if fire_number is not None and fire_number.amount_minor > 0:
        progress_today = _half_up(
            Fraction(inputs.start_portfolio.amount_minor * 10_000, fire_number.amount_minor)
        )

    retirement_start = max(1, inputs.stop_working_age - current_age)
    retirement = rows[retirement_start:]
    depletion_age = next((row.age for row in retirement if row.net_worth.amount_minor < 0), None)
    min_after_stop = min((row.net_worth.amount_minor for row in retirement), default=None)

    return Projection(
        inputs=inputs,
        rows=tuple(rows),
        fire_index=fire_index,
        fire_year=inputs.start_year + fire_index if fire_index is not None else None,
        fire_age=current_age + fire_index if fire_index is not None else None,
        years_to_fire=fire_index,
        networth_at_fire=fire_row.net_worth if fire_row is not None else None,
        real_networth_at_fire=fire_row.real_net_worth if fire_row is not None else None,
        fire_number_today=fire_number,
        progress_sheet_bp=progress_sheet,
        progress_today_bp=progress_today,
        depletion_age=depletion_age,
        min_networth_after_stop=(_eur(min_after_stop) if min_after_stop is not None else None),
    )


_ROW_FIELDS = (
    "year",
    "age",
    "income",
    "expenses",
    "net",
    "gain",
    "cash",
    "net_worth",
    "real_income",
    "real_expenses",
    "real_net",
    "real_gain",
    "real_cash",
    "real_net_worth",
    "is_fire",
    "income_stopped",
)
_METRIC_FIELDS = (
    "fire_index",
    "fire_year",
    "fire_age",
    "years_to_fire",
    "networth_at_fire",
    "real_networth_at_fire",
    "fire_number_today",
    "progress_sheet_bp",
    "progress_today_bp",
    "depletion_age",
    "min_networth_after_stop",
)


def _assert_matches_oracle(inputs: ProjectionInputs) -> Projection:
    result = project(inputs)
    expected = _oracle(inputs)
    assert result.inputs == expected.inputs
    assert len(result.rows) == len(expected.rows)
    for got, want in zip(result.rows, expected.rows, strict=True):
        for field in _ROW_FIELDS:
            assert getattr(got, field) == getattr(want, field), (got.year, field)
    for field in _METRIC_FIELDS:
        assert getattr(result, field) == getattr(expected, field), field
    return result


_BASE = ProjectionInputs(
    currency="EUR",
    birth_year=1985,
    start_year=2026,
    horizon_age=90,
    stop_working_age=55,
    inflation_bp=250,
    real_return_bp=500,
    income_growth_bp=300,
    rule=Rule.RETURNS_COVER_EXPENSES,
    swr_bp=None,
    start_income=_eur(6_000_000),
    start_expenses=_eur(4_000_000),
    start_portfolio=_eur(10_000_000),
)


def _variant(**changes: Any) -> ProjectionInputs:
    return replace(_BASE, **changes)


_G2 = _variant(
    stop_working_age=50,
    real_return_bp=100,
    start_income=_eur(5_000_000),
    start_expenses=_eur(8_000_000),
    start_portfolio=_eur(1_000_000),
)
_G3 = _variant(
    contributions=tuple(
        ContributionOverride(
            2027 + index,
            _eur(amount),
        )
        for index, amount in enumerate(
            (300_000, 0, -100_000, 400_000, -250_000, 600_000, 150_000, -50_000, 0, 900_000)
        )
    )
)
_G4 = _variant(
    events=(
        ProjectionEvent(2026, EventKind.CASH, _eur(500_000)),
        ProjectionEvent(2026, EventKind.EXPENSE_CHANGE, _eur(-200_000)),
        ProjectionEvent(2030, EventKind.CASH, _eur(-300_000)),
        ProjectionEvent(2032, EventKind.INCOME_CHANGE, _eur(1_200_000)),
        ProjectionEvent(2032, EventKind.INCOME_CHANGE, _eur(300_000)),
        ProjectionEvent(2035, EventKind.EXPENSE_CHANGE, _eur(500_000)),
    )
)
_G5 = _variant(
    rule=Rule.SWR,
    swr_bp=400,
    start_portfolio=_eur(50_000_000),
)


@pytest.mark.parametrize(
    "inputs",
    [
        pytest.param(_BASE, id="G1-parity"),
        pytest.param(_G2, id="G2-never-fire-depletion"),
        pytest.param(_G3, id="G3-first-ten-years-overrides"),
        pytest.param(_G4, id="G4-events"),
        pytest.param(_G5, id="G5-swr"),
    ],
)
def test_golden_sets_match_the_independent_oracle(inputs: ProjectionInputs) -> None:
    _assert_matches_oracle(inputs)


def test_parity_t0_and_key_metrics_are_hand_checkable() -> None:
    result = project(_BASE)
    first = result.rows[0]
    assert (first.year, first.age) == (2026, 41)
    assert (first.income.amount_minor, first.expenses.amount_minor, first.net.amount_minor) == (
        6_000_000,
        4_000_000,
        10_000_000,
    )
    # 10,000,000 * (2.5% + 5%) = 750,000.
    assert first.gain.amount_minor == 750_000
    assert first.net_worth.amount_minor == 10_750_000
    assert first.real_income == first.income
    assert first.real_net_worth == first.net_worth
    assert (result.fire_index, result.fire_year, result.fire_age) == (14, 2040, 55)
    assert result.years_to_fire == 14
    # 4,000,000 / 7.5% = 53,333,333.33..., rounded once.
    assert result.fire_number_today == _eur(53_333_333)
    assert result.progress_sheet_bp == 1328
    assert result.progress_today_bp == 1875
    assert result.depletion_age == 81
    assert result.rows[-1].year == 2075


def _edge(**changes: Any) -> ProjectionInputs:
    return replace(
        ProjectionInputs(
            currency="EUR",
            birth_year=1985,
            start_year=2026,
            horizon_age=50,
            stop_working_age=60,
            inflation_bp=0,
            real_return_bp=0,
            income_growth_bp=0,
            rule=Rule.RETURNS_COVER_EXPENSES,
            swr_bp=None,
            start_income=_eur(0),
            start_expenses=_eur(0),
            start_portfolio=_eur(0),
        ),
        **changes,
    )


def test_t0_reuses_the_net_column_and_cash_starts_earning_next_year() -> None:
    inputs = _edge(
        horizon_age=43,
        real_return_bp=1_000,
        start_income=_eur(500_000),
        start_expenses=_eur(500_000),
        start_portfolio=_eur(1_000_000),
        events=(ProjectionEvent(2026, EventKind.CASH, _eur(100_000)),),
    )
    result = project(inputs)
    first, second = result.rows[:2]
    assert first.net == _eur(1_000_000)
    assert first.gain == _eur(100_000)
    assert first.cash == _eur(100_000)
    assert first.net_worth == _eur(1_200_000)
    assert first.real_net_worth == first.net_worth
    # The cash added after the gain earns only from the following year.
    assert second.gain == _eur(120_000)
    assert second.net_worth == _eur(1_320_000)


def test_every_row_keeps_the_net_worth_recurrence() -> None:
    result = project(_G4)
    previous: Money | None = None
    for row in result.rows:
        if previous is None:
            assert row.net_worth == row.net + row.gain + row.cash
        else:
            assert row.net_worth == previous + row.net + row.gain + row.cash
        previous = row.net_worth


def test_an_override_replaces_the_net_line_but_keeps_cash_and_shown_series() -> None:
    inputs = _edge(
        horizon_age=43,
        start_income=_eur(100_000),
        start_expenses=_eur(40_000),
        start_portfolio=_eur(1_000_000),
        contributions=(
            ContributionOverride(2027, _eur(0)),
            ContributionOverride(2028, _eur(-200_000)),
        ),
        events=(ProjectionEvent(2027, EventKind.CASH, _eur(50_000)),),
    )
    result = project(inputs)
    zero_override, negative_override = result.rows[1], result.rows[2]
    assert (zero_override.income, zero_override.expenses) == (_eur(100_000), _eur(40_000))
    assert zero_override.net == _eur(0)
    assert zero_override.cash == _eur(50_000)
    assert zero_override.net_worth == _eur(1_050_000)
    assert (negative_override.income, negative_override.expenses) == (
        _eur(100_000),
        _eur(40_000),
    )
    assert negative_override.net == _eur(-200_000)
    assert negative_override.net_worth == _eur(850_000)


def test_event_changes_are_inflated_from_the_start_year_and_start_at_their_year() -> None:
    inputs = _edge(
        horizon_age=46,
        inflation_bp=1_000,
        income_growth_bp=2_000,
        start_income=_eur(100_000),
        start_expenses=_eur(100_000),
        events=(
            ProjectionEvent(2028, EventKind.EXPENSE_CHANGE, _eur(10_000)),
            ProjectionEvent(2030, EventKind.INCOME_CHANGE, _eur(5_000)),
        ),
    )
    rows = project(inputs).rows
    # 2027 (t=1): no event yet; both series have grown once.
    assert rows[1].income == _eur(120_000)
    assert rows[1].expenses == _eur(110_000)
    # The 2028 expense change is today's money, so at t=2 it is 10,000 * 1.1^2.
    assert rows[2].expenses == _eur(121_000 + 12_100)
    assert rows[3].expenses == _eur(133_100 + 13_310)
    assert rows[3].income == _eur(172_800)
    # The 2030 income change is today's money too: 5,000 * 1.2^4.
    assert rows[4].income == _eur(207_360 + 10_368)
    assert rows[4].expenses == _eur(146_410 + 14_641)


def test_same_kind_and_year_events_are_summed_before_the_single_rounding() -> None:
    inputs = _edge(
        horizon_age=43,
        inflation_bp=5_000,
        events=(
            ProjectionEvent(2027, EventKind.EXPENSE_CHANGE, _eur(1)),
            ProjectionEvent(2027, EventKind.EXPENSE_CHANGE, _eur(1)),
        ),
    )
    # Summed first: round(2 * 1.5) = 3. Rounding separately would give 2 + 2 = 4.
    assert project(inputs).rows[1].expenses == _eur(3)


def test_income_changes_continue_after_the_salary_stop() -> None:
    inputs = _edge(
        horizon_age=45,
        stop_working_age=42,
        start_income=_eur(100_000),
        events=(ProjectionEvent(2027, EventKind.INCOME_CHANGE, _eur(10_000)),),
    )
    rows = project(inputs).rows
    assert rows[0].income == _eur(100_000)
    assert not rows[0].income_stopped
    assert rows[1].income == _eur(10_000)
    assert rows[1].income_stopped
    assert rows[2].income == _eur(10_000)
    assert rows[2].income_stopped


def test_negative_overrides_and_series_are_not_clamped() -> None:
    inputs = _edge(
        horizon_age=43,
        start_income=_eur(100_000),
        start_expenses=_eur(50_000),
        contributions=(ContributionOverride(2027, _eur(-300_000)),),
        events=(ProjectionEvent(2026, EventKind.EXPENSE_CHANGE, _eur(-200_000)),),
    )
    rows = project(inputs).rows
    assert rows[0].expenses == _eur(-150_000)
    assert rows[1].expenses == _eur(-150_000)
    assert rows[1].income == _eur(100_000)
    assert rows[1].net == _eur(-300_000)


def test_returns_rule_uses_strict_less_than_and_the_previous_net_worth() -> None:
    inputs = _edge(
        horizon_age=43,
        real_return_bp=1_000,
        start_income=_eur(100_000),
        start_expenses=_eur(100_000),
        start_portfolio=_eur(1_000_000),
    )
    result = project(inputs)
    # At t=0 expenses (100,000) equal the gain on the starting portfolio exactly: not FIRE.
    assert not result.rows[0].is_fire
    # At t=1 the gain is 10% of 1,100,000, which is strictly above expenses.
    assert result.rows[1].is_fire
    assert result.fire_index == 1


def test_swr_rule_is_inclusive_and_uses_the_previous_net_worth() -> None:
    boundary = _edge(
        horizon_age=43,
        rule=Rule.SWR,
        swr_bp=400,
        start_expenses=_eur(40_000),
        start_portfolio=_eur(1_000_000),
    )
    result = project(boundary)
    # 1,000,000 * 400 == 40,000 * 10,000 exactly: the >= boundary fires at t=0.
    assert result.rows[0].is_fire
    assert result.fire_index == 0

    later = _edge(
        horizon_age=44,
        rule=Rule.SWR,
        swr_bp=400,
        real_return_bp=500,
        start_income=_eur(40_000),
        start_expenses=_eur(40_000),
        start_portfolio=_eur(900_000),
    )
    result = project(later)
    assert [row.is_fire for row in result.rows] == [False, False, False, True]
    # t=2 used the previous row's 992,250 opening and rounded 49,612.5 half up to 49,613.
    assert result.rows[2].gain == _eur(49_613)
    assert result.fire_index == 3


def test_negative_opening_net_worth_blocks_the_fire_predicate() -> None:
    inputs = _edge(
        horizon_age=45,
        start_expenses=_eur(10_000),
        start_portfolio=_eur(100_000),
        contributions=(ContributionOverride(2027, _eur(-200_000)),),
        events=(ProjectionEvent(2028, EventKind.EXPENSE_CHANGE, _eur(-20_000)),),
    )
    result = project(inputs)
    assert result.rows[1].net_worth == _eur(-100_000)
    assert result.rows[2].expenses == _eur(-10_000)
    # expenses < gain would be true, but the opening net worth is negative.
    assert not result.rows[2].is_fire
    assert result.fire_index is None


def test_fire_can_happen_at_the_horizon() -> None:
    inputs = _edge(
        horizon_age=43,
        rule=Rule.SWR,
        swr_bp=400,
        real_return_bp=1_000,
        start_income=_eur(40_000),
        start_expenses=_eur(40_000),
        start_portfolio=_eur(900_000),
    )
    result = project(inputs)
    assert result.fire_index == 2
    assert (result.fire_year, result.fire_age, result.years_to_fire) == (2028, 43, 2)
    assert result.rows[-1].is_fire


def test_cash_does_not_enter_the_fire_predicate_in_its_event_year() -> None:
    inputs = _edge(
        horizon_age=43,
        real_return_bp=1_000,
        start_expenses=_eur(50_000),
        events=(ProjectionEvent(2026, EventKind.CASH, _eur(1_000_000)),),
    )
    result = project(inputs)
    # If cash were added before the gain, t=0 would fire with a 100,000 gain.
    assert result.rows[0].gain == _eur(0)
    assert not result.rows[0].is_fire
    # It earns from the next year, where the gain covers expenses.
    assert result.rows[1].gain == _eur(100_000)
    assert result.rows[1].is_fire


def test_never_fire_leaves_the_fire_metrics_none_but_keeps_todays_progress() -> None:
    result = project(_G2)
    assert result.fire_index is None
    assert result.fire_year is None
    assert result.fire_age is None
    assert result.years_to_fire is None
    assert result.networth_at_fire is None
    assert result.real_networth_at_fire is None
    assert result.progress_sheet_bp is None
    assert result.fire_number_today == _eur(228_571_429)
    assert result.progress_today_bp == 44
    assert result.depletion_age == 50
    assert result.min_networth_after_stop is not None


def test_fire_number_none_without_a_positive_gain_rate_and_zero_expenses() -> None:
    no_gain = _edge(horizon_age=43, start_expenses=_eur(100_000))
    result = project(no_gain)
    assert result.fire_number_today is None
    assert result.progress_today_bp is None

    zero_expenses = _edge(
        horizon_age=43,
        real_return_bp=-100,
        start_expenses=_eur(0),
    )
    result = project(zero_expenses)
    assert result.fire_number_today == _eur(0)
    assert result.progress_today_bp is None

    swr_zero = _edge(
        horizon_age=43,
        rule=Rule.SWR,
        swr_bp=400,
        start_expenses=_eur(0),
    )
    assert project(swr_zero).fire_number_today == _eur(0)


def test_depletion_window_starts_at_the_stop_row_inclusive() -> None:
    inputs = _edge(
        horizon_age=45,
        stop_working_age=42,
        start_expenses=_eur(10_000),
    )
    result = project(inputs)
    assert result.rows[0].net_worth == _eur(0)
    assert result.rows[1].net_worth == _eur(-10_000)
    assert result.depletion_age == 42
    assert result.min_networth_after_stop == _eur(-40_000)


def test_depletion_window_never_counts_the_start_row() -> None:
    inputs = _edge(
        horizon_age=45,
        stop_working_age=41,
        start_expenses=_eur(10_000),
        events=(ProjectionEvent(2026, EventKind.CASH, _eur(-100_000)),),
    )
    result = project(inputs)
    assert result.rows[0].net_worth == _eur(-100_000)
    # The window starts at t=1 even though the stop age equals the age at the start.
    assert result.depletion_age == 42


def test_no_retirement_rows_when_the_stop_age_is_after_the_horizon() -> None:
    inputs = _edge(horizon_age=45, stop_working_age=90)
    result = project(inputs)
    assert result.rows[-1].age == 45
    assert result.depletion_age is None
    assert result.min_networth_after_stop is None


def test_rows_run_to_the_horizon_after_fire_and_income_stop() -> None:
    result = project(_BASE)
    assert len(result.rows) == 50
    assert (result.rows[0].year, result.rows[-1].year) == (2026, 2075)
    assert result.fire_index == 14
    assert sum(row.is_fire for row in result.rows) == 1
    assert result.rows[-1].income_stopped
    assert result.rows[-1].income == _eur(0)


def test_half_up_rounds_negative_values_away_from_zero() -> None:
    negative_gain = _edge(
        horizon_age=43,
        real_return_bp=5_000,
        events=(ProjectionEvent(2026, EventKind.CASH, _eur(-3)),),
    )
    result = project(negative_gain)
    assert result.rows[0].net_worth == _eur(-3)
    # -3 * 50% = -1.5, rounded away from zero.
    assert result.rows[1].gain == _eur(-2)

    negative_expense = _edge(
        horizon_age=43,
        inflation_bp=5_000,
        events=(ProjectionEvent(2027, EventKind.EXPENSE_CHANGE, _eur(-1)),),
    )
    # -1 * 1.5 = -1.5, rounded away from zero.
    assert project(negative_expense).rows[1].expenses == _eur(-2)


def test_the_projection_contract_uses_integers_for_money_and_rates() -> None:
    hints = []
    for cls in (
        ContributionOverride,
        ProjectionEvent,
        ProjectionInputs,
        ProjectionRow,
        Projection,
    ):
        hints.extend(get_type_hints(cls).values())
    assert all("float" not in str(hint) for hint in hints)
    result = project(_BASE)
    assert type(result.rows[0].net_worth.amount_minor) is int
    assert type(result.fire_index) is int


@pytest.mark.parametrize(
    ("changes", "field", "code"),
    [
        ({"currency": "ZZZ"}, "currency", "invalid_currency"),
        ({"start_income": Money(1, "USD")}, "start_income", "invalid_currency"),
        ({"birth_year": 2027}, "birth_year", "invalid_birth_year"),
        ({"birth_year": 1899}, "birth_year", "invalid_birth_year"),
        ({"start_year": 2200}, "start_year", "invalid_start_year"),
        ({"horizon_age": 41}, "horizon_age", "horizon_not_after_age"),
        ({"horizon_age": 0}, "horizon_age", "invalid_horizon"),
        ({"horizon_age": 121}, "horizon_age", "invalid_horizon"),
        (
            {"start_year": 2199, "birth_year": 2100, "horizon_age": 120},
            "horizon_age",
            "invalid_horizon",
        ),
        ({"stop_working_age": 40}, "stop_working_age", "stop_before_age"),
        ({"stop_working_age": 121}, "stop_working_age", "invalid_stop_age"),
        ({"stop_working_age": -1}, "stop_working_age", "invalid_stop_age"),
        ({"inflation_bp": -2_001}, "inflation_bp", "invalid_rate"),
        ({"real_return_bp": 5_001}, "real_return_bp", "invalid_rate"),
        ({"income_growth_bp": -2_001}, "income_growth_bp", "invalid_rate"),
        ({"rule": "bogus"}, "rule", "invalid_rule"),
        ({"rule": Rule.SWR, "swr_bp": None}, "swr_bp", "invalid_swr"),
        ({"rule": Rule.SWR, "swr_bp": 49}, "swr_bp", "invalid_swr"),
        ({"rule": Rule.SWR, "swr_bp": 2_001}, "swr_bp", "invalid_swr"),
        ({"swr_bp": 400}, "swr_bp", "invalid_swr"),
        ({"start_income": _eur(-1)}, "start_income", "invalid_amount"),
        ({"start_expenses": _eur(MAX_AMOUNT_MINOR + 1)}, "start_expenses", "invalid_amount"),
        ({"start_portfolio": _eur(-1)}, "start_portfolio", "invalid_amount"),
        (
            {"contributions": (ContributionOverride(2026, _eur(1)),)},
            "contributions",
            "rows_outside_window",
        ),
        (
            {"contributions": (ContributionOverride(2076, _eur(1)),)},
            "contributions",
            "rows_outside_window",
        ),
        (
            {
                "contributions": (
                    ContributionOverride(2027, _eur(1)),
                    ContributionOverride(2027, _eur(2)),
                )
            },
            "contributions",
            "duplicate_contribution_year",
        ),
        (
            {"contributions": (ContributionOverride(2027, _eur(MAX_AMOUNT_MINOR + 1)),)},
            "contributions",
            "invalid_amount",
        ),
        (
            {"contributions": (ContributionOverride(2027, Money(1, "USD")),)},
            "contributions",
            "invalid_currency",
        ),
        (
            {"events": (ProjectionEvent(2025, EventKind.CASH, _eur(1)),)},
            "events",
            "rows_outside_window",
        ),
        (
            {"events": (ProjectionEvent(2076, EventKind.CASH, _eur(1)),)},
            "events",
            "rows_outside_window",
        ),
        (
            {"events": (ProjectionEvent(2026, cast(EventKind, "bogus"), _eur(1)),)},
            "events",
            "invalid_event_kind",
        ),
        (
            {"events": (ProjectionEvent(2026, EventKind.CASH, _eur(MAX_AMOUNT_MINOR + 1)),)},
            "events",
            "invalid_amount",
        ),
        (
            {"events": (ProjectionEvent(2026, EventKind.CASH, Money(1, "USD")),)},
            "events",
            "invalid_currency",
        ),
        ({"inflation_bp": "250"}, "inflation_bp", "invalid_rate"),
        ({"horizon_age": "90"}, "horizon_age", "invalid_horizon"),
    ],
)
def test_validation_reports_the_field_and_code(
    changes: dict[str, Any], field: str, code: str
) -> None:
    with pytest.raises(FireProjectionError) as error:
        project(replace(_BASE, **changes))
    assert (error.value.field, error.value.code) == (field, code)


def test_max_year_is_the_last_supported_horizon_year() -> None:
    inputs = _edge(
        start_year=2179,
        birth_year=2100,
        horizon_age=99,
        stop_working_age=99,
    )
    result = project(inputs)
    assert result.rows[-1].year == MAX_YEAR
