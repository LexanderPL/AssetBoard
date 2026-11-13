from __future__ import annotations

from datetime import date

import pytest

from assetboard.domain.loans import (
    HIGH_RATE_CONFIRMATION_BP,
    MAX_LOAN_RATE_BP,
    MAX_TERM_MONTHS,
    LoanKind,
    LoanValidationError,
    RateMode,
    RateSegment,
    build_rate_timeline,
    clean_loan_name,
    cost_active_on,
    first_installment,
    latest_value_on_or_before,
    payment_date_matches_day,
    requires_high_rate_confirmation,
    validate_cost_dates,
    validate_margin_bp,
    validate_payment_day,
    validate_rate_bp,
    validate_term_months,
)
from assetboard.domain.money import Money


def test_first_installment_annuity_and_equal_principal_are_rounded_once() -> None:
    principal = Money(30_000_000, "EUR")  # 300,000.00
    annuity = first_installment(principal, 550, 360, LoanKind.ANNUITY)
    equal = first_installment(principal, 550, 360, LoanKind.EQUAL_PRINCIPAL)
    assert annuity == Money(170_337, "EUR")  # 1,703.37, half up
    assert equal == Money(220_833, "EUR")  # 833.33 principal + 1,375.00 interest


def test_first_installment_zero_rate_divides_the_principal() -> None:
    principal = Money(12_000, "EUR")
    assert first_installment(principal, 0, 12, LoanKind.ANNUITY) == Money(1_000, "EUR")
    assert first_installment(principal, 0, 12, LoanKind.EQUAL_PRINCIPAL) == Money(1_000, "EUR")


@pytest.mark.parametrize(("rate", "expected"), [(2_500, False), (2_501, True), (10_000, True)])
def test_high_rate_confirmation_boundary(rate: int, expected: bool) -> None:
    assert requires_high_rate_confirmation(rate) is expected
    assert HIGH_RATE_CONFIRMATION_BP == 2_500


def test_rate_and_term_ranges() -> None:
    assert validate_rate_bp(MAX_LOAN_RATE_BP) == MAX_LOAN_RATE_BP
    with pytest.raises(LoanValidationError):
        validate_rate_bp(MAX_LOAN_RATE_BP + 1)
    with pytest.raises(LoanValidationError):
        validate_margin_bp(-1)
    assert validate_term_months(MAX_TERM_MONTHS) == MAX_TERM_MONTHS
    with pytest.raises(LoanValidationError):
        validate_term_months(0)
    with pytest.raises(LoanValidationError):
        validate_payment_day(32)


def test_first_payment_date_uses_the_nominal_day_clamped_to_the_month() -> None:
    assert payment_date_matches_day(date(2026, 2, 28), 31)
    assert payment_date_matches_day(date(2024, 2, 29), 31)
    assert payment_date_matches_day(date(2026, 4, 30), 31)
    assert not payment_date_matches_day(date(2026, 3, 30), 31)
    assert payment_date_matches_day(date(2026, 3, 15), 15)


def test_cost_dates_are_inclusive() -> None:
    assert cost_active_on(date(2026, 1, 1), date(2026, 12, 31), date(2026, 1, 1))
    assert cost_active_on(date(2026, 1, 1), date(2026, 12, 31), date(2026, 12, 31))
    assert not cost_active_on(date(2026, 1, 1), date(2026, 12, 31), date(2027, 1, 1))
    validate_cost_dates(date(2026, 1, 1), date(2026, 1, 1))
    with pytest.raises(LoanValidationError):
        validate_cost_dates(date(2026, 2, 1), date(2026, 1, 31))


def test_latest_value_and_name_cleaning() -> None:
    values = [(date(2026, 1, 1), 100), (date(2026, 6, 1), 200)]
    assert latest_value_on_or_before(values, date(2026, 5, 31)) == 100
    assert latest_value_on_or_before(values, date(2026, 6, 1)) == 200
    assert latest_value_on_or_before(values, date(2025, 12, 31)) is None
    assert clean_loan_name("  My   mortgage ") == "My mortgage"
    with pytest.raises(LoanValidationError):
        clean_loan_name("   ")


def test_mixed_rate_timeline_resolves_fixed_and_reference_segments() -> None:
    start = date(2026, 1, 1)
    timeline = build_rate_timeline(
        [
            RateSegment(
                effective_from=date(2026, 1, 1),
                mode=RateMode.FIXED,
                rate_bp=550,
            ),
            RateSegment(
                effective_from=date(2027, 1, 1),
                mode=RateMode.VARIABLE,
                margin_bp=150,
                reference_values=(
                    (date(2026, 7, 1), 450),
                    (date(2028, 1, 1), 500),
                ),
            ),
            RateSegment(
                effective_from=date(2029, 1, 1),
                mode=RateMode.FIXED,
                rate_bp=700,
            ),
        ],
        start,
    )
    assert [(row.effective_from, row.annual_rate_bp) for row in timeline] == [
        (date(2026, 1, 1), 550),
        (date(2027, 1, 1), 600),
        (date(2028, 1, 1), 650),
        (date(2029, 1, 1), 700),
    ]


def test_reference_values_at_or_before_a_variable_start_set_its_opening_rate() -> None:
    timeline = build_rate_timeline(
        [
            RateSegment(
                effective_from=date(2027, 1, 1),
                mode=RateMode.VARIABLE,
                margin_bp=100,
                reference_values=(
                    (date(2026, 1, 1), 400),
                    (date(2027, 7, 1), 450),
                ),
            )
        ],
        date(2026, 1, 1),
    )
    assert [(row.effective_from, row.annual_rate_bp) for row in timeline] == [
        (date(2027, 1, 1), 500),
        (date(2027, 7, 1), 550),
    ]


def test_a_variable_segment_needs_a_value_on_or_before_its_start() -> None:
    with pytest.raises(LoanValidationError) as error:
        build_rate_timeline(
            [
                RateSegment(
                    effective_from=date(2027, 1, 1),
                    mode=RateMode.VARIABLE,
                    margin_bp=100,
                    reference_values=((date(2027, 2, 1), 400),),
                )
            ],
            date(2026, 1, 1),
        )
    assert error.value.code == "missing_reference_value"


def test_rate_segments_cannot_start_before_the_loan_or_share_a_date() -> None:
    with pytest.raises(LoanValidationError):
        build_rate_timeline(
            [RateSegment(effective_from=date(2025, 12, 31), mode=RateMode.FIXED, rate_bp=500)],
            date(2026, 1, 1),
        )
    with pytest.raises(LoanValidationError):
        build_rate_timeline(
            [
                RateSegment(effective_from=date(2026, 1, 1), mode=RateMode.FIXED, rate_bp=500),
                RateSegment(effective_from=date(2026, 1, 1), mode=RateMode.FIXED, rate_bp=600),
            ],
            date(2026, 1, 1),
        )
