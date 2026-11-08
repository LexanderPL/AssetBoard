"""Provider catalogue and pure conversion rules (F6.4).

The catalogue names the built-in providers, what they can quote and the attribution they need.
The functions below convert provider observations into the project's conventions without I/O:
FX rates are base-currency units per one quote-currency unit, NBP gold is published in PLN
per gram and GoldAPI.io metals per troy ounce, while the default metal unit is a troy ounce.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Literal

PROVIDER_DECIMAL_PRECISION = 80
TROY_OUNCE_GRAMS = Decimal("31.1034768")

NBP_KEY = "nbp"
ECB_KEY = "ecb"
COINGECKO_KEY = "coingecko"
EODHD_KEY = "eodhd"
GOLDAPI_KEY = "goldapi"

_COINGECKO_SYMBOL = re.compile(r"[a-z0-9-]{1,64}")
_EODHD_SYMBOL = re.compile(r"[A-Za-z0-9._-]{1,64}")

GOLDAPI_SYMBOLS = frozenset({"XAU", "XAG", "XPT", "XPD"})
# The quote currencies GoldAPI.io documents (72 codes on its supported-currencies list). Its list
# also names BTC and XAG; neither is a currency an instrument can be quoted in here, so they are
# left out on purpose.
GOLDAPI_CURRENCIES = frozenset(
    {
        "AED",
        "ARS",
        "AUD",
        "BDT",
        "BGN",
        "BHD",
        "BOB",
        "BRL",
        "CAD",
        "CHF",
        "CLP",
        "CNY",
        "COP",
        "CZK",
        "DKK",
        "DOP",
        "DZD",
        "EGP",
        "EUR",
        "FJD",
        "GBP",
        "GHS",
        "GTQ",
        "HKD",
        "IDR",
        "ILS",
        "INR",
        "IQD",
        "IRR",
        "ISK",
        "JOD",
        "JPY",
        "KES",
        "KHR",
        "KRW",
        "KWD",
        "LAK",
        "LBP",
        "LKR",
        "MAD",
        "MMK",
        "MXN",
        "MYR",
        "NGN",
        "NOK",
        "NPR",
        "NZD",
        "OMR",
        "PEN",
        "PHP",
        "PKR",
        "PLN",
        "PYG",
        "QAR",
        "RON",
        "RUB",
        "SAR",
        "SEK",
        "SGD",
        "SYP",
        "THB",
        "TRY",
        "TWD",
        "UAH",
        "USD",
        "UYU",
        "UZS",
        "VND",
        "YER",
        "ZAR",
    }
)

_GRAMS = frozenset({"g", "gram", "grams", "gramy", "gramow", "gramów"})
_KILOGRAMS = frozenset({"kg", "kilogram", "kilograms", "kilogramy", "kilogramów"})
_TROY_OUNCES = frozenset(
    {
        "oz",
        "oz t",
        "troy ounce",
        "troy ounces",
        "uncja",
        "uncje",
        "uncji",
        "uncja trojanska",
        "uncja trojańska",
    }
)


def _(message: str) -> str:
    """Marks a message for extraction; it is translated where it is rendered."""
    return message


@dataclass(frozen=True, slots=True)
class ProviderDefinition:
    key: str
    label: str
    description: str
    kind: Literal["price", "fx", "both"]
    natural_base: str
    supported_types: frozenset[str]
    quote_currencies: frozenset[str] | None
    requires_key: bool
    symbol_required: bool
    symbol_help: str
    attribution: str | None
    docs_url: str
    key_label: str = ""
    default_daily_budget: int | None = None


NBP = ProviderDefinition(
    key=NBP_KEY,
    label="NBP",
    description=_(
        "Official National Bank of Poland FX rates against PLN and the official gold price."
    ),
    kind="both",
    natural_base="PLN",
    supported_types=frozenset({"metal"}),
    quote_currencies=frozenset({"PLN"}),
    requires_key=False,
    symbol_required=False,
    symbol_help=_("NBP gold uses XAU; the price is quoted per gram and converted to the unit."),
    attribution="Narodowy Bank Polski",
    docs_url="https://api.nbp.pl/",
)

ECB = ProviderDefinition(
    key=ECB_KEY,
    label="ECB",
    description=_("European Central Bank euro reference rates against EUR."),
    kind="fx",
    natural_base="EUR",
    supported_types=frozenset(),
    quote_currencies=frozenset(),
    requires_key=False,
    symbol_required=False,
    symbol_help="",
    attribution="European Central Bank",
    docs_url="https://www.ecb.europa.eu/stats/eurofxref/",
)

COINGECKO = ProviderDefinition(
    key=COINGECKO_KEY,
    label="CoinGecko",
    description=_("Crypto prices quoted directly in USD, EUR or PLN."),
    kind="price",
    natural_base="USD",
    supported_types=frozenset({"crypto"}),
    quote_currencies=frozenset({"USD", "EUR", "PLN"}),
    requires_key=False,
    symbol_required=True,
    symbol_help=_("CoinGecko coin id, for example bitcoin."),
    attribution="Powered by CoinGecko",
    docs_url="https://docs.coingecko.com/",
    key_label=_("Demo API key (optional)"),
)

EODHD = ProviderDefinition(
    key=EODHD_KEY,
    label="EODHD",
    description=_("Optional end-of-day equities and ETFs through EODHD."),
    kind="price",
    natural_base="",
    supported_types=frozenset({"equity", "etf"}),
    quote_currencies=None,
    requires_key=True,
    symbol_required=True,
    symbol_help=_("EODHD ticker with exchange suffix, for example VWCE.DE."),
    attribution="EODHD",
    docs_url="https://eodhd.com/pricing",
    key_label=_("API key"),
    default_daily_budget=20,
)

GOLDAPI = ProviderDefinition(
    key=GOLDAPI_KEY,
    label="GoldAPI.io",
    description=_(
        "Optional spot prices for gold, silver, platinum and palladium in many currencies."
    ),
    kind="price",
    natural_base="",
    supported_types=frozenset({"metal"}),
    quote_currencies=GOLDAPI_CURRENCIES,
    requires_key=True,
    symbol_required=True,
    symbol_help=_("GoldAPI metal symbol: XAU, XAG, XPT or XPD; the price is per troy ounce."),
    attribution=None,
    docs_url="https://www.goldapi.io/api-documentation",
    key_label=_("API key"),
    default_daily_budget=4,
)

PROVIDERS: dict[str, ProviderDefinition] = {
    NBP.key: NBP,
    ECB.key: ECB,
    COINGECKO.key: COINGECKO,
    EODHD.key: EODHD,
    GOLDAPI.key: GOLDAPI,
}


class UnsupportedUnit(ValueError):
    """The instrument unit cannot be derived from the provider's quoted unit."""


