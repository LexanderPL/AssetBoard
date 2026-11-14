"""F8.3 results in Chromium: charts, live preview, basis toggle and save (opt-in)."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from uuid import UUID

import pytest
from playwright.sync_api import Page, expect, sync_playwright

from assetboard.app import create_app
from assetboard.config import Settings
from assetboard.db.models.fire import FireScenario
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


def test_results_draw_preview_without_navigation_and_save(tmp_path: Path) -> None:
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

            page.goto(f"{base}/fire/new", wait_until="networkidle")
            page.get_by_label("Scenario name").fill("Results plan")
            page.get_by_label("Birth year").fill(str(start_year - 41))
            page.get_by_label("Horizon age").fill("90")
            page.get_by_label("Income stops at age").fill("55")
            page.get_by_label("Annual income").fill("50000")
            page.get_by_label("Annual expenses").fill("30000")
            page.get_by_label("Starting portfolio").fill("1000000")
            with page.expect_navigation(wait_until="networkidle"):
                page.locator("[data-fire-save]").click()

            results_url = page.url
            assert "/fire/" in results_url
            expect(
                page.locator('[data-fire-chart="net-worth"] [data-chart="line"]')
            ).to_have_attribute("data-chart-ready", "true")
            expect(
                page.locator('[data-fire-chart="expenses-gain"] [data-chart="line"]')
            ).to_have_attribute("data-chart-ready", "true")
            assert page.locator("[data-fire-chart] canvas").count() == 2
            expect(page.locator("[data-fire-metrics]")).to_be_visible()
            expect(page.locator("[data-fire-table]")).to_be_visible()

            # A preview updates the results in place, keeps the focus in the edited input and
            # never navigates or writes.
            page.locator("#inputs summary").click()
            expenses = page.get_by_label("Annual expenses")
            expenses.fill("80000")
            expect(page.locator("[data-fire-preview-note]")).to_be_visible()
            assert page.url == results_url
            assert page.evaluate("document.activeElement && document.activeElement.name") == (
                "expenses_manual"
            )
            expect(page.locator("[data-fire-fire-age]")).to_have_attribute(
                "data-fire-fire-age-value", "43"
            )

            # Reloading still shows the saved amount: the preview did not persist.
            page.reload(wait_until="networkidle")
            assert page.get_by_label("Annual expenses").input_value() == "30000.00"

            # Type an unsaved change, then switch the basis. The basis control belongs to the
            # form, so the preview keeps the typed value and never navigates; reloading still
            # shows the saved amount (the preview wrote nothing).
            page.locator("#inputs summary").click()
            expenses = page.get_by_label("Annual expenses")
            expenses.fill("70000")
            expect(page.locator("[data-fire-preview-note]")).to_be_visible()
            expect(page.locator("[data-fire-fire-age]")).to_have_attribute(
                "data-fire-fire-age-value", "41"
            )
            page.locator('label:has([data-fire-basis="real"])').click()
            assert page.url == results_url
            expect(page.locator("[data-fire-table]")).to_have_attribute("data-fire-basis", "real")
            expect(page.locator("[data-fire-preview-note]")).to_be_visible()
            assert page.get_by_label("Annual expenses").input_value() == "70000"
            expect(page.locator("[data-fire-fire-age]")).to_have_attribute(
                "data-fire-fire-age-value", "41"
            )
            expect(
                page.locator('[data-fire-chart="net-worth"] [data-chart="line"]')
            ).to_have_attribute("data-chart-ready", "true")
            page.reload(wait_until="networkidle")
            assert page.get_by_label("Annual expenses").input_value() == "30000.00"
            expect(page.locator("[data-fire-table]")).to_have_attribute(
                "data-fire-basis", "nominal"
            )

            # Saving persists the previewed amount and keeps the chosen basis in the redirect.
            page.locator("#inputs summary").click()
            page.get_by_label("Annual expenses").fill("80000")
            expect(page.locator("[data-fire-preview-note]")).to_be_visible()
            page.locator('label:has([data-fire-basis="real"])').click()
            expect(page.locator("[data-fire-table]")).to_have_attribute("data-fire-basis", "real")
            with page.expect_navigation(wait_until="networkidle"):
                page.locator("[data-fire-save]").click()
            expect(page.locator("[data-action-notice]")).to_be_visible()
            assert "basis=real" in page.url
            page.reload(wait_until="networkidle")
            assert page.get_by_label("Annual expenses").input_value() == "80000.00"
            expect(page.locator("[data-fire-table]")).to_have_attribute("data-fire-basis", "real")

            # A never-FIRE, depleted plan explains both states in the preview fragment.
            page.locator("#inputs summary").click()
            page.get_by_label("Real return (%)").fill("0")
            page.get_by_label("Inflation (%)").fill("0")
            page.get_by_label("Income growth (%)").fill("0")
            page.get_by_label("Income stops at age").fill("41")
            page.get_by_label("Annual income").fill("0")
            page.get_by_label("Annual expenses").fill("30000")
            page.get_by_label("Starting portfolio").fill("100000")
            expect(page.locator("[data-fire-never]")).to_be_visible()
            expect(page.locator("[data-fire-depletion]")).to_be_visible()
            assert "basis=real" in page.url

            # A saved plan can be blocked (a linked value with no ledger data, or a defensive
            # invalid plan). The page still boots the chart scripts so a later preview can draw
            # the charts that were absent when the blocked page loaded.
            scenario_id = UUID(page.url.split("/fire/", 1)[1].split("?")[0].split("#")[0])
            with app.state.session_factory() as session, session.begin():
                row = session.get_one(FireScenario, scenario_id)
                row.income_mode = "trailing_12m"
                row.income_manual_minor = None
            page.reload(wait_until="networkidle")
            expect(page.locator("[data-fire-blocked]")).to_be_visible()
            assert page.locator("[data-chart]").count() == 0
            assert page.locator("script[src*='charts.js']").count() == 1
            page.locator("#inputs summary").click()
            page.locator('select[name="income_mode"]').select_option("manual")
            page.get_by_label("Annual income").fill("50000")
            expect(
                page.locator('[data-fire-chart="net-worth"] [data-chart="line"]')
            ).to_have_attribute("data-chart-ready", "true")
            expect(
                page.locator('[data-fire-chart="expenses-gain"] [data-chart="line"]')
            ).to_have_attribute("data-chart-ready", "true")

            assert page.evaluate("window.__cspViolations") == []
            _no_page_overflow(page)
            browser.close()
