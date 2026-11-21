"""The year overview's per-row heat gate (reports polish 2), reusing domain.shading."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from assetboard.domain.money import Money
from assetboard.web.reports import _heat_steps


def _steps(*values: int, fill: int = 0) -> tuple[int, ...]:
    amounts = [Money(value, "EUR") for value in values]
    amounts.extend(Money(fill, "EUR") for _ in range(12 - len(amounts)))
    return _heat_steps(tuple(amounts))


def test_a_row_compares_only_its_non_zero_months() -> None:
    assert _steps(100, 100, 100, 400) == (0, 0, 0, 4, 0, 0, 0, 0, 0, 0, 0, 0)


def test_fewer_than_three_non_zero_months_stay_neutral() -> None:
    assert _steps(100, 1_000) == (0,) * 12
    assert _steps(1_000) == (0,) * 12


def test_the_four_steps_are_relative_to_the_rows_largest_month() -> None:
    assert _steps(100, 100, 100, 100, 200, 300, 450, 600) == (
        0,
        0,
        0,
        0,
        0,
        2,
        3,
        4,
        0,
        0,
        0,
        0,
    )


def test_zero_months_do_not_drag_the_median_down() -> None:
    # The non-zero median stays 100, so 400 is the only eligible month.
    assert _steps(100, 100, 400) == (0, 0, 4, 0, 0, 0, 0, 0, 0, 0, 0, 0)


def test_each_row_is_scaled_on_its_own() -> None:
    # A small row's outlier stands out even though another category spends much more.
    assert _steps(1, 1, 1, 4) == (0, 0, 0, 4, 0, 0, 0, 0, 0, 0, 0, 0)


def test_zero_and_negative_months_are_never_shaded() -> None:
    assert _steps(-100, -100, 1_000) == (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)


@given(st.lists(st.integers(min_value=-(10**6), max_value=10**6), min_size=12, max_size=12))
def test_steps_are_bounded_and_non_positive_months_stay_neutral(values: list[int]) -> None:
    steps = _heat_steps(tuple(Money(value, "EUR") for value in values))
    assert len(steps) == 12
    assert all(0 <= step <= 4 for step in steps)
    assert all(step == 0 for value, step in zip(values, steps, strict=True) if value <= 0)


@given(
    st.lists(st.integers(min_value=0, max_value=10**6), min_size=12, max_size=12),
    st.integers(min_value=1, max_value=50),
)
def test_scaling_a_row_keeps_its_steps(values: list[int], factor: int) -> None:
    original = _heat_steps(tuple(Money(value, "EUR") for value in values))
    scaled = _heat_steps(tuple(Money(value * factor, "EUR") for value in values))
    assert scaled == original