class InvalidProviderRate(ValueError):
    """A provider value is not a usable positive finite decimal."""


class InvalidProviderSymbol(ValueError):
    """A provider symbol does not fit the provider's identifier format."""


def provider_definition(key: str) -> ProviderDefinition | None:
    return PROVIDERS.get(key)


def provider_supports_currency(definition: ProviderDefinition, currency: str) -> bool:
    """Whether the provider publishes instrument prices in ``currency``."""
    if definition.kind == "fx":
        return False
    if definition.quote_currencies is None:
        return True
    return currency.upper() in definition.quote_currencies


def nbp_gold_symbol(value: str | None) -> str | None:
    """The canonical gold symbol, or ``None`` for anything else."""
    cleaned = (value or "").strip().upper()
    if cleaned in {"", "XAU", "GOLD"}:
        return "XAU"
    return None


def clean_provider_symbol(
    definition: ProviderDefinition, provider_symbol: str | None
) -> str | None:
    """Validate and canonicalise a provider symbol; blank means none."""
    cleaned = (provider_symbol or "").strip()
    if definition.key == NBP_KEY:
        return nbp_gold_symbol(cleaned)
    if not cleaned:
        return None
    if definition.key == COINGECKO_KEY:
        normalized = cleaned.lower()
        if _COINGECKO_SYMBOL.fullmatch(normalized) is None:
            raise InvalidProviderSymbol
        return normalized
    if definition.key == EODHD_KEY:
        normalized = cleaned.upper()
        if _EODHD_SYMBOL.fullmatch(normalized) is None:
            raise InvalidProviderSymbol
        return normalized
    if definition.key == GOLDAPI_KEY:
        normalized = cleaned.upper()
        if normalized not in GOLDAPI_SYMBOLS:
            raise InvalidProviderSymbol
        return normalized
    return cleaned


def normalize_instrument_symbol(
    definition: ProviderDefinition, provider_symbol: str | None
) -> str | None:
    """The canonical symbol a price provider expects for an instrument."""
    try:
        return clean_provider_symbol(definition, provider_symbol)
    except InvalidProviderSymbol:
        return None


