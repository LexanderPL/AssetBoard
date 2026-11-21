"""Option lists for the workspace fields (first-run setup, "Create workspace", settings)."""

from __future__ import annotations

from typing import TypedDict

from assetboard.services.workspace_options import (
    country_currency_map,
    country_names,
    currency_codes,
    locale_codes,
    timezone_names,
)

LOCALE_NAMES = {"en": "English", "pl": "Polski"}


class WorkspaceFormOptions(TypedDict):
    currency_options: list[tuple[str, str]]
    country_options: list[tuple[str, str]]
    # Country code -> the base currency the country's option suggests (W-1).
    country_currencies: dict[str, str]
    timezone_options: list[tuple[str, str]]
    locales: list[tuple[str, str]]


def workspace_form_options(locale: str) -> WorkspaceFormOptions:
    """Currencies, countries (named in ``locale``), their currencies, time zones and languages."""
    return {
        "currency_options": [(code, code) for code in currency_codes()],
        "country_options": list(country_names(locale).items()),
        "country_currencies": country_currency_map(),
        "timezone_options": [(zone, zone) for zone in timezone_names()],
        "locales": [(code, LOCALE_NAMES.get(code, code)) for code in locale_codes()],
    }
