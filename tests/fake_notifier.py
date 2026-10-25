"""A ``Notifier`` for tests: records messages, can fail on demand, never uses the network."""

from __future__ import annotations

from dataclasses import dataclass, field

from assetboard.services.notify import InvalidNotificationUrl, SendResult, UrlInfo


@dataclass(frozen=True)
class SentMessage:
    url: str
    title: str
    body: str


@dataclass
class FakeNotifier:
    sent: list[SentMessage] = field(default_factory=list[SentMessage])
    # URLs whose sends fail with this code (None: every send succeeds).
    fail_with: str | None = None
    failing_urls: set[str] = field(default_factory=set[str])

    def inspect(self, url: str) -> UrlInfo:
        scheme, _, rest = url.partition("://")
        if not rest or "invalid" in rest:
            raise InvalidNotificationUrl
        return UrlInfo(service_name=f"Fake {scheme}")

    def send(self, url: str, title: str, body: str) -> SendResult:
        if self.fail_with is not None and (not self.failing_urls or url in self.failing_urls):
            return SendResult(False, self.fail_with)
        self.sent.append(SentMessage(url, title, body))
        return SendResult(True)
