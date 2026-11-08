"""Amounts entered in the base currency ("settlement", F6.2a). Pure: no I/O and no clock.

A broker that shows only base-currency amounts for an instrument quoted in another currency
gives the user a base unit price and a base fee. The row keeps those as entered and also stores
the quote-currency values derived from them with the FX rate, so the F6.3 transaction price and
transaction FX rate stay right (a USD price and the real rate, never a base price at rate 1).

All arithmetic is exact ``Decimal`` in an 80-digit local context, never ``float``.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, localcontext

from assetboard.domain.money import Money, minor_unit_for

# The DecimalType scale: derived unit prices are quantised half-up to 18 places.
PRICE_SCALE = 18
_PRECISION = 80
_PRICE_QUANTUM = Decimal(1).scaleb(-PRICE_SCALE)


def _quantize_price(value: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = _PRECISION
        return value.quantize(_PRICE_QUANTUM, rounding=ROUND_HALF_UP)


def _positive_rate(fx_to_base: Decimal) -> None:
    if type(fx_to_base) is not Decimal or not fx_to_base.is_finite() or fx_to_base <= 0:
        raise ValueError("fx_to_base must be a positive finite Decimal")


def derive_quote_values(
    settlement_unit_price: Decimal,
    settlement_fee_minor: int,
    fx_to_base: Decimal,
    *,
    base_currency: str,
    quote_currency: str,
) -> tuple[Decimal, int]:
    """The quote-currency unit price and fee (minor units) of a base-currency entry.

    ``unit_price = settlement_unit_price / fx_to_base`` quantised half-up to 18 places;
    ``fee_minor = round_half_up(settlement_fee / fx_to_base)`` in quote minor units.
    """
    _positive_rate(fx_to_base)
    with localcontext() as context:
        context.prec = _PRECISION
        unit_price = _quantize_price(settlement_unit_price / fx_to_base)
        fee = Decimal(settlement_fee_minor).scaleb(-minor_unit_for(base_currency)) / fx_to_base
        fee_minor = Money.from_decimal(fee, quote_currency).amount_minor
    return unit_price, fee_minor


def base_values_from_quote(
    unit_price: Decimal,
    fee_minor: int,
    fx_to_base: Decimal,
    *,
    base_currency: str,
    quote_currency: str,
) -> tuple[Decimal, int]:
    """Base-currency unit price and fee for switching a quote-mode row to base mode.

    ``base price = unit_price × fx_to_base`` (18 places, half-up) and
    ``base fee = round_half_up(fee × fx_to_base)`` in base minor units. Only a form pre-fill.
    """
    _positive_rate(fx_to_base)
    with localcontext() as context:
        context.prec = _PRECISION
        price = _quantize_price(unit_price * fx_to_base)
        fee = Decimal(fee_minor).scaleb(-minor_unit_for(quote_currency)) * fx_to_base
        base_fee_minor = Money.from_decimal(fee, base_currency).amount_minor
    return price, base_fee_minor


def needs_fx_check(*, quote_currency: str, base_currency: str, fx_to_base: Decimal | None) -> bool:
    """A non-base instrument's row stored at rate 1: likely base amounts entered as quote ones."""
    return quote_currency != base_currency and fx_to_base is not None and fx_to_base == 1
