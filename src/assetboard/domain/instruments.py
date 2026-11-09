"""Investment instrument rules (F6.1): types, defaults, names, allocations and the seed set.

Pure: no I/O and no clock. Instruments are the things bought, sold and valued; an asset class
groups them for the portfolio and allocation views. Allocation weights are integer basis points
and must total exactly 10 000 (100%) per instrument.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from assetboard.domain.categories import MAX_NAME_KEY_LENGTH, clean_color, name_key
from assetboard.domain.money import minor_unit_for
from assetboard.domain.text import collapse_whitespace, has_rejected_characters

MAX_ASSET_CLASS_NAME_LENGTH = 80
MAX_INSTRUMENT_NAME_LENGTH = 200
MAX_SYMBOL_LENGTH = 32
MAX_UNIT_LABEL_LENGTH = 32
ALLOCATION_TOTAL_BP = 10_000


def _(message: str) -> str:
    """Marks a message for extraction; it is translated where it is used."""
    return message


class InstrumentType(StrEnum):
    EQUITY = "equity"
    ETF = "etf"
    BOND_MARKET = "bond_market"
    BOND_MODEL = "bond_model"
    METAL = "metal"
    CRYPTO = "crypto"
    CURRENCY = "currency"
    REAL_ESTATE = "real_estate"
    OTHER = "other"


class ValuationSource(StrEnum):
    PROVIDER = "provider"
    MANUAL = "manual"
    BOND_MODEL = "bond_model"


class RiskTier(StrEnum):
    SAFE = "safe"
    GROWTH = "growth"
    SPECULATIVE = "speculative"


class InvalidAssetClassName(ValueError):
    code = "invalid_name"


class InvalidInstrumentName(ValueError):
    code = "invalid_name"


class InvalidSymbol(ValueError):
    code = "invalid_symbol"


class InvalidUnitLabel(ValueError):
    code = "invalid_unit_label"


class InvalidQuoteCurrency(ValueError):
    code = "invalid_currency"


class InvalidAllocation(ValueError):
    """An allocation set is unusable; ``code`` is a short stable token for the UI."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _clean_name(value: str, *, maximum: int, error: type[ValueError]) -> str:
    if has_rejected_characters(value.strip()):
        raise error
    cleaned = collapse_whitespace(value)
    if not cleaned or len(cleaned) > maximum:
        raise error
    if len(name_key(cleaned)) > MAX_NAME_KEY_LENGTH:
        raise error
    return cleaned


def clean_asset_class_name(value: str) -> str:
    """Trim, collapse whitespace, refuse control/invisible characters and bad lengths."""
    return _clean_name(value, maximum=MAX_ASSET_CLASS_NAME_LENGTH, error=InvalidAssetClassName)


def clean_instrument_name(value: str) -> str:
    """Trim, collapse whitespace, refuse control/invisible characters and bad lengths."""
    return _clean_name(value, maximum=MAX_INSTRUMENT_NAME_LENGTH, error=InvalidInstrumentName)


def clean_symbol(value: str) -> str | None:
    """An optional provider/ticker symbol; blank means none."""
    if has_rejected_characters(value.strip()):
        raise InvalidSymbol
    cleaned = collapse_whitespace(value)
    if not cleaned:
        return None
    if len(cleaned) > MAX_SYMBOL_LENGTH:
        raise InvalidSymbol
    return cleaned


def clean_quote_currency(value: str) -> str:
    """An ISO 4217 code, uppercased; the instrument's unit price is quoted in it."""
    currency = value.strip().upper()
    try:
        minor_unit_for(currency)
    except ValueError:
        raise InvalidQuoteCurrency from None
    return currency


def clean_unit_label(
    value: str,
    *,
    instrument_type: InstrumentType,
    quote_currency: str,
) -> str:
    """The label of one unit. A currency instrument's unit is its own currency code."""
    if instrument_type is InstrumentType.CURRENCY:
        return quote_currency
    if has_rejected_characters(value.strip()):
        raise InvalidUnitLabel
    cleaned = collapse_whitespace(value)
    if not cleaned or len(cleaned) > MAX_UNIT_LABEL_LENGTH:
        raise InvalidUnitLabel
    return cleaned


def clean_colour(value: str) -> str:
    return clean_color(value)


@dataclass(frozen=True, slots=True)
class TypeDefaults:
    unit_label: str  # message id; translated when a blank unit label is filled in
    valuation_source: ValuationSource
    quantity_is_one: bool = False
    unit_from_quote_currency: bool = False


TYPE_DEFAULTS: dict[InstrumentType, TypeDefaults] = {
    InstrumentType.EQUITY: TypeDefaults(_("share"), ValuationSource.MANUAL),
    InstrumentType.ETF: TypeDefaults(_("share"), ValuationSource.MANUAL),
    InstrumentType.BOND_MARKET: TypeDefaults(_("bond"), ValuationSource.MANUAL),
    InstrumentType.BOND_MODEL: TypeDefaults(_("bond"), ValuationSource.BOND_MODEL),
    InstrumentType.METAL: TypeDefaults(_("oz"), ValuationSource.MANUAL),
    InstrumentType.CRYPTO: TypeDefaults(_("coin"), ValuationSource.MANUAL),
    InstrumentType.CURRENCY: TypeDefaults(
        "", ValuationSource.MANUAL, unit_from_quote_currency=True
    ),
    InstrumentType.REAL_ESTATE: TypeDefaults(
        _("property"), ValuationSource.MANUAL, quantity_is_one=True
    ),
    InstrumentType.OTHER: TypeDefaults(_("unit"), ValuationSource.MANUAL),
}

