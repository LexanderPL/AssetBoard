"""HTTP transport, retries, response cache and per-provider rate limits (F6.4).

Provider calls are the only outbound HTTP in this feature. The transport is deliberately small
and standard-library based: HTTPS only, hardcoded provider hosts, no redirects, bounded response
bodies, and errors reduced to short codes so API tokens in query strings can never reach logs.
"""

from __future__ import annotations

import hashlib
import math
import ssl
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from assetboard.branding import USER_AGENT
from assetboard.clock import Clock
from assetboard.config import Settings

HTTP_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
WEB_WAIT_SECONDS = 3.0
BACKGROUND_WAIT_SECONDS = 300.0
RETRY_ATTEMPTS = 3
RETRY_BASE_SECONDS = 0.5


class ProviderTransportError(Exception):
    """A provider HTTP call failed; ``code`` is a short, secret-free token."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RateLimited(ProviderTransportError):
    """The provider cannot be called within the caller's wait budget."""

    def __init__(self, retry_after_seconds: float) -> None:
        seconds = max(1, int(math.ceil(retry_after_seconds)))
        super().__init__("rate_limited")
        self.retry_after_seconds = seconds


class ProviderPayloadError(Exception):
    """A provider response could not be parsed into usable observations."""

    def __init__(self, code: str = "malformed_response") -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]


class HttpClient(Protocol):
    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = HTTP_TIMEOUT_SECONDS,
        max_bytes: int = MAX_RESPONSE_BYTES,
    ) -> HttpResponse: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


class UrllibHttpClient:
    """A small HTTPS client: one GET, no redirects, no cookies, bounded body."""

    def __init__(self) -> None:
        self._opener = build_opener(
            HTTPSHandler(context=ssl.create_default_context()),
            _NoRedirect(),
        )

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = HTTP_TIMEOUT_SECONDS,
        max_bytes: int = MAX_RESPONSE_BYTES,
    ) -> HttpResponse:
        if not url.startswith("https://"):
            raise ProviderTransportError("insecure_url")
        request_headers = {"User-Agent": USER_AGENT, **dict(headers or {})}
        request = Request(url, method="GET", headers=request_headers)
        try:
            with self._opener.open(request, timeout=timeout) as response:
                body = response.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise ProviderTransportError("response_too_large")
                return HttpResponse(
                    status=int(response.status),
                    body=body,
                    headers=dict(response.headers.items()),
                )
        except HTTPError as error:
            body = b""
            try:
                body = error.read(max_bytes + 1)
            except Exception:
                body = b""
            if len(body) > max_bytes:
                raise ProviderTransportError("response_too_large") from None
            return HttpResponse(
                status=int(error.code),
                body=body,
                headers=dict(error.headers.items()),
            )
        except (TimeoutError, URLError) as error:
            code = "timeout" if _is_timeout(error) else "network"
            raise ProviderTransportError(code) from None
        except OSError:
            raise ProviderTransportError("network") from None
        except ValueError:
            raise ProviderTransportError("invalid_url") from None


def _is_timeout(error: BaseException) -> bool:
    if isinstance(error, TimeoutError):
        return True
    if isinstance(error, URLError):
        reason = error.reason
        return isinstance(reason, TimeoutError) or "timed out" in str(reason).casefold()
    return False


def _retry_after(response: HttpResponse, now: datetime) -> float | None:
    raw = next(
        (value for key, value in response.headers.items() if key.casefold() == "retry-after"),
        None,
    )
    if raw is None:
        return None
    text = raw.strip()
    if text.isdigit():
        return float(text)
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - now).total_seconds())


class RetryingHttpClient:
    """Retry 429/5xx/network failures with bounded exponential backoff."""

    def __init__(
        self,
        client: HttpClient,
        *,
        clock: Clock,
        sleep: Callable[[float], None] = time.sleep,
        attempts: int = RETRY_ATTEMPTS,
    ) -> None:
        self.client = client
        self.clock = clock
        self.sleep = sleep
        self.attempts = max(1, attempts)

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = HTTP_TIMEOUT_SECONDS,
        max_bytes: int = MAX_RESPONSE_BYTES,
        wait: bool = True,
        max_wait: float = BACKGROUND_WAIT_SECONDS,
    ) -> HttpResponse:
        last_code = "network"
        for attempt in range(self.attempts):
            try:
                response = self.client.get(
                    url, headers=headers, timeout=timeout, max_bytes=max_bytes
                )
            except ProviderTransportError as error:
                last_code = error.code
                if error.code not in {"network", "timeout"} or attempt == self.attempts - 1:
                    raise
                self._wait(RETRY_BASE_SECONDS * (2**attempt), wait, max_wait)
                continue

            if 200 <= response.status < 300:
                return response
            if response.status == 429:
                delay = _retry_after(response, self.clock.now()) or RETRY_BASE_SECONDS * (
                    2**attempt
                )
                if attempt == self.attempts - 1:
                    raise RateLimited(delay)
                self._wait(delay, wait, max_wait)
                last_code = "http_429"
                continue
            if 500 <= response.status < 600:
                if attempt == self.attempts - 1:
                    raise ProviderTransportError(f"http_{response.status}")
                self._wait(RETRY_BASE_SECONDS * (2**attempt), wait, max_wait)
                last_code = f"http_{response.status}"
                continue
            raise ProviderTransportError(f"http_{response.status}")
        raise ProviderTransportError(last_code)

    def _wait(self, delay: float, wait: bool, max_wait: float) -> None:
        del wait
        delay = max(0.0, delay)
        if delay > max_wait:
            raise RateLimited(delay)
        if delay:
            self.sleep(delay)


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    expires_at: datetime
    response: HttpResponse


