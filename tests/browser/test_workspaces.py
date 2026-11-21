"""Self-service workspaces in Chromium (pre-release A): an account without a workspace creates
one from the "No workspace" page, and an administrator turns creation off. No CSP violations."""

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
from assetboard.db.models.identity import User
from assetboard.services.auth import argon2_hash, run_initial_setup

pytestmark = pytest.mark.skipif(
    os.environ.get("ASSETBOARD_RUN_BROWSER_TESTS") != "1",
    reason="Set ASSETBOARD_RUN_BROWSER_TESTS=1 to run the Chromium tests",
)

PASSWORD = "Correct Horse Battery!"
VIOLATION_RECORDER = """
window.__cspViolations = [];
document.addEventListener("securitypolicyviolation", (event) => {
  window.__cspViolations.push(event.violatedDirective + " " + (event.blockedURI || "inline"));
});
"""


def _sign_in(page: Page, base: str, email: str) -> None:
    page.goto(f"{base}/login", wait_until="networkidle")
    page.get_by_label("Email").fill(email)
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()


def test_self_service_workspaces_in_a_browser(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'browser.db'}",
            secret_key="test-key-5d364b22dfd4d0ff6f63873",
        )
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
        with app.state.session_factory.begin() as session:
            session.add(
                User(
                    email="bea@example.com", display_name="Bea", password_hash=argon2_hash(PASSWORD)
                )
            )
        base = f"http://127.0.0.1:{port}"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()

            # Bea has no workspace: the 403 page offers to create one.
            bea = browser.new_context(color_scheme="light", timezone_id="America/New_York")
            bea.add_init_script(VIOLATION_RECORDER)
            page = bea.new_page()
            _sign_in(page, base, "bea@example.com")
            expect(page.locator("[data-no-workspace]")).to_be_visible()
            page.locator("[data-create-workspace]").click()
            page.wait_for_url(f"{base}/workspaces/new")
            expect(page.get_by_label("Workspace name")).to_have_value("Bea")
            page.get_by_label("Workspace name").fill("Bea's budget")
            # W-1: a fresh form seeds the time zone from the browser.
            expect(page.get_by_label("Time zone")).to_have_value("America/New_York")
            # The country picks the base currency; a blank country keeps the previous value.
            page.get_by_label("Country").select_option("PL")
            expect(page.get_by_label("Base currency")).to_have_value("PLN")
            page.get_by_label("Country").select_option("US")
            expect(page.get_by_label("Base currency")).to_have_value("USD")
            page.get_by_label("Country").select_option("")
            expect(page.get_by_label("Base currency")).to_have_value("USD")
            # A time zone the user chose survives a later country change.
            page.get_by_label("Time zone").select_option("Europe/Warsaw")
            page.get_by_label("Country").select_option("PL")
            expect(page.get_by_label("Base currency")).to_have_value("PLN")
            expect(page.get_by_label("Time zone")).to_have_value("Europe/Warsaw")
            # The recorder lives on this document: check it before the form navigates away.
            assert page.evaluate("window.__cspViolations") == []
            page.get_by_role("button", name="Create workspace").click()
            page.wait_for_url(f"{base}/")
            expect(page.locator("[data-current-workspace]")).to_have_text("Bea's budget")
            page.locator("[data-user-menu] summary").click()  # Categories sit in the user menu
            page.locator("[data-categories-link]").click()
            page.wait_for_url(f"{base}/categories")
            expense = page.locator('[data-section="expense"] [data-category-name]')
            expect(expense.first).to_have_text("Food")
            # The chosen defaults were stored with the new workspace.
            page.goto(f"{base}/workspace/settings", wait_until="networkidle")
            expect(page.get_by_label("Base currency")).to_have_value("PLN")
            expect(page.get_by_label("Time zone")).to_have_value("Europe/Warsaw")
            assert page.evaluate("window.__cspViolations") == []

            # The administrator turns self-service creation off.
            admin = browser.new_context(color_scheme="light")
            admin.add_init_script(VIOLATION_RECORDER)
            admin_page = admin.new_page()
            _sign_in(admin_page, base, "admin@example.com")
            admin_page.wait_for_url(f"{base}/")
            admin_page.goto(f"{base}/admin/settings", wait_until="networkidle")
            admin_page.get_by_label("Who can create workspaces").select_option("off")
            admin_page.get_by_label("Your password").fill(PASSWORD)
            admin_page.get_by_role("button", name="Save settings").click()
            expect(admin_page.locator("#toast-container [data-toast]")).to_contain_text(
                "Instance settings saved."
            )
            assert admin_page.evaluate("window.__cspViolations") == []

            # Bea no longer sees the offer.
            page.goto(f"{base}/", wait_until="networkidle")
            expect(page.locator("[data-new-workspace-link]")).to_have_count(0)
            assert page.evaluate("window.__cspViolations") == []
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
