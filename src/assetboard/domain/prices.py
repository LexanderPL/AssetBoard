"""Price and FX rules (F6.3): source priority, staleness and provider shapes.

Prices are Decimal values in the instrument's quote currency. FX rates are stored as
**base-currency units per one quote-currency unit**; the workspace base currency itself has rate
1. Transaction-sourced points are derived at read time, never stored.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol
from uuid import UUID

DEFAULT_STALE_AFTER_DAYS = 5

# On one date: a manual point beats a provider point, which beats a transaction fallback.
SOURCE_RANK = {"manual": 0, "provider": 1, "transaction": 2}


def source_rank(source: str) -> int:
    """The priority of a source on the same date (lower wins)."""
    if source == "manual":
        return SOURCE_RANK["manual"]
    if source == "transaction":
        return SOURCE_RANK["transaction"]
    # Any provider key (`provider`, `nbp`, `ecb`, future) shares the provider rank.
    return SOURCE_RANK["provider"]


def is_stale(
    as_of: date | None, on: date, stale_after_days: int = DEFAULT_STALE_AFTER_DAYS
) -> bool:
    """Whether an observation is older than the threshold, measured against ``on``."""
    if as_of is None:
        return False
    return (on - as_of).days > stale_after_days


@dataclass(frozen=True, slots=True)
class ProviderInstrument:
    """The provider-facing fields of an instrument (no ORM object)."""

    id: UUID
    type: str
    quote_currency: str
    provider_key: str | None
    provider_symbol: str | None


@dataclass(frozen=True, slots=True)
class ProviderQuote:
    symbol: str
    date: date
    price: Decimal


@dataclass(frozen=True, slots=True)
class ProviderRateQuote:
    """One FX observation in the provider's natural base currency."""

    currency: str
    date: date
    rate: Decimal


@dataclass(frozen=True, slots=True)
class ProviderRateLimit:
    requests: int
    per_seconds: int


class PriceProvider(Protocol):
    key: str
    rate_limit: ProviderRateLimit

    def supports(self, instrument: ProviderInstrument) -> bool: ...

    def fetch_latest(
        self,
        symbols: Sequence[str],
        quote_currency: str = "",
        *,
        wait: bool = True,
    ) -> Mapping[str, ProviderQuote]: ...

    def fetch_history(
        self,
        symbol: str,
        start: date,
        end: date,
        quote_currency: str = "",
        *,
        wait: bool = True,
    ) -> Sequence[ProviderQuote]: ...


class FxProvider(Protocol):
    key: str
    natural_base: str
    rate_limit: ProviderRateLimit

    def fetch_latest_fx(
        self, currencies: Sequence[str], *, wait: bool = True
    ) -> Mapping[str, ProviderRateQuote]: ...

    def fetch_history_fx(
        self, currency: str, start: date, end: date, *, wait: bool = True
    ) -> Sequence[ProviderRateQuote]: ...