TYPE_LABELS: dict[InstrumentType, str] = {
    InstrumentType.EQUITY: _("Stock"),
    InstrumentType.ETF: _("ETF"),
    InstrumentType.BOND_MARKET: _("Bond (market price)"),
    InstrumentType.BOND_MODEL: _("Bond (model value)"),
    InstrumentType.METAL: _("Metal"),
    InstrumentType.CRYPTO: _("Crypto"),
    InstrumentType.CURRENCY: _("Currency"),
    InstrumentType.REAL_ESTATE: _("Real estate"),
    InstrumentType.OTHER: _("Other"),
}

VALUATION_SOURCE_LABELS: dict[ValuationSource, str] = {
    ValuationSource.PROVIDER: _("Price provider"),
    ValuationSource.MANUAL: _("Manual price"),
    ValuationSource.BOND_MODEL: _("Model value (net of tax)"),
}

RISK_TIER_LABELS: dict[RiskTier, str] = {
    RiskTier.SAFE: _("Safe"),
    RiskTier.GROWTH: _("Growth"),
    RiskTier.SPECULATIVE: _("Speculative"),
}


def parse_instrument_type(value: str) -> InstrumentType:
    try:
        return InstrumentType(value)
    except ValueError:
        raise ValueError("invalid_type") from None


def parse_valuation_source(value: str) -> ValuationSource:
    try:
        return ValuationSource(value)
    except ValueError:
        raise ValueError("invalid_valuation_source") from None


def percent_to_bp(value: Decimal) -> int:
    """A percentage with at most two decimals as integer basis points."""
    if not value.is_finite() or value < 0 or value > 100:
        raise InvalidAllocation("invalid_weight")
    basis_points = value.scaleb(2)
    if basis_points != basis_points.to_integral_value():
        raise InvalidAllocation("invalid_weight")
    return int(basis_points)


def validate_allocation(rows: Sequence[tuple[UUID, int]]) -> None:
    """Every instrument has at least one class and its weights total exactly 100%."""
    if not rows:
        raise InvalidAllocation("allocation_required")
    seen: set[UUID] = set()
    total = 0
    for class_id, weight_bp in rows:
        if class_id in seen:
            raise InvalidAllocation("duplicate_class")
        seen.add(class_id)
        if type(weight_bp) is not int or not 0 < weight_bp <= ALLOCATION_TOTAL_BP:
            raise InvalidAllocation("invalid_weight")
        total += weight_bp
    if total != ALLOCATION_TOTAL_BP:
        raise InvalidAllocation("allocation_total")


@dataclass(frozen=True, slots=True)
class SeedAssetClass:
    name: str  # message id; translated when the seed runs
    investable: bool
    color: str
    portfolio_default: bool = False  # the portfolio chart's default scope (F6.6)


# The set shipped until the owner decision of 2026-11-09 (F5.7: property lives in net worth).
# Kept frozen so a workspace seeded with it is still recognised as an untouched suggested set
# (the F6.9 restore check); workspaces seeded with it keep these classes as user data.
ASSET_CLASS_SEED_V1: tuple[SeedAssetClass, ...] = (
    SeedAssetClass(_("Stocks"), True, "#2563eb", portfolio_default=True),
    SeedAssetClass(_("Bonds"), True, "#16a34a"),
    SeedAssetClass(_("Metals"), True, "#eab308"),
    SeedAssetClass(_("Crypto"), True, "#f97316"),
    SeedAssetClass(_("Currencies"), True, "#0ea5e9"),
    SeedAssetClass(_("Real estate"), False, "#8b5cf6"),
    SeedAssetClass(_("Primary residence"), False, "#64748b"),
    SeedAssetClass(_("Other"), False, "#475569"),
)
# F5.7: no property classes, and Other is investable (``investable`` only decides what FIRE
# counts as the starting portfolio; the portfolio page counts every class).
ASSET_CLASS_SEED: tuple[SeedAssetClass, ...] = (
    SeedAssetClass(_("Stocks"), True, "#2563eb", portfolio_default=True),
    SeedAssetClass(_("Bonds"), True, "#16a34a"),
    SeedAssetClass(_("Metals"), True, "#eab308"),
    SeedAssetClass(_("Crypto"), True, "#f97316"),
    SeedAssetClass(_("Currencies"), True, "#0ea5e9"),
    SeedAssetClass(_("Other"), True, "#475569"),
)
# Every shipped set, oldest first, for comparisons with an untouched seed.
ASSET_CLASS_SEEDS: tuple[tuple[SeedAssetClass, ...], ...] = (ASSET_CLASS_SEED_V1, ASSET_CLASS_SEED)
