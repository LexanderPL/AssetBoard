"""Pure demo plan tests (F9.11a): determinism, ordering and the amount conversion table."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from assetboard.domain.demo import (
    CURRENCY_FACTORS,
    DEMO_DATASET_VERSION,
    DEMO_EXCLUDED_MODELS,
    build_plan,
    convert_amount,
)


def _plan(reference: date = date(2026, 11, 20), *, locale: str = "en", currency: str = "EUR"):
    return build_plan(
        reference_date=reference,
        locale=locale,
        base_currency=currency,
        translate=lambda message: message,
    )


def test_same_reference_locale_and_currency_produce_an_equal_plan() -> None:
    first = _plan()
    second = _plan()
    assert first == second
    assert first.version == DEMO_DATASET_VERSION


def test_a_different_reference_date_shifts_every_month_and_changes_the_sample() -> None:
    first = _plan(date(2026, 11, 20))
    second = _plan(date(2026, 12, 20))
    assert first.months != second.months
    assert first.closed_month != second.closed_month
    assert first.transactions != second.transactions
    assert first.transactions[0].day != second.transactions[0].day


def test_the_plan_is_stably_sorted() -> None:
    plan = _plan()
    assert list(plan.transactions) == sorted(
        plan.transactions,
        key=lambda row: (row.day, row.direction, row.description, row.category),
    )
    assert list(plan.planned_values) == sorted(
        plan.planned_values, key=lambda row: (row.month, row.category)
    )
    assert list(plan.price_points) == sorted(
        plan.price_points, key=lambda row: (row.instrument, row.day)
    )
    assert list(plan.fx_points) == sorted(
        plan.fx_points, key=lambda row: (row.quote_currency, row.day)
    )
    assert list(plan.investment_transactions) == sorted(
        plan.investment_transactions,
        key=lambda row: (row.day, row.type, row.instrument, row.quantity),
    )


@pytest.mark.parametrize(
    ("amount", "currency", "expected_minor"),
    [
        (Decimal("10"), "EUR", 1000),
        (Decimal("10"), "PLN", 4300),
        (Decimal("10"), "USD", 1100),
        (Decimal("10"), "GBP", 850),
        (Decimal("10"), "CHF", 950),
        (Decimal("10"), "SEK", 1000),  # unknown currencies keep the EUR amount
        (Decimal("12.34"), "PLN", 5306),  # 12.34 * 4.3 = 53.062, half up
    ],
)
def test_amount_conversion_table(amount: Decimal, currency: str, expected_minor: int) -> None:
    money = convert_amount(amount, currency)
    assert (money.amount_minor, money.currency) == (expected_minor, currency)


def test_bill_amounts_round_to_whole_currency_units() -> None:
    assert convert_amount(Decimal("12.34"), "PLN", whole=True).amount_minor == 5300
    assert convert_amount(Decimal("12.50"), "EUR", whole=True).amount_minor == 1300
    assert convert_amount(Decimal("12.50"), "JPY").amount_minor == 13  # 0 minor units


def test_conversion_refuses_negative_or_non_finite_amounts() -> None:
    with pytest.raises(ValueError):
        convert_amount(Decimal("-1"), "EUR")
    with pytest.raises(ValueError):
        convert_amount(Decimal("NaN"), "EUR")


def test_currency_factors_are_the_documented_table() -> None:
    assert dict(CURRENCY_FACTORS) == {
        "EUR": Decimal("1"),
        "PLN": Decimal("4.3"),
        "USD": Decimal("1.1"),
        "GBP": Decimal("0.85"),
        "CHF": Decimal("0.95"),
    }


def test_excluded_models_are_the_five_operational_tables_with_reasons() -> None:
    assert set(DEMO_EXCLUDED_MODELS) == {
        "Invitation",
        "NotificationChannel",
        "NotificationLog",
        "PriceSourceConfig",
        "ProviderError",
    }
    assert all(reason.strip() for reason in DEMO_EXCLUDED_MODELS.values())


def test_plan_amounts_use_the_base_currency_and_expected_shapes() -> None:
    plan = _plan(currency="PLN")
    assert all(row.amount.currency == "PLN" for row in plan.transactions)
    assert all(row.amount.currency == "PLN" for row in plan.planned_values)
    assert all(
        value.value.currency == "PLN" for item in plan.networth_items for value in item.values
    )
    assert len(plan.months) == 4
    assert len(plan.planned_values) == 21 * 4
    assert len(plan.snapshot_dates) == 12
    assert {row.type for row in plan.investment_transactions} >= {
        "buy",
        "sell",
        "dividend",
        "split",
    }
    assert {row.freq for row in plan.recurring} == {"once", "weekly", "monthly", "yearly"}


def test_nothing_is_dated_after_t_except_planned_future_rows() -> None:
    reference = date(2026, 11, 20)
    plan = _plan(reference)
    assert all(row.day <= reference for row in plan.transactions)
    assert all(row.day <= reference for row in plan.investment_transactions)
    assert all(row.day <= reference for row in plan.price_points)
    assert all(row.day <= reference for row in plan.fx_points)
    assert all(point.as_of <= reference for item in plan.networth_items for point in item.values)
    assert all(day <= reference for day in plan.snapshot_dates)
    future_items = [row for row in plan.recurring if row.start_date > reference]
    assert {row.key for row in future_items} == {"insurance", "one-time"}
