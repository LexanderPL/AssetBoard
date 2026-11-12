from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import numpy_financial as npf
import pytest

from assetboard.domain.loans import (
    CostPeriod,
    DayCount,
    LoanKind,
    LoanTerms,
    OneOffOverpayment,
    OverpaymentMode,
    OverpaymentStrategy,
    RatePeriod,
    ScheduleAnchor,
    ScheduleRow,
    build_schedule,
    summarize_schedule,
)
from assetboard.domain.money import Money
from assetboard.domain.percent_or_fixed import PercentOrFixed


def _minor_from_float(value: float) -> int:
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def test_annuity_matches_numpy_financial_row_by_row() -> None:
    terms = LoanTerms(
        kind=LoanKind.ANNUITY,
        currency="EUR",
        principal_original=Money(30_000_000, "EUR"),
        origination_date=date(2026, 1, 1),
        first_payment_date=date(2026, 2, 1),
        term_months=360,
        payment_day=1,
        installment_override=None,
        overpayment_mode=OverpaymentMode.NONE,
        overpayment_amount=None,
    )
    schedule = build_schedule(
        terms,
        [RatePeriod(date(2026, 1, 1), 550)],
        [],
        [],
        ScheduleAnchor(date(2026, 1, 1), Money(30_000_000, "EUR")),
        date(2026, 1, 1),
        OverpaymentStrategy.REDUCE_TERM,
        DayCount.MONTHLY,
    )
    monthly_rate = 0.055 / 12
    for index, row in enumerate(schedule.rows, start=1):
        remaining = 360 - (index - 1)
        present = -row.opening.amount_minor / 100
        interest = npf.ipmt(monthly_rate, 1, remaining, present)
        principal = npf.ppmt(monthly_rate, 1, remaining, present)
        payment = npf.pmt(monthly_rate, remaining, present)
        assert row.annual_rate_bp == 550
        assert row.interest.amount_minor == _minor_from_float(interest)
        assert row.principal.amount_minor == _minor_from_float(principal)
        assert row.installment_total.amount_minor == (
            _minor_from_float(interest) + _minor_from_float(principal)
        )
        assert payment == pytest.approx(float(interest + principal), abs=1e-9)
        assert row.closing == row.opening - row.principal
    assert schedule.rows[-1].closing.amount_minor == 0


def _half_up(value: int, multiplier: int, divisor: int) -> int:
    return (value * multiplier * 2 + divisor) // (2 * divisor)


