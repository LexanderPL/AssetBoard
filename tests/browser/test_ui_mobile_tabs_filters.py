"""Mobile tabs and filter disclosures in Chromium: both tab strips are one row of navigation
links with the current page marked by aria-current, the strips scroll sideways on a phone with
the current tab in view, and the Investments/Audit/Performance filters are disclosures that start
collapsed at 390 px and open at 1440 px. No page scrolls sideways and there are no CSP
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


def _no_page_overflow(page: Page) -> None:
    sizes = page.evaluate(
        """() => ({
          document: document.documentElement.scrollWidth,
          body: document.body.scrollWidth,
          viewport: window.innerWidth,
        })"""
    )
    assert sizes["document"] <= sizes["viewport"] + 1, sizes
    assert sizes["body"] <= sizes["viewport"] + 1, sizes


def _one_tab_row(page: Page, strip: str, active: str) -> None:
    tops = page.eval_on_selector(
        strip,
        """(strip) => Array.from(strip.querySelectorAll(":scope > a"))
          .map((link) => Math.round(link.getBoundingClientRect().top))""",
    )
    assert len(tops) >= 2
    assert len(set(tops)) == 1, tops
    visible = page.evaluate(
        """([stripSelector, activeSelector]) => {
          const stripBox = document.querySelector(stripSelector).getBoundingClientRect();
          const activeBox = document.querySelector(activeSelector).getBoundingClientRect();
          return activeBox.left >= stripBox.left - 1 && activeBox.right <= stripBox.right + 1;
        }""",
        [strip, active],
    )
    assert visible, (strip, active)
    _no_page_overflow(page)


def _open_filters(page: Page, form: str) -> None:
    details = page.locator("details[data-filter-disclosure]")
    expect(details).to_have_count(1)
    expect(details).to_have_attribute("open", "")
    expect(details.locator("summary")).to_be_visible()
    expect(details.locator(form)).to_be_visible()


def _collapsed_filters(page: Page, form: str) -> None:
    details = page.locator("details[data-filter-disclosure]")
    expect(details).to_have_count(1)
    expect(details).not_to_have_attribute("open", "")
    expect(details.locator("summary")).to_be_visible()
    expect(details.locator(form)).to_be_hidden()


def test_tabs_and_filters_at_desktop_and_phone_widths(tmp_path: Path) -> None:
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
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            context.add_init_script(VIOLATION_RECORDER)
            page = context.new_page()
            page.goto(f"{base}/login", wait_until="networkidle")
            page.get_by_label("Email").fill("admin@example.com")
            page.get_by_label("Password").fill(PASSWORD)
            page.get_by_role("button", name="Sign in").click()
            page.wait_for_url(f"{base}/")

            # Desktop: the tab strips are one row and the filters are open.
            page.goto(f"{base}/investments/transactions", wait_until="networkidle")
            _one_tab_row(page, "[data-tab-strip]", "[data-investment-transactions-tab]")
            expect(page.locator("[data-investment-transactions-tab]")).to_have_attribute(
                "aria-current", "page"
            )
            expect(page.locator("[data-tab-strip]")).to_have_attribute("aria-label", "Investments")
            _open_filters(page, "[data-transaction-filters]")
            history = page.locator("[data-transaction-history]").bounding_box()
            assert history is not None and history["y"] < 900

            page.goto(f"{base}/audit?scope=workspace", wait_until="networkidle")
            _open_filters(page, "[data-audit-filters]")
            assert page.locator("[data-audit-rows]").count() == 1

            page.goto(f"{base}/net-worth", wait_until="networkidle")
            _one_tab_row(page, "[data-tab-strip]", '[data-side-tab="asset"]')
            expect(page.locator("[data-tab-strip]")).to_have_attribute("aria-label", "Net worth")
            expect(page.locator('[data-side-tab="asset"]')).to_have_attribute(
                "aria-current", "page"
            )

            page.goto(f"{base}/investments/performance", wait_until="networkidle")
            _open_filters(page, "[data-performance-class]")
            expect(page.locator("[data-performance-range]")).to_be_visible()

            # Phone: the same strips stay on one row and scroll to the current tab; the filters
            # start collapsed, so the data area follows the visible summary in the first screen.
            page.set_viewport_size({"width": 390, "height": 844})
            page.goto(f"{base}/investments/transactions", wait_until="networkidle")
            _one_tab_row(page, "[data-tab-strip]", "[data-investment-transactions-tab]")
            strip_widths = page.eval_on_selector(
                "[data-tab-strip]",
                "(strip) => [strip.clientWidth, strip.scrollWidth, strip.scrollLeft]",
            )
            assert strip_widths[1] > strip_widths[0], strip_widths
            assert strip_widths[2] > 0, strip_widths
            _collapsed_filters(page, "[data-transaction-filters]")
            history = page.locator("[data-transaction-history]").bounding_box()
            assert history is not None and history["y"] < 844

            page.goto(f"{base}/audit?scope=workspace", wait_until="networkidle")
            _collapsed_filters(page, "[data-audit-filters]")
            rows = page.locator("[data-audit-rows]").bounding_box()
            assert rows is not None and rows["y"] < 844

            page.goto(f"{base}/net-worth", wait_until="networkidle")
            _one_tab_row(page, "[data-tab-strip]", '[data-side-tab="asset"]')

            page.goto(f"{base}/investments/performance", wait_until="networkidle")
            _collapsed_filters(page, "[data-performance-class]")
            expect(page.locator("[data-performance-range]")).to_be_visible()

            assert page.evaluate("window.__cspViolations") == []
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
