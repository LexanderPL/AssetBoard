"""F11.10 in Chromium: chart edge labels, the empty Home widget and the FIRE table collapse."""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from playwright.sync_api import Page, expect, sync_playwright
from sqlalchemy import select

from assetboard.app import create_app
from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.db.migrate import upgrade_database
from assetboard.db.models.identity import User
from assetboard.services.auth import run_initial_setup

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
NOW = "2026-11-20T12:00:00+00:00"


@contextmanager
def _server(tmp_path: Path, *, demo: bool) -> Iterator[tuple[FastAPI, str]]:
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'browser.db'}",
            secret_key="test-key-5d364b22dfd4d0ff6f63873",
            demo_enabled=demo,
            demo_timezone="UTC",
            demo_reset_time="03:00",
        )
    )
    clock = FixedClock(NOW)
    app.state.clock = clock
    app.state.session_factory.configure(info={"clock": clock})
    upgrade_database(app.state.settings)
    run_initial_setup(
        app.state.session_factory,
        email="admin@example.com",
        display_name="Browser Admin",
        password=PASSWORD,
        workspace_name="Browser Home",
        settings=app.state.settings,
        clock=clock,
    )
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        threading.Event().wait(0.01)
    assert server.started
    try:
        yield app, f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _sign_in(page: Page, base: str) -> None:
    page.goto(f"{base}/login", wait_until="networkidle")
    page.get_by_label("Email").fill("admin@example.com")
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(f"{base}/")


def _sign_in_demo(page: Page, base: str) -> None:
    page.goto(f"{base}/login", wait_until="networkidle")
    page.get_by_role("button", name="Try the demo").click()
    page.wait_for_url(f"{base}/")


def _wait_for_chart(page: Page, selector: str) -> None:
    page.wait_for_function(
        "selector => document.querySelector(selector + '[data-chart-ready=\"true\"]') !== null",
        arg=selector,
    )


def _last_label_box(page: Page, selector: str) -> dict[str, object]:
    """The last category label's rendered box, measured on the ECharts canvas."""
    return page.locator(selector).evaluate(
        """(element) => {
          const canvas = element.querySelector("[data-chart-canvas]");
          if (!canvas) return {found: false, reason: "no canvas"};
          const chart = window.echarts.getInstanceByDom(canvas);
          if (!chart) return {found: false, reason: "no chart"};
          const option = chart.getOption();
          const axis = Array.isArray(option.xAxis) ? option.xAxis[0] : option.xAxis;
          const labels = axis && Array.isArray(axis.data) ? axis.data : [];
          const last = labels[labels.length - 1];
          if (!last) return {found: false, reason: "no last label"};
          const tspans = chart.getZr().storage.getDisplayList().filter(
            (item) => item.type === "tspan" && item.style && item.style.text === last
          );
          if (!tspans.length) return {found: false, reason: "label not drawn", last};
          const item = tspans[tspans.length - 1];
          const rect = item.getBoundingRect();
          const transform = item.transform || [1, 0, 0, 1, 0, 0];
          const left = transform[4] + rect.x;
          const right = left + rect.width;
          const width = canvas.getBoundingClientRect().width;
          return {
            found: true,
            last,
            left,
            right,
            width,
            inside: left >= 0 && right <= width,
          };
        }"""
    )


def _assert_last_label_inside(page: Page, selector: str) -> None:
    _wait_for_chart(page, selector)
    box = _last_label_box(page, selector)
    assert box["found"], box
    assert box["inside"], box


def test_empty_home_does_not_claim_nothing_needs_attention(tmp_path: Path) -> None:
    with _server(tmp_path, demo=False) as (_app, base), sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(color_scheme="light", viewport={"width": 1440, "height": 900})
        context.add_init_script(VIOLATION_RECORDER)
        page = context.new_page()
        _sign_in(page, base)
        widget = page.locator('[data-home-widget="missing-data"]')
        expect(widget).to_be_visible()
        expect(widget.locator("[data-empty-setup]")).to_be_visible()
        expect(widget).to_contain_text("Add a net-worth value, an income source or an investment")
        expect(page.get_by_text("Nothing needs attention.")).to_have_count(0)
        assert page.evaluate("window.__cspViolations") == []
        browser.close()


def test_chart_labels_fit_and_the_fire_table_collapses(tmp_path: Path) -> None:
    with _server(tmp_path, demo=True) as (app, base), sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for locale, width in (("en", 1440), ("en", 390), ("pl", 1440), ("pl", 390)):
            with app.state.session_factory.begin() as session:
                user = session.scalars(select(User).where(User.is_demo.is_(True))).one()
                user.locale = locale
            context = browser.new_context(
                color_scheme="light", viewport={"width": width, "height": 900}
            )
            context.add_init_script(VIOLATION_RECORDER)
            page = context.new_page()
            _sign_in_demo(page, base)

            _assert_last_label_inside(page, '[data-home-widget="net-worth"] [data-chart="line"]')
            page.goto(f"{base}/net-worth/summary?range=1y", wait_until="networkidle")
            _assert_last_label_inside(page, '#net-worth-trend [data-chart="line"]')
            page.goto(f"{base}/reports", wait_until="networkidle")
            _assert_last_label_inside(page, '[data-report-series="net-worth"] [data-chart="line"]')
            assert page.evaluate("window.__cspViolations") == []
            context.close()

        context = browser.new_context(color_scheme="light", viewport={"width": 1440, "height": 900})
        context.add_init_script(VIOLATION_RECORDER)
        page = context.new_page()
        with app.state.session_factory.begin() as session:
            user = session.scalars(select(User).where(User.is_demo.is_(True))).one()
            user.locale = "en"
        _sign_in_demo(page, base)
        page.goto(f"{base}/fire", wait_until="networkidle")
        section = page.locator("[data-fire-table-section]")
        rows = section.locator("[data-fire-table] tbody tr")
        assert rows.count() > 12
        expect(rows.nth(11)).to_be_visible()
        expect(rows.nth(12)).to_be_hidden()
        button = section.locator("[data-fire-show-all]")
        expect(button).to_be_visible()
        expect(button).to_have_attribute("aria-expanded", "false")
        expect(section).to_contain_text("Showing the first 12 of")
        button.click()
        expect(rows.nth(12)).to_be_visible()
        expect(button).to_have_attribute("aria-expanded", "true")
        expect(button).to_have_text("Show fewer years")
        expect(section.locator("[data-fire-table-note]")).to_be_hidden()
        button.click()
        expect(rows.nth(12)).to_be_hidden()
        expect(button).to_have_attribute("aria-expanded", "false")
        expect(section.locator("[data-fire-table-note]")).to_be_visible()

        # Without JavaScript every year is visible, and the collapse control is not shown.
        no_js = browser.new_context(
            color_scheme="light",
            viewport={"width": 1440, "height": 900},
            java_script_enabled=False,
            storage_state=context.storage_state(),
        )
        no_js_page = no_js.new_page()
        no_js_page.goto(f"{base}/fire", wait_until="networkidle")
        no_js_section = no_js_page.locator("[data-fire-table-section]")
        expect(no_js_section.locator("[data-fire-table] tbody tr").nth(12)).to_be_visible()
        expect(no_js_section.locator("[data-fire-table-more]")).to_be_hidden()
        expect(no_js_section.locator("[data-fire-show-all]")).to_be_hidden()
        expect(no_js_section.locator("[data-fire-table-note]")).to_be_hidden()
        no_js.close()

        assert page.evaluate("window.__cspViolations") == []
        browser.close()
