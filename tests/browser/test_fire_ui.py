"""FIRE scenario form in Chromium: the row editors and both paste-column formats (F8.1)."""

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


def _values(page: Page, selector: str) -> list[str]:
    return page.locator(selector).evaluate_all("(nodes) => nodes.map((node) => node.value)")


def test_the_paste_editor_fills_bare_amounts_and_year_pairs(tmp_path: Path) -> None:
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
            page.get_by_label("Scenario name").fill("Browser plan")
            page.get_by_label("Birth year").fill(str(start_year - 40))
            page.get_by_label("Horizon age").fill("90")
            page.get_by_label("Income stops at age").fill("55")
            page.get_by_label("Annual income").fill("50000")
            page.get_by_label("Annual expenses").fill("30000")
            page.get_by_label("Starting portfolio").fill("1000000")

            # Bare amounts: the first two amounts fill consecutive years; a blank line skips one.
            page.locator("[data-contribution-paste]").fill("12000\n13000\n\n15000")
            page.locator("[data-paste-contributions]").click()
            years = _values(page, "[data-contribution-rows] [data-contribution-year]")
            amounts = _values(page, "[data-contribution-rows] [data-contribution-amount]")
            assert years == [
                str(start_year + 1),
                str(start_year + 2),
                str(start_year + 4),
            ]
            assert amounts == ["12000", "13000", "15000"]

            # Year and amount pairs: the same paste box accepts them in any order.
            page.locator("[data-contribution-paste]").fill(
                f"{start_year + 5}\t2500\n{start_year + 3}, 500"
            )
            page.locator("[data-paste-contributions]").click()
            years = _values(page, "[data-contribution-rows] [data-contribution-year]")
            amounts = _values(page, "[data-contribution-rows] [data-contribution-amount]")
            assert years == [str(start_year + 3), str(start_year + 5)]
            assert amounts == ["500", "2500"]

            # Rows can be added and removed; the empty event row is ignored on save.
            page.locator("[data-add-contribution]").click()
            expect(page.locator("[data-contribution-rows] tr[data-editor-row]")).to_have_count(3)
            page.locator("[data-contribution-rows] tr[data-editor-row]").last.locator(
                "[data-remove-row]"
            ).click()
            expect(page.locator("[data-contribution-rows] tr[data-editor-row]")).to_have_count(2)
            page.locator("[data-add-event]").click()
            expect(page.locator("[data-event-rows] tr[data-editor-row]")).to_have_count(2)
            page.locator("[data-event-rows] tr[data-editor-row]").last.locator(
                "[data-remove-row]"
            ).click()
            expect(page.locator("[data-event-rows] tr[data-editor-row]")).to_have_count(1)

            with page.expect_navigation(wait_until="networkidle"):
                page.locator("[data-fire-save]").click()
            if not page.url.startswith(f"{base}/fire/"):
                errors = page.locator("[data-action-error]").all_text_contents()
                assert not errors, errors
            assert page.url.startswith(f"{base}/fire/")
            # The combined page shows the saved overrides in the plan-inputs editor, where input
            # values are ungrouped; the editor keeps one empty row ready for a new contribution.
            amounts = _values(page, "[data-contribution-rows] [data-contribution-amount]")
            assert amounts[:2] == ["500.00", "2500.00"]
            expect(page.locator("[data-contribution-rows] tr[data-editor-row]")).to_have_count(3)
            assert page.evaluate("window.__cspViolations") == []
            _no_page_overflow(page)
