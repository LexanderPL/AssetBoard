"""GoldAPI.io adapter against recorded fixtures (F6.5 follow-up)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

import pytest

from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.domain.prices import ProviderInstrument
from assetboard.domain.providers import GOLDAPI_KEY
from assetboard.services.providers.goldapi import (
    HISTORY_URL,
    PRICE_URL,
    GoldApiProvider,
)
from assetboard.services.providers.registry import build_provider_runtime
from assetboard.services.providers.transport import (
    HttpResponse,
    ProviderPayloadError,
    ProviderTransportError,
)
from tests.fake_provider_http import FakeProviderHttpClient, FakeTime, ResponseValue

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "providers"
NOW = FixedClock("2026-03-02T12:00:00+00:00")
SETTINGS = Settings(_env_file=None, secret_key="test-secret")
SECRET = "GOLDAPI-SECRET"


def _latest_url(metal: str = "XAG", currency: str = "PLN") -> str:
    query = urlencode({"melt_price": "false", "currency_info": "false"})
    return f"{PRICE_URL.format(metal=metal, currency=currency)}?{query}"


def _history_url(start: date, end: date, metal: str = "XAG", currency: str = "PLN") -> str:
    query = urlencode({"from": start.isoformat(), "to": end.isoformat()})
    return f"{HISTORY_URL.format(metal=metal, currency=currency)}?{query}"


def _provider(
    responses: dict[str, ResponseValue], *, time_source: FakeTime | None = None
) -> tuple[GoldApiProvider, FakeProviderHttpClient]:
    client = FakeProviderHttpClient(responses)
    time_source = time_source or FakeTime()
    runtime = build_provider_runtime(
        SETTINGS,
        NOW,
        http=client,
        sleep=time_source.sleep,
        monotonic=time_source.monotonic,
    )
    provider = runtime.providers[GOLDAPI_KEY]
    assert isinstance(provider, GoldApiProvider)
    return provider, client


def test_supports_metals_in_the_documented_currencies() -> None:
    provider, _ = _provider({})
    assert provider.supports(ProviderInstrument(uuid4(), "metal", "PLN", "goldapi", "XAG"))
    assert provider.supports(ProviderInstrument(uuid4(), "metal", "EUR", "goldapi", "XAU"))
    assert not provider.supports(ProviderInstrument(uuid4(), "etf", "PLN", "goldapi", "XAG"))
    assert not provider.supports(ProviderInstrument(uuid4(), "metal", "ABC", "goldapi", "XAG"))
    assert not provider.supports(ProviderInstrument(uuid4(), "metal", "PLN", "goldapi", "BTC"))


def test_latest_sends_the_key_in_the_header_and_reads_the_ounce_price() -> None:
    provider, client = _provider({_latest_url(): (FIXTURES / "goldapi_latest.json").read_bytes()})
    quotes = provider.fetch_latest(["XAG"], "PLN", settings={"api_key": SECRET})
    assert quotes["XAG"].price == Decimal("123.4567")
    assert quotes["XAG"].date == date(2026, 3, 2)
    url, headers = client.calls[0]
    assert SECRET not in url
    assert headers["x-access-token"] == SECRET


def test_history_returns_the_inclusive_dates() -> None:
    provider, _ = _provider(
        {
            _history_url(date(2026, 2, 27), date(2026, 3, 2)): (
                FIXTURES / "goldapi_history.json"
            ).read_bytes()
        }
    )
    quotes = provider.fetch_history(
        "XAG",
        date(2026, 2, 27),
        date(2026, 3, 2),
        "PLN",
        settings={"api_key": SECRET},
    )
    assert [(quote.date, quote.price) for quote in quotes] == [
        (date(2026, 2, 27), Decimal("120.5")),
        (date(2026, 3, 2), Decimal("123.4567")),
    ]


def test_history_is_clamped_to_the_provider_90_day_window() -> None:
    end = date(2026, 3, 2)
    window_start = end - timedelta(days=89)
    provider, client = _provider(
        {_history_url(window_start, end): (FIXTURES / "goldapi_history.json").read_bytes()}
    )
    provider.fetch_history("XAG", date(2020, 1, 1), end, "PLN", settings={"api_key": SECRET})
    assert client.calls[0][0] == _history_url(window_start, end)


def test_missing_key_and_unsupported_inputs_are_refused() -> None:
    provider, _ = _provider({})
    with pytest.raises(ProviderPayloadError) as caught:
        provider.fetch_latest(["XAG"], "PLN")
    assert caught.value.code == "missing_api_key"
    with pytest.raises(ProviderPayloadError):
        provider.fetch_latest(["XAG"], "ABC", settings={"api_key": SECRET})
    with pytest.raises(ProviderPayloadError):
        provider.fetch_latest(["XAUUSD"], "PLN", settings={"api_key": SECRET})


def test_malformed_payload_is_a_payload_error() -> None:
    provider, _ = _provider({_latest_url(): b"not json"})
    with pytest.raises(ProviderPayloadError):
        provider.fetch_latest(["XAG"], "PLN", settings={"api_key": SECRET})


def test_the_key_never_reaches_transport_errors(caplog: pytest.LogCaptureFixture) -> None:
    provider, _ = _provider({_latest_url(): HttpResponse(500, b"", {})})
    with caplog.at_level("DEBUG"), pytest.raises(ProviderTransportError) as caught:
        provider.fetch_latest(["XAG"], "PLN", settings={"api_key": SECRET})
    assert caught.value.code == "http_500"
    assert SECRET not in str(caught.value)
    assert SECRET not in repr(caught.value)
    assert SECRET not in caplog.text