def _naive_schedule(
    *,
    terms: LoanTerms,
    rates: list[RatePeriod],
    costs: list[CostPeriod],
    overpayments: list[OneOffOverpayment],
    anchor: ScheduleAnchor,
) -> list[ScheduleRow]:
    """A literal spreadsheet loop, deliberately independent of the engine helpers."""
    currency = terms.currency
    dates: list[date] = []
    first = terms.first_payment_date
    if first <= anchor.date:
        year, month = anchor.date.year, anchor.date.month
        day = min(terms.payment_day, _days_in_month(year, month))
        candidate = date(year, month, day)
        if candidate <= anchor.date:
            year, month = _shift_month(year, month, 1)
            day = min(terms.payment_day, _days_in_month(year, month))
            candidate = date(year, month, day)
        first = candidate
    for index in range(terms.term_months):
        year, month = _shift_month(first.year, first.month, index)
        dates.append(date(year, month, min(terms.payment_day, _days_in_month(year, month))))
    ordered_rates = sorted(rates, key=lambda row: row.effective_from)
    ordered_costs = list(costs)
    ordered_overpayments = sorted(overpayments, key=lambda row: row.date)
    rows: list[ScheduleRow] = []
    balance = anchor.balance.amount_minor
    base_principal = _half_up(balance, 1, terms.term_months)
    one_off_index = 0
    for index, due in enumerate(dates, start=1):
        if balance <= 0:
            break
        rate = 0
        for period in ordered_rates:
            if period.effective_from <= due:
                rate = period.annual_rate_bp
        interest = _half_up(balance, rate, 120_000)
        cost_total = 0
        for cost in ordered_costs:
            if cost.effective_from <= due and (
                cost.effective_to is None or due <= cost.effective_to
            ):
                if cost.value.mode == "percent":
                    assert cost.value.rate_bp is not None
                    cost_total += _half_up(balance, cost.value.rate_bp, 10_000)
                else:
                    assert cost.value.fixed is not None
                    cost_total += cost.value.fixed.amount_minor
        one_off = 0
        while (
            one_off_index < len(ordered_overpayments)
            and ordered_overpayments[one_off_index].date <= due
        ):
            if ordered_overpayments[one_off_index].date > anchor.date:
                one_off += ordered_overpayments[one_off_index].amount.amount_minor
            one_off_index += 1
        if index == terms.term_months:
            principal = balance
            overpayment = 0
        else:
            principal = min(base_principal, balance)
            if terms.overpayment_mode == OverpaymentMode.FIXED_EXTRA:
                assert terms.overpayment_amount is not None
                regular = terms.overpayment_amount.amount_minor
            elif terms.overpayment_mode == OverpaymentMode.TARGET_TOTAL:
                assert terms.overpayment_amount is not None
                regular = max(
                    0, terms.overpayment_amount.amount_minor - principal - interest - cost_total
                )
            else:
                regular = 0
            overpayment = min(regular + one_off, balance - principal)
        closing = balance - principal - overpayment
        rows.append(
            ScheduleRow(
                n=index,
                date=due,
                annual_rate_bp=rate,
                opening=Money(balance, currency),
                interest=Money(interest, currency),
                principal=Money(principal, currency),
                overpayment=Money(overpayment, currency),
                costs=Money(cost_total, currency),
                installment_total=Money(interest + principal + overpayment + cost_total, currency),
                closing=Money(closing, currency),
            )
        )
        balance = closing
    return rows


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - date(year, month, 1)).days


def _shift_month(year: int, month: int, months: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + months
    return index // 12, index % 12 + 1


def test_independent_naive_loop_matches_a_complex_equal_principal_loan() -> None:
    terms = LoanTerms(
        kind=LoanKind.EQUAL_PRINCIPAL,
        currency="EUR",
        principal_original=Money(120_000_000, "EUR"),
        origination_date=date(2026, 1, 1),
        first_payment_date=date(2026, 2, 1),
        term_months=240,
        payment_day=1,
        installment_override=None,
        overpayment_mode=OverpaymentMode.FIXED_EXTRA,
        overpayment_amount=Money(25_000, "EUR"),
    )
    rates = [
        RatePeriod(date(2026, 1, 1), 400),
        RatePeriod(date(2027, 1, 1), 550),
    ]
    costs = [
        CostPeriod(PercentOrFixed.percent(15), date(2026, 1, 1)),
        CostPeriod(PercentOrFixed.fixed_amount(Money(500, "EUR")), date(2026, 1, 1)),
    ]
    overpayments = [OneOffOverpayment(date(2026, 9, 15), Money(500_000, "EUR"))]
    anchor = ScheduleAnchor(date(2026, 1, 1), Money(120_000_000, "EUR"))
    schedule = build_schedule(
        terms,
        rates,
        costs,
        overpayments,
        anchor,
        date(2026, 1, 1),
        OverpaymentStrategy.REDUCE_TERM,
        DayCount.MONTHLY,
    )
    expected = _naive_schedule(
        terms=terms,
        rates=rates,
        costs=costs,
        overpayments=overpayments,
        anchor=anchor,
    )
    assert schedule.rows == tuple(expected)
    assert summarize_schedule(schedule, date(2026, 1, 1)).payoff_date == expected[-1].date
    assert schedule.rows[-1].closing.amount_minor == 0
