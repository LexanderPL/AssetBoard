from __future__ import annotations

import statistics
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from assetboard.domain.money import Money
from assetboard.domain.shading import (
    MAX_STEP,
    MIN_GROUP_SIZE,
    OUTLIER_MULTIPLE,
    large_gates,
    maxima,
    shade,
    shade_step,
)


def _rows(*values: int, currency: str = "EUR") -> list[Money]:
    return [Money(value, currency) for value in values]


def _steps(rows: list[Money]) -> list[int]:
    top = maxima(rows)
    gates = large_gates(rows)
    return [shade(row, top, gates) for row in rows]


@pytest.mark.parametrize(
    ("amount", "top", "step"),
    [
        (0, 10_000, 0),
        (2_499, 10_000, 0),  # just under 25%
        (2_500, 10_000, 1),  # 25%
        (4_999, 10_000, 1),
        (5_000, 10_000, 2),  # 50%
        (7_499, 10_000, 2),
        (7_500, 10_000, 3),  # 75%
        (8_999, 10_000, 3),
        (9_000, 10_000, 4),  # 90%
        (10_000, 10_000, 4),  # the maximum itself
        (1, 4, 1),  # exact quarter of a small maximum
        (-5_000, 10_000, 2),  # absolute values
        (5_000, -10_000, 2),
        (0, 0, 0),  # max 0: no tint
        (100, 0, 0),
    ],
)
def test_shade_step(amount: int, top: int, step: int) -> None:
    assert shade_step(amount, top) == step


@given(st.integers(min_value=0, max_value=10**15), st.integers(min_value=1, max_value=10**15))
def test_shade_step_is_bounded_and_monotonic(amount: int, top: int) -> None:
    step = shade_step(amount, top)
    assert 0 <= step <= MAX_STEP
    assert shade_step(amount + 1, top) >= step


def test_the_gate_constants_are_the_approved_rule() -> None:
    assert MIN_GROUP_SIZE == 3
    assert OUTLIER_MULTIPLE == 2


@pytest.mark.parametrize("values", [(), (1_234,), (1_234, 99)])
def test_fewer_than_three_entries_have_no_gate(values: tuple[int, ...]) -> None:
    rows = _rows(*values)
    assert large_gates(rows) == {}
    assert _steps(rows) == [0] * len(rows)


def test_three_similar_entries_stay_neutral() -> None:
    rows = _rows(100, 100, 100)
    assert large_gates(rows) == {"EUR": 200}
    assert _steps(rows) == [0, 0, 0]


def test_an_entry_at_twice_the_median_is_tinted() -> None:
    rows = _rows(100, 100, 1_000)
    assert large_gates(rows) == {"EUR": 200}
    assert _steps(rows) == [0, 0, 4]


def test_an_entry_below_twice_the_median_is_neutral() -> None:
    rows = _rows(100, 101, 199)
    assert large_gates(rows) == {"EUR": 202}
    assert _steps(rows) == [0, 0, 0]


def test_an_even_count_uses_the_exact_doubled_median() -> None:
    # Median 2.5; the gate is 5, not a rounded 4 or 6.
    rows = _rows(1, 2, 3, 100)
    assert large_gates(rows) == {"EUR": 5}
    assert _steps(rows) == [0, 0, 0, 4]


def test_zero_median_and_an_all_zero_group() -> None:
    assert large_gates(_rows(0, 0, 100)) == {"EUR": 0}
    assert _steps(_rows(0, 0, 100)) == [0, 0, 4]
    assert large_gates(_rows(0, 0, 0)) == {"EUR": 0}
    assert _steps(_rows(0, 0, 0)) == [0, 0, 0]


def test_each_currency_has_its_own_gate_and_maximum() -> None:
    rows = [
        *_rows(100, 100, 1_000, currency="PLN"),
        *_rows(10, 10, 10, currency="EUR"),
        *_rows(5, currency="USD"),
    ]
    assert large_gates(rows) == {"PLN": 200, "EUR": 20}
    assert maxima(rows) == {"PLN": 1_000, "EUR": 10, "USD": 5}
    assert _steps(rows) == [0, 0, 4, 0, 0, 0, 0]


def test_an_eligible_entry_below_a_quarter_of_the_largest_stays_neutral() -> None:
    # Median 100, gate 200: 250 is eligible but only 2.5% of the largest amount.
    rows = _rows(100, 100, 100, 250, 10_000)
    assert large_gates(rows) == {"EUR": 200}
    assert _steps(rows) == [0, 0, 0, 0, 4]


def test_absolute_amounts_are_used() -> None:
    rows = _rows(-100, -100, -1_000)
    assert large_gates(rows) == {"EUR": 200}
    assert _steps(rows) == [0, 0, 4]


def test_maxima_of_nothing_is_empty() -> None:
    assert maxima([]) == {}


@given(st.lists(st.integers(min_value=0, max_value=10**6), min_size=3, max_size=20))
def test_the_gate_is_twice_the_median(values: list[int]) -> None:
    gate = large_gates(_rows(*values))["EUR"]
    # statistics.median is an independent oracle; for integers it is an int or a .5 float.
    assert gate == 2 * Fraction(statistics.median(values))


@given(
    st.lists(st.integers(min_value=0, max_value=10**9), min_size=3, max_size=20),
)
def test_no_tint_below_the_gate(values: list[int]) -> None:
    rows = _rows(*values)
    top = maxima(rows)
    gates = large_gates(rows)
    gate = gates["EUR"]
    for row in rows:
        if abs(row.amount_minor) < gate:
            assert shade(row, top, gates) == 0


@given(st.lists(st.integers(min_value=0, max_value=10**9), max_size=20))
def test_small_groups_and_all_steps_stay_bounded(values: list[int]) -> None:
    rows = _rows(*values)
    top = maxima(rows)
    gates = large_gates(rows)
    for row in rows:
        assert 0 <= shade(row, top, gates) <= MAX_STEP


@given(
    st.lists(st.integers(min_value=0, max_value=10**9), min_size=3, max_size=20),
    st.integers(min_value=0, max_value=10**12),
    st.integers(min_value=0, max_value=10**12),
)
def test_shade_is_monotonic_in_the_amount(values: list[int], first: int, second: int) -> None:
    rows = _rows(*values)
    top = maxima(rows)
    gates = large_gates(rows)
    low, high = sorted((first, second))
    assert shade(Money(low, "EUR"), top, gates) <= shade(Money(high, "EUR"), top, gates)


@given(
    st.lists(st.integers(min_value=0, max_value=10**9), min_size=1, max_size=20),
    st.integers(min_value=1, max_value=10_000),
)
def test_scaling_the_month_keeps_the_steps(values: list[int], factor: int) -> None:
    rows = _rows(*values)
    scaled = _rows(*(value * factor for value in values))
    assert _steps(rows) == _steps(scaled)
