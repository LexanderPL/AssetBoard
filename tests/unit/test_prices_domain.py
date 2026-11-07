"""Price and FX rules and provider shapes (F6.3)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

from assetboard.domain.prices import (
    DEFAULT_STALE_AFTER_DAYS,
    SOURCE_RANK,
    ProviderInstrument,
    ProviderQuote,
    is_stale,
    source_rank,
)
from assetboard.services.prices import FakeProvider, ManualProvider


def _instrument(
    *,
    type_value: str = "etf",
    quote_currency: str = "EUR",
    provider_key: str | None = None,
    provider_symbol: str | None = None,
) -> ProviderInstrument:
    return ProviderInstrument(
        id=uuid4(),
        type=type_value,
        quote_currency=quote_currency,
        provider_key=provider_key,
        provider_symbol=provider_symbol,
    )


def test_default_stale_threshold_is_five_days() -> None:
    assert DEFAULT_STALE_AFTER_DAYS == 5


def test_staleness_is_measured_against_the_requested_date_inclusively() -> None:
    observed = date(2026, 3, 1)
    assert not is_stale(observed, date(2026, 3, 6))
    assert is_stale(observed, date(2026, 3, 7))
    assert not is_stale(observed, date(2026, 3, 1))
    # A requested date before the observation is not stale, it simply has no value there.
    assert not is_stale(observed, date(2026, 2, 28))


def test_a_missing_observation_is_not_stale() -> None:
    assert not is_stale(None, date(2026, 3, 1))


def test_source_priority_is_manual_then_provider_then_transaction() -> None:
    assert SOURCE_RANK["manual"] < SOURCE_RANK["provider"]
    assert SOURCE_RANK["provider"] < SOURCE_RANK["transaction"]
    assert source_rank("manual") == SOURCE_RANK["manual"]
    assert source_rank("provider") == SOURCE_RANK["provider"]
    assert source_rank("nbp") == SOURCE_RANK["provider"]
    assert source_rank("ecb") == SOURCE_RANK["provider"]
    assert source_rank("transaction") == SOURCE_RANK["transaction"]


def test_manual_provider_never_fetches_but_supports_everything() -> None:
    provider = ManualProvider()
    assert provider.key == "manual"
    assert provider.rate_limit.requests == 0
    assert provider.supports(_instrument())
    assert provider.fetch_latest(["ONE"]) == {}
    assert provider.fetch_history("ONE", date(2026, 1, 1), date(2026, 2, 1)) == ()


def test_fake_provider_supports_types_and_filters_quotes() -> None:
    first = ProviderQuote("ONE", date(2026, 3, 1), Decimal("10"))
    second = ProviderQuote("ONE", date(2026, 3, 5), Decimal("11"))
    provider = FakeProvider(
        key="test",
        latest={"ONE": second},
        history={"ONE": (first, second)},
        supported_types={"etf"},
    )
    assert provider.key == "test"
    assert provider.supports(_instrument(type_value="etf"))
    assert not provider.supports(_instrument(type_value="crypto"))
    assert provider.fetch_latest(["ONE", "TWO"]) == {"ONE": second}
    assert provider.fetch_history("ONE", date(2026, 3, 2), date(2026, 3, 6)) == (second,)
    assert provider.fetch_history("MISSING", date(2026, 3, 1), date(2026, 3, 2)) == ()


def test_fake_provider_without_type_filter_supports_every_instrument() -> None:
    provider = FakeProvider()
    assert provider.supports(_instrument(type_value="crypto"))
    assert provider.supports(_instrument(type_value="real_estate"))
