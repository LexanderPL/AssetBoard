"""EODHD adapter against recorded fixtures (F6.4)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

import pytest

from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.domain.prices import ProviderInstrument
from assetboard.domain.providers import EODHD_KEY
from assetboard.services.providers.eodhd import EOD_URL, EodhdProvider
from assetboard.services.providers.registry import build_provider_runtime
from assetboard.services.providers.transport import (
    HttpResponse,
    ProviderPayloadError,
    ProviderTransportError,
)
from tests.fake_provider_http import FakeProviderHttpClient, FakeTime

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "providers"
NOW = FixedClock("2026-03-02T12:00:00+00:00")
SETTINGS = Settings(_env_file=None, secret_key="test-secret")
SECRET = "EODHD-SECRET"


def _latest_url(symbol: str = "VWCE.DE") -> str:
    query = urlencode({"api_token": SECRET, "fmt": "json", "order": "d", "limit": 1})
    return f"{EOD_URL.format(symbol=symbol)}?{query}"


def _history_url(symbol: str = "VWCE.DE") -> str:
    query = urlencode(
        {
            "api_token": SECRET,
            "fmt": "json",
            "order": "a",
            "from": "2026-02-27",
            "to": "2026-03-02",
        }
    )
    return f"{EOD_URL.format(symbol=symbol)}?{query}"


def _provider(
    responses: dict[str, Any], *, time_source: FakeTime | None = None
) -> tuple[EodhdProvider, FakeProviderHttpClient]:
    client = FakeProviderHttpClient(responses)
    time_source = time_source or FakeTime()
    runtime = build_provider_runtime(
        SETTINGS,
        NOW,
        http=client,
        sleep=time_source.sleep,
        monotonic=time_source.monotonic,
    )
    provider = runtime.providers[EODHD_KEY]
    assert isinstance(provider, EodhdProvider)
    return provider, client


def test_supports_equities_and_etfs_with_a_ticker() -> None:
    provider, _ = _provider({})
    assert provider.supports(ProviderInstrument(uuid4(), "etf", "EUR", "eodhd", "VWCE.DE"))
    assert provider.supports(ProviderInstrument(uuid4(), "equity", "USD", "eodhd", "AAPL.US"))
    assert not provider.supports(ProviderInstrument(uuid4(), "crypto", "USD", "eodhd", "BTC"))
    assert not provider.supports(ProviderInstrument(uuid4(), "etf", "EUR", "eodhd", ""))


def test_latest_uses_adjusted_close() -> None:
    provider, _ = _provider({_latest_url(): (FIXTURES / "eodhd_eod.json").read_bytes()})
    quotes = provider.fetch_latest(["VWCE.DE"], "", settings={"api_key": SECRET})
    assert quotes["VWCE.DE"].price == Decimal("101.5")
    assert quotes["VWCE.DE"].date == date(2026, 3, 2)


def test_history_returns_adjusted_close_in_date_order() -> None:
    provider, _ = _provider({_history_url(): (FIXTURES / "eodhd_eod.json").read_bytes()})
    quotes = provider.fetch_history(
        "VWCE.DE", date(2026, 2, 27), date(2026, 3, 2), settings={"api_key": SECRET}
    )
    assert [(quote.date, quote.price) for quote in quotes] == [
        (date(2026, 2, 27), Decimal("99.5")),
        (date(2026, 3, 2), Decimal("101.5")),
    ]


def test_missing_key_is_refused() -> None:
    provider, _ = _provider({})
    with pytest.raises(ProviderPayloadError) as caught:
        provider.fetch_latest(["VWCE.DE"], "")
    assert caught.value.code == "missing_api_key"


def test_malformed_response_is_a_payload_error() -> None:
    provider, _ = _provider({_latest_url(): b"not json"})
    with pytest.raises(ProviderPayloadError):
        provider.fetch_latest(["VWCE.DE"], "", settings={"api_key": SECRET})


def test_the_token_never_reaches_transport_errors(caplog: pytest.LogCaptureFixture) -> None:
    provider, _ = _provider({_latest_url(): HttpResponse(500, b"", {})})
    with caplog.at_level("DEBUG"), pytest.raises(ProviderTransportError) as caught:
        provider.fetch_latest(["VWCE.DE"], "", settings={"api_key": SECRET})
    assert caught.value.code == "http_500"
    assert SECRET not in str(caught.value)
    assert SECRET not in repr(caught.value)
    assert SECRET not in caplog.text
