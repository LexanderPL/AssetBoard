from __future__ import annotations

from datetime import date
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from assetboard.domain.money import Money
from assetboard.domain.percent_or_fixed import PercentOrFixed
from assetboard.domain.taxes import (
    PaymentTiming,
    TaxBase,
    TaxInput,
    TaxKind,
    calculate_taxes,
    tax_payment_date,
    tax_payment_date_with_offset,
    timing_month_offset,
)

ADDED, SELF, PAYER = TaxKind.ADDED, TaxKind.SELF_PAID, TaxKind.PAYER_WITHHELD


def _golden_rules(income_tax: TaxKind, levy: TaxKind) -> tuple[TaxInput, ...]:
    return (
        TaxInput("VAT", ADDED, TaxBase.NET, PercentOrFixed.percent(2300)),
        TaxInput("Income tax", income_tax, TaxBase.NET, PercentOrFixed.percent(1200)),
        TaxInput(
            "Fixed levy",
            levy,
            TaxBase.GROSS,
            PercentOrFixed.fixed_amount(Money(100000, "PLN")),
        ),
    )


@pytest.mark.parametrize(
    ("income_tax", "levy", "received_minor", "payments"),
    [
        # Everything paid by the user: received = gross 28 745.10.
        (
            SELF,
            SELF,
            2_874_510,
            {"VAT": 537_510, "Income tax": 280_440, "Fixed levy": 100_000},
        ),
        # The payer withholds income tax: 28 745.10 - 2 804.40 = 25 940.70.
        (PAYER, SELF, 2_594_070, {"VAT": 537_510, "Fixed levy": 100_000}),
        # The payer withholds both: 28 745.10 - 3 804.40 = 24 940.70; only VAT is paid.
        (PAYER, PAYER, 2_494_070, {"VAT": 537_510}),
    ],
)
def test_golden_tax_breakdown(income_tax, levy, received_minor, payments) -> None:
    # Oracle: the F3.3 golden example in docs/specs/M3-recurring-income-and-taxes.md, with the
    # ADR-0022 kinds. Hand computation (minor units, PLN):
    #   net        = 2 337 000
    #   VAT 23%    = 2 337 000 * 2300 / 10000 = 537 510 exactly          -> 5 375.10
    #   gross      = 2 337 000 + 537 510      = 2 874 510                -> 28 745.10
    #   income 12% = 2 337 000 * 1200 / 10000 = 280 440 exactly (on net) -> 2 804.40
    #   levy fixed = 100 000                                              -> 1 000.00
    #   take-home  = 2 337 000 - 280 440 - 100 000 = 1 956 560           -> 19 565.60
    #   received   = net + added - payer_withheld (see each case above)
    result = calculate_taxes(Money(2_337_000, "PLN"), _golden_rules(income_tax, levy))
    assert [(line.name, line.amount.amount_minor) for line in result.added] == [("VAT", 537_510)]
    assert [(line.name, line.amount.amount_minor) for line in result.deducted] == [
        ("Income tax", 280_440),
        ("Fixed levy", 100_000),
    ]
    assert result.gross == Money(2_874_510, "PLN")
    assert result.take_home == Money(1_956_560, "PLN")
    assert result.ledger_amount == Money(received_minor, "PLN")
    assert {line.name: line.amount.amount_minor for line in result.payments} == payments


@pytest.mark.parametrize(
    ("kind", "received", "payment"),
    [
        # Table: 10 000.00 net, one 12% tax of each kind (1 200.00).
        (ADDED, 1_120_000, 120_000),
        (SELF, 1_000_000, 120_000),
        (PAYER, 880_000, None),
    ],
)
def test_each_kind_changes_received_and_payments(kind, received, payment) -> None:
    result = calculate_taxes(
        Money(1_000_000, "EUR"), [TaxInput("Tax", kind, TaxBase.NET, PercentOrFixed.percent(1200))]
    )
    assert result.ledger_amount == Money(received, "EUR")
    assert [line.amount.amount_minor for line in result.payments] == (
        [payment] if payment is not None else []
    )


def test_generate_payment_off_plans_nothing() -> None:
    result = calculate_taxes(
        Money(1_000_000, "EUR"),
        [TaxInput("Tax", SELF, TaxBase.NET, PercentOrFixed.percent(1200), generate_payment=False)],
    )
    assert result.payments == ()
    assert result.ledger_amount == Money(1_000_000, "EUR")


