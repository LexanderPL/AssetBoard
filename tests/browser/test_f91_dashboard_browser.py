"""F9.1 in Chromium: lazy Home widgets, Customise, phone width and the no-JS fallback."""

from __future__ import annotations

import os
import re
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import Page, expect, sync_playwright
from sqlalchemy import select

from assetboard.app import create_app
from assetboard.config import Settings
from assetboard.db.models.dashboard import DashboardPreference

pytestmark = pytest.mark.skipif(
    os.environ.get("ASSETBOARD_RUN_BROWSER_TESTS") != "1",
    reason="Set ASSETBOARD_RUN_BROWSER_TESTS=1 to run the Chromium tests",
)

VIOLATION_RECORDER = """
window.__cspViolations = [];
document.addEventListener("securitypolicyviolation", (event) => {
  window.__cspViolations.push(event.violatedDirective + " " + (event.blockedURI || "inline"));
});
"""
PASSWORD = "Correct Horse Battery!"


def _start(tmp_path: Path):
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
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        threading.Event().wait(0.01)
    assert server.started
    return app, server, thread, port


def _setup(page: Page, base: str, app) -> None:
    page.goto(f"{base}/", wait_until="networkidle")
    page.get_by_label("Setup token").fill(app.state.setup_token)
    page.get_by_label("Email").fill("admin@example.com")
    page.get_by_label("Display name").fill("Browser Admin")
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_label("Workspace name").fill("Browser Home")
    page.get_by_role("button", name="Create administrator").click()
    page.wait_for_url(f"{base}/")


def _home_order(page: Page) -> list[str]:
    return page.locator("[data-widget]").evaluate_all(
        "els => els.map(el => el.getAttribute('data-widget'))"
    )


def _customise_order(page: Page) -> list[str]:
    return page.locator("[data-customise-widget]").evaluate_all(
        "els => els.map(el => el.getAttribute('data-customise-widget'))"
    )


def _stored_preferences(app) -> DashboardPreference | None:
    with app.state.session_factory() as session:
        return session.scalars(select(DashboardPreference)).first()


def _actions_are_inline(page: Page) -> None:
    save = page.locator("[data-customise-save]").bounding_box()
    reset = page.locator("[data-customise-reset]").bounding_box()
    assert save is not None and reset is not None
    assert abs(save["y"] - reset["y"]) <= 1, (save, reset)
    assert save["x"] < reset["x"], (save, reset)


