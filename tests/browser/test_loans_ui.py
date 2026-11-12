"""Loans in Chromium (F7.1): the original-terms wizard leaves the payment day to default from
the first payment date, the detail page keeps its actions under the tab strip, the rate-change
dialog saves, and the list/detail pages fit 390 and 1440 without sideways scrolling. No CSP
violations."""

from __future__ import annotations

import os
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


def _actions_under_the_tabs(page: Page) -> None:
    tabs = page.locator("[data-tab-strip]").bounding_box()
    actions = page.locator("[data-page-actions]").bounding_box()
    assert tabs is not None and actions is not None
    assert actions["y"] >= tabs["y"] + tabs["height"] - 1


def test_loan_wizard_and_detail_at_desktop_and_phone_widths(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'browser.db'}",
            secret_key="browser-test-key",
        )
    )
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
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            context.add_init_script(VIOLATION_RECORDER)
            page = context.new_page()
            page.goto(f"{base}/login", wait_until="networkidle")
            page.get_by_label("Email").fill("admin@example.com")
            page.get_by_label("Password").fill(PASSWORD)
            page.get_by_role("button", name="Sign in").click()
            page.wait_for_url(f"{base}/")

            page.goto(f"{base}/loans", wait_until="networkidle")
            page.locator("[data-new-loan]").click()
            page.wait_for_url(f"{base}/loans/new")

            page.get_by_label("Name", exact=True).fill("Home mortgage")
            page.get_by_role("button", name="Next").click()
            page.wait_for_load_state("networkidle")
            page.get_by_label("Original principal", exact=False).fill("300,000")
            page.get_by_label("Origination date").fill("2026-01-01")
            page.get_by_label("Term in months").fill("360")
            page.get_by_label("First payment date", exact=True).fill("2026-02-15")
            # Payment day stays empty: it must default to the first payment date's day.
            page.get_by_role("button", name="Next").click()
            page.wait_for_load_state("networkidle")
            page.get_by_label("Annual interest rate", exact=False).fill("5.5")
            page.get_by_role("button", name="Next").click()
            page.wait_for_load_state("networkidle")
            page.get_by_role("button", name="Save loan").click()

            expect(page.locator("#loan-summary-heading")).to_have_text("Home mortgage")
            expect(page.locator("dt", has_text="First payment")).to_be_visible()
            expect(page.locator("dt", has_text="First payment").locator("..")).to_contain_text(
                "day 15"
            )
            _no_page_overflow(page)
            _actions_under_the_tabs(page)
            expect(page.locator("[data-edit-terms]")).to_be_visible()

            page.get_by_role("link", name="Add rate change").click()
            dialog = page.locator("#rate-dialog")
            expect(dialog).to_be_visible()
            dialog.get_by_label("Applies from").fill("2027-01-01")
            dialog.get_by_label("Annual rate").fill("6")
            dialog.get_by_role("button", name="Save rate change").click()
            page.wait_for_load_state("networkidle")
            expect(page.locator("[data-rate-change]")).to_have_count(2)
            expect(page.locator("[data-action-notice]")).to_contain_text("Rate change saved.")
            _no_page_overflow(page)

            page.set_viewport_size({"width": 390, "height": 844})
            page.goto(f"{base}/loans", wait_until="networkidle")
            expect(page.locator("[data-loan]")).to_have_count(1)
            _no_page_overflow(page)

            page.locator("[data-open-loan]").click()
            page.wait_for_load_state("networkidle")
            expect(page.locator("#loan-summary-heading")).to_have_text("Home mortgage")
            _no_page_overflow(page)
            _actions_under_the_tabs(page)
            expect(page.get_by_role("link", name="Back to Loans")).to_be_visible()
            expect(page.get_by_role("link", name="Edit terms")).to_be_visible()

            page.get_by_role("link", name="Add cost").click()
            cost_dialog = page.locator("#cost-dialog")
            expect(cost_dialog).to_be_visible()
            expect(cost_dialog.get_by_label("Name", exact=True)).to_be_visible()
            _no_page_overflow(page)
            cost_dialog.get_by_role("link", name="Cancel").click()
            expect(cost_dialog).to_be_hidden()

            assert page.evaluate("window.__cspViolations") == []
            browser.close()
