from __future__ import annotations

from decimal import Decimal

from babel import Locale
from babel.core import UnknownLocaleError
from babel.numbers import (
    NumberFormatError,
    UnknownCurrencyError,
    format_currency,
    get_group_symbol,
    parse_decimal,
)

from assetboard.domain.money import Money
from assetboard.i18n import gettext


def _normalize_number_locale(locale: str) -> str:
    try:
        return str(Locale.parse(locale.replace("-", "_")))
    except (UnknownLocaleError, ValueError) as exc:
        raise ValueError(f"Unsupported locale: {locale}") from exc


def _invalid_amount_message(locale: str) -> str:
    language = locale.split("_", maxsplit=1)[0]
    return gettext("Enter a valid amount.", language if language in {"en", "pl"} else "en")


def parse_amount(text: str, locale: str) -> Decimal:
    normalized_locale = _normalize_number_locale(locale)
    normalized_text = text.strip()
    if not normalized_text:
        raise ValueError(_invalid_amount_message(normalized_locale))

    group_symbol = get_group_symbol(Locale.parse(normalized_locale))
    normalized_text = normalized_text.replace("\u00a0", group_symbol).replace(
        "\u202f", group_symbol
    )
    if " " in normalized_text and group_symbol != " ":
        normalized_text = normalized_text.replace(" ", group_symbol)

    try:
        amount = parse_decimal(normalized_text, locale=normalized_locale, strict=True)
    except (NumberFormatError, ValueError) as exc:
        raise ValueError(_invalid_amount_message(normalized_locale)) from exc
    if not amount.is_finite():
        raise ValueError(_invalid_amount_message(normalized_locale))
    return amount


def format_money(money: Money, locale: str) -> str:
    normalized_locale = _normalize_number_locale(locale)
    try:
        return format_currency(
            money.to_decimal(),
            money.currency,
            locale=normalized_locale,
            decimal_quantization=False,
        )
    except UnknownCurrencyError as exc:
        raise ValueError(f"Unsupported currency: {money.currency}") from exc
