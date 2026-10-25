"""Delivery through Apprise (ADR-0021). The only module that imports it."""

from __future__ import annotations

import logging
import threading

from apprise import Apprise, AppriseAsset, NotifyFormat

from assetboard.branding import APP_NAME
from assetboard.services.notify import InvalidNotificationUrl, SendResult, UrlInfo

# Apprise logs delivery details (hosts, parts of URLs, the service's replies). Keep them out
# of the application log; failures are logged by the caller with identifiers only.
logging.getLogger("apprise").setLevel(logging.CRITICAL)


class AppriseNotifier:
    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = float(timeout_seconds)

    def _asset(self) -> AppriseAsset:
        return AppriseAsset(app_id=APP_NAME, app_desc=APP_NAME, secure_logging=True)

    def _plugin(self, url: str):  # noqa: ANN202 - Apprise's plugin base class is untyped here
        plugin = Apprise.instantiate(url, asset=self._asset(), suppress_exceptions=True)
        if plugin is None:
            raise InvalidNotificationUrl
        return plugin

    def inspect(self, url: str) -> UrlInfo:
        plugin = self._plugin(url)
        return UrlInfo(service_name=str(getattr(plugin, "service_name", "") or "")[:100])

    def send(self, url: str, title: str, body: str) -> SendResult:
        try:
            plugin = self._plugin(url)
        except InvalidNotificationUrl:
            return SendResult(False, "invalid_url")
        # The user's URL may ask for longer timeouts (cto/rto); ours win.
        plugin.socket_connect_timeout = self.timeout_seconds
        plugin.socket_read_timeout = self.timeout_seconds
        outcome: list[bool] = []

        def deliver() -> None:
            try:
                outcome.append(
                    bool(plugin.notify(body=body, title=title, body_format=NotifyFormat.TEXT))
                )
            except Exception:  # noqa: BLE001 - any failure is a failed delivery
                outcome.append(False)

        # A hard deadline: a service that keeps the connection busy (retries, slow replies)
        # cannot hold the scheduler tick or a request for long. It allows one connect plus one
        # read at the socket timeout; past it the message may still arrive, so callers treat
        # "timeout" as an unknown outcome (``UNCERTAIN_ERRORS``). The daemon thread ends by
        # itself when its socket timeouts expire.
        worker = threading.Thread(target=deliver, name="notification-send", daemon=True)
        worker.start()
        worker.join(2 * self.timeout_seconds + 1)
        if worker.is_alive() or not outcome:
            return SendResult(False, "timeout")
        return SendResult(True) if outcome[0] else SendResult(False, "delivery_failed")
