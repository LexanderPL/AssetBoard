"""NBP adapter against recorded fixtures (F6.4)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.domain.prices import ProviderInstrument
from assetboard.domain.providers import NBP_KEY
from assetboard.services.providers.nbp import (
    GOLD_LATEST,
    TABLE_A_LATEST,
    TABLE_B_LATEST,
    NbpProvider,
    _chunks,
)
from assetboard.services.providers.registry import build_provider_runtime
from assetboard.services.providers.transport import (
    HttpResponse,
    ProviderPayloadError,
)
from tests.fake_provider_http import FakeProviderHttpClient, FakeTime

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "providers"
NOW = FixedClock("2026-03-02T12:00:00+00:00")
SETTINGS = Settings(_env_file=None, secret_key="test-secret")


def _body(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _provider(
    responses: dict[str, HttpResponse | bytes], *, time_source: FakeTime
) -> tuple[NbpProvider, FakeProviderHttpClient]:
    client = FakeProviderHttpClient(responses)
    runtime = build_provider_runtime(
        SETTINGS,
        NOW,
        http=client,
        sleep=time_source.sleep,
        monotonic=time_source.monotonic,
    )
    provider = runtime.providers[NBP_KEY]
    assert isinstance(provider, NbpProvider)
    return provider, client


def test_supports_only_pln_gold() -> None:
    provider, _ = _provider({}, time_source=FakeTime())
    assert provider.supports(ProviderInstrument(uuid4(), "metal", "PLN", "nbp", "XAU"))
    assert provider.supports(ProviderInstrument(uuid4(), "metal", "PLN", "nbp", None))
    assert not provider.supports(ProviderInstrument(uuid4(), "metal", "EUR", "nbp", "XAU"))
    assert not provider.supports(ProviderInstrument(uuid4(), "etf", "PLN", "nbp", "XAU"))


def test_latest_fx_uses_table_a_and_b_fallback() -> None:
    time_source = FakeTime()
    provider, client = _provider(
        {
            TABLE_A_LATEST: _body("nbp_table_a.json"),
            TABLE_B_LATEST: _body("nbp_table_b.json"),
        },
        time_source=time_source,
    )
    quotes = provider.fetch_latest_fx(["EUR", "USD", "HUF"], wait=True)
    assert quotes["EUR"].rate == Decimal("4.3000")
    assert quotes["USD"].rate == Decimal("3.9000")
    assert quotes["HUF"].rate == Decimal("0.0110")
    assert quotes["EUR"].date == date(2026, 3, 2)
    assert len(client.calls) == 2
    assert time_source.sleeps == [1.0]


def test_latest_gold_returns_pln_per_gram() -> None:
    provider, _ = _provider({GOLD_LATEST: _body("nbp_gold.json")}, time_source=FakeTime())
    quotes = provider.fetch_latest(["XAU"], "PLN", wait=True)
    assert quotes["XAU"].date == date(2026, 3, 2)
    assert quotes["XAU"].price == Decimal("250.00")


def test_gold_history_and_fx_range_history() -> None:
    gold_url = "https://api.nbp.pl/api/cenyzlota/2026-02-27/2026-03-02/?format=json"
    table_url = "https://api.nbp.pl/api/exchangerates/tables/a/2026-02-27/2026-03-02/?format=json"
    time_source = FakeTime()
    provider, _ = _provider(
        {
            gold_url: _body("nbp_gold_history.json"),
            table_url: _body("nbp_table_a.json"),
        },
        time_source=time_source,
    )
    gold = provider.fetch_history("XAU", date(2026, 2, 27), date(2026, 3, 2), "PLN")
    assert [(quote.date, quote.price) for quote in gold] == [
        (date(2026, 2, 27), Decimal("245.00")),
        (date(2026, 3, 2), Decimal("250.00")),
    ]
    fx = provider.fetch_history_fx("USD", date(2026, 2, 27), date(2026, 3, 2))
    assert [(quote.date, quote.rate) for quote in fx] == [(date(2026, 3, 2), Decimal("3.9000"))]


def test_history_ranges_are_at_most_93_days_and_cover_without_gaps() -> None:
    chunks = _chunks(date(2026, 1, 1), date(2026, 6, 30))
    assert chunks[0][0] == date(2026, 1, 1)
    assert chunks[-1][1] == date(2026, 6, 30)
    for first, last in chunks:
        assert (last - first).days <= 92
    for (_, previous_last), (next_first, _) in zip(chunks, chunks[1:], strict=False):
        assert next_first == previous_last + timedelta(days=1)


def test_missing_latest_gold_is_empty() -> None:
    provider, _ = _provider({GOLD_LATEST: HttpResponse(404, b"", {})}, time_source=FakeTime())
    assert provider.fetch_latest(["XAU"], "PLN") == {}


def test_malformed_json_is_a_payload_error() -> None:
    provider, _ = _provider({GOLD_LATEST: b"not json"}, time_source=FakeTime())
    with pytest.raises(ProviderPayloadError):
        provider.fetch_latest(["XAU"], "PLN")


def test_unsupported_gold_currency_is_refused() -> None:
    provider, _ = _provider({}, time_source=FakeTime())
    with pytest.raises(ProviderPayloadError):
        provider.fetch_latest(["XAU"], "EUR")
    with pytest.raises(ProviderPayloadError):
        provider.fetch_latest(["XAG"], "PLN")
