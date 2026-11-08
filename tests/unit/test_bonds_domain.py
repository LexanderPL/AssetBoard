"""Bond model valuation (F6.5): golden formula, modes, rounding and properties."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, localcontext

import pytest
from hypothesis import given
from hypothesis import strategies as st

from assetboard.domain.bonds import (
    BondLot,
    BondRatePeriod,
    BondTerms,
    InterestMode,
    InvalidBondTerms,
    bond_value,
)
from assetboard.domain.money import Money
from assetboard.domain.percent_or_fixed import PercentOrFixed

FACE = Money(10_000, "PLN")
START = date(2020, 1, 1)


def _terms(
    *,
    mode: InterestMode = InterestMode.CAPITALIZED_ANNUALLY,
    rates: tuple[int, ...] = (500,),
    maturity: date = date(2022, 1, 1),
    tax_rate_bp: int = 0,
    fee: PercentOrFixed | None = None,
    face: Money = FACE,
) -> BondTerms:
    schedule = tuple(
        BondRatePeriod(from_date=START.replace(year=START.year + index), annual_rate_bp=rate)
        for index, rate in enumerate(rates)
    )
    return BondTerms(
        face_value=face,
        maturity_date=maturity,
        interest_mode=mode,
        rate_schedule=schedule,
        tax_rate_bp=tax_rate_bp,
        early_redemption_fee=fee,
    )


def _lot(day: date = START, quantity: str = "1") -> BondLot:
    return BondLot(day, Decimal(quantity))


def _rounded(value: Decimal) -> Money:
    return Money.from_decimal(value, "PLN")


def test_golden_closed_form_is_exact_to_the_minor_unit() -> None:
    terms = _terms(
        rates=(435,) * 10,
        maturity=date(2030, 1, 1),
        tax_rate_bp=1900,
    )
    with localcontext() as context:
        context.prec = 80
        fv = Decimal(100) * (Decimal("1.0435") ** 10)
        expected = fv - (fv - Decimal(100)) * Decimal("0.19")
        assert fv.quantize(Decimal("1e-18")) == Decimal("153.082143254677507882")
    valuation = bond_value(terms, [_lot()], date(2030, 1, 1))
    assert valuation.projected_net_at_maturity == _rounded(expected)
    assert valuation.net == valuation.projected_net_at_maturity


def test_capitalized_compounds_at_full_periods_and_earns_linearly_mid_period() -> None:
    terms = _terms(rates=(500, 500), maturity=date(2022, 1, 1))
    one_year = bond_value(terms, [_lot()], date(2021, 1, 1))
    assert one_year.net == Money(10_500, "PLN")

    with localcontext() as context:
        context.prec = 80
        assert (date(2020, 1, 1) - date(2021, 1, 1)).days == -366
        mid = bond_value(terms, [_lot()], date(2020, 7, 2))
        expected = Decimal(100) * (1 + Decimal("0.05") * Decimal(183) / Decimal(366))
    assert mid.net == _rounded(expected)


def test_paid_out_values_the_current_coupon_and_resets_at_period_ends() -> None:
    terms = _terms(
        mode=InterestMode.PAID_OUT_ANNUALLY,
        rates=(500, 500),
        maturity=date(2022, 1, 1),
    )
    assert bond_value(terms, [_lot()], date(2021, 1, 1)).net == Money(10_000, "PLN")
    with localcontext() as context:
        context.prec = 80
        mid = bond_value(terms, [_lot()], date(2021, 7, 2))
        expected = Decimal(100) + Decimal(100) * Decimal("0.05") * Decimal(182) / Decimal(365)
    assert mid.net == _rounded(expected)
    assert mid.net.amount_minor > 10_000


def test_at_maturity_simple_does_not_compound() -> None:
    terms = _terms(
        mode=InterestMode.AT_MATURITY_SIMPLE,
        rates=(500, 500),
        maturity=date(2022, 1, 1),
    )
    at_maturity = bond_value(terms, [_lot()], date(2022, 1, 1))
    assert at_maturity.net == Money(11_000, "PLN")
    assert at_maturity.projected_net_at_maturity == Money(11_000, "PLN")


def test_tax_is_on_interest_after_the_fee() -> None:
    terms = _terms(
        rates=(500,),
        maturity=date(2022, 1, 1),
        tax_rate_bp=1900,
        fee=PercentOrFixed.percent(200),
    )
    valuation = bond_value(terms, [_lot()], date(2021, 1, 1))
    with localcontext() as context:
        context.prec = 80
        interest = Decimal("5")
        tax = (interest - Decimal(2)) * Decimal("0.19")
        expected = Decimal(100) + interest - Decimal(2) - tax
    assert valuation.net == _rounded(expected)


def test_fee_is_capped_at_the_accrued_interest_and_never_reduces_face() -> None:
    terms = _terms(
        rates=(500,),
        maturity=date(2021, 1, 1),
        fee=PercentOrFixed.percent(5_000),
    )
    valuation = bond_value(terms, [_lot()], date(2020, 7, 2))
    assert valuation.fee == Money(250, "PLN")
    assert valuation.net == Money(10_000, "PLN")
    assert valuation.net.amount_minor >= FACE.amount_minor


def test_fee_fixed_amount_is_per_unit() -> None:
    terms = _terms(
        rates=(500,),
        maturity=date(2021, 1, 1),
        fee=PercentOrFixed.fixed_amount(Money(100, "PLN")),
    )
    valuation = bond_value(terms, [_lot(quantity="2")], date(2020, 7, 2))
    assert valuation.fee == Money(200, "PLN")


def test_at_or_after_maturity_is_frozen_and_has_no_fee() -> None:
    terms = _terms(
        rates=(500,),
        maturity=date(2021, 1, 1),
        fee=PercentOrFixed.percent(1000),
    )
    at_maturity = bond_value(terms, [_lot()], date(2021, 1, 1))
    later = bond_value(terms, [_lot()], date(2030, 1, 1))
    assert at_maturity.net == at_maturity.projected_net_at_maturity
    assert later.net == later.projected_net_at_maturity
    assert at_maturity.fee == Money(0, "PLN")


def test_projection_repeats_the_last_known_rate_through_maturity() -> None:
    terms = _terms(rates=(500,), maturity=date(2023, 1, 1))
    valuation = bond_value(terms, [_lot()], date(2021, 1, 1))
    expected = Decimal(100) * (Decimal("1.05") ** 3)
    assert valuation.projected_net_at_maturity == _rounded(expected)


def test_a_partial_final_period_is_prorated_over_the_full_year() -> None:
    terms = _terms(rates=(500,), maturity=date(2020, 4, 1))
    valuation = bond_value(terms, [_lot()], date(2020, 4, 1))
    with localcontext() as context:
        context.prec = 80
        expected = Decimal(100) + Decimal(100) * Decimal("0.05") * Decimal(91) / Decimal(366)
    assert valuation.net == _rounded(expected)


def test_a_leap_day_schedule_uses_the_28th_in_common_years() -> None:
    schedule = (
        BondRatePeriod(date(2024, 2, 29), 400),
        BondRatePeriod(date(2025, 2, 28), 500),
        BondRatePeriod(date(2026, 2, 28), 600),
        BondRatePeriod(date(2027, 2, 28), 700),
        BondRatePeriod(date(2028, 2, 29), 800),
    )
    terms = BondTerms(
        face_value=FACE,
        maturity_date=date(2029, 1, 1),
        interest_mode=InterestMode.CAPITALIZED_ANNUALLY,
        rate_schedule=schedule,
    )
    assert terms.rate_schedule[4].from_date == date(2028, 2, 29)


def test_invalid_schedule_and_missing_periods_are_refused() -> None:
    with pytest.raises(InvalidBondTerms):
        BondTerms(FACE, date(2022, 1, 1), InterestMode.CAPITALIZED_ANNUALLY, ())
    with pytest.raises(InvalidBondTerms):
        BondTerms(
            FACE,
            date(2022, 1, 1),
            InterestMode.CAPITALIZED_ANNUALLY,
            (
                BondRatePeriod(START, 500),
                BondRatePeriod(date(2021, 1, 2), 500),
            ),
        )
    with pytest.raises(InvalidBondTerms):
        BondTerms(
            FACE,
            START,
            InterestMode.CAPITALIZED_ANNUALLY,
            (BondRatePeriod(START, 500),),
        )
    with pytest.raises(InvalidBondTerms):
        BondRatePeriod(START, 100_001)


@given(
    rate_bp=st.integers(min_value=0, max_value=10_000),
    days=st.integers(min_value=0, max_value=740),
)
def test_value_is_monotonic_and_never_below_face(rate_bp: int, days: int) -> None:
    terms = _terms(rates=(rate_bp,), maturity=date(2022, 1, 1))
    first = bond_value(terms, [_lot()], date(2020, 1, 1) + timedelta(days=days))
    second = bond_value(terms, [_lot()], date(2020, 1, 1) + timedelta(days=min(days + 1, 740)))
    assert second.net.amount_minor >= first.net.amount_minor
    assert first.net.amount_minor >= FACE.amount_minor
    assert first.tax.amount_minor >= 0


@given(
    rate_bp=st.integers(min_value=0, max_value=10_000),
    tax_bp=st.integers(min_value=0, max_value=10_000),
    days_after_maturity=st.integers(min_value=0, max_value=1000),
)
def test_frozen_at_maturity_and_fee_never_breaks_the_face_floor(
    rate_bp: int, tax_bp: int, days_after_maturity: int
) -> None:
    terms = _terms(
        rates=(rate_bp,),
        maturity=date(2021, 1, 1),
        tax_rate_bp=tax_bp,
        fee=PercentOrFixed.percent(10_000),
    )
    maturity = bond_value(terms, [_lot()], date(2021, 1, 1))
    later = bond_value(terms, [_lot()], date(2021, 1, 1) + timedelta(days=days_after_maturity))
    assert maturity.net == maturity.projected_net_at_maturity
    assert later.net == later.projected_net_at_maturity
    assert maturity.net.amount_minor >= FACE.amount_minor
    assert later.net.amount_minor >= FACE.amount_minor
