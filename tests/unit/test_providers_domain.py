"""Pure provider conversion rules (F6.4)."""

from __future__ import annotations

from decimal import Decimal, localcontext

import pytest

from assetboard.domain.providers import (
    COINGECKO,
    ECB,
    EODHD,
    NBP,
    InvalidProviderRate,
    InvalidProviderSymbol,
    UnsupportedUnit,
    clean_provider_symbol,
    convert_to_workspace_base,
    ecb_quote_to_rate,
    nbp_gold_symbol,
    nbp_gold_to_unit,
    normalize_instrument_symbol,
    normalize_provider_decimal,
    provider_supports_instrument,
)


def test_nbp_gold_symbol_normalizes_blank_and_xau() -> None:
    assert nbp_gold_symbol(None) == "XAU"
    assert nbp_gold_symbol("") == "XAU"
    assert nbp_gold_symbol("xau") == "XAU"
    assert nbp_gold_symbol("gold") == "XAU"
    assert nbp_gold_symbol("XAG") is None


def test_nbp_gold_converts_grams_to_troy_ounces_for_english_and_polish_units() -> None:
    result = nbp_gold_to_unit(Decimal("250.00"), "oz")
    with localcontext() as context:
        context.prec = 80
        expected = Decimal("250.00") * Decimal("31.1034768")
    assert result == expected
    assert nbp_gold_to_unit(Decimal("250"), "uncja") == expected
    assert nbp_gold_to_unit(Decimal("250"), "g") == Decimal("250")
    with pytest.raises(UnsupportedUnit):
        nbp_gold_to_unit(Decimal("250"), "kilogram")


def test_ecb_quote_is_inverted_with_high_precision() -> None:
    with localcontext() as context:
        context.prec = 80
        expected = Decimal(1) / Decimal("4.3000")
    assert ecb_quote_to_rate(Decimal("4.3000")) == expected
    with pytest.raises(InvalidProviderRate):
        ecb_quote_to_rate(Decimal(0))


def test_workspace_conversion_matches_direct_and_provider_base_paths() -> None:
    # NBP in a EUR workspace: PLN per USD / PLN per EUR.
    with localcontext() as context:
        context.prec = 80
        expected = Decimal("3.9000") / Decimal("4.3000")
    assert convert_to_workspace_base(Decimal("3.9000"), Decimal("4.3000")) == expected

    # ECB in a PLN workspace: (EUR per USD) / (EUR per PLN).
    usd = ecb_quote_to_rate(Decimal("1.1000"))
    pln = ecb_quote_to_rate(Decimal("4.3000"))
    with localcontext() as context:
        context.prec = 80
        direct = Decimal("4.3000") / Decimal("1.1000")
    assert convert_to_workspace_base(usd, pln) == direct

    with pytest.raises(InvalidProviderRate):
        convert_to_workspace_base(Decimal("1"), Decimal(0))


def test_provider_decimals_are_fitted_to_the_decimal_type() -> None:
    assert normalize_provider_decimal(Decimal("0"), positive=False) == 0
    with pytest.raises(InvalidProviderRate):
        normalize_provider_decimal(Decimal("-1"), positive=False)
    with pytest.raises(InvalidProviderRate):
        normalize_provider_decimal(Decimal(0), positive=True)
    value = normalize_provider_decimal(Decimal("0.1234567890123456789"), positive=False)
    assert value == Decimal("0.123456789012345679")
    assert len(value.as_tuple().digits) <= 38


def test_provider_symbol_normalization() -> None:
    assert normalize_instrument_symbol(NBP, None) == "XAU"
    assert normalize_instrument_symbol(NBP, "GOLD") == "XAU"
    assert normalize_instrument_symbol(NBP, "XAG") is None
    assert normalize_instrument_symbol(ECB, "anything") == "anything"


def test_coingecko_and_eodhd_symbols_are_canonicalised() -> None:
    assert clean_provider_symbol(COINGECKO, "Bitcoin") == "bitcoin"
    assert clean_provider_symbol(EODHD, "vwce.de") == "VWCE.DE"
    with pytest.raises(InvalidProviderSymbol):
        clean_provider_symbol(COINGECKO, "bad symbol!")
    with pytest.raises(InvalidProviderSymbol):
        clean_provider_symbol(EODHD, "bad symbol!")


def test_provider_support_helpers_match_the_adapter_rules() -> None:
    assert provider_supports_instrument(
        COINGECKO, type="crypto", quote_currency="USD", provider_symbol="bitcoin"
    )
    assert not provider_supports_instrument(
        COINGECKO, type="etf", quote_currency="USD", provider_symbol="bitcoin"
    )
    assert not provider_supports_instrument(
        COINGECKO, type="crypto", quote_currency="CHF", provider_symbol="bitcoin"
    )
    assert provider_supports_instrument(
        EODHD, type="etf", quote_currency="EUR", provider_symbol="VWCE.DE"
    )
    assert not provider_supports_instrument(
        EODHD, type="crypto", quote_currency="EUR", provider_symbol="VWCE.DE"
    )
