from __future__ import annotations

from dataclasses import dataclass

from assetboard.domain.money import Money

MAX_RATE_BP = 2_147_483_647


@dataclass(frozen=True, slots=True)
class PercentOrFixed:
    """A reusable percentage (basis points) or fixed amount."""

    mode: str
    rate_bp: int | None = None
    fixed: Money | None = None

    def __post_init__(self) -> None:
        if self.mode == "percent":
            if type(self.rate_bp) is not int or not 0 <= self.rate_bp <= MAX_RATE_BP:
                raise ValueError("invalid_rate")
            if self.fixed is not None:
                raise ValueError("invalid_value")
        elif self.mode == "fixed":
            if self.fixed is None or self.rate_bp is not None or self.fixed.amount_minor < 0:
                raise ValueError("invalid_value")
        else:
            raise ValueError("invalid_mode")

    @classmethod
    def percent(cls, rate_bp: int) -> PercentOrFixed:
        return cls("percent", rate_bp=rate_bp)

    @classmethod
    def fixed_amount(cls, amount: Money) -> PercentOrFixed:
        return cls("fixed", fixed=amount)
