"""The Apprise adapter, without the network: parsing is offline and sends use stub plugins."""

from __future__ import annotations

import threading
from typing import Any

import pytest

from assetboard import notify_apprise
from assetboard.notify_apprise import AppriseNotifier
from assetboard.services.notify import InvalidNotificationUrl, SendResult


class StubPlugin:
    service_name = "Stub"

    def __init__(self, behaviour: str, release: threading.Event | None = None) -> None:
        self.behaviour = behaviour
        self.release = release
        self.socket_connect_timeout = 99.0
        self.socket_read_timeout = 99.0
        self.calls: list[dict[str, Any]] = []

    def notify(self, **kwargs: Any) -> bool:
        self.calls.append(kwargs)
        if self.behaviour == "hang":
            assert self.release is not None
            self.release.wait(timeout=30)
        if self.behaviour == "raise":
            raise ConnectionError("refused")
        return self.behaviour == "ok"


def use_plugin(monkeypatch: pytest.MonkeyPatch, plugin: StubPlugin | None) -> None:
    monkeypatch.setattr(notify_apprise.Apprise, "instantiate", lambda *_a, **_k: plugin)


def test_inspect_parses_offline_and_names_the_service() -> None:
    notifier = AppriseNotifier(10)
    assert notifier.inspect("ntfys://ntfy.example.com/topic").service_name == "ntfy"
    assert notifier.inspect("tgram://123456789:abcdefg_hijklmnop/12345").service_name == "Telegram"
    with pytest.raises(InvalidNotificationUrl):
        notifier.inspect("nonsense://x")
    with pytest.raises(InvalidNotificationUrl):
        notifier.inspect("not a url")


def test_send_overrides_the_url_timeouts_and_sends_plain_text(monkeypatch) -> None:
    plugin = StubPlugin("ok")
    use_plugin(monkeypatch, plugin)
    assert AppriseNotifier(7).send("ntfy://x/topic?cto=60&rto=60", "Title", "Body") == SendResult(
        True
    )
    assert (plugin.socket_connect_timeout, plugin.socket_read_timeout) == (7.0, 7.0)
    [call] = plugin.calls
    assert call["title"] == "Title" and call["body"] == "Body"
    assert call["body_format"] == notify_apprise.NotifyFormat.TEXT


@pytest.mark.parametrize(
    ("behaviour", "error"), [("fail", "delivery_failed"), ("raise", "delivery_failed")]
)
def test_failures_become_codes(monkeypatch, behaviour: str, error: str) -> None:
    use_plugin(monkeypatch, StubPlugin(behaviour))
    assert AppriseNotifier(5).send("ntfy://x/t", "T", "B") == SendResult(False, error)


def test_an_unparseable_url_is_a_failed_send(monkeypatch) -> None:
    use_plugin(monkeypatch, None)
    assert AppriseNotifier(5).send("ntfy://x/t", "T", "B") == SendResult(False, "invalid_url")


def test_a_hung_service_is_abandoned_after_the_timeout(monkeypatch) -> None:
    release = threading.Event()
    use_plugin(monkeypatch, StubPlugin("hang", release))
    try:
        assert AppriseNotifier(0.2).send("ntfy://x/t", "T", "B") == SendResult(False, "timeout")
    finally:
        release.set()
