"""Shared HTML form parsing helpers.

`PercentOrFixed` is used by tax rules, loan cost components and loan rates. Parsing stays in the
web layer because it is locale-aware; the domain object validates the stored shape.
"""

from __future__ import annotations

from assetboard.domain.money import Money
from assetboard.domain.percent_or_fixed import PercentOrFixed
from assetboard.domain.transactions import MAX_AMOUNT_MINOR, amount_minor
from assetboard.i18n.numbers import parse_amount


class InvalidPercentOrFixed(ValueError):
    """The form did not contain a valid percent or fixed amount; ``code`` names the problem."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def percent_to_bp(text: str, locale: str) -> int:
    """A locale-aware percentage with at most two decimals as integer basis points."""
    percent = parse_amount(text, locale)
    basis_points = percent.scaleb(2)
    if percent < 0 or basis_points != basis_points.to_integral_value():
        raise InvalidPercentOrFixed("invalid_rate")
    return int(basis_points)


def fixed_to_minor(text: str, locale: str, currency: str) -> int:
    amount = parse_amount(text, locale)
    if amount < 0:
        raise InvalidPercentOrFixed("invalid_value")
    minor = 0 if amount == 0 else amount_minor(amount, currency)
    if minor > MAX_AMOUNT_MINOR:
        raise InvalidPercentOrFixed("invalid_value")
    return minor


def parse_percent_or_fixed(
    values: dict[str, str],
    locale: str,
    currency: str,
    *,
    mode_key: str = "mode",
    percent_key: str = "rate_percent",
    fixed_key: str = "fixed_amount",
) -> PercentOrFixed:
    """The shared ``PercentOrFixed`` widget parser (percent or fixed amount row)."""
    mode = values.get(mode_key)
    if mode == "percent":
        try:
            return PercentOrFixed.percent(percent_to_bp(values.get(percent_key, ""), locale))
        except (ValueError, InvalidPercentOrFixed):
            raise InvalidPercentOrFixed("invalid_rate") from None
    if mode == "fixed":
        try:
            return PercentOrFixed.fixed_amount(
                Money(fixed_to_minor(values.get(fixed_key, ""), locale, currency), currency)
            )
        except (ValueError, InvalidPercentOrFixed):
            raise InvalidPercentOrFixed("invalid_value") from None
    raise InvalidPercentOrFixed("invalid_value")
