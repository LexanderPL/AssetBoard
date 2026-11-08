"""Provider transport: HTTPS-only, retries, cache and rate limits (F6.4)."""

from __future__ import annotations

import time
from datetime import timedelta

import pytest

from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.services.providers.transport import (
    HttpResponse,
    ProviderRuntime,
    ProviderTransportError,
    RateLimited,
    RateLimiterRegistry,
    ResponseCache,
    RetryingHttpClient,
    UrllibHttpClient,
    WindowRateLimiter,
)
from tests.fake_provider_http import FakeProviderHttpClient, FakeTime

NOW = FixedClock("2026-03-02T12:00:00+00:00")
SETTINGS = Settings(_env_file=None, secret_key="test-secret")


def test_urllib_client_refuses_plain_http_without_touching_the_network() -> None:
    with pytest.raises(ProviderTransportError) as caught:
        UrllibHttpClient().get("http://example.com")
    assert caught.value.code == "insecure_url"


def test_retries_5xx_then_returns_success() -> None:
    time = FakeTime()
    client = FakeProviderHttpClient(
        {"https://provider.test/data": [HttpResponse(500, b"", {}), b"ok"]}
    )
    retrying = RetryingHttpClient(client, clock=NOW, sleep=time.sleep)
    response = retrying.get("https://provider.test/data")
    assert response.body == b"ok"
    assert time.sleeps == [0.5]
    assert len(client.calls) == 2


def test_does_not_retry_a_401() -> None:
    client = FakeProviderHttpClient({"https://provider.test/data": HttpResponse(401, b"", {})})
    retrying = RetryingHttpClient(client, clock=NOW)
    with pytest.raises(ProviderTransportError) as caught:
        retrying.get("https://provider.test/data")
    assert caught.value.code == "http_401"
    assert len(client.calls) == 1


def test_honours_retry_after_in_the_background() -> None:
    time = FakeTime()
    client = FakeProviderHttpClient(
        {
            "https://provider.test/data": [
                HttpResponse(429, b"", {"Retry-After": "2"}),
                b"ok",
            ]
        }
    )
    retrying = RetryingHttpClient(client, clock=NOW, sleep=time.sleep)
    assert retrying.get("https://provider.test/data").body == b"ok"
    assert time.sleeps == [2.0]


def test_web_wait_budget_returns_rate_limited_instead_of_sleeping() -> None:
    client = FakeProviderHttpClient(
        {"https://provider.test/data": HttpResponse(429, b"", {"Retry-After": "3600"})}
    )
    retrying = RetryingHttpClient(client, clock=NOW)
    with pytest.raises(RateLimited) as caught:
        retrying.get("https://provider.test/data", wait=False)
    assert caught.value.retry_after_seconds == 3600


def test_window_rate_limiter_never_sleeps_for_a_web_request() -> None:
    time = FakeTime()
    limiter = WindowRateLimiter(1, 60, sleep=time.sleep, monotonic=time.monotonic)
    limiter.acquire(wait=True, max_wait=120)
    with pytest.raises(RateLimited):
        limiter.acquire(wait=False, max_wait=3)
    assert time.sleeps == []


def test_window_rate_limiter_waits_in_background_jobs() -> None:
    time = FakeTime()
    limiter = WindowRateLimiter(1, 60, sleep=time.sleep, monotonic=time.monotonic)
    limiter.acquire(wait=True, max_wait=120)
    limiter.acquire(wait=True, max_wait=120)
    assert time.sleeps == [60]


def test_registry_uses_the_provider_limit() -> None:
    time = FakeTime()
    registry = RateLimiterRegistry({"nbp": (1, 1)}, sleep=time.sleep, monotonic=time.monotonic)
    registry.acquire("nbp", wait=True, max_wait=2)
    with pytest.raises(RateLimited):
        registry.acquire("nbp", wait=False, max_wait=0)
    assert time.sleeps == []


def test_urllib_client_enforces_the_response_size_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Response:
        status = 200
        headers: dict[str, str] = {}

        def read(self, _size: int) -> bytes:
            return b"12345"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _Opener:
        def open(self, _request, timeout: float):
            del timeout
            return _Response()

    client = UrllibHttpClient()
    monkeypatch.setattr(client, "_opener", _Opener())
    with pytest.raises(ProviderTransportError) as caught:
        client.get("https://provider.test/data", max_bytes=4)
    assert caught.value.code == "response_too_large"


def test_response_cache_is_bounded_and_expires() -> None:
    class AdvancingClock(FixedClock):
        def advance(self, delta: timedelta) -> None:
            self._value += delta

    clock = AdvancingClock("2026-03-02T12:00:00+00:00")
    cache = ResponseCache(clock, max_entries=2)
    key = ResponseCache.key("https://provider.test/data")
    cache.set(key, HttpResponse(200, b"one", {}), timedelta(minutes=1))
    assert cache.get(key) is not None
    clock.advance(timedelta(minutes=2))
    assert cache.get(key) is None


def test_runtime_caches_successful_responses() -> None:
    client = FakeProviderHttpClient({"https://provider.test/data": b"body"})
    time = FakeTime()
    retrying = RetryingHttpClient(client, clock=NOW, sleep=time.sleep)
    runtime = ProviderRuntime(
        SETTINGS,
        NOW,
        retrying,
        ResponseCache(NOW),
        RateLimiterRegistry({"nbp": (10, 1)}, sleep=time.sleep, monotonic=time.monotonic),
    )
    first = runtime.fetch("nbp", "https://provider.test/data", ttl=timedelta(hours=1), wait=False)
    second = runtime.fetch("nbp", "https://provider.test/data", ttl=timedelta(hours=1), wait=False)
    assert (first, second) == (b"body", b"body")
    assert len(client.calls) == 1
    assert time.sleeps == []


def test_runtime_wraps_a_non_200_response() -> None:
    client = FakeProviderHttpClient({"https://provider.test/data": HttpResponse(404, b"", {})})
    retrying = RetryingHttpClient(client, clock=NOW)
    runtime = ProviderRuntime(
        SETTINGS,
        NOW,
        retrying,
        ResponseCache(NOW),
        RateLimiterRegistry({}, sleep=time.sleep, monotonic=time.monotonic),
    )
    with pytest.raises(ProviderTransportError) as caught:
        runtime.fetch("nbp", "https://provider.test/data", ttl=timedelta(hours=1), wait=True)
    assert caught.value.code == "http_404"
