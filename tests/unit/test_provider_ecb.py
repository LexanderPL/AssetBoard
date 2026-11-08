"""ECB adapter against a recorded history XML (F6.4)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.domain.providers import ECB_KEY, ecb_quote_to_rate
from assetboard.services.providers.ecb import (
    HISTORY_90D_URL,
    HISTORY_URL,
    EcbProvider,
)
from assetboard.services.providers.registry import build_provider_runtime
from assetboard.services.providers.transport import ProviderPayloadError
from tests.fake_provider_http import FakeProviderHttpClient, FakeTime

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "providers"
NOW = FixedClock("2026-03-02T12:00:00+00:00")
SETTINGS = Settings(_env_file=None, secret_key="test-secret")


def _provider(body: bytes) -> EcbProvider:
    time_source = FakeTime()
    runtime = build_provider_runtime(
        SETTINGS,
        NOW,
        http=FakeProviderHttpClient({HISTORY_URL: body, HISTORY_90D_URL: body}),
        sleep=time_source.sleep,
        monotonic=time_source.monotonic,
    )
    provider = runtime.providers[ECB_KEY]
    assert isinstance(provider, EcbProvider)
    return provider


def test_latest_rates_are_eur_per_currency() -> None:
    provider = _provider((FIXTURES / "ecb_history.xml").read_bytes())
    quotes = provider.fetch_latest_fx(["USD", "PLN", "GBP"])
    assert quotes["USD"].date == date(2026, 3, 2)
    assert quotes["USD"].rate == ecb_quote_to_rate(Decimal("1.1000"))
    assert quotes["PLN"].rate == ecb_quote_to_rate(Decimal("4.3000"))
    assert quotes["GBP"].rate == ecb_quote_to_rate(Decimal("0.8500"))


def test_history_returns_one_observation_per_published_date() -> None:
    provider = _provider((FIXTURES / "ecb_history.xml").read_bytes())
    quotes = provider.fetch_history_fx("USD", date(2026, 2, 27), date(2026, 3, 1))
    assert [(quote.date, quote.rate) for quote in quotes] == [
        (date(2026, 2, 27), ecb_quote_to_rate(Decimal("1.0900")))
    ]


def test_recent_history_uses_the_90_day_file() -> None:
    time_source = FakeTime()
    client = FakeProviderHttpClient(
        {
            HISTORY_90D_URL: (FIXTURES / "ecb_history.xml").read_bytes(),
            HISTORY_URL: b"<not-used>",
        }
    )
    runtime = build_provider_runtime(
        SETTINGS,
        NOW,
        http=client,
        sleep=time_source.sleep,
        monotonic=time_source.monotonic,
    )
    provider = runtime.providers[ECB_KEY]
    assert isinstance(provider, EcbProvider)
    provider.fetch_history_fx("USD", date(2026, 2, 27), date(2026, 3, 1))
    assert [url for url, _headers in client.calls] == [HISTORY_90D_URL]


def test_missing_currency_is_omitted() -> None:
    provider = _provider((FIXTURES / "ecb_history.xml").read_bytes())
    assert provider.fetch_latest_fx(["JPY"]) == {}


def test_malformed_xml_is_a_payload_error() -> None:
    provider = _provider(b"<not-xml")
    with pytest.raises(ProviderPayloadError):
        provider.fetch_latest_fx(["USD"])


def test_ecb_inversion_keeps_high_precision() -> None:
    rate = ecb_quote_to_rate(Decimal("4.3000"))
    with localcontext() as context:
        context.prec = 80
        assert rate == Decimal(1) / Decimal("4.3000")
