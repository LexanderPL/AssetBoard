from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from babel import Locale
from babel.core import UnknownLocaleError
from babel.numbers import (
    UnknownCurrencyError,
    format_currency,
    format_percent,
    get_decimal_symbol,
    get_group_symbol,
)

from assetboard.domain.money import Money
from assetboard.i18n import gettext

_MAX_AMOUNT_LENGTH = 64
_SPACE_GROUPS = " \u00a0\u202f"


def _normalize_number_locale(locale: str) -> str:
    try:
        return str(Locale.parse(locale.replace("-", "_")))
    except (UnknownLocaleError, ValueError) as exc:
        raise ValueError(f"Unsupported locale: {locale}") from exc


def _invalid_amount_message(locale: str) -> str:
    language = locale.split("_", maxsplit=1)[0]
    return gettext("Enter a valid amount.", language if language in {"en", "pl"} else "en")


def _valid_groups(groups: list[str]) -> bool:
    """Thousands groups: a first group of 1-3 digits without a leading zero, then groups of
    exactly 3 digits (``1,234,567``). ``0,001`` and ``01,234`` are not grouped numbers."""
    return (
        len(groups) > 1
        and 1 <= len(groups[0]) <= 3
        and groups[0].isascii()
        and groups[0].isdigit()
        and not groups[0].startswith("0")
        and all(len(group) == 3 and group.isascii() and group.isdigit() for group in groups[1:])
    )


def _grouped_integer(value: str, separator: str) -> bool:
    return _valid_groups(value.split(separator))


def _remove_space_groups(value: str) -> str:
    if not any(character in _SPACE_GROUPS for character in value):
        return value
    groups = re.split(r"[ \u00a0\u202f]", value)
    if not _valid_groups(groups):
        raise ValueError
    return "".join(groups)


def parse_amount(text: str, locale: str) -> Decimal:
    normalized_locale = _normalize_number_locale(locale)
    invalid = _invalid_amount_message(normalized_locale)
    normalized_text = text.strip().replace("\u2212", "-")
    if not normalized_text or len(normalized_text) > _MAX_AMOUNT_LENGTH:
        raise ValueError(invalid)

    sign = ""
    if normalized_text[0] in "+-":
        sign, normalized_text = normalized_text[0], normalized_text[1:]
    if not normalized_text:
        raise ValueError(invalid)

    decimal_symbol = get_decimal_symbol(normalized_locale)
    group_symbol = get_group_symbol(normalized_locale)
    punctuation = {character for character in normalized_text if character in ".,"}
    try:
        if len(punctuation) == 2:
            decimal_separator = max(
                (normalized_text.rfind("."), "."),
                (normalized_text.rfind(","), ","),
            )[1]
            if normalized_text.count(decimal_separator) != 1:
                raise ValueError
            integer_part, fraction = normalized_text.rsplit(decimal_separator, maxsplit=1)
            group_separator = next(iter(punctuation - {decimal_separator}))
            if (
                not fraction.isascii()
                or not fraction.isdigit()
                or not _grouped_integer(integer_part, group_separator)
            ):
                raise ValueError
            normalized_text = integer_part.replace(group_separator, "") + "." + fraction
        elif len(punctuation) == 1:
            separator = next(iter(punctuation))
            count = normalized_text.count(separator)
            if separator == decimal_symbol:
                if count != 1:
                    raise ValueError
                integer_part, fraction = normalized_text.split(separator, maxsplit=1)
                integer_part = _remove_space_groups(integer_part)
                if not fraction.isascii() or not fraction.isdigit():
                    raise ValueError
                normalized_text = integer_part + "." + fraction
            elif count > 1:
                if separator != group_symbol or not _grouped_integer(normalized_text, separator):
                    raise ValueError
                normalized_text = normalized_text.replace(separator, "")
            else:
                integer_part, fraction = normalized_text.split(separator, maxsplit=1)
                if not fraction.isascii() or not fraction.isdigit():
                    raise ValueError
                if len(fraction) == 3:
                    if separator == group_symbol and _grouped_integer(normalized_text, separator):
                        normalized_text = normalized_text.replace(separator, "")
                    elif separator == decimal_symbol:
                        normalized_text = integer_part + "." + fraction
                    else:
                        raise ValueError
                elif separator == group_symbol and len(fraction) != 1 and len(fraction) != 2:
                    raise ValueError
                else:
                    integer_part = _remove_space_groups(integer_part)
                    normalized_text = integer_part + "." + fraction
        else:
            normalized_text = _remove_space_groups(normalized_text)

        if not normalized_text.isascii() or not re.fullmatch(r"\d+(?:\.\d+)?", normalized_text):
            raise ValueError
        amount = Decimal(sign + normalized_text)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(invalid) from exc
    if not amount.is_finite():
        raise ValueError(invalid)
    return amount


