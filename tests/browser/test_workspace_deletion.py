"""Deleting workspaces in Chromium (ADR-0020): from the settings page's danger zone, through the
typed-name confirmation, to another workspace and then to the "No workspace" page. No CSP
violations."""

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


def _delete_current(page: Page, base: str, name: str) -> None:
    page.goto(f"{base}/workspace/settings", wait_until="networkidle")
    page.locator("[data-delete-workspace-link]").click()
    page.wait_for_url(f"{base}/workspace/delete")
    expect(page.locator("[data-export-link]")).to_be_visible()
    field = page.get_by_label("To confirm, type the workspace name")
    field.fill(name.lower())
    page.get_by_role("button", name="Delete this workspace").click()
    expect(page.locator("[data-delete-error]")).to_be_visible()
    page.get_by_label("To confirm, type the workspace name").fill(name)
    page.get_by_role("button", name="Delete this workspace").click()
    page.wait_for_url(f"{base}/")


def test_deleting_workspaces_in_a_browser(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'browser.db'}",
            secret_key="browser-test-key",
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
        base = f"http://127.0.0.1:{port}"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(color_scheme="dark")
            context.add_init_script(VIOLATION_RECORDER)
            page = context.new_page()
            page.goto(f"{base}/login", wait_until="networkidle")
            page.get_by_label("Email").fill("admin@example.com")
            page.get_by_label("Password").fill(PASSWORD)
            page.get_by_role("button", name="Sign in").click()
            page.wait_for_url(f"{base}/")

            # A second workspace; creating it selects it.
            page.goto(f"{base}/workspaces/new", wait_until="networkidle")
            page.get_by_label("Workspace name").fill("Second")
            page.get_by_role("button", name="Create workspace").click()
            page.wait_for_url(f"{base}/")
            expect(page.locator("[data-current-workspace]")).to_have_text("Second")

            _delete_current(page, base, "Second")
            expect(page.locator("[data-current-workspace]")).to_have_text("Browser Home")
            assert page.evaluate("window.__cspViolations") == []

            _delete_current(page, base, "Browser Home")
            expect(page.locator("[data-no-workspace]")).to_be_visible()
            expect(page.locator("[data-create-workspace]")).to_be_visible()
            assert page.evaluate("window.__cspViolations") == []
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