@given(
    net_minor=st.integers(min_value=0, max_value=10**12),
    definitions=st.lists(
        st.tuples(
            st.sampled_from(list(TaxKind)),
            st.sampled_from(list(TaxBase)),
            st.sampled_from(["percent", "fixed"]),
            st.integers(min_value=0, max_value=100_000),
        ),
        max_size=8,
    ),
)
def test_generated_payments_reconcile_received_amount_to_take_home(
    net_minor: int, definitions: list[tuple]
) -> None:
    rules = tuple(
        TaxInput(
            name=f"Tax {index}",
            kind=kind,
            base=TaxBase.NET if kind == ADDED else base,
            value=(
                PercentOrFixed.percent(amount)
                if mode == "percent"
                else PercentOrFixed.fixed_amount(Money(amount, "EUR"))
            ),
        )
        for index, (kind, base, mode, amount) in enumerate(definitions)
    )
    result = calculate_taxes(Money(net_minor, "EUR"), rules)
    # Invariant: received - planned payments == take-home (every rule generates payments),
    # and received == net + added - payer_withheld.
    added = sum(line.amount.amount_minor for line in result.added)
    withheld = sum(line.amount.amount_minor for line in result.deducted if line.kind == PAYER)
    assert result.ledger_amount.amount_minor == net_minor + added - withheld
    assert (
        result.ledger_amount.amount_minor
        - sum(payment.amount.amount_minor for payment in result.payments)
        == result.take_home.amount_minor
    )


@pytest.mark.parametrize(
    ("net_minor", "rate_bp", "expected_minor"),
    [(100, 50, 1), (101, 50, 1), (99, 50, 0), (100, 150, 2)],
)
def test_percentage_rounds_half_up_per_rule(net_minor, rate_bp, expected_minor) -> None:
    result = calculate_taxes(
        Money(net_minor, "EUR"),
        [TaxInput("Tax", ADDED, TaxBase.NET, PercentOrFixed.percent(rate_bp))],
    )
    assert result.added[0].amount == Money(expected_minor, "EUR")


def test_fixed_rule_and_mismatched_currency() -> None:
    rule = TaxInput(
        "Levy",
        SELF,
        TaxBase.NET,
        PercentOrFixed.fixed_amount(Money(1234, "EUR")),
    )
    assert calculate_taxes(Money(10_000, "EUR"), [rule]).deducted[0].amount == Money(1234, "EUR")
    invalid = TaxInput(
        "Levy",
        SELF,
        TaxBase.NET,
        PercentOrFixed.fixed_amount(Money(1234, "USD")),
    )
    with pytest.raises(ValueError, match="currency_mismatch"):
        calculate_taxes(Money(10_000, "EUR"), [invalid])


def test_withheld_percentage_can_use_gross_as_its_base() -> None:
    result = calculate_taxes(
        Money(10_000, "EUR"),
        [
            TaxInput("Added", ADDED, TaxBase.NET, PercentOrFixed.percent(1000)),
            TaxInput("Withheld", PAYER, TaxBase.GROSS, PercentOrFixed.percent(1000)),
        ],
    )
    assert result.gross == Money(11_000, "EUR")
    assert result.deducted[0].amount == Money(1_100, "EUR")


def test_tax_input_validation() -> None:
    with pytest.raises(ValueError, match="added_tax_base_must_be_net"):
        TaxInput("VAT", ADDED, TaxBase.GROSS, PercentOrFixed.percent(2300))


def test_percent_or_fixed_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="invalid_rate"):
        PercentOrFixed.percent(-1)
    with pytest.raises(ValueError, match="invalid_mode"):
        PercentOrFixed("unknown")
    with pytest.raises(ValueError, match="invalid_value"):
        PercentOrFixed.fixed_amount(Money(-1, "EUR"))


@given(
    net_minor=st.integers(min_value=0, max_value=10**15),
    rates=st.lists(st.integers(min_value=0, max_value=1_000_000), max_size=5),
    fixed=st.integers(min_value=0, max_value=10**9),
    base=st.sampled_from(list(TaxBase)),
)
def test_breakdown_identities_and_monotonicity(
    net_minor: int, rates: list[int], fixed: int, base: TaxBase
) -> None:
    rules = [
        *(
            TaxInput(f"Added {i}", ADDED, TaxBase.NET, PercentOrFixed.percent(rate))
            for i, rate in enumerate(rates)
        ),
        TaxInput("Withheld", PAYER, base, PercentOrFixed.percent(1234)),
        TaxInput("Levy", SELF, base, PercentOrFixed.fixed_amount(Money(fixed, "EUR"))),
    ]
    result = calculate_taxes(Money(net_minor, "EUR"), rules)
    added = sum(line.amount.amount_minor for line in result.added)
    withheld = sum(line.amount.amount_minor for line in result.deducted)
    assert result.gross.amount_minor == net_minor + added
    assert result.take_home.amount_minor == net_minor - withheld
    for line, rate in zip(result.added, rates, strict=True):
        # Independent oracle: exact rational half-up, then compare.
        exact = Fraction(net_minor * rate, 10_000)
        assert line.amount.amount_minor == int(exact + Fraction(1, 2))
    bigger = calculate_taxes(Money(net_minor + 1, "EUR"), rules)
    for small, large in zip(
        result.added + result.deducted, bigger.added + bigger.deducted, strict=True
    ):
        assert small.amount.amount_minor <= large.amount.amount_minor


