"""Build the built-in provider runtime (F6.4)."""

from __future__ import annotations

import time
from collections.abc import Callable

from assetboard.clock import Clock
from assetboard.config import Settings
from assetboard.domain.providers import (
    COINGECKO_KEY,
    ECB_KEY,
    EODHD_KEY,
    GOLDAPI_KEY,
    NBP_KEY,
)
from assetboard.services.providers.coingecko import CoinGeckoProvider
from assetboard.services.providers.ecb import EcbProvider
from assetboard.services.providers.eodhd import EodhdProvider
from assetboard.services.providers.goldapi import GoldApiProvider
from assetboard.services.providers.nbp import NbpProvider
from assetboard.services.providers.transport import (
    HttpClient,
    ProviderRuntime,
    RateLimiterRegistry,
    ResponseCache,
    RetryingHttpClient,
    UrllibHttpClient,
)


def build_provider_runtime(
    settings: Settings,
    clock: Clock,
    *,
    http: HttpClient | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> ProviderRuntime:
    """The process-wide provider runtime; tests pass a fixture HTTP client and fake time."""
    retrying = RetryingHttpClient(http or UrllibHttpClient(), clock=clock, sleep=sleep)
    runtime = ProviderRuntime(
        settings=settings,
        clock=clock,
        http=retrying,
        cache=ResponseCache(clock),
        limiters=RateLimiterRegistry(
            {
                NBP_KEY: (
                    NbpProvider.rate_limit.requests,
                    NbpProvider.rate_limit.per_seconds,
                ),
                ECB_KEY: (
                    EcbProvider.rate_limit.requests,
                    EcbProvider.rate_limit.per_seconds,
                ),
                COINGECKO_KEY: (
                    CoinGeckoProvider.rate_limit.requests,
                    CoinGeckoProvider.rate_limit.per_seconds,
                ),
                EODHD_KEY: (
                    EodhdProvider.rate_limit.requests,
                    EodhdProvider.rate_limit.per_seconds,
                ),
                GOLDAPI_KEY: (
                    GoldApiProvider.rate_limit.requests,
                    GoldApiProvider.rate_limit.per_seconds,
                ),
            },
            sleep=sleep,
            monotonic=monotonic,
        ),
    )
    runtime.providers = {
        NBP_KEY: NbpProvider(runtime),
        ECB_KEY: EcbProvider(runtime),
        COINGECKO_KEY: CoinGeckoProvider(runtime),
        EODHD_KEY: EodhdProvider(runtime),
        GOLDAPI_KEY: GoldApiProvider(runtime),
    }
    return runtime
