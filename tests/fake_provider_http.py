"""Offline HTTP fakes for provider tests; no test ever opens a socket."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from assetboard.services.providers.transport import HttpResponse, ProviderTransportError

ResponseValue = HttpResponse | bytes | Exception | list[HttpResponse | bytes | Exception]


class FakeProviderHttpClient:
    """Serve recorded bodies by URL (or a callable), and record the calls made."""

    def __init__(
        self,
        responses: Mapping[str, ResponseValue] | Callable[[str], ResponseValue],
    ) -> None:
        self.responses = responses
        self.calls: list[tuple[str, Mapping[str, str]]] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = 10.0,
        max_bytes: int = 8 * 1024 * 1024,
    ) -> HttpResponse:
        del timeout, max_bytes
        self.calls.append((url, headers or {}))
        if callable(self.responses):
            value = self.responses(url)
        else:
            value = self.responses.get(url)
            if value is None:
                raise AssertionError(f"unexpected provider URL: {url}")
        if isinstance(value, list):
            if not value:
                raise AssertionError(f"no queued response for {url}")
            value = value.pop(0)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, bytes):
            return HttpResponse(200, value, {})
        return value


class FailingProviderHttpClient:
    """A client that proves a code path never touches the network."""

    def __init__(self) -> None:
        self.called = False

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = 10.0,
        max_bytes: int = 8 * 1024 * 1024,
    ) -> HttpResponse:
        del headers, timeout, max_bytes
        self.called = True
        raise AssertionError(f"provider network was used: {url}")


@dataclass
class FakeTime:
    value: float = 0.0
    sleeps: list[float] = field(default_factory=list[float])

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


def error(code: str) -> ProviderTransportError:
    return ProviderTransportError(code)