def test_zero_net_and_negative_net() -> None:
    rules = _golden_rules(SELF, SELF)
    zero = calculate_taxes(Money(0, "PLN"), rules)
    assert zero.gross == Money(0, "PLN")
    # The fixed levy is still due, so take-home may go negative.
    assert zero.take_home == Money(-100_000, "PLN")
    with pytest.raises(ValueError, match="net_must_not_be_negative"):
        calculate_taxes(Money(-1, "PLN"), rules)


def test_percentage_is_exact_for_very_large_amounts() -> None:
    # (10**40 + 5000) / 10000 = 10**36 + 0.5 needs 38 digits; Decimal's default 28 would fail.
    net = 10**40 + 5_000
    result = calculate_taxes(
        Money(net, "EUR"),
        [TaxInput("Tax", ADDED, TaxBase.NET, PercentOrFixed.percent(1))],
    )
    assert result.added[0].amount.amount_minor == 10**36 + 1


@pytest.mark.parametrize(
    ("paid_on", "day", "timing", "expected"),
    [
        (date(2026, 11, 10), 25, PaymentTiming.NEXT_MONTH, date(2026, 12, 25)),
        (date(2026, 12, 10), 25, PaymentTiming.NEXT_MONTH, date(2027, 1, 25)),
        (date(2026, 11, 10), 25, PaymentTiming.SAME_MONTH, date(2026, 11, 25)),
        (date(2026, 1, 31), 31, PaymentTiming.NEXT_MONTH, date(2026, 2, 28)),  # clamped
        (date(2028, 1, 31), 31, PaymentTiming.NEXT_MONTH, date(2028, 2, 29)),  # leap year
        # After the quarter: Q1 (Jan-Mar) -> April, Q2 -> July, Q3 -> October, Q4 -> January.
        (date(2026, 1, 10), 20, PaymentTiming.AFTER_QUARTER, date(2026, 4, 20)),
        (date(2026, 3, 10), 20, PaymentTiming.AFTER_QUARTER, date(2026, 4, 20)),
        (date(2026, 6, 30), 20, PaymentTiming.AFTER_QUARTER, date(2026, 7, 20)),
        (date(2026, 8, 1), 31, PaymentTiming.AFTER_QUARTER, date(2026, 10, 31)),
        (date(2026, 12, 10), 20, PaymentTiming.AFTER_QUARTER, date(2027, 1, 20)),
    ],
)
def test_tax_payment_date(paid_on, day, timing, expected) -> None:
    assert tax_payment_date(paid_on, day, timing) == expected


def test_payment_date_with_explicit_offset_and_validation() -> None:
    assert tax_payment_date_with_offset(date(2026, 11, 10), 5, 2) == date(2027, 1, 5)
    with pytest.raises(ValueError, match="invalid_payment_schedule"):
        tax_payment_date_with_offset(date(2026, 11, 10), 0, 1)
    with pytest.raises(ValueError, match="invalid_payment_schedule"):
        tax_payment_date_with_offset(date(2026, 11, 10), 5, -1)


@given(
    paid_on=st.dates(min_value=date(1900, 1, 1), max_value=date(2199, 11, 30)),
    day=st.integers(min_value=1, max_value=31),
    timing=st.sampled_from(list(PaymentTiming)),
)
def test_payment_date_properties(paid_on: date, day: int, timing: PaymentTiming) -> None:
    result = tax_payment_date(paid_on, day, timing)
    months = (result.year - paid_on.year) * 12 + result.month - paid_on.month
    assert months == timing_month_offset(paid_on, timing)
    assert 0 <= months <= 3
    assert result.day == day or result.day < day  # clamped only at month end
    if timing == PaymentTiming.AFTER_QUARTER:
        assert result.month in (1, 4, 7, 10)
