from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from assetboard.domain.money import Money
from assetboard.domain.percent_or_fixed import PercentOrFixed
from assetboard.domain.taxes import (
    RecordAs,
    TaxBase,
    TaxEffect,
    TaxInput,
    calculate_taxes,
)


def _golden_rules() -> tuple[TaxInput, ...]:
    return (
        TaxInput(
            "VAT",
            TaxEffect.ADDED,
            TaxBase.NET,
            PercentOrFixed.percent(2300),
        ),
        TaxInput(
            "Income tax",
            TaxEffect.WITHHELD,
            TaxBase.NET,
            PercentOrFixed.percent(1200),
        ),
        TaxInput(
            "Fixed levy",
            TaxEffect.WITHHELD,
            TaxBase.GROSS,
            PercentOrFixed.fixed_amount(Money(100000, "PLN")),
        ),
    )


@pytest.mark.parametrize(
    ("record_as", "ledger_minor", "payments"),
    [
        (
            RecordAs.CASH_RECEIVED,
            2_874_510,
            {"VAT": 537_510, "Income tax": 280_440, "Fixed levy": 100_000},
        ),
        (RecordAs.NET, 2_337_000, {"Income tax": 280_440, "Fixed levy": 100_000}),
        (RecordAs.TAKE_HOME, 1_956_560, {}),
    ],
)
def test_golden_tax_breakdown(record_as, ledger_minor, payments) -> None:
    result = calculate_taxes(Money(2_337_000, "PLN"), _golden_rules(), record_as)
    assert [(line.name, line.amount.amount_minor) for line in result.added] == [("VAT", 537_510)]
    assert [(line.name, line.amount.amount_minor) for line in result.withheld] == [
        ("Income tax", 280_440),
        ("Fixed levy", 100_000),
    ]
    assert result.gross == Money(2_874_510, "PLN")
    assert result.take_home == Money(1_956_560, "PLN")
    assert result.ledger_amount == Money(ledger_minor, "PLN")
    assert {line.name: line.amount.amount_minor for line in result.payments} == payments


@given(
    net_minor=st.integers(min_value=0, max_value=10**12),
    record_as=st.sampled_from(list(RecordAs)),
    definitions=st.lists(
        st.tuples(
            st.sampled_from(list(TaxEffect)),
            st.sampled_from(list(TaxBase)),
            st.sampled_from(["percent", "fixed"]),
            st.integers(min_value=0, max_value=100_000),
        ),
        max_size=8,
    ),
)
def test_generated_payments_reconcile_ledger_amount_to_take_home(
    net_minor: int, record_as: RecordAs, definitions: list[tuple]
) -> None:
    rules = tuple(
        TaxInput(
            name=f"Tax {index}",
            effect=effect,
            base=TaxBase.NET if effect == TaxEffect.ADDED else base,
            value=(
                PercentOrFixed.percent(amount)
                if mode == "percent"
                else PercentOrFixed.fixed_amount(Money(amount, "EUR"))
            ),
        )
        for index, (effect, base, mode, amount) in enumerate(definitions)
    )
    result = calculate_taxes(Money(net_minor, "EUR"), rules, record_as)
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
        [TaxInput("Tax", TaxEffect.ADDED, TaxBase.NET, PercentOrFixed.percent(rate_bp))],
        RecordAs.CASH_RECEIVED,
    )
    assert result.added[0].amount == Money(expected_minor, "EUR")


def test_fixed_rule_and_mismatched_currency() -> None:
    rule = TaxInput(
        "Levy",
        TaxEffect.WITHHELD,
        TaxBase.NET,
        PercentOrFixed.fixed_amount(Money(1234, "EUR")),
    )
    assert calculate_taxes(Money(10_000, "EUR"), [rule], RecordAs.NET).withheld[0].amount == Money(
        1234, "EUR"
    )
    invalid = TaxInput(
        "Levy",
        TaxEffect.WITHHELD,
        TaxBase.NET,
        PercentOrFixed.fixed_amount(Money(1234, "USD")),
    )
    with pytest.raises(ValueError, match="currency_mismatch"):
        calculate_taxes(Money(10_000, "EUR"), [invalid], RecordAs.NET)


def test_withheld_percentage_can_use_gross_as_its_base() -> None:
    result = calculate_taxes(
        Money(10_000, "EUR"),
        [
            TaxInput("Added", TaxEffect.ADDED, TaxBase.NET, PercentOrFixed.percent(1000)),
            TaxInput("Withheld", TaxEffect.WITHHELD, TaxBase.GROSS, PercentOrFixed.percent(1000)),
        ],
        RecordAs.NET,
    )
    assert result.gross == Money(11_000, "EUR")
    assert result.withheld[0].amount == Money(1_100, "EUR")


def test_tax_input_validation() -> None:
    with pytest.raises(ValueError, match="added_tax_base_must_be_net"):
        TaxInput("VAT", TaxEffect.ADDED, TaxBase.GROSS, PercentOrFixed.percent(2300))


def test_percent_or_fixed_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="invalid_rate"):
        PercentOrFixed.percent(-1)
    with pytest.raises(ValueError, match="invalid_mode"):
        PercentOrFixed("unknown")
    with pytest.raises(ValueError, match="invalid_value"):
        PercentOrFixed.fixed_amount(Money(-1, "EUR"))
