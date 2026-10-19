"""Open registration and password reset by email in Chromium (pre-release B, ADR-0017): an
administrator turns registration on, a visitor registers through the emailed link, then resets
the password through another. Mail goes to a fake mailer. No CSP violations."""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import Page, expect, sync_playwright

from assetboard.app import create_app
from assetboard.config import Settings
from assetboard.services.auth import run_initial_setup
from tests.conftest import FakeMailer, mail_body

pytestmark = pytest.mark.skipif(
    os.environ.get("ASSETBOARD_RUN_BROWSER_TESTS") != "1",
    reason="Set ASSETBOARD_RUN_BROWSER_TESTS=1 to run the Chromium tests",
)

PASSWORD = "Correct Horse Battery!"
NEW_PASSWORD = "Another Strong Password"
VIOLATION_RECORDER = """
window.__cspViolations = [];
document.addEventListener("securitypolicyviolation", (event) => {
  window.__cspViolations.push(event.violatedDirective + " " + (event.blockedURI || "inline"));
});
"""


def _last_link(app: object, mailer: FakeMailer, base: str, prefix: str) -> str:
    app.state.mail_outbox.join()  # type: ignore[attr-defined]
    for word in mail_body(mailer.sent[-1]).split():
        if word.startswith(f"{base}{prefix}"):
            return word
    raise AssertionError(f"no {prefix} link")


def _sign_in(page: Page, base: str, email: str, password: str) -> None:
    page.goto(f"{base}/login", wait_until="networkidle")
    page.get_by_label("Email").fill(email)
    page.get_by_label("Password").fill(password)
    page.get_by_role("button", name="Sign in").click()


def test_registration_and_password_reset_by_email(tmp_path: Path) -> None:
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    base = f"http://127.0.0.1:{listener.getsockname()[1]}"
    mailer = FakeMailer()
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'browser.db'}",
            secret_key="browser-test-key",
            base_url=base,
            smtp_host="smtp.example.test",
            smtp_from="budget@example.test",
        ),
        mailer=mailer,
    )
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
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()

            # The administrator turns open registration on.
            admin = browser.new_context(color_scheme="light")
            admin.add_init_script(VIOLATION_RECORDER)
            admin_page = admin.new_page()
            _sign_in(admin_page, base, "admin@example.com", PASSWORD)
            admin_page.wait_for_url(f"{base}/")
            admin_page.goto(f"{base}/admin/settings", wait_until="networkidle")
            admin_page.get_by_label("Open registration").select_option("on")
            admin_page.get_by_label("Your password").fill(PASSWORD)
            admin_page.get_by_role("button", name="Save settings").click()
            expect(admin_page.locator("[data-admin-notice]")).to_have_text(
                "Instance settings saved."
            )
            assert admin_page.evaluate("window.__cspViolations") == []

            # A visitor registers through the emailed link.
            visitor = browser.new_context(color_scheme="dark")
            visitor.add_init_script(VIOLATION_RECORDER)
            page = visitor.new_page()
            page.goto(f"{base}/login", wait_until="networkidle")
            page.get_by_role("link", name="Create an account").click()
            page.wait_for_url(f"{base}/register")
            page.get_by_label("Email").fill("nina@example.com")
            page.get_by_role("button", name="Send the link").click()
            expect(page.locator("[data-email-notice]")).to_contain_text("we sent it a link")
            page.goto(_last_link(app, mailer, base, "/register/complete/"))
            expect(page.locator("[data-register-email]")).to_contain_text("nina@example.com")
            page.get_by_label("Display name").fill("Nina")
            page.get_by_label("Password", exact=True).fill(PASSWORD)
            page.get_by_role("button", name="Create account").click()
            page.wait_for_url(f"{base}/")
            expect(page.locator("[data-current-workspace]")).to_have_text("Nina")
            assert page.evaluate("window.__cspViolations") == []

            # Another browser resets Nina's password; her first session ends.
            other = browser.new_context(color_scheme="light")
            other.add_init_script(VIOLATION_RECORDER)
            reset = other.new_page()
            reset.goto(f"{base}/login", wait_until="networkidle")
            reset.get_by_role("link", name="Forgot your password?").click()
            reset.wait_for_url(f"{base}/password/forgot")
            reset.get_by_label("Email").fill("nina@example.com")
            reset.get_by_role("button", name="Send the link").click()
            expect(reset.locator("[data-email-notice]")).to_contain_text("If an account uses")
            reset.goto(_last_link(app, mailer, base, "/password/reset/"))
            reset.get_by_label("New password", exact=True).fill(NEW_PASSWORD)
            reset.get_by_label("Repeat the new password").fill(NEW_PASSWORD)
            reset.get_by_role("button", name="Change password").click()
            expect(reset.locator("[data-email-notice]")).to_contain_text("password was changed")
            _sign_in(reset, base, "nina@example.com", NEW_PASSWORD)
            reset.wait_for_url(f"{base}/")
            assert reset.evaluate("window.__cspViolations") == []
            page.goto(f"{base}/", wait_until="networkidle")
            assert page.url.startswith(f"{base}/login")
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
