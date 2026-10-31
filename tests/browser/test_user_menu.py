"""The top-right user menu in Chromium: it opens from the header, switches the workspace with a
POST form, closes on a press outside and on Escape, and works by keyboard. The header shows the
workspace name; the sidebar footer shows the version. A phone-width page wraps without
horizontal scrolling. No CSP violations."""

from __future__ import annotations

import os
import socket
import threading
import time
from importlib.metadata import version
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


def _box(page: Page, selector: str) -> dict[str, float]:
    box = page.locator(selector).first.bounding_box()
    assert box is not None, selector
    return {"x": box["x"], "y": box["y"], "width": box["width"], "height": box["height"]}


def test_the_user_menu_in_a_browser(tmp_path: Path) -> None:
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
            context = browser.new_context(
                color_scheme="light", viewport={"width": 1280, "height": 800}
            )
            context.add_init_script(VIOLATION_RECORDER)
            page = context.new_page()
            page.goto(f"{base}/login", wait_until="networkidle")
            page.get_by_label("Email").fill("admin@example.com")
            page.get_by_label("Password").fill(PASSWORD)
            page.get_by_role("button", name="Sign in").click()
            page.wait_for_url(f"{base}/")

            # Header: the workspace name above the title; the version in the sidebar footer.
            expect(page.locator("[data-current-workspace]")).to_have_text("Browser Home")
            expect(page.locator("[data-app-version]")).to_have_text(f"v{version('assetboard')}")
            expect(page.locator("[data-update-available]")).to_have_count(0)
            # The menu sits at the top right of the main area, level with the title.
            menu_box = _box(page, "[data-user-menu] summary")
            main_box = _box(page, "#main-content")
            title_box = _box(page, "h1")
            assert menu_box["x"] + menu_box["width"] > main_box["x"] + main_box["width"] * 0.75
            assert menu_box["y"] < title_box["y"] + title_box["height"]

            # A second workspace; creating it selects it.
            page.goto(f"{base}/workspaces/new", wait_until="networkidle")
            page.get_by_label("Workspace name").fill("Second")
            page.get_by_role("button", name="Create workspace").click()
            page.wait_for_url(f"{base}/")
            expect(page.locator("[data-current-workspace]")).to_have_text("Second")

            menu = page.locator("[data-user-menu] details[data-popover]")
            summary = menu.locator("summary")
            summary.click()
            expect(menu).to_have_attribute("open", "")
            expect(menu.locator("[data-workspace-choice-current]")).to_contain_text("Second")
            expect(menu.locator("[data-workspace-choice-current]")).to_have_attribute(
                "aria-current", "true"
            )
            expect(page.locator("[data-profile-link]")).to_be_in_viewport()
            # A press outside closes it.
            page.locator("h1").click()
            expect(menu).not_to_have_attribute("open", "")

            # Switch back from the dropdown.
            summary.click()
            menu.get_by_role("button", name="Browser Home").click()
            page.wait_for_url(f"{base}/")
            expect(page.locator("[data-current-workspace]")).to_have_text("Browser Home")
            expect(menu).not_to_have_attribute("open", "")

            # Keyboard: Enter on the summary opens it, Tab reaches the items, Escape closes it
            # and returns the focus to the summary.
            summary.focus()
            page.keyboard.press("Enter")
            expect(menu).to_have_attribute("open", "")
            page.keyboard.press("Tab")
            expect(menu.locator("[data-workspace-choice]").last).to_be_focused()
            page.keyboard.press("Escape")
            expect(menu).not_to_have_attribute("open", "")
            expect(summary).to_be_focused()
            assert page.evaluate("window.__cspViolations") == []

            # Phone width: the sidebar is a block above the content, the header wraps, the menu
            # stays reachable and fits the screen, nothing scrolls sideways.
            page.set_viewport_size({"width": 390, "height": 844})
            for path in ("/", "/history", "/admin/settings"):
                page.goto(f"{base}{path}", wait_until="networkidle")
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
                ), path
            summary.click()
            panel = _box(page, "[data-user-menu-panel]")
            assert panel["x"] >= 0 and panel["x"] + panel["width"] <= 390
            assert page.evaluate(
                "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
            )
            page.keyboard.press("Escape")
            sidebar = _box(page, "[data-sidebar]")
            main = _box(page, "#main-content")
            assert sidebar["y"] + sidebar["height"] <= main["y"] + 1
            assert page.evaluate("window.__cspViolations") == []
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
