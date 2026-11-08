"""GoldAPI.io adapter against recorded fixtures (F6.5 follow-up); no network."""

from __future__ import annotations

import json
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
    RateLimited,
)
from tests.fake_provider_http import FakeProviderHttpClient, FakeTime, ResponseValue

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "providers"
NOW = FixedClock("2026-03-02T12:00:00+00:00")
SETTINGS = Settings(_env_file=None, secret_key="test-secret")
SECRET = "GOLDAPI-SENTINEL-KEY-7f3a"
KEY = {"api_key": SECRET}


def _latest_url(metal: str = "XAG", currency: str = "PLN") -> str:
    return PRICE_URL.format(metal=metal, currency=currency)


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


def _latest_body(fields: str) -> bytes:
    return ('{"metal": "XAG", "currency": "PLN", "price": 123.4567' + fields + "}").encode()


def test_endpoints_are_the_documented_https_paths() -> None:
    assert _latest_url() == "https://www.goldapi.io/api/XAG/PLN"
    assert _history_url(date(2026, 1, 1), date(2026, 3, 1)).startswith(
        "https://www.goldapi.io/api/history/XAG/PLN?from=2026-01-01&to=2026-03-01"
    )


def test_supports_metals_in_the_documented_currencies() -> None:
    provider, _ = _provider({})
    for currency in ("PLN", "EUR", "USD", "GBP", "CHF"):
        assert provider.supports(ProviderInstrument(uuid4(), "metal", currency, "goldapi", "XAG"))
    for metal in ("XAU", "xag", "XPT", "XPD"):
        assert provider.supports(ProviderInstrument(uuid4(), "metal", "EUR", "goldapi", metal))
    assert not provider.supports(ProviderInstrument(uuid4(), "etf", "PLN", "goldapi", "XAG"))
    assert not provider.supports(ProviderInstrument(uuid4(), "metal", "ABC", "goldapi", "XAG"))
    assert not provider.supports(ProviderInstrument(uuid4(), "metal", "BTC", "goldapi", "XAU"))
    assert not provider.supports(ProviderInstrument(uuid4(), "metal", "PLN", "goldapi", "BTC"))
    assert not provider.supports(ProviderInstrument(uuid4(), "metal", "PLN", "goldapi", None))


def test_live_xag_pln_sends_the_key_only_in_the_header() -> None:
    provider, client = _provider({_latest_url(): (FIXTURES / "goldapi_latest.json").read_bytes()})
    quotes = provider.fetch_latest(["xag"], "pln", settings=KEY)
    assert quotes["XAG"].price == Decimal("123.4567")
    assert isinstance(quotes["XAG"].price, Decimal)
    assert quotes["XAG"].date == date(2026, 3, 2)
    url, headers = client.calls[0]
    assert url == "https://www.goldapi.io/api/XAG/PLN"
    assert SECRET not in url
    assert headers["x-access-token"] == SECRET


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        (1772409600, date(2026, 3, 2)),  # midnight UTC
        (1772495999, date(2026, 3, 2)),  # last second of the UTC day
        (1772496000, date(2026, 3, 3)),  # next UTC day; the service caps it at today
    ],
)
def test_timestamp_is_a_utc_date(timestamp: int, expected: date) -> None:
    provider, _ = _provider({_latest_url(): _latest_body(f', "timestamp": {timestamp}')})
    assert provider.fetch_latest(["XAG"], "PLN", settings=KEY)["XAG"].date == expected


def test_missing_timestamp_falls_back_to_the_utc_today_and_extra_fields_are_ignored() -> None:
    provider, _ = _provider({_latest_url(): _latest_body(', "unexpected": {"nested": [1, 2]}')})
    quote = provider.fetch_latest(["XAG"], "PLN", settings=KEY)["XAG"]
    assert (quote.date, quote.price) == (date(2026, 3, 2), Decimal("123.4567"))


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'{"metal": "XAG", "currency": "PLN"}',
        b'{"error": "Invalid symbol"}',
        b'{"price": "123.45", "timestamp": 1772409600}',
        b'{"price": true, "timestamp": 1772409600}',
        b'{"price": 0, "timestamp": 1772409600}',
        b'{"price": -1.5, "timestamp": 1772409600}',
        b'{"price": 1.5, "timestamp": "yesterday"}',
        b'{"price": 1.5, "timestamp": -1}',
        b'{"price": 1.5, "timestamp": 99999999999999999999}',
    ],
)
def test_malformed_latest_payloads_are_payload_errors(body: bytes) -> None:
    provider, _ = _provider({_latest_url(): body})
    with pytest.raises(ProviderPayloadError) as caught:
        provider.fetch_latest(["XAG"], "PLN", settings=KEY)
    assert caught.value.code == "malformed_response"


def test_history_returns_the_inclusive_dates_in_order() -> None:
    provider, _ = _provider(
        {
            _history_url(date(2026, 2, 27), date(2026, 3, 2)): (
                FIXTURES / "goldapi_history.json"
            ).read_bytes()
        }
    )
    quotes = provider.fetch_history("XAG", date(2026, 2, 27), date(2026, 3, 2), "PLN", settings=KEY)
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
    quotes = provider.fetch_history("XAG", date(2020, 1, 1), end, "PLN", settings=KEY)
    assert len(client.calls) == 1
    url, headers = client.calls[0]
    assert url == _history_url(window_start, end)
    assert SECRET not in url
    assert headers["x-access-token"] == SECRET
    assert [quote.date for quote in quotes] == [date(2026, 2, 27), date(2026, 3, 2)]