class ResponseCache:
    """A bounded, thread-safe TTL cache for successful provider responses."""

    def __init__(self, clock: Clock, *, max_entries: int = 128) -> None:
        self.clock = clock
        self.max_entries = max(1, max_entries)
        self._entries: dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()

    @staticmethod
    def key(url: str, headers: Mapping[str, str] | None = None) -> str:
        header_text = "\n".join(
            f"{key.casefold()}:{value}" for key, value in sorted((headers or {}).items())
        )
        return hashlib.sha256(f"GET\n{url}\n{header_text}".encode()).hexdigest()

    def get(self, key: str) -> HttpResponse | None:
        now = self.clock.now()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= now:
                self._entries.pop(key, None)
                return None
            return entry.response

    def set(self, key: str, response: HttpResponse, ttl: timedelta) -> None:
        if response.status != 200:
            return
        now = self.clock.now()
        with self._lock:
            if len(self._entries) >= self.max_entries:
                self._evict(now)
            self._entries[key] = _CacheEntry(now + ttl, response)

    def _evict(self, now: datetime) -> None:
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            self._entries.pop(key, None)
        while len(self._entries) >= self.max_entries:
            self._entries.pop(next(iter(self._entries)))


class WindowRateLimiter:
    """At most ``requests`` calls in a sliding ``per_seconds`` window."""

    def __init__(
        self,
        requests: int,
        per_seconds: float,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.requests = max(1, requests)
        self.per_seconds = max(0.001, per_seconds)
        self.sleep = sleep
        self.monotonic = monotonic
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self, *, wait: bool, max_wait: float) -> None:
        while True:
            with self._lock:
                now = self.monotonic()
                while self._calls and self._calls[0] <= now - self.per_seconds:
                    self._calls.popleft()
                if len(self._calls) < self.requests:
                    self._calls.append(now)
                    return
                delay = self._calls[0] + self.per_seconds - now
            if delay <= 0:
                continue
            if delay > max_wait:
                raise RateLimited(delay)
            self.sleep(delay)


class RateLimiterRegistry:
    def __init__(
        self,
        limits: Mapping[str, tuple[int, float]],
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limiters = {
            key: WindowRateLimiter(requests, seconds, sleep=sleep, monotonic=monotonic)
            for key, (requests, seconds) in limits.items()
        }

    def acquire(self, provider_key: str, *, wait: bool, max_wait: float) -> None:
        limiter = self._limiters.get(provider_key)
        if limiter is not None:
            limiter.acquire(wait=wait, max_wait=max_wait)


class ProviderRuntime:
    """Shared provider dependencies; tests inject a fake HTTP client and sleep hooks."""

    def __init__(
        self,
        settings: Settings,
        clock: Clock,
        http: RetryingHttpClient,
        cache: ResponseCache,
        limiters: RateLimiterRegistry,
    ) -> None:
        self.settings = settings
        self.clock = clock
        self.http = http
        self.cache = cache
        self.limiters = limiters
        self.providers: dict[str, object] = {}

    def fetch(
        self,
        provider_key: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        ttl: timedelta,
        wait: bool,
        max_bytes: int = MAX_RESPONSE_BYTES,
        max_wait: float = BACKGROUND_WAIT_SECONDS,
    ) -> bytes:
        key = ResponseCache.key(url, headers)
        cached = self.cache.get(key)
        if cached is not None:
            return cached.body
        effective_wait = max_wait if wait else min(max_wait, WEB_WAIT_SECONDS)
        self.limiters.acquire(provider_key, wait=wait, max_wait=effective_wait)
        response = self.http.get(
            url,
            headers=headers,
            timeout=HTTP_TIMEOUT_SECONDS,
            max_bytes=max_bytes,
            wait=wait,
            max_wait=effective_wait,
        )
        if response.status != 200:
            raise ProviderTransportError(f"http_{response.status}")
        self.cache.set(key, response, ttl)
        return response.body
