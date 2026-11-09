"""Investment domain rules (F6.1): types, names, allocation weights and the seed set."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from assetboard.domain.instruments import (
    ALLOCATION_TOTAL_BP,
    ASSET_CLASS_SEED,
    ASSET_CLASS_SEED_V1,
    ASSET_CLASS_SEEDS,
    TYPE_DEFAULTS,
    TYPE_LABELS,
    VALUATION_SOURCE_LABELS,
    InstrumentType,
    InvalidAllocation,
    InvalidAssetClassName,
    InvalidInstrumentName,
    InvalidQuoteCurrency,
    InvalidSymbol,
    InvalidUnitLabel,
    RiskTier,
    ValuationSource,
    clean_asset_class_name,
    clean_instrument_name,
    clean_quote_currency,
    clean_symbol,
    clean_unit_label,
    parse_risk_tier,
    percent_to_bp,
    validate_allocation,
)


def test_type_defaults_match_the_spec() -> None:
    assert TYPE_DEFAULTS[InstrumentType.CRYPTO].unit_label == "coin"
    assert TYPE_DEFAULTS[InstrumentType.METAL].unit_label == "oz"
    assert TYPE_DEFAULTS[InstrumentType.CURRENCY].unit_from_quote_currency is True
    assert TYPE_DEFAULTS[InstrumentType.REAL_ESTATE].quantity_is_one is True
    assert TYPE_DEFAULTS[InstrumentType.REAL_ESTATE].valuation_source is ValuationSource.MANUAL
    assert TYPE_DEFAULTS[InstrumentType.BOND_MODEL].valuation_source is (ValuationSource.BOND_MODEL)
    assert set(TYPE_DEFAULTS) == set(InstrumentType)
    assert set(TYPE_LABELS) == set(InstrumentType)
    assert set(VALUATION_SOURCE_LABELS) == set(ValuationSource)


def test_the_seed_has_the_spec_names_order_and_investable_flags() -> None:
    # F5.7 (owner decision 2026-11-09): property lives in net worth, so neither property class
    # is seeded, and Other is investable.
    assert [seed.name for seed in ASSET_CLASS_SEED] == [
        "Stocks",
        "Bonds",
        "Metals",
        "Crypto",
        "Currencies",
        "Other",
    ]
    assert all(seed.investable for seed in ASSET_CLASS_SEED)
    assert all(len(seed.color) == 7 for seed in ASSET_CLASS_SEED)
    assert {seed.name: seed.risk_tier for seed in ASSET_CLASS_SEED} == {
        "Stocks": RiskTier.GROWTH,
        "Bonds": RiskTier.SAFE,
        "Metals": RiskTier.SAFE,
        "Crypto": RiskTier.SPECULATIVE,
        "Currencies": RiskTier.GROWTH,
        "Other": None,
    }


def test_the_pre_f57_seed_is_kept_for_untouched_set_checks() -> None:
    assert ASSET_CLASS_SEEDS == (ASSET_CLASS_SEED_V1, ASSET_CLASS_SEED)
    assert [seed.name for seed in ASSET_CLASS_SEED_V1] == [
        "Stocks",
        "Bonds",
        "Metals",
        "Crypto",
        "Currencies",
        "Real estate",
        "Primary residence",
        "Other",
    ]
    assert [seed.investable for seed in ASSET_CLASS_SEED_V1] == [
        True,
        True,
        True,
        True,
        True,
        False,
        False,
        False,
    ]
    # Unchanged classes keep their colours, so both sets compare as shipped.
    v1 = {seed.name: seed.color for seed in ASSET_CLASS_SEED_V1}
    assert all(v1[seed.name] == seed.color for seed in ASSET_CLASS_SEED)
    assert {seed.name: seed.risk_tier for seed in ASSET_CLASS_SEED_V1} == {
        "Stocks": RiskTier.GROWTH,
        "Bonds": RiskTier.SAFE,
        "Metals": RiskTier.SAFE,
        "Crypto": RiskTier.SPECULATIVE,
        "Currencies": RiskTier.GROWTH,
        "Real estate": None,
        "Primary residence": None,
        "Other": None,
    }


def test_asset_class_names_are_cleaned_and_bounded() -> None:
    assert clean_asset_class_name("  Stocks  ") == "Stocks"
    assert clean_asset_class_name("Real   estate") == "Real estate"
    with pytest.raises(InvalidAssetClassName):
        clean_asset_class_name("")
    with pytest.raises(InvalidAssetClassName):
        clean_asset_class_name("x" * 81)
    with pytest.raises(InvalidAssetClassName):
        clean_asset_class_name("Stocks\u202e")


def test_instrument_names_are_cleaned_and_bounded() -> None:
    assert clean_instrument_name("  World   ETF ") == "World ETF"
    with pytest.raises(InvalidInstrumentName):
        clean_instrument_name("")
    with pytest.raises(InvalidInstrumentName):
        clean_instrument_name("x" * 201)


def test_symbols_are_optional_and_bounded() -> None:
    assert clean_symbol("") is None
    assert clean_symbol("  VWCE.DE ") == "VWCE.DE"
    with pytest.raises(InvalidSymbol):
        clean_symbol("x" * 33)
    with pytest.raises(InvalidSymbol):
        clean_symbol("BTC\u202e")


def test_quote_currency_is_uppercased_and_validated() -> None:
    assert clean_quote_currency(" eur ") == "EUR"
    with pytest.raises(InvalidQuoteCurrency):
        clean_quote_currency("XXX")
    with pytest.raises(InvalidQuoteCurrency):
        clean_quote_currency("EU")
    # BTC is a symbol, not an ISO 4217 quote currency; it must not become one just because a
    # crypto instrument can be quoted in USD.
    with pytest.raises(InvalidQuoteCurrency):
        clean_quote_currency("BTC")


def test_a_currency_instrument_always_uses_its_currency_code_as_the_unit() -> None:
    assert (
        clean_unit_label("coin", instrument_type=InstrumentType.CURRENCY, quote_currency="USD")
        == "USD"
    )
    assert (
        clean_unit_label(" share ", instrument_type=InstrumentType.EQUITY, quote_currency="EUR")
        == "share"
    )
    with pytest.raises(InvalidUnitLabel):
        clean_unit_label("", instrument_type=InstrumentType.EQUITY, quote_currency="EUR")


@pytest.mark.parametrize(
    ("percent", "expected"),
    [("0", 0), ("0.01", 1), ("12.5", 1250), ("100", ALLOCATION_TOTAL_BP)],
)
def test_percent_to_bp_accepts_at_most_two_decimals(percent: str, expected: int) -> None:
    assert percent_to_bp(Decimal(percent)) == expected


@pytest.mark.parametrize("percent", ["-0.01", "100.01", "12.345"])
def test_percent_to_bp_refuses_invalid_weights(percent: str) -> None:
    with pytest.raises(InvalidAllocation):
        percent_to_bp(Decimal(percent))


def test_allocation_must_have_at_least_one_class() -> None:
    with pytest.raises(InvalidAllocation) as caught:
        validate_allocation([])
    assert caught.value.code == "allocation_required"


def test_allocation_weights_must_total_exactly_100_percent() -> None:
    one, two = uuid4(), uuid4()
    validate_allocation([(one, 6_000), (two, 4_000)])
    with pytest.raises(InvalidAllocation) as caught:
        validate_allocation([(one, 6_000), (two, 3_999)])
    assert caught.value.code == "allocation_total"
    with pytest.raises(InvalidAllocation) as caught:
        validate_allocation([(one, 0), (two, 10_000)])
    assert caught.value.code == "invalid_weight"
    with pytest.raises(InvalidAllocation) as caught:
        validate_allocation([(one, 5_000), (one, 5_000)])
    assert caught.value.code == "duplicate_class"


def test_risk_tiers_are_the_three_owner_agreed_values() -> None:
    assert {tier.value for tier in RiskTier} == {"safe", "growth", "speculative"}
    assert parse_risk_tier(" growth ") is RiskTier.GROWTH
    assert parse_risk_tier("") is None
    with pytest.raises(ValueError):
        parse_risk_tier("risky")


@given(st.integers(min_value=0, max_value=ALLOCATION_TOTAL_BP))
def test_percent_to_bp_round_trips_every_basis_point(basis_points: int) -> None:
    assert percent_to_bp(Decimal(basis_points).scaleb(-2)) == basis_points


@given(
    st.lists(
        st.integers(min_value=1, max_value=ALLOCATION_TOTAL_BP),
        min_size=1,
        max_size=20,
    )
)
def test_validate_allocation_accepts_only_exact_totals(weights: list[int]) -> None:
    rows = [(uuid4(), weight) for weight in weights]
    try:
        validate_allocation(rows)
    except InvalidAllocation:
        assert sum(weights) != ALLOCATION_TOTAL_BP
    else:
        assert sum(weights) == ALLOCATION_TOTAL_BP
