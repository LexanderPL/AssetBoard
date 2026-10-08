from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

_CURRENCY_PATTERN = re.compile(r"[A-Z]{3}\Z")


@dataclass(frozen=True, slots=True)
class Money:
    amount_minor: int
    currency: str

    def __post_init__(self) -> None:
        if type(self.amount_minor) is not int:
            raise TypeError("amount_minor must be an integer")
        currency = self.currency.upper()
        if not currency.isascii() or _CURRENCY_PATTERN.fullmatch(currency) is None:
            raise ValueError("currency must be a three-letter ISO currency code")
        object.__setattr__(self, "currency", currency)

    @classmethod
    def from_decimal(
        cls,
        amount: Decimal,
        currency: str,
        minor_unit: int = 2,
    ) -> Money:
        if type(amount) is not Decimal:
            raise TypeError("amount must be Decimal")
        if not amount.is_finite():
            raise ValueError("amount must be finite")
        if type(minor_unit) is not int or not 0 <= minor_unit <= 18:
            raise ValueError("minor_unit must be an integer between 0 and 18")

        quantum = Decimal(1).scaleb(-minor_unit)
        rounded = amount.quantize(quantum, rounding=ROUND_HALF_UP)
        amount_minor = int(rounded.scaleb(minor_unit))
        return cls(amount_minor, currency)

    def to_decimal(self, minor_unit: int = 2) -> Decimal:
        if type(minor_unit) is not int or not 0 <= minor_unit <= 18:
            raise ValueError("minor_unit must be an integer between 0 and 18")
        return Decimal(self.amount_minor).scaleb(-minor_unit)

    def __composite_values__(self) -> tuple[int, str]:
        return self.amount_minor, self.currency

    def __add__(self, other: object) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return Money(self.amount_minor + other.amount_minor, self.currency)

    def __sub__(self, other: object) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return Money(self.amount_minor - other.amount_minor, self.currency)

    def _require_same_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise ValueError("cannot combine Money values with different currencies")
