from __future__ import annotations

from decimal import Decimal

import pytest

from assetboard.domain.fire import (
    ILLUSTRATIVE_INFLATION_BP,
    INCOME_GROWTH_SANE_BP,
    INFLATION_SANE_BP,
    REAL_RETURN_SANE_BP,
    SWR_SANE_BP,
    age_at_start,
    clean_event_label,
    clean_scenario_name,
    contribution_years,
    default_horizon_age,
    event_years,
    extreme_rates,
    in_range,
    signed_amount_minor,
    year_in_window,
)
from assetboard.domain.transactions import EntryError


def test_age_and_windows_follow_the_start_year_and_horizon() -> None:
    assert age_at_start(2026, 1985) == 41
    contributions = contribution_years(2026, 1985, 90)
    events = event_years(2026, 1985, 90)
    assert (contributions.start, contributions.stop) == (2027, 2076)
    assert (events.start, events.stop) == (2026, 2076)
    assert year_in_window(2027, contributions)
    assert not year_in_window(2026, contributions)
    assert year_in_window(2026, events)
    assert not year_in_window(2076, contributions)
    assert not year_in_window(2076, events)


def test_extreme_rates_table() -> None:
    assert in_range(INFLATION_SANE_BP[0], INFLATION_SANE_BP)
    assert not extreme_rates(
        inflation_bp=250,
        real_return_bp=500,
        income_growth_bp=300,
        swr_bp=400,
    )
    assert extreme_rates(
        inflation_bp=INFLATION_SANE_BP[1] + 1,
        real_return_bp=500,
        income_growth_bp=300,
        swr_bp=None,
    )
    assert extreme_rates(
        inflation_bp=250,
        real_return_bp=REAL_RETURN_SANE_BP[0] - 1,
        income_growth_bp=300,
        swr_bp=None,
    )
    assert extreme_rates(
        inflation_bp=250,
        real_return_bp=500,
        income_growth_bp=INCOME_GROWTH_SANE_BP[1] + 1,
        swr_bp=None,
    )
    assert extreme_rates(
        inflation_bp=250,
        real_return_bp=500,
        income_growth_bp=300,
        swr_bp=SWR_SANE_BP[0] - 1,
    )
    assert not extreme_rates(
        inflation_bp=ILLUSTRATIVE_INFLATION_BP,
        real_return_bp=500,
        income_growth_bp=300,
        swr_bp=SWR_SANE_BP[1],
    )


def test_default_horizon_age_is_ninety_without_a_profile_value() -> None:
    assert default_horizon_age(None) == 90
    assert default_horizon_age(95) == 95


def test_clean_names_collapse_whitespace_and_refuse_empty_or_invisible_text() -> None:
    assert clean_scenario_name("  Retire   at 55 ") == "Retire at 55"
    for value in ("", "   ", "bad\x00name", "x" * 201):
        with pytest.raises(ValueError):
            clean_scenario_name(value)
    assert clean_event_label("  Mortgage   repaid ") == "Mortgage repaid"
    for value in ("", "   ", "bad\x00label"):
        with pytest.raises(ValueError):
            clean_event_label(value)


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (Decimal("0"), 0),
        (Decimal("1.25"), 125),
        (Decimal("-1.25"), -125),
        (Decimal("1000000"), 100_000_000),
    ],
)
def test_signed_amount_minor_keeps_the_sign(amount: Decimal, expected: int) -> None:
    assert signed_amount_minor(amount, "EUR") == expected


def test_signed_amount_minor_refuses_invalid_or_too_precise_amounts() -> None:
    with pytest.raises(EntryError) as exc:
        signed_amount_minor(Decimal("1.001"), "EUR")
    assert exc.value.code == "too_many_decimals"
    with pytest.raises(EntryError):
        signed_amount_minor(Decimal("NaN"), "EUR")
    with pytest.raises(EntryError):
        signed_amount_minor(Decimal(10**16), "EUR")