# "Native style everywhere" (owner, 2026-10-24): a currency with one obvious home country is
# written the way that country writes it, whatever the interface language (PLN is
# ``1 234,50 zł`` in English too, USD ``$1,234.50`` in Polish too). Currencies shared by several
# countries (EUR) or not listed here follow the interface locale, and so do CAD, AUD, NZD and
# MXN, whose home style is a bare "$" that could not be told apart from USD ("CA$" in English).
# Input fields do not: they follow the interface locale (``format_amount_input``, ``parse_amount``).
CURRENCY_HOME_LOCALES: dict[str, str] = {
    "BGN": "bg_BG",
    "BRL": "pt_BR",
    "CHF": "de_CH",
    "CNY": "zh_CN",
    "CZK": "cs_CZ",
    "DKK": "da_DK",
    "GBP": "en_GB",
    "HUF": "hu_HU",
    "INR": "en_IN",
    "ISK": "is_IS",
    "JPY": "ja_JP",
    "KRW": "ko_KR",
    "NOK": "nb_NO",
    "PLN": "pl_PL",
    "RON": "ro_RO",
    "SEK": "sv_SE",
    "TRY": "tr_TR",
    "UAH": "uk_UA",
    "USD": "en_US",
}
# A letter code glued to the number ("THB1,234.50", "1,234.50THB", "CHF-1’234.50").
_CODE_BEFORE_NUMBER = re.compile(r"(?<![A-Za-z])([A-Z]{3})(?=[\d\-\u2212])")
_CODE_AFTER_NUMBER = re.compile(r"(?<=\d)([A-Z]{3})(?![A-Za-z])")


def format_money(money: Money, locale: str) -> str:
    """The amount with its currency, in the currency's native style (``CURRENCY_HOME_LOCALES``)."""
    normalized_locale = _normalize_number_locale(locale)
    try:
        text = format_currency(
            money.to_decimal(),
            money.currency,
            locale=CURRENCY_HOME_LOCALES.get(money.currency, normalized_locale),
            decimal_quantization=False,
        )
    except UnknownCurrencyError as exc:
        raise ValueError(f"Unsupported currency: {money.currency}") from exc
    text = _CODE_BEFORE_NUMBER.sub("\\1\u00a0", text)
    return _CODE_AFTER_NUMBER.sub("\u00a0\\1", text)


def format_amount_input(money: Money, locale: str) -> str:
    """The amount for an input field: the locale's decimal sign, no grouping, no currency.

    ``parse_amount`` reads it back unchanged in the same locale: ``1234,56`` (pl),
    ``1234.56`` (en), ``1234`` (JPY).
    """
    decimal_symbol = get_decimal_symbol(_normalize_number_locale(locale))
    text = format(money.to_decimal(), "f")
    return text.replace(".", decimal_symbol)


def format_quantity_input(value: Decimal, locale: str) -> str:
    """A quantity (units worked) with the locale's decimal sign, no grouping and no trailing
    zeros: ``7,5`` (pl), ``7.5`` (en). ``parse_amount`` reads it back unchanged."""
    decimal_symbol = get_decimal_symbol(_normalize_number_locale(locale))
    text = format(value.normalize(), "f")
    return text.replace(".", decimal_symbol)


def format_rate_tenths(tenths: int, locale: str) -> str:
    """A rate given in tenths of a percent (already rounded once), with one decimal:
    ``-43.3%`` (en), ``-43,3%`` (pl). Exact: the value is a Decimal, never a float."""
    value = Decimal(tenths).scaleb(-3)
    return format_percent(value, format="#,##0.0%", locale=_normalize_number_locale(locale))


def format_rate_short(rate_bp: int, locale: str) -> str:
    """A basis-point rate without trailing zero decimals ("23%", "12,5%")."""
    value = Decimal(rate_bp).scaleb(-4)
    return format_percent(value, format="#,##0.##%", locale=_normalize_number_locale(locale))


def format_rate_basis_points(rate_bp: int, locale: str) -> str:
    """Format an integer basis-point rate with two decimal places in the given locale."""
    value = Decimal(rate_bp).scaleb(-4)
    return format_percent(value, format="#,##0.00%", locale=_normalize_number_locale(locale))