def test_history_drops_rows_outside_the_requested_window_and_empty_ranges() -> None:
    body = json.dumps(
        {"prices": [{"date": "2026-02-26", "price": 1}, {"date": "2026-03-01", "price": 2}]}
    ).encode()
    provider, client = _provider({_history_url(date(2026, 2, 27), date(2026, 3, 1)): body})
    quotes = provider.fetch_history("XAG", date(2026, 2, 27), date(2026, 3, 1), "PLN", settings=KEY)
    assert [(quote.date, quote.price) for quote in quotes] == [(date(2026, 3, 1), Decimal(2))]
    assert provider.fetch_history("XAG", date(2026, 3, 2), date(2026, 3, 1), "PLN") == ()
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "body",
    [
        b"{}",
        b'{"prices": {}}',
        b'{"prices": [1]}',
        b'{"prices": [{"date": "2026-03-01"}]}',
        b'{"prices": [{"date": "03/01/2026", "price": 1}]}',
        b'{"prices": [{"date": 20260301, "price": 1}]}',
        b'{"prices": [{"date": "2026-03-01", "price": "1"}]}',
    ],
)
def test_malformed_history_payloads_are_payload_errors(body: bytes) -> None:
    provider, _ = _provider({_history_url(date(2026, 2, 27), date(2026, 3, 2)): body})
    with pytest.raises(ProviderPayloadError):
        provider.fetch_history("XAG", date(2026, 2, 27), date(2026, 3, 2), "PLN", settings=KEY)


def test_missing_key_and_unsupported_inputs_are_refused_without_a_call() -> None:
    provider, client = _provider({})
    with pytest.raises(ProviderPayloadError) as caught:
        provider.fetch_latest(["XAG"], "PLN")
    assert caught.value.code == "missing_api_key"
    with pytest.raises(ProviderPayloadError) as caught:
        provider.fetch_latest(["XAG"], "PLN", settings={"api_key": "  "})
    assert caught.value.code == "missing_api_key"
    with pytest.raises(ProviderPayloadError) as caught:
        provider.fetch_latest(["XAG"], "ABC", settings=KEY)
    assert caught.value.code == "unsupported_quote_currency"
    with pytest.raises(ProviderPayloadError) as caught:
        provider.fetch_latest(["XAUUSD"], "PLN", settings=KEY)
    assert caught.value.code == "unsupported_symbol"
    with pytest.raises(ProviderPayloadError) as caught:
        provider.fetch_history("XRH", date(2026, 1, 1), date(2026, 1, 2), "PLN", settings=KEY)
    assert caught.value.code == "unsupported_symbol"
    assert client.calls == []


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_are_short_codes_without_the_key(
    status: int, caplog: pytest.LogCaptureFixture
) -> None:
    provider, client = _provider(
        {_latest_url(): HttpResponse(status, b'{"error": "Invalid API key"}', {})}
    )
    with caplog.at_level("DEBUG"), pytest.raises(ProviderTransportError) as caught:
        provider.fetch_latest(["XAG"], "PLN", settings=KEY)
    assert caught.value.code == f"http_{status}"
    assert len(client.calls) == 1  # never retried
    assert SECRET not in str(caught.value) and SECRET not in repr(caught.value)
    assert SECRET not in caplog.text


def test_429_raises_rate_limited_at_once_for_web_requests() -> None:
    time_source = FakeTime()
    provider, client = _provider(
        {_latest_url(): HttpResponse(429, b"", {"Retry-After": "120"})},
        time_source=time_source,
    )
    with pytest.raises(RateLimited) as caught:
        provider.fetch_latest(["XAG"], "PLN", wait=False, settings=KEY)
    assert caught.value.retry_after_seconds == 120
    assert len(client.calls) == 1
    assert time_source.sleeps == []


def test_web_requests_never_sleep_behind_the_rate_limiter() -> None:
    time_source = FakeTime()
    body = (FIXTURES / "goldapi_latest.json").read_bytes()
    provider, _ = _provider(
        {_latest_url(): body, _latest_url("XAU"): body}, time_source=time_source
    )
    provider.fetch_latest(["XAG"], "PLN", wait=False, settings=KEY)
    with pytest.raises(RateLimited):
        provider.fetch_latest(["XAU"], "PLN", wait=False, settings=KEY)
    assert time_source.sleeps == []


def test_the_key_never_reaches_transport_errors(caplog: pytest.LogCaptureFixture) -> None:
    provider, _ = _provider({_latest_url(): HttpResponse(500, b"", {})})
    with caplog.at_level("DEBUG"), pytest.raises(ProviderTransportError) as caught:
        provider.fetch_latest(["XAG"], "PLN", settings=KEY)
    assert caught.value.code == "http_500"
    assert SECRET not in str(caught.value)
    assert SECRET not in repr(caught.value)
    assert SECRET not in caplog.text
