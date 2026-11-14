"""F8.5 comparison and sensitivity in Chromium: overlay chart, HTMX grid and CSP (opt-in)."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect, sync_playwright

from assetboard.app import create_app
from assetboard.config import Settings
from assetboard.services.auth import run_initial_setup
from tests.conftest import live_server

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


def _create_scenario(page: Page, base: str, name: str, start_year: int) -> str:
    page.goto(f"{base}/fire/new", wait_until="networkidle")
    page.get_by_label("Scenario name").fill(name)
    page.get_by_label("Birth year").fill(str(start_year - 41))
    page.get_by_label("Horizon age").fill("90")
    page.get_by_label("Income stops at age").fill("55")
    page.get_by_label("Annual income").fill("50000")
    page.get_by_label("Annual expenses").fill("30000")
    page.get_by_label("Starting portfolio").fill("1000000")
    with page.expect_navigation(wait_until="networkidle"):
        page.locator("[data-fire-save]").click()
    assert "/fire/" in page.url
    return page.url.split("/fire/", 1)[1].split("?")[0].split("#")[0]


def test_the_comparison_chart_and_on_demand_sensitivity_grid(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'browser.db'}",
            secret_key="browser-test-key",
        )
    )
    start_year = date.today().year
    with live_server(app) as base:
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
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            context.add_init_script(VIOLATION_RECORDER)
            page = context.new_page()
            page.goto(f"{base}/login", wait_until="networkidle")
            page.get_by_label("Email").fill("admin@example.com")
            page.get_by_label("Password").fill(PASSWORD)
            page.get_by_role("button", name="Sign in").click()
            page.wait_for_url(f"{base}/")

            first = _create_scenario(page, base, "Compare one", start_year)
            second = _create_scenario(page, base, "Compare two", start_year)

            page.goto(
                f"{base}/fire/compare?scenario={first}&scenario={second}",
                wait_until="networkidle",
            )
            expect(page.locator("[data-fire-comparison-table]")).to_be_visible()
            expect(page.locator("[data-fire-comparison-row]")).to_have_count(2)
            chart = page.locator("[data-fire-comparison-chart] [data-chart='line']")
            expect(chart).to_have_attribute("data-chart-ready", "true")
            series_count = page.evaluate(
                """() => JSON.parse(
                    document
                      .querySelector("[data-fire-comparison-chart] [data-chart='line']")
                      .getAttribute("data-chart-json")
                ).series.length"""
            )
            assert series_count == 2
            assert page.evaluate("window.__cspViolations") == []
            _no_page_overflow(page)

            # The grid loads through HTMX without leaving the results page.
            page.goto(f"{base}/fire/{first}", wait_until="networkidle")
            results_url = page.url
            page.locator("[data-fire-sensitivity-load]").click()
            grid = page.locator("[data-fire-sensitivity-grid]")
            expect(grid).to_be_visible()
            assert page.url == results_url
            expect(page.locator("[data-fire-sensitivity-cell]")).to_have_count(25)
            expect(page.locator("[data-fire-sensitivity-base]")).to_have_count(1)
            expect(page.locator('[data-fire-sensitivity-expense-factor="8000"]')).to_contain_text(
                "-20%"
            )
            assert page.evaluate("window.__cspViolations") == []
            _no_page_overflow(page)
            browser.close()
