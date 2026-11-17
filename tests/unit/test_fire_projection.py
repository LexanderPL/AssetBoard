"""Golden and edge tests for the pure FIRE projection engine (F8.2, corrected by F8.19).

The oracle below is a literal transcription of the formulas in docs/specs/M8-fire-planner-v2.md
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
    EventKind,
    FireProjectionError,
    Projection,
    ProjectionEvent,
    ProjectionInputs,
    ProjectionLoan,
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


def _oracle(inputs: ProjectionInputs) -> Projection:
    current_age = inputs.start_year - inputs.birth_year
    last_year = inputs.birth_year + inputs.horizon_age
    # Exact nominal compounding straight from the definition:
    # (1 + inflation) * (1 + return above inflation) - 1.
    inflation = Fraction(10_000 + inputs.inflation_bp, 10_000)
    growth = Fraction(10_000 + inputs.income_growth_bp, 10_000)
    gain_rate = inflation * Fraction(10_000 + inputs.real_return_bp, 10_000) - 1

    overrides: dict[int, int] = {}
    cash: defaultdict[int, int] = defaultdict(int)
    income_changes: defaultdict[int, int] = defaultdict(int)
    expense_changes: defaultdict[int, int] = defaultdict(int)
    for event in inputs.events:
        amount = event.amount.amount_minor
        if event.kind is EventKind.CASH:
            cash[event.year] += amount
        elif event.kind is EventKind.INCOME_CHANGE:
            income_changes[event.year] += amount
        elif event.kind is EventKind.EXPENSE_CHANGE:
            expense_changes[event.year] += amount
        else:
            overrides[event.year] = amount

    loans = tuple(
        (loan.annual_payment.amount_minor, loan.payoff_year, loan.included) for loan in inputs.loans
    )

    def build_rows(stop_working_age: int) -> list[ProjectionRow]:
        income_exact = Fraction(inputs.start_income.amount_minor)
        expenses_exact = Fraction(inputs.start_expenses.amount_minor)
        rows: list[ProjectionRow] = []
        previous_net_worth = 0
        for offset, year in enumerate(range(inputs.start_year, last_year + 1)):
            age = year - inputs.birth_year
            if offset > 0:
                if age < stop_working_age:
                    income_exact *= growth
                else:
                    income_exact = Fraction(0)
                expenses_exact *= inflation
            elif age >= stop_working_age:
                # The first row also has no base income when income stops at the current age.
                income_exact = Fraction(0)

            income = _half_up(income_exact)
            for event_year, amount in income_changes.items():
                if event_year <= year:
                    income += _half_up(Fraction(amount) * growth**offset)
            gross_expenses = _half_up(expenses_exact)
            for event_year, amount in expense_changes.items():
                if event_year <= year:
                    gross_expenses += _half_up(Fraction(amount) * inflation**offset)
            loan_drop = sum(
                amount
                for amount, payoff_year, included in loans
                if included and payoff_year <= year
            )
            loan_payment = sum(
                amount
                for amount, payoff_year, included in loans
                if not included or payoff_year > year
            )
            expenses = gross_expenses - loan_drop
            if expenses < 0:
                raise FireProjectionError("expenses", "expenses_negative", year=year)

            if offset == 0:
                net = inputs.start_portfolio.amount_minor
                opening = inputs.start_portfolio.amount_minor
            else:
                net = overrides.get(year, income - expenses)
                opening = previous_net_worth
            cash_amount = cash[year]
            gain = _half_up(Fraction(opening) * gain_rate)
            net_worth = (
                net + gain + cash_amount if offset == 0 else opening + net + gain + cash_amount
            )
            rows.append(
                ProjectionRow(
                    year=year,
                    age=age,
                    income=_eur(income),
                    expenses=_eur(expenses),
                    loan_payment=_eur(loan_payment),
                    net=_eur(net),
                    gain=_eur(gain),
                    cash=_eur(cash_amount),
                    net_worth=_eur(net_worth),
                    is_fire=False,
                    income_stopped=offset > 0 and age >= stop_working_age,
                )
            )
            previous_net_worth = net_worth
        return rows

    rows = build_rows(inputs.stop_working_age)
    fire_index: int | None = None
    if inputs.rule is Rule.LASTS_TO_HORIZON:
        last_candidate = min(inputs.stop_working_age, inputs.horizon_age - 1)
        for stop_age in range(current_age, last_candidate + 1):
            candidate = build_rows(stop_age)
            fire_offset = stop_age - current_age
            opening = (
                inputs.start_portfolio.amount_minor
                if fire_offset == 0
                else candidate[fire_offset - 1].net_worth.amount_minor
            )
            if opening < 0:
                continue
            if all(row.net_worth.amount_minor >= 0 for row in candidate[fire_offset:]):
                fire_index = fire_offset
                break
    else:
        for offset, row in enumerate(rows):
            opening = (
                inputs.start_portfolio.amount_minor
                if offset == 0
                else rows[offset - 1].net_worth.amount_minor
            )
            if opening < 0:
                continue
            if inputs.rule is Rule.SWR:
                assert inputs.swr_bp is not None
                if opening * inputs.swr_bp >= row.expenses.amount_minor * 10_000:
                    fire_index = offset
                    break
            elif opening * inputs.real_return_bp > row.expenses.amount_minor * 10_000:
                fire_index = offset
                break
    if fire_index is not None:
        rows[fire_index] = replace(rows[fire_index], is_fire=True)

    fire_row = rows[fire_index] if fire_index is not None else None
    fire_number: Money | None = None
    if fire_row is not None and inputs.rule is not Rule.LASTS_TO_HORIZON:
        rate_bp = inputs.swr_bp if inputs.rule is Rule.SWR else inputs.real_return_bp
        if rate_bp is not None and rate_bp > 0:
            fire_number = _eur(
                _half_up(Fraction(fire_row.expenses.amount_minor) / Fraction(rate_bp, 10_000))
            )

    plan_progress: int | None = None
    if (
        fire_row is not None
        and inputs.rule is not Rule.LASTS_TO_HORIZON
        and fire_row.net_worth.amount_minor > 0
    ):
        plan_progress = _half_up(
            Fraction(inputs.start_portfolio.amount_minor)
            / Fraction(fire_row.net_worth.amount_minor)
            * 10_000
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
        fire_number=fire_number,
        plan_progress_bp=plan_progress,
        depletion_age=depletion_age,
        min_networth_after_stop=(_eur(min_after_stop) if min_after_stop is not None else None),
    )


_ROW_FIELDS = (
    "year",
    "age",
    "income",
    "expenses",
    "loan_payment",
    "net",
    "gain",
    "cash",
    "net_worth",
    "is_fire",
    "income_stopped",
)
_METRIC_FIELDS = (
    "fire_index",
    "fire_year",
    "fire_age",
    "years_to_fire",
    "networth_at_fire",
    "fire_number",
    "plan_progress_bp",
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


_RETURNS_FIRE = _variant(start_portfolio=_eur(30_000_000))
_G2 = _variant(
    stop_working_age=50,
    real_return_bp=100,
    start_income=_eur(5_000_000),
    start_expenses=_eur(8_000_000),
    start_portfolio=_eur(1_000_000),
)
_G3 = _variant(
    events=tuple(
        ProjectionEvent(
            2027 + index,
            EventKind.SAVING_OVERRIDE,
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
_HORIZON = _variant(
    rule=Rule.LASTS_TO_HORIZON,
    swr_bp=None,
    horizon_age=87,
    stop_working_age=68,
    inflation_bp=0,
    real_return_bp=0,
    income_growth_bp=0,
    start_income=_eur(5_000_000),
    start_expenses=_eur(3_000_000),
    start_portfolio=_eur(10_000_000),
)


@pytest.mark.parametrize(
    "inputs",
    [
        pytest.param(_BASE, id="G1-base-never-fire-under-real-return"),
        pytest.param(_RETURNS_FIRE, id="G1b-returns-cover-fire"),
        pytest.param(_G2, id="G2-never-fire-depletion"),
        pytest.param(_G3, id="G3-first-ten-years-overrides"),
        pytest.param(_G4, id="G4-events"),
        pytest.param(_G5, id="G5-swr"),
        pytest.param(_HORIZON, id="G6-horizon"),
    ],
)
def test_golden_sets_match_the_independent_oracle(inputs: ProjectionInputs) -> None:
    _assert_matches_oracle(inputs)


def test_t0_and_key_metrics_are_hand_checkable() -> None:
    result = project(_RETURNS_FIRE)
    first = result.rows[0]
    assert (first.year, first.age) == (2026, 41)
    assert (first.income.amount_minor, first.expenses.amount_minor, first.net.amount_minor) == (
        6_000_000,
        4_000_000,
        30_000_000,
    )
    # 30,000,000 * ((1.025 * 1.05) - 1) = 30,000,000 * 7.625% = 2,287,500.
    assert first.gain.amount_minor == 2_287_500
    assert first.net_worth.amount_minor == 32_287_500
    assert (result.fire_index, result.fire_year, result.fire_age) == (12, 2038, 53)
    assert result.years_to_fire == 12
    # 5,379,555 / 5% = 107,591,100, using the FIRE row's nominal expenses and real return.
    assert result.fire_number == _eur(107_591_100)
    # 30,000,000 / 124,568,658 = 24.083...%, using the starting portfolio, not row 0.
    assert result.plan_progress_bp == 2408
    assert result.depletion_age is None
    assert result.rows[-1].year == 2075


def test_horizon_rule_finds_the_earliest_age_where_stopping_income_lasts() -> None:
    result = project(_HORIZON)
    # Working one more year than 68 would also survive; retiring at 41 or 67 would not.
    assert (result.fire_age, result.fire_year, result.years_to_fire) == (68, 2053, 27)
    assert result.fire_index is not None
    assert result.rows[result.fire_index].is_fire
    assert sum(row.is_fire for row in result.rows) == 1
    assert result.networth_at_fire is not None
    assert result.fire_number is None
    assert result.plan_progress_bp is None


def test_horizon_rule_never_fires_after_income_stops() -> None:
    result = project(replace(_HORIZON, stop_working_age=67))
    assert result.fire_index is None
    assert result.fire_age is None
    assert result.fire_number is None
    assert result.plan_progress_bp is None


def test_horizon_rule_does_not_consider_retiring_at_the_horizon() -> None:
    # Retiring at 43 (the horizon year) would leave a positive balance, but the rule stops at the
    # year before the horizon; retiring at 42 runs out of money, so there is no FIRE age.
    result = project(
        _edge(
            rule=Rule.LASTS_TO_HORIZON,
            horizon_age=43,
            stop_working_age=90,
            start_income=_eur(100_000),
            start_expenses=_eur(10_000),
            start_portfolio=_eur(0),
        )
    )
    assert result.fire_index is None
    assert result.fire_age is None


def test_horizon_rule_allows_the_year_before_the_horizon() -> None:
    # Retiring at 42 fails; retiring at 43 (the last candidate, one year before the horizon)
    # succeeds because the salary at 42 is still paid. The candidate bound is inclusive.
    result = _assert_matches_oracle(
        _edge(
            rule=Rule.LASTS_TO_HORIZON,
            horizon_age=44,
            stop_working_age=90,
            start_income=_eur(100_000),
            start_expenses=_eur(10_000),
            start_portfolio=_eur(0),
        )
    )
    assert result.fire_age == 43
    # The table keeps the configured income-stop age, so the FIRE row still shows its salary.
    assert result.rows[result.fire_index].income == _eur(100_000)


def test_horizon_rule_has_no_candidate_when_income_stopped_before_today() -> None:
    # The plan already has no base income and survives to the horizon, but the rule cannot
    # declare FIRE after the configured income-stop age.
    result = project(
        replace(
            _HORIZON,
            stop_working_age=40,
            start_portfolio=_eur(200_000_000),
        )
    )
    assert result.rows[-1].net_worth.amount_minor > 0
    assert result.fire_index is None
    assert result.fire_age is None
    assert result.networth_at_fire is None
    assert result.fire_number is None
    assert result.plan_progress_bp is None


def test_horizon_rule_keeps_events_and_loans_in_the_candidate_simulation() -> None:
    # The loan payoff drop, the post-retirement saving override and the expense change stay in
    # the candidate simulation; without the drop the plan would not last at 67.
    inputs = replace(
        _HORIZON,
        stop_working_age=67,
        events=(
            ProjectionEvent(2060, EventKind.SAVING_OVERRIDE, _eur(1_000_000)),
            ProjectionEvent(2071, EventKind.EXPENSE_CHANGE, _eur(500_000)),
        ),
        loans=(ProjectionLoan(_eur(1_000_000), 2070, True),),
    )
    result = _assert_matches_oracle(inputs)
    assert result.fire_age == 67
    assert result.rows[-1].net_worth.amount_minor > 0


def test_horizon_rule_counts_zero_at_the_horizon_as_lasting() -> None:
    result = project(replace(_HORIZON, horizon_age=86, stop_working_age=67))
    assert result.fire_age == 67
    assert result.rows[-1].net_worth == _eur(0)


def test_horizon_rule_tests_income_stopping_at_the_candidate_not_the_plan_path() -> None:
    # The full plan (income to 68) survives to the horizon, but retiring at 41 does not; a rule
    # that checked the full plan's path would wrongly fire at the current age.
    result = project(_HORIZON)
    assert result.fire_age == 68
    assert not result.rows[0].is_fire


def test_horizon_rule_validates_without_a_withdrawal_rate() -> None:
    result = project(
        _edge(
            rule=Rule.LASTS_TO_HORIZON,
            horizon_age=43,
            stop_working_age=42,
            start_expenses=_eur(0),
            start_portfolio=_eur(0),
        )
    )
    assert result.fire_age == 41
    assert result.fire_number is None
    assert result.plan_progress_bp is None


def test_oracle_agrees_when_income_stops_in_the_current_year() -> None:
    inputs = _edge(
        rule=Rule.LASTS_TO_HORIZON,
        horizon_age=43,
        stop_working_age=41,
        start_income=_eur(100_000),
        start_expenses=_eur(50_000),
        start_portfolio=_eur(0),
    )
    result = _assert_matches_oracle(inputs)
    assert result.rows[0].income == _eur(0)
    assert result.fire_index is None


def test_the_gain_compounds_inflation_and_real_return_exactly() -> None:
    result = project(_variant(start_portfolio=_eur(10_000_000)))
    # 10,000,000 * ((1.025 * 1.05) - 1) = 762,500, not the additive 10,000,000 * 7.5%.
    assert result.rows[0].gain == _eur(762_500)
    assert result.rows[0].gain != _eur(750_000)


def test_returns_cover_uses_the_real_return_not_the_nominal_gain() -> None:
    # The nominal gain (15.5%) covers 100,000 on a 1,000,000 portfolio, but the return above
    # inflation (5%) does not: the plan has not preserved its real capital, so this is not FIRE.
    nominal_only = _edge(
        horizon_age=43,
        inflation_bp=1_000,
        real_return_bp=500,
        start_expenses=_eur(100_000),
        start_portfolio=_eur(1_000_000),
    )
    result = project(nominal_only)
    assert result.rows[0].gain == _eur(155_000)
    assert not result.rows[0].is_fire
    assert result.fire_index != 0

    # With a 2,500,000 portfolio the real return alone covers the same expenses.
    real_cover = replace(nominal_only, start_portfolio=_eur(2_500_000))
    assert project(real_cover).rows[0].is_fire


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
        events=(
            ProjectionEvent(2027, EventKind.SAVING_OVERRIDE, _eur(0)),
            ProjectionEvent(2028, EventKind.SAVING_OVERRIDE, _eur(-200_000)),
            ProjectionEvent(2027, EventKind.CASH, _eur(50_000)),
        ),
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
    # The 2028 expense change is start-year money, so at t=2 it is 10,000 * 1.1^2.
    assert rows[2].expenses == _eur(121_000 + 12_100)
    assert rows[3].expenses == _eur(133_100 + 13_310)
    assert rows[3].income == _eur(172_800)
    # The 2030 income change is start-year money too: 5,000 * 1.2^4.
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


def test_different_event_years_are_inflated_and_rounded_separately() -> None:
    inputs = _edge(
        horizon_age=44,
        inflation_bp=5_000,
        events=(
            ProjectionEvent(2027, EventKind.EXPENSE_CHANGE, _eur(1)),
            ProjectionEvent(2028, EventKind.EXPENSE_CHANGE, _eur(1)),
        ),
    )
    # At t=2 each year's amount is round(1 * 1.5^2) = 2, so the total is 4. Summing the
    # two event years first would give round(2 * 1.5^2) = 5.
    assert project(inputs).rows[2].expenses == _eur(4)


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


def test_start_year_income_is_zero_when_the_salary_stops_at_the_current_age() -> None:
    inputs = _edge(horizon_age=43, stop_working_age=41, start_income=_eur(100_000))
    rows = project(inputs).rows
    # v2: income is paid only while age < stop age, including the current row.
    assert rows[0].income == _eur(0)
    assert not rows[0].income_stopped
    assert rows[1].income == _eur(0)
    assert rows[1].income_stopped


def test_negative_overrides_are_not_clamped_and_expense_changes_stay_non_negative() -> None:
    inputs = _edge(
        horizon_age=43,
        start_income=_eur(100_000),
        start_expenses=_eur(50_000),
        events=(
            ProjectionEvent(2027, EventKind.SAVING_OVERRIDE, _eur(-300_000)),
            ProjectionEvent(2026, EventKind.EXPENSE_CHANGE, _eur(-20_000)),
        ),
    )
    rows = project(inputs).rows
    assert rows[0].expenses == _eur(30_000)
    assert rows[1].expenses == _eur(30_000)
    assert rows[1].income == _eur(100_000)
    assert rows[1].net == _eur(-300_000)


def test_a_negative_expense_line_raises_with_the_row_year() -> None:
    from_change = _edge(
        horizon_age=43,
        start_expenses=_eur(100_000),
        events=(ProjectionEvent(2026, EventKind.EXPENSE_CHANGE, _eur(-200_000)),),
    )
    with pytest.raises(FireProjectionError) as error:
        project(from_change)
    assert (error.value.field, error.value.code, error.value.year) == (
        "events",
        "expenses_negative",
        2026,
    )

    from_loan = _edge(
        horizon_age=43,
        start_expenses=_eur(100_000),
        start_portfolio=_eur(0),
        loans=(ProjectionLoan(_eur(150_000), 2026, True),),
    )
    with pytest.raises(FireProjectionError) as error:
        project(from_loan)
    assert (error.value.field, error.value.code, error.value.year) == (
        "loans",
        "expenses_negative",
        2026,
    )


def test_a_zero_portfolio_with_negative_expenses_cannot_declare_fire() -> None:
    inputs = _edge(
        horizon_age=43,
        start_expenses=_eur(0),
        start_portfolio=_eur(0),
        events=(ProjectionEvent(2026, EventKind.EXPENSE_CHANGE, _eur(-1)),),
    )
    with pytest.raises(FireProjectionError) as error:
        project(inputs)
    assert error.value.code == "expenses_negative"
    assert error.value.year == 2026


def test_returns_rule_uses_strictly_greater_and_the_previous_net_worth() -> None:
    inputs = _edge(
        horizon_age=43,
        real_return_bp=1_000,
        start_income=_eur(100_000),
        start_expenses=_eur(100_000),
        start_portfolio=_eur(1_000_000),
    )
    result = project(inputs)
    # At t=0 the opening portfolio's real return equals expenses exactly: 1,000,000 * 10% is
    # not strictly greater than 100,000, so the strict comparison does not fire.
    assert not result.rows[0].is_fire
    # At t=1 the opening balance is 1,100,000, so its real return (110,000) is strictly above
    # expenses.
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
        real_return_bp=500,
        start_expenses=_eur(10_000),
        start_portfolio=_eur(100_000),
        events=(ProjectionEvent(2027, EventKind.SAVING_OVERRIDE, _eur(-200_000)),),
    )
    result = project(inputs)
    # t=0 ends at 105,000; t=1 earns 5% and then applies the -200,000 override.
    assert result.rows[0].net_worth == _eur(105_000)
    assert result.rows[1].net_worth == _eur(-89_750)
    # The return on a negative opening balance cannot cover positive expenses.
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


def test_never_fire_leaves_every_fire_metric_undefined() -> None:
    result = project(_G2)
    assert result.fire_index is None
    assert result.fire_year is None
    assert result.fire_age is None
    assert result.years_to_fire is None
    assert result.networth_at_fire is None
    assert result.fire_number is None
    assert result.plan_progress_bp is None
    assert result.depletion_age == 50
    assert result.min_networth_after_stop is not None


def test_fire_number_needs_a_fire_row_and_a_positive_rate() -> None:
    no_gain = _edge(horizon_age=43, start_expenses=_eur(100_000))
    result = project(no_gain)
    assert result.fire_number is None
    assert result.plan_progress_bp is None

    # A zero real return can never cover a positive expense line, so there is no FIRE row and
    # therefore no FIRE number, even with a positive portfolio.
    zero_return = _edge(
        horizon_age=43,
        real_return_bp=0,
        start_expenses=_eur(0),
        start_portfolio=_eur(1_000_000),
    )
    result = project(zero_return)
    assert result.fire_index is None
    assert result.fire_number is None
    assert result.plan_progress_bp is None


def test_zero_expenses_with_a_positive_rate_give_a_zero_fire_number() -> None:
    swr_zero = _edge(
        horizon_age=43,
        rule=Rule.SWR,
        swr_bp=400,
        start_expenses=_eur(0),
    )
    result = project(swr_zero)
    assert result.fire_index == 0
    assert result.fire_number == _eur(0)
    # The FIRE row's net worth is zero, so progress has no positive denominator.
    assert result.plan_progress_bp is None

    returns_zero = _edge(
        horizon_age=43,
        real_return_bp=1_000,
        start_expenses=_eur(0),
        start_portfolio=_eur(1_000_000),
    )
    result = project(returns_zero)
    assert result.fire_index == 0
    assert result.fire_number == _eur(0)


def test_a_negative_expense_line_is_never_a_fire_row_even_under_swr() -> None:
    inputs = _edge(
        horizon_age=43,
        rule=Rule.SWR,
        swr_bp=400,
        start_expenses=_eur(100_000),
        start_portfolio=_eur(1_000_000),
        events=(ProjectionEvent(2026, EventKind.EXPENSE_CHANGE, _eur(-500_000)),),
    )
    with pytest.raises(FireProjectionError) as error:
        project(inputs)
    assert (error.value.code, error.value.year) == ("expenses_negative", 2026)


def test_plan_progress_uses_the_starting_portfolio_not_the_first_row() -> None:
    result = project(_RETURNS_FIRE)
    assert result.networth_at_fire is not None
    assert result.plan_progress_bp == _half_up(
        Fraction(
            _RETURNS_FIRE.start_portfolio.amount_minor * 10_000,
            result.networth_at_fire.amount_minor,
        )
    )
    assert result.plan_progress_bp != _half_up(
        Fraction(
            result.rows[0].net_worth.amount_minor * 10_000,
            result.networth_at_fire.amount_minor,
        )
    )


def test_live_loan_payoff_changes_the_nominal_metrics() -> None:
    included = project(replace(_RETURNS_FIRE, loans=(ProjectionLoan(_eur(1_000_000), 2030, True),)))
    ignored = project(replace(_RETURNS_FIRE, loans=(ProjectionLoan(_eur(1_000_000), 2030, False),)))
    assert (included.fire_index, included.fire_year) == (8, 2034)
    assert included.fire_number == _eur(77_472_240)
    assert included.plan_progress_bp == 3383
    included_index = included.fire_index
    assert included_index is not None
    assert included.rows[included_index].loan_payment == _eur(0)

    assert (ignored.fire_index, ignored.fire_year) == (12, 2038)
    assert ignored.fire_number == _eur(107_591_100)
    assert ignored.plan_progress_bp == 2408
    ignored_index = ignored.fire_index
    assert ignored_index is not None
    assert ignored.rows[ignored_index].loan_payment == _eur(1_000_000)


def test_projection_outputs_have_no_real_money_fields() -> None:
    from dataclasses import fields

    row_fields = {field.name for field in fields(ProjectionRow)}
    metric_fields = {field.name for field in fields(Projection)}
    assert row_fields == {
        "year",
        "age",
        "income",
        "expenses",
        "loan_payment",
        "net",
        "gain",
        "cash",
        "net_worth",
        "is_fire",
        "income_stopped",
    }
    assert metric_fields == {
        "inputs",
        "rows",
        "fire_index",
        "fire_year",
        "fire_age",
        "years_to_fire",
        "networth_at_fire",
        "fire_number",
        "plan_progress_bp",
        "depletion_age",
        "min_networth_after_stop",
    }
    assert not any(name.startswith("real_") for name in row_fields | metric_fields)
    assert not any("today" in name or "sheet" in name for name in metric_fields)


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
    result = project(_RETURNS_FIRE)
    assert len(result.rows) == 50
    assert (result.rows[0].year, result.rows[-1].year) == (2026, 2075)
    assert result.fire_index == 12
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
        start_expenses=_eur(100_000),
        events=(ProjectionEvent(2027, EventKind.EXPENSE_CHANGE, _eur(-1)),),
    )
    # The base line is 150,000 and the change -1 * 1.5 = -1.5, rounded away from zero to -2.
    assert project(negative_expense).rows[1].expenses == _eur(149_998)


def test_projection_returns_inputs_with_the_canonical_currency() -> None:
    result = project(_variant(currency="eur"))
    assert result.inputs.currency == "EUR"
    assert result.rows[0].net_worth.currency == "EUR"


def test_the_projection_contract_uses_integers_for_money_and_rates() -> None:
    hints = []
    for cls in (
        ProjectionEvent,
        ProjectionInputs,
        ProjectionRow,
        Projection,
    ):
        hints.extend(get_type_hints(cls).values())
    assert all("float" not in str(hint) for hint in hints)
    result = project(_RETURNS_FIRE)
    assert type(result.rows[0].net_worth.amount_minor) is int
    assert type(result.fire_index) is int


@pytest.mark.parametrize(
    ("changes", "field", "code"),
    [
        ({"currency": "ZZZ"}, "currency", "invalid_currency"),
        ({"currency": None}, "currency", "invalid_currency"),
        ({"currency": 978}, "currency", "invalid_currency"),
        ({"start_income": Money(1, "USD")}, "start_income", "invalid_currency"),
        ({"birth_year": 2027}, "birth_year", "invalid_birth_year"),
        ({"birth_year": 1899}, "birth_year", "invalid_birth_year"),
        ({"birth_year": "1985"}, "birth_year", "invalid_birth_year"),
        ({"start_year": 2200}, "start_year", "invalid_start_year"),
        ({"start_year": 2026.0}, "start_year", "invalid_start_year"),
        ({"horizon_age": 41}, "horizon_age", "horizon_not_after_age"),
        ({"horizon_age": 0}, "horizon_age", "invalid_horizon"),
        ({"horizon_age": 121}, "horizon_age", "invalid_horizon"),
        (
            {"start_year": 2199, "birth_year": 2100, "horizon_age": 120},
            "horizon_age",
            "invalid_horizon",
        ),
        ({"stop_working_age": 121}, "stop_working_age", "invalid_stop_age"),
        ({"stop_working_age": -1}, "stop_working_age", "invalid_stop_age"),
        ({"stop_working_age": 55.0}, "stop_working_age", "invalid_stop_age"),
        ({"inflation_bp": -2_001}, "inflation_bp", "invalid_rate"),
        ({"inflation_bp": 250.0}, "inflation_bp", "invalid_rate"),
        ({"real_return_bp": 5_001}, "real_return_bp", "invalid_rate"),
        ({"real_return_bp": True}, "real_return_bp", "invalid_rate"),
        ({"income_growth_bp": -2_001}, "income_growth_bp", "invalid_rate"),
        ({"rule": "bogus"}, "rule", "invalid_rule"),
        ({"rule": None}, "rule", "invalid_rule"),
        ({"rule": Rule.SWR, "swr_bp": None}, "swr_bp", "invalid_swr"),
        ({"rule": Rule.SWR, "swr_bp": 49}, "swr_bp", "invalid_swr"),
        ({"rule": Rule.SWR, "swr_bp": 2_001}, "swr_bp", "invalid_swr"),
        ({"rule": Rule.SWR, "swr_bp": 400.0}, "swr_bp", "invalid_swr"),
        ({"rule": Rule.LASTS_TO_HORIZON, "swr_bp": 400}, "swr_bp", "invalid_swr"),
        ({"swr_bp": 400}, "swr_bp", "invalid_swr"),
        ({"start_income": _eur(-1)}, "start_income", "invalid_amount"),
        ({"start_expenses": _eur(MAX_AMOUNT_MINOR + 1)}, "start_expenses", "invalid_amount"),
        ({"start_portfolio": _eur(-1)}, "start_portfolio", "invalid_amount"),
        (
            {"events": (ProjectionEvent(2026, EventKind.SAVING_OVERRIDE, _eur(1)),)},
            "events",
            "saving_override_current_year",
        ),
        (
            {"events": (ProjectionEvent(2076, EventKind.SAVING_OVERRIDE, _eur(1)),)},
            "events",
            "rows_outside_window",
        ),
        (
            {
                "events": (
                    ProjectionEvent(2027, EventKind.SAVING_OVERRIDE, _eur(1)),
                    ProjectionEvent(2027, EventKind.SAVING_OVERRIDE, _eur(2)),
                )
            },
            "events",
            "duplicate_saving_override",
        ),
        (
            {
                "events": (
                    ProjectionEvent(2027, EventKind.SAVING_OVERRIDE, _eur(MAX_AMOUNT_MINOR + 1)),
                )
            },
            "events",
            "invalid_amount",
        ),
        (
            {"events": (ProjectionEvent(2027, EventKind.SAVING_OVERRIDE, Money(1, "USD")),)},
            "events",
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
            {"events": (ProjectionEvent(2026, cast(EventKind, None), _eur(1)),)},
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
