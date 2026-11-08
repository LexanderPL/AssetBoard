"""CoinGecko adapter against recorded fixtures (F6.4)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.domain.prices import ProviderInstrument
from assetboard.domain.providers import COINGECKO_KEY
from assetboard.services.providers.coingecko import (
    MARKET_CHART_URL,
    SIMPLE_PRICE_URL,
    CoinGeckoProvider,
)
from assetboard.services.providers.registry import build_provider_runtime
from assetboard.services.providers.transport import ProviderPayloadError
from tests.fake_provider_http import FakeProviderHttpClient, FakeTime

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "providers"
NOW = FixedClock("2026-03-02T12:00:00+00:00")
SETTINGS = Settings(_env_file=None, secret_key="test-secret")


def _provider(
    responses: dict[str, bytes], *, time_source: FakeTime | None = None
) -> tuple[CoinGeckoProvider, FakeProviderHttpClient]:
    client = FakeProviderHttpClient(responses)
    time_source = time_source or FakeTime()
    runtime = build_provider_runtime(
        SETTINGS,
        NOW,
        http=client,
        sleep=time_source.sleep,
        monotonic=time_source.monotonic,
    )
    provider = runtime.providers[COINGECKO_KEY]
    assert isinstance(provider, CoinGeckoProvider)
    return provider, client


def _latest_url(vs: str) -> str:
    return f"{SIMPLE_PRICE_URL}?ids=bitcoin&vs_currencies={vs}&include_last_updated_at=true"


def _history_url(vs: str) -> str:
    return f"{MARKET_CHART_URL.format(id='bitcoin')}?vs_currency={vs}&days=max&interval=daily"


def test_supports_crypto_in_the_three_quoted_currencies() -> None:
    provider, _ = _provider({})
    assert provider.supports(ProviderInstrument(uuid4(), "crypto", "USD", "coingecko", "bitcoin"))
    assert provider.supports(ProviderInstrument(uuid4(), "crypto", "PLN", "coingecko", "bitcoin"))
    assert not provider.supports(
        ProviderInstrument(uuid4(), "crypto", "CHF", "coingecko", "bitcoin")
    )
    assert not provider.supports(ProviderInstrument(uuid4(), "etf", "USD", "coingecko", "bitcoin"))


def test_latest_quote_uses_the_direct_currency_and_last_updated_date() -> None:
    provider, client = _provider(
        {_latest_url("usd"): (FIXTURES / "coingecko_simple.json").read_bytes()}
    )
    quotes = provider.fetch_latest(["bitcoin"], "USD")
    assert quotes["bitcoin"].price == Decimal("12345.67")
    assert quotes["bitcoin"].date == date(2026, 3, 2)
    assert len(client.calls) == 1


def test_a_demo_key_is_sent_as_a_header() -> None:
    provider, client = _provider(
        {_latest_url("usd"): (FIXTURES / "coingecko_simple.json").read_bytes()}
    )
    provider.fetch_latest(["bitcoin"], "USD", settings={"api_key": "demo-key"})
    _url, headers = client.calls[0]
    assert headers["x-cg-demo-api-key"] == "demo-key"


def test_history_filters_the_chart_to_the_requested_dates() -> None:
    provider, _ = _provider({_history_url("usd"): (FIXTURES / "coingecko_chart.json").read_bytes()})
    quotes = provider.fetch_history("bitcoin", date(2026, 2, 27), date(2026, 3, 2), "USD")
    assert [(quote.date, quote.price) for quote in quotes] == [
        (date(2026, 2, 27), Decimal("100.25")),
        (date(2026, 3, 2), Decimal("120.50")),
    ]


def test_unsupported_quote_currency_is_refused() -> None:
    provider, _ = _provider({})
    with pytest.raises(ProviderPayloadError):
        provider.fetch_latest(["bitcoin"], "CHF")
    with pytest.raises(ProviderPayloadError):
        provider.fetch_history("bitcoin", date(2026, 2, 1), date(2026, 3, 1), "CHF")


def test_malformed_json_is_a_payload_error() -> None:
    provider, _ = _provider({_latest_url("usd"): b"not json"})
    with pytest.raises(ProviderPayloadError):
        provider.fetch_latest(["bitcoin"], "USD")
