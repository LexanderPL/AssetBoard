"""Notification channels in Chromium (F3.6): add a channel, see only the masked URL, send a test
message to a fake notifier, edit and delete it (confirmation dialog). No CSP violations."""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright

from assetboard.app import create_app
from assetboard.config import Settings
from assetboard.services.auth import run_initial_setup
from tests.fake_notifier import FakeNotifier

pytestmark = pytest.mark.skipif(
    os.environ.get("ASSETBOARD_RUN_BROWSER_TESTS") != "1",
    reason="Set ASSETBOARD_RUN_BROWSER_TESTS=1 to run the Chromium tests",
)

PASSWORD = "Correct Horse Battery!"
URL = "ntfys://ntfy.example.com/browser-secret-topic"
VIOLATION_RECORDER = """
window.__cspViolations = [];
document.addEventListener("securitypolicyviolation", (event) => {
  window.__cspViolations.push(event.violatedDirective + " " + (event.blockedURI || "inline"));
});
"""


def test_notification_channels_in_a_browser(tmp_path: Path) -> None:
    notifier = FakeNotifier()
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'browser.db'}",
            secret_key="browser-test-key",
        ),
        notifier=notifier,
    )
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    server_thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    server_thread.start()
    deadline = time.monotonic() + 10
    while not server.started and server_thread.is_alive() and time.monotonic() < deadline:
        threading.Event().wait(0.01)

    try:
        assert server.started
        run_initial_setup(
            app.state.session_factory,
            email="admin@example.com",
            display_name="Browser Admin",
            password=PASSWORD,
            workspace_name="Browser Home",
            settings=app.state.settings,
            clock=app.state.clock,
        )
        base = f"http://127.0.0.1:{port}"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context()
            context.add_init_script(VIOLATION_RECORDER)
            page = context.new_page()
            page.goto(f"{base}/login", wait_until="networkidle")
            page.get_by_label("Email").fill("admin@example.com")
            page.get_by_label("Password").fill(PASSWORD)
            page.get_by_role("button", name="Sign in").click()
            page.wait_for_url(f"{base}/")

            page.goto(f"{base}/notifications", wait_until="networkidle")
            expect(page.locator("[data-no-channels]")).to_be_visible()
            form = page.locator("[data-new-channel]")
            form.get_by_label("Name").fill("Phone")
            form.get_by_label("Apprise URL").fill(URL)
            form.get_by_label("Daily at").fill("07:15")
            form.get_by_role("button", name="Add channel").click()
            expect(page.locator("[data-notice]")).to_be_visible()
            expect(page.locator("[data-masked-url]")).to_have_text("ntfys://••••••")
            assert "browser-secret-topic" not in page.content()

            page.locator("[data-send-test]").click()
            expect(page.locator("[data-notice]")).to_contain_text("Test message sent")
            assert [message.url for message in notifier.sent] == [URL]

            channel = page.locator("[data-channel]")
            channel.locator("summary").click()
            channel.get_by_label("Name").fill("Phone 2")
            channel.get_by_role("button", name="Save channel").click()
            expect(page.locator("[data-channel] h2")).to_have_text("Phone 2")

            page.locator("[data-channel]").get_by_role("button", name="Delete").click()
            confirm = page.locator("#confirm-dialog")
            expect(confirm).to_contain_text("Delete the channel Phone 2?")
            page.get_by_role("button", name="Continue").click()
            expect(page.locator("[data-no-channels]")).to_be_visible()
            assert page.evaluate("window.__cspViolations") == []
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