def test_dashboard_in_a_browser(tmp_path: Path) -> None:
    app, server, thread, port = _start(tmp_path)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(
                color_scheme="light", viewport={"width": 1440, "height": 900}
            )
            context.add_init_script(VIOLATION_RECORDER)
            page = context.new_page()
            base = f"http://127.0.0.1:{port}"
            _setup(page, base, app)

            # Every lazy widget loads into its placeholder, without a CSP violation.
            expect(page.locator("[data-widget='month-in-out'] [data-summary-kpis]")).to_have_count(
                1
            )
            expect(
                page.locator("[data-widget='month-expenses'] [data-donut='expense']")
            ).to_have_count(1)
            expect(
                page.locator("[data-widget='month-income'] [data-donut='income']")
            ).to_have_count(1)
            expect(page.locator("[data-chart-empty]")).to_have_count(2)
            expect(page.locator("[data-widget='upcoming']")).to_be_visible()
            expect(page.locator("[data-widget='missing-data']")).to_be_visible()
            assert page.evaluate("window.__cspViolations") == []

            # Desktop: the widget grid has two columns.
            columns = page.locator("[data-home-widgets]").evaluate(
                "el => getComputedStyle(el).gridTemplateColumns.split(' ').length"
            )
            assert columns >= 2

            # Phone: Home does not scroll sideways and Customise stays below the widgets.
            page.set_viewport_size({"width": 390, "height": 844})
            page.reload(wait_until="networkidle")
            expect(page.locator("[data-widget='month-in-out'] [data-summary-kpis]")).to_have_count(
                1
            )
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
            phone_columns = page.locator("[data-home-widgets]").evaluate(
                "el => getComputedStyle(el).gridTemplateColumns.split(' ').length"
            )
            assert phone_columns == 1
            widgets_box = page.locator("[data-home-widgets]").bounding_box()
            customise_box = page.locator("[data-customise-link]").bounding_box()
            assert widgets_box is not None and customise_box is not None
            assert customise_box["y"] >= widgets_box["y"] + widgets_box["height"] - 1

            # Customise: the Customise link is below the widgets, the arrows reorder rows
            # locally, and Save/Reset share one horizontal action row. No drag and drop.
            page.locator("[data-customise-link]").click()
            page.wait_for_url(f"{base}/dashboard/customise")
            expect(page.locator("[data-customise-widget]")).to_have_count(11)
            expect(page.locator("[data-customise-nojs-note]")).to_have_count(0)
            expect(page.locator("[data-customise-save]")).to_have_text("Save")
            expect(page.locator('[data-customise-move="loans:up"]')).to_have_attribute(
                "type", "button"
            )
            expect(page.locator('[data-customise-move="month-in-out:up"]')).to_be_disabled()
            expect(page.locator('[data-customise-move="missing-data:down"]')).to_be_disabled()
            _actions_are_inline(page)
            assert _stored_preferences(app) is None

            before = _customise_order(page)
            loans = page.locator("[data-customise-widget='loans']")
            loans.locator("button[value='loans:up']").focus()
            page.keyboard.press("Enter")
            assert page.url == f"{base}/dashboard/customise"
            expect(page.locator("[data-customise-widget='loans']")).to_be_visible()
            after = _customise_order(page)
            assert after != before and after.index("loans") < after.index("fire")
            assert page.evaluate("document.activeElement?.dataset?.customiseMove") == "loans:up"
            assert _stored_preferences(app) is None  # local until Save

            expenses = page.locator("[data-customise-widget='month-expenses']")
            expenses.locator("button[value='month-expenses:down']").click()
            assert page.url == f"{base}/dashboard/customise"
            expect(page.locator("[data-customise-widget='month-expenses']")).to_be_visible()
            moved_down = _customise_order(page)
            assert moved_down.index("month-expenses") > after.index("month-expenses")

            page.locator("#customise-month-expenses").uncheck()
            page.locator("[data-customise-upcoming-limit]").fill("3")
            page.locator("[data-customise-portfolio-range]").select_option("3m")
            page.locator("[data-customise-save]").click()
            page.wait_for_url(f"{base}/?saved=1")
            order = _home_order(page)
            assert "month-expenses" not in order and order.index("loans") < order.index("fire")
            stored = _stored_preferences(app)
            assert stored is not None
            assert stored.upcoming_limit == 3 and stored.portfolio_range == "3m"
            assert page.evaluate("window.__cspViolations") == []

            page.locator("[data-customise-link]").click()
            page.wait_for_url(f"{base}/dashboard/customise")
            expect(page.locator("[data-customise-upcoming-limit]")).to_have_value("3")
            expect(page.locator("[data-customise-portfolio-range]")).to_have_value("3m")
            _actions_are_inline(page)
            page.locator("[data-customise-reset]").click()
            page.wait_for_url(f"{base}/?reset=1")
            assert "month-expenses" in _home_order(page)
            assert _stored_preferences(app) is None

            # A failing fragment keeps a usable shell instead of a permanent "Loading…".
            page.evaluate(
                """() => {
                  const host = document.createElement("section");
                  host.setAttribute("data-widget", "failed-test");
                  host.setAttribute("aria-busy", "true");
                  host.innerHTML =
                    '<p data-widget-loading>Loading</p>' +
                    '<p data-widget-error-text>Could not load</p>' +
                    '<p data-widget-fallback><a href="/investments/portfolio">Open</a></p>';
                  document.querySelector("[data-home-widgets]").append(host);
                  host.dispatchEvent(new CustomEvent("htmx:responseError", {
                    detail: { elt: host, target: document.querySelector("#toast-container") },
                    bubbles: true,
                  }));
                }"""
            )
            failed = page.locator("[data-widget='failed-test'][data-widget-failed]")
            expect(failed).to_have_count(1)
            expect(failed.locator("[data-widget-fallback]")).to_be_visible()
            expect(failed.locator("[data-widget-loading]")).to_be_hidden()
            expect(failed.locator("[data-widget-error-text]")).to_be_visible()
            assert failed.get_attribute("aria-busy") is None

            # No JavaScript: the placeholder points at the module, and the plain-form picker
            # still saves a layout.
            nojs = browser.new_context(
                java_script_enabled=False,
                viewport={"width": 1440, "height": 900},
                storage_state=context.storage_state(),
            )
            nojs_page = nojs.new_page()
            nojs_page.goto(f"{base}/", wait_until="load")
            expect(
                nojs_page.locator("[data-widget='month-in-out'] [data-widget-fallback]")
            ).to_be_visible()
            nojs_page.locator("[data-customise-link]").click()
            nojs_page.wait_for_url(re.compile(r"/dashboard/customise$"))
            expect(nojs_page.locator("[data-customise-widget]")).to_have_count(11)
            expect(nojs_page.locator("[data-customise-nojs-note]")).to_be_visible()
            expect(nojs_page.locator('[data-customise-move="upcoming:up"]')).to_have_attribute(
                "type", "submit"
            )
            upcoming = nojs_page.locator("[data-customise-widget='upcoming']")
            upcoming.locator("button[value='upcoming:up']").click()
            expect(nojs_page.locator("[data-customise-widget='upcoming']")).to_be_visible()
            # Enter in a settings field uses the hidden default Save button, not the first
            # arrow, even without JavaScript.
            nojs_page.locator("#customise-upcoming-limit").fill("4")
            nojs_page.locator("#customise-upcoming-limit").press("Enter")
            nojs_page.wait_for_url(f"{base}/?saved=1")
            assert _stored_preferences(app).upcoming_limit == 4
            nojs_page.locator("[data-customise-link]").click()
            nojs_page.wait_for_url(re.compile(r"/dashboard/customise$"))
            nojs_page.locator("#customise-upcoming").uncheck()
            nojs_page.locator("[data-customise-save]").click()
            nojs_page.wait_for_url(f"{base}/?saved=1")
            assert "upcoming" not in _home_order(nojs_page)
            nojs.close()
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
