"""The ``Notifier`` interface between reminders and the delivery library (ADR-0021).

Only ``assetboard/notify_apprise.py`` talks to Apprise; tests use a fake that records messages
and never touches the network.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class InvalidNotificationUrl(ValueError):
    """The URL is not one valid Apprise URL."""


@dataclass(frozen=True, slots=True)
class UrlInfo:
    """What may be shown about a saved URL: never the URL itself."""

    service_name: str


@dataclass(frozen=True, slots=True)
class SendResult:
    ok: bool
    # A short code, never the service's reply (it can name hosts or echo the URL).
    error: str | None = None


class Notifier(Protocol):
    def inspect(self, url: str) -> UrlInfo:
        """Parse ``url`` offline; raise ``InvalidNotificationUrl`` when it is not usable."""
        ...

    def send(self, url: str, title: str, body: str) -> SendResult:
        """Deliver one message; never raises, returns within the configured timeout."""
        ...
