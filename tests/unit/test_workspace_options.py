from __future__ import annotations

import pytest

from assetboard.domain.money import CURRENCY_MINOR_UNITS
from assetboard.services.workspace_options import (
    WorkspaceOptions,
    country_currency_map,
    country_default_currency,
    country_names,
    currency_codes,
    locale_codes,
    timezone_names,
    validate_workspace_options,
)


def test_option_lists_come_from_existing_helpers() -> None:
    assert set(currency_codes()) == set(CURRENCY_MINOR_UNITS)
    assert {"EUR", "PLN", "JPY", "KWD"} <= set(currency_codes())
    assert {"Europe/Warsaw", "UTC", "America/New_York"} <= set(timezone_names())
    assert country_names("en")["PL"] == "Poland"
    assert country_names("pl")["PL"] == "Polska"
    assert {"ZZ", "EU", "UN", "001"}.isdisjoint(country_names("en"))
    assert locale_codes() == ("en", "pl")


def test_country_default_currency_uses_babels_current_tender_data() -> None:
    assert country_default_currency("PL") == "PLN"
    assert country_default_currency("us") == "USD"
    assert country_default_currency(" GB ") == "GBP"
    assert country_default_currency("DE") == "EUR"
    # The first current tender currency wins, even where a second one is also current.
    assert country_default_currency("PA") == "PAB"
    # ZWG is current for Zimbabwe but the app cannot store it, so USD comes first.
    assert country_default_currency("ZW") == "USD"
    assert country_default_currency(None) is None
    assert country_default_currency("") is None
    assert country_default_currency("ZZ") is None


def test_country_currency_map_matches_the_offered_countries() -> None:
    mapping = country_currency_map()
    assert set(mapping) <= set(country_names("en"))
    assert set(mapping.values()) <= set(currency_codes())
    assert mapping["PL"] == "PLN" and mapping["US"] == "USD"
    # Babel lists only IRR for Iran; the app does not support it, so the country has no default.
    assert "IR" not in mapping
    assert country_currency_map() is mapping  # cached: built once per process


def test_workspace_options_are_normalized() -> None:
    assert validate_workspace_options(
        base_currency=" pln ", country_code=" pl ", timezone="Europe/Warsaw", locale="pl-PL"
    ) == WorkspaceOptions(
        base_currency="PLN", country_code="PL", timezone="Europe/Warsaw", locale="pl"
    )
    assert (
        validate_workspace_options(
            base_currency="EUR", country_code="", timezone="UTC", locale="en"
        )
    ).country_code is None


@pytest.mark.parametrize(
    "values",
    [
        {"base_currency": "XYZ"},
        {"base_currency": "EURO"},
        {"country_code": "ZZ"},
        {"country_code": "POL"},
        {"timezone": "Europe/Atlantis"},
        {"timezone": ""},
        {"locale": "de"},
    ],
)
def test_workspace_options_reject_unknown_values(values: dict[str, str]) -> None:
    arguments = {"base_currency": "EUR", "country_code": None, "timezone": "UTC", "locale": "en"}
    with pytest.raises(ValueError):
        validate_workspace_options(**(arguments | values))  # type: ignore[arg-type]
