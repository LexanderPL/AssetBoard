"""Loans in Chromium (F7.1): the original-terms wizard leaves the payment day to default from
the first payment date, the detail page keeps its actions under the tab strip, the rate-change
dialog saves, and the list/detail pages fit 390 and 1440 without sideways scrolling. No CSP
violations."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

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
            expect(page.locator("[data-loan-overview]")).to_be_visible()
            expect(page.locator("[data-loan-schedule]")).to_be_visible()
            expect(page.locator('[data-chart="line"]')).to_have_attribute(
                "data-chart-ready", "true"
            )
            expect(page.locator('[data-chart="loan-payments"]')).to_have_attribute(
                "data-chart-ready", "true"
            )
            expect(page.locator("[data-loan-integrations]")).to_be_visible()
            page.get_by_role("button", name="Create net-worth item").click()
            page.wait_for_load_state("networkidle")
            expect(page.locator("[data-linked-item]")).to_be_visible()
            page.locator('input[type="checkbox"][name="enabled"]').check()
            page.get_by_role("button", name="Save setting").click()
            page.wait_for_load_state("networkidle")
            expect(page.locator("[data-action-notice]")).to_contain_text(
                "Automatic payment recording saved."
            )
            category_select = page.locator('[data-scheduled-generation] select[name="category_id"]')
            category_select.select_option(index=1)
            page.get_by_role("button", name="Generate occurrences").click()
            page.wait_for_load_state("networkidle")
            expect(page.get_by_role("button", name="Refresh occurrences")).to_be_visible()
            _no_page_overflow(page)
            loan_id = urlsplit(page.url).path.rstrip("/").split("/")[-1]
            csv = page.request.get(f"{base}/loans/{loan_id}/schedule.csv")
            assert csv.status == 200
            assert "text/csv" in csv.headers["content-type"]
            assert "attachment" in csv.headers["content-disposition"]
            assert csv.text().startswith(
                "n,date,rate,opening,interest,principal,overpayment,costs,total,closing,currency\r\n"
            )
            assert "\r\n" in csv.text()

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

            page.get_by_role("link", name="Scenarios").click()
            page.wait_for_url(f"{base}/loans/{loan_id}/scenarios")
            expect(page.locator("[data-scenario-comparison]")).to_be_visible()
            expect(page.locator("[data-scenario-chart]")).to_be_visible()
            expect(page.locator('[data-chart="line"]')).to_have_attribute(
                "data-chart-ready", "true"
            )
            _actions_under_the_tabs(page)
            _no_page_overflow(page)
            row = page.locator('[data-scenario-form-row="1"]')
            row.locator("[data-scenario-name]").fill("Extra 200")
            row.locator('select[name="overpayment_mode_1"]').select_option("fixed_extra")
            row.locator("[data-scenario-overpayment-amount]").fill("200")
            page.get_by_role("button", name="Compare and save named scenarios").click()
            page.wait_for_load_state("networkidle")
            expect(page.locator("[data-saved-scenario]")).to_have_count(1)
            expect(page.locator("[data-action-notice]")).to_contain_text("Saved scenarios updated")
            expect(page.locator('[data-chart="line"]')).to_have_attribute(
                "data-chart-ready", "true"
            )
            _no_page_overflow(page)

            page.goto(f"{base}/loans/{loan_id}/payments", wait_until="networkidle")
            expect(page.locator("[data-loan-payments-list]")).to_be_visible()
            expect(page.locator("[data-loan-checkpoints-list]")).to_be_visible()
            _actions_under_the_tabs(page)
            _no_page_overflow(page)
            page.get_by_role("link", name="Record payment").click()
            payment_dialog = page.locator("#payment-dialog")
            expect(payment_dialog).to_be_visible()
            payment_dialog.locator('input[name="date"]').fill("2026-02-15")
            payment_dialog.locator('input[name="total"]').fill("100")
            payment_dialog.get_by_role("button", name="Save payment").click()
            page.wait_for_load_state("networkidle")
            expect(page.locator("[data-action-notice]")).to_contain_text("Payment saved.")
            expect(page.locator("[data-payment]")).to_have_count(1)
            _no_page_overflow(page)
            page.goto(f"{base}/loans/{loan_id}?schedule_page=1", wait_until="networkidle")
            expect(page.locator("[data-schedule-actual]")).to_be_visible()
            expect(page.locator("[data-actual-total]")).to_be_visible()
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
            expect(page.locator("[data-loan-schedule]")).to_be_visible()
            expect(page.locator("[data-loan-integrations]")).to_be_visible()
            _no_page_overflow(page)
            scroll = page.locator("[data-schedule-scroll]").evaluate(
                "el => ({scrollWidth: el.scrollWidth, clientWidth: el.clientWidth})"
            )
            assert scroll["scrollWidth"] > scroll["clientWidth"]
            expect(page.locator('[data-chart="line"]')).to_have_attribute(
                "data-chart-ready", "true"
            )
            expect(page.locator('[data-chart="loan-payments"]')).to_have_attribute(
                "data-chart-ready", "true"
            )
            _no_page_overflow(page)

            page.get_by_role("link", name="Add cost").click()
            cost_dialog = page.locator("#cost-dialog")
            expect(cost_dialog).to_be_visible()
            expect(cost_dialog.get_by_label("Name", exact=True)).to_be_visible()
            _no_page_overflow(page)
            cost_dialog.get_by_role("link", name="Cancel").click()
            expect(cost_dialog).to_be_hidden()

            page.goto(f"{base}/loans/{loan_id}/scenarios", wait_until="networkidle")
            expect(page.locator("[data-scenario-comparison]")).to_be_visible()
            print(
                "SCENARIO_WIDTH",
                page.evaluate("document.documentElement.scrollWidth"),
                page.evaluate("document.querySelector('[data-scenario-table-scroll]').scrollWidth"),
            )
            _no_page_overflow(page)
            _actions_under_the_tabs(page)
            scenario_scroll = page.locator("[data-scenario-table-scroll]").evaluate(
                "el => ({scrollWidth: el.scrollWidth, clientWidth: el.clientWidth})"
            )
            assert scenario_scroll["scrollWidth"] > scenario_scroll["clientWidth"]
            expect(page.locator('[data-chart="line"]')).to_have_attribute(
                "data-chart-ready", "true"
            )

            page.goto(f"{base}/loans/{loan_id}/payments", wait_until="networkidle")
            expect(page.locator("[data-loan-payments-list]")).to_be_visible()
            expect(page.get_by_role("link", name="Record payment")).to_be_visible()
            _no_page_overflow(page)
            _actions_under_the_tabs(page)
            payments_scroll = page.locator("[data-payments-scroll]").evaluate(
                "el => ({scrollWidth: el.scrollWidth, clientWidth: el.clientWidth})"
            )
            assert payments_scroll["scrollWidth"] > payments_scroll["clientWidth"]

            assert page.evaluate("window.__cspViolations") == []
            browser.close()