def provider_supports_instrument(
    definition: ProviderDefinition,
    *,
    type: str,
    quote_currency: str,
    provider_symbol: str | None,
) -> bool:
    """Whether the definition can price this instrument shape (without I/O)."""
    if definition.kind == "fx" or type not in definition.supported_types:
        return False
    if definition.quote_currencies is not None and quote_currency.upper() not in (
        definition.quote_currencies
    ):
        return False
    try:
        symbol = clean_provider_symbol(definition, provider_symbol)
    except InvalidProviderSymbol:
        return False
    if definition.key == NBP_KEY:
        return symbol == "XAU"
    return not (definition.symbol_required and symbol is None)


def nbp_gold_to_unit(price_per_gram: Decimal, unit_label: str) -> Decimal:
    """Convert NBP's PLN-per-gram gold price to the instrument's unit."""
    if not price_per_gram.is_finite() or price_per_gram < 0:
        raise InvalidProviderRate
    normalized = unit_label.strip().casefold()
    if normalized in _GRAMS:
        return price_per_gram
    if normalized in _TROY_OUNCES:
        with localcontext() as context:
            context.prec = max(context.prec, PROVIDER_DECIMAL_PRECISION)
            return price_per_gram * TROY_OUNCE_GRAMS
    raise UnsupportedUnit


def goldapi_ounce_to_unit(price_per_ounce: Decimal, unit_label: str) -> Decimal:
    """Convert GoldAPI's price per troy ounce to the instrument's unit (oz, g or kg).

    One troy ounce is exactly 31.1034768 g. The gram and kilogram prices are a single division
    in an 80-digit context, so ``g × 1000`` and ``kg`` agree to that precision.
    """
    if not price_per_ounce.is_finite() or price_per_ounce < 0:
        raise InvalidProviderRate
    normalized = unit_label.strip().casefold()
    if normalized in _TROY_OUNCES:
        return price_per_ounce
    if normalized in _GRAMS:
        grams_per_unit = Decimal(1)
    elif normalized in _KILOGRAMS:
        grams_per_unit = Decimal(1000)
    else:
        raise UnsupportedUnit
    with localcontext() as context:
        context.prec = max(context.prec, PROVIDER_DECIMAL_PRECISION)
        return price_per_ounce * grams_per_unit / TROY_OUNCE_GRAMS


def provider_price_to_unit(provider_key: str, price: Decimal, unit_label: str) -> Decimal:
    """A provider price in the instrument's unit: NBP quotes per gram, GoldAPI per troy ounce."""
    if provider_key == NBP_KEY:
        return nbp_gold_to_unit(price, unit_label)
    if provider_key == GOLDAPI_KEY:
        return goldapi_ounce_to_unit(price, unit_label)
    return price


def ecb_quote_to_rate(quote: Decimal) -> Decimal:
    """ECB publishes currency units per EUR; return EUR units per one currency unit."""
    if not quote.is_finite() or quote <= 0:
        raise InvalidProviderRate
    with localcontext() as context:
        context.prec = max(context.prec, PROVIDER_DECIMAL_PRECISION)
        return Decimal(1) / quote


def convert_to_workspace_base(native_per_currency: Decimal, native_per_base: Decimal) -> Decimal:
    """Convert a provider's natural-base rate into the workspace base currency.

    ``native_per_currency`` and ``native_per_base`` are both expressed in the provider's
    natural base. The result is workspace-base units per one currency unit.
    """
    if (
        not native_per_currency.is_finite()
        or native_per_currency <= 0
        or not native_per_base.is_finite()
        or native_per_base <= 0
    ):
        raise InvalidProviderRate
    with localcontext() as context:
        context.prec = max(context.prec, PROVIDER_DECIMAL_PRECISION)
        return native_per_currency / native_per_base


def normalize_provider_decimal(value: Decimal, *, positive: bool) -> Decimal:
    """Validate and fit a provider observation to ``DecimalType`` (38 digits, 18 decimals)."""
    if not value.is_finite():
        raise InvalidProviderRate
    if value < 0 or (positive and value == 0):
        raise InvalidProviderRate
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int):
        raise InvalidProviderRate
    scale = max(-exponent, 0)
    integer_digits = 0 if value.is_zero() else max(value.adjusted() + 1, 0)
    if scale > 18 or integer_digits > 20 or integer_digits + scale > 38:
        with localcontext() as context:
            context.prec = max(context.prec, PROVIDER_DECIMAL_PRECISION)
            value = value.quantize(Decimal("1e-18"), rounding=ROUND_HALF_UP)
        exponent = value.as_tuple().exponent
        if not isinstance(exponent, int):
            raise InvalidProviderRate
        scale = max(-exponent, 0)
        integer_digits = 0 if value.is_zero() else max(value.adjusted() + 1, 0)
        if scale > 18 or integer_digits > 20 or integer_digits + scale > 38:
            raise InvalidProviderRate
    return value
