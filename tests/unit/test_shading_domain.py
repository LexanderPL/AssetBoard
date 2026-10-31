from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from assetboard.domain.money import Money
from assetboard.domain.shading import MAX_STEP, maxima, shade, shade_step


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


def test_a_single_row_is_the_maximum_and_gets_the_strongest_step() -> None:
    only = Money(1_234, "PLN")
    assert shade(only, maxima([only])) == MAX_STEP


def test_each_currency_is_compared_with_its_own_maximum() -> None:
    rows = [Money(100_000, "PLN"), Money(20_000, "PLN"), Money(1_000, "EUR"), Money(950, "EUR")]
    top = maxima(rows)
    assert top == {"PLN": 100_000, "EUR": 1_000}
    assert [shade(m, top) for m in rows] == [4, 0, 4, 4]
    assert shade(Money(500, "USD"), top) == 0  # a currency without a maximum


def test_maxima_of_nothing_is_empty() -> None:
    assert maxima([]) == {}
