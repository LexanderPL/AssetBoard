"""Loans in Chromium: the loan subnavigation, the read-only overview and the Settings page.

The wizard saves into Settings, configuration actions (rate, cost, overpayment, property,
auto-record, generation, archive) live there, and the overview keeps the progress bar, schedule,
charts, property LTV card, quick-record link and plan-vs-actual status. Both pages fit 390 and
1440 without sideways scrolling and without CSP violations.
"""

from __future__ import annotations

import os
import re
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


def _loan_sections(page: Page) -> None:
    expect(page.locator("[data-loan-tabs]")).to_be_visible()
    expect(page.locator("[data-loan-tabs] a")).to_have_count(5)


def test_loan_wizard_settings_and_overview_at_desktop_and_phone_widths(tmp_path: Path) -> None:
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
            expect(page.locator("[data-wizard-review]")).to_be_visible()
            expect(page.locator("[data-review-first-installment]")).to_contain_text("1,703.37")
            expect(page.get_by_role("button", name="Save and add another")).to_be_visible()
            _no_page_overflow(page)
            page.get_by_role("button", name="Save loan").click()

            # A new loan lands on Settings, where all configuration lives.
            expect(page.locator("[data-loan-settings-page]")).to_be_visible()
            _loan_sections(page)
            expect(page.locator("[data-loan-property-settings]")).to_be_visible()
            expect(page.locator("[data-loan-rates-settings]")).to_be_visible()
            _no_page_overflow(page)

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

            # The overview is read-only insight: no configuration forms or dialogs.
            page.locator('[data-loan-tab="overview"]').click()
            page.wait_for_load_state("networkidle")
            expect(page.locator("#loan-summary-heading")).to_contain_text("Home mortgage")
            expect(page.locator("dt", has_text="First payment")).to_be_visible()
            expect(page.locator("dt", has_text="First payment").locator("..")).to_contain_text(
                "day 15"
            )
            _actions_under_the_tabs(page)
            expect(page.locator("[data-loan-overview]")).to_be_visible()
            expect(page.locator('[data-loan-status-badge="active"]')).to_be_visible()
            expect(page.locator("[data-loan-progress-bar]")).to_be_visible()
            expect(page.locator("[data-loan-property-summary]")).to_be_visible()
            expect(page.locator("[data-manage-property]")).to_be_visible()
            expect(page.locator("[data-quick-record]")).to_be_visible()
            expect(page.locator("[data-plan-actual]")).to_be_visible()
            expect(page.locator("[data-plan-actual]")).to_contain_text(
                "No actual payments recorded yet."
            )
            expect(page.locator("[data-loan-schedule]")).to_be_visible()
            expect(page.locator('[data-chart="line"]')).to_have_attribute(
                "data-chart-ready", "true"
            )
            expect(page.locator('[data-chart="loan-payments"]')).to_have_attribute(
                "data-chart-ready", "true"
            )
            position = page.locator("[data-schedule-row] th").first.evaluate(
                "el => getComputedStyle(el).position"
            )
            assert position == "sticky"
            page.locator('[data-schedule-view="planned"]').click()
            page.wait_for_load_state("networkidle")
            expect(page).to_have_url(re.compile(r"view=planned"))
            _no_page_overflow(page)
            page.locator('[data-schedule-view="actual"]').click()
            page.wait_for_load_state("networkidle")
            expect(page.locator("#rate-dialog")).to_have_count(0)
            expect(page.locator("#cost-dialog")).to_have_count(0)
            expect(page.locator("#overpayment-dialog")).to_have_count(0)
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

            page.locator('[data-loan-tab="scenarios"]').click()
            page.wait_for_url(f"{base}/loans/{loan_id}/scenarios")
            _loan_sections(page)
            expect(page.locator("[data-scenario-comparison]")).to_be_visible()
            expect(page.locator("[data-scenario-chart]")).to_be_visible()
            expect(page.locator('[data-chart="line"]')).to_have_attribute(
                "data-chart-ready", "true"
            )
            _no_page_overflow(page)
            row = page.locator('[data-scenario-form-row="1"]')
            row.locator("[data-scenario-name]").fill("Extra 200")
            row.locator('select[name="overpayment_mode_1"]').select_option("fixed_extra")
            row.locator("[data-scenario-overpayment-amount]").fill("200")
            page.get_by_role("button", name="Compare and save named scenarios").click()
            page.wait_for_load_state("networkidle")
            expect(page.locator("[data-saved-scenario]")).to_have_count(1)
            expect(page.locator("[data-action-notice]")).to_contain_text("Saved scenarios updated")
            expect(page.locator("[data-saved-delta]")).to_be_visible()
            expect(page.locator("[data-saved-interest-delta]")).to_be_visible()
            expect(page.locator("[data-saved-months-delta]")).to_be_visible()
            expect(page.locator("[data-saved-interest-delta]")).to_contain_text("Saves")
            expect(page.locator("[data-saved-months-delta]")).to_contain_text("sooner")
            expect(page.locator('[data-chart="line"]')).to_have_attribute(
                "data-chart-ready", "true"
            )
            _no_page_overflow(page)

            third = page.locator('[data-scenario-form-row="3"]')
            rate_preset = third.locator('[data-scenario-preset="rate"]')
            rate_preset.focus()
            page.keyboard.press("Enter")
            expect(third.locator("[data-scenario-rate-shift]")).to_have_value("1")
            expect(third.locator("[data-scenario-rate-shift-from]")).not_to_have_value("")
            expect(third.locator("[data-scenario-rate-shift]")).to_be_focused()
            third.locator("[data-scenario-clear]").click()
            expect(third.locator("[data-scenario-rate-shift]")).to_have_value("")
            expect(third.locator("[data-scenario-rate-shift-from]")).to_have_value("")
            expect(third.locator("[data-scenario-name]")).to_be_focused()
            _no_page_overflow(page)

            page.locator('[data-loan-tab="payments"]').click()
            page.wait_for_url(f"{base}/loans/{loan_id}/payments")
            _loan_sections(page)
            expect(page.locator("[data-loan-payments-list]")).to_be_visible()
            expect(page.locator("[data-loan-checkpoints-list]")).to_be_visible()
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
            expect(page.locator("[data-payment-variance]")).to_be_visible()
            expect(page.locator("[data-payment-variance-label]")).to_have_text(
                "Less than projected"
            )
            expect(page.locator("[data-payment-total-variance]")).to_be_visible()
            payment_display = page.locator("[data-payment]").first.evaluate(
                "el => getComputedStyle(el).display"
            )
            assert payment_display == "table-row"
            _no_page_overflow(page)

            page.locator('[data-loan-tab="overview"]').click()
            page.wait_for_load_state("networkidle")
            expect(page.locator("[data-schedule-actual]")).to_be_visible()
            expect(page.locator("[data-actual-total]")).to_be_visible()
            page.locator('[data-schedule-view="planned"]').click()
            page.wait_for_load_state("networkidle")
            expect(page.locator("[data-schedule-planned]")).to_be_visible()
            expect(page.locator("[data-schedule-actual]")).to_have_count(0)
            expect(page.locator("[data-actual-total]")).to_have_count(0)
            _no_page_overflow(page)
            page.locator('[data-schedule-view="actual"]').click()
            page.wait_for_load_state("networkidle")
            expect(page.locator("[data-schedule-actual]")).to_be_visible()
            _no_page_overflow(page)

            page.locator('[data-loan-tab="payments"]').click()
            page.wait_for_load_state("networkidle")
            page.get_by_role("link", name="Add checkpoint").click()
            checkpoint_dialog = page.locator("#checkpoint-dialog")
            expect(checkpoint_dialog).to_be_visible()
            checkpoint_dialog.locator('input[name="as_of"]').fill("2026-06-01")
            checkpoint_dialog.locator('input[name="remaining_principal"]').fill("290,000")
            checkpoint_dialog.get_by_role("button", name="Save checkpoint").click()
            page.wait_for_load_state("networkidle")
            expect(page.locator("[data-action-notice]")).to_contain_text(
                "Balance checkpoint saved."
            )

            page.locator('[data-loan-tab="overview"]').click()
            page.wait_for_load_state("networkidle")
            expect(page.locator("[data-plan-last-payment]")).to_contain_text("100.00")
            expect(page.locator("[data-plan-checkpoint]")).to_contain_text("290,000.00")
            expect(page.locator("[data-plan-actual]")).to_have_attribute(
                "data-plan-status", "ahead"
            )
            expect(page.locator("[data-plan-status-line]")).to_contain_text(
                "Ahead of the projected balance"
            )
            _no_page_overflow(page)

            page.goto(f"{base}/loans/installments", wait_until="networkidle")
            expect(page.locator("[data-installments-tab]")).to_be_visible()
            page.locator("[data-new-installment-plan]").first.click()
            page.wait_for_load_state("networkidle")
            page.get_by_label("Name", exact=True).fill("Phone plan")
            page.locator('input[name="installment"]').fill("50")
            page.locator('input[name="remaining_count"]').fill("3")
            page.locator('input[name="payment_day"]').fill("5")
            page.locator('select[name="category_id"]').select_option(index=1)
            page.get_by_role("button", name="Save plan").click()
            page.wait_for_load_state("networkidle")
            expect(page.locator("[data-installment-plan]")).to_have_count(1)
            expect(page.locator("[data-installment-progress]")).to_be_visible()
            expect(page.locator('[data-installment-status-badge="open"]')).to_be_visible()
            expect(page.get_by_role("link", name="Back to Installments")).to_have_count(0)
            page.locator("[data-open-installment-plan]").click()
            page.wait_for_load_state("networkidle")
            expect(page.locator("[data-installment-plan-detail]")).to_be_visible()
            expect(page.locator("[data-plan-remaining]")).to_have_text("3")
            expect(page.locator("[data-installment-progress-detail]")).to_be_visible()
            expect(page.get_by_role("button", name="Post next installment")).to_have_count(0)
            expect(page.get_by_role("link", name="Back to Installments")).to_have_count(0)
            _actions_under_the_tabs(page)
            _no_page_overflow(page)
            plan_url = page.url

            page.set_viewport_size({"width": 390, "height": 844})
            page.goto(f"{base}/loans", wait_until="networkidle")
            expect(page.locator("[data-loan]")).to_have_count(1)
            expect(page.locator("[data-loan-portfolio]")).to_be_visible()
            expect(page.locator("[data-loan-row-progress]")).to_be_visible()
            _no_page_overflow(page)

            page.locator("[data-open-loan]").click()
            page.wait_for_load_state("networkidle")
            expect(page.locator("#loan-summary-heading")).to_contain_text("Home mortgage")
            _loan_sections(page)
            _no_page_overflow(page)
            _actions_under_the_tabs(page)
            expect(page.get_by_role("link", name="Back to Loans")).to_have_count(0)
            expect(page.locator("[data-quick-record]")).to_be_visible()
            expect(page.locator("[data-loan-property-summary]")).to_be_visible()
            expect(page.locator("[data-plan-status-line]")).to_be_visible()
            expect(page.locator("[data-loan-schedule]")).to_be_visible()
            expect(page.locator("[data-schedule-row]")).to_have_count(24)
            _no_page_overflow(page)
            scroll = page.locator("[data-schedule-scroll]").evaluate(
                "el => ({scrollWidth: el.scrollWidth, clientWidth: el.clientWidth})"
            )
            assert scroll["scrollWidth"] <= scroll["clientWidth"] + 1
            expect(page.locator('[data-chart="line"]')).to_have_attribute(
                "data-chart-ready", "true"
            )
            expect(page.locator('[data-chart="loan-payments"]')).to_have_attribute(
                "data-chart-ready", "true"
            )
            _no_page_overflow(page)

            page.locator('[data-loan-tab="settings"]').click()
            page.wait_for_load_state("networkidle")
            expect(page.locator("[data-loan-settings-page]")).to_be_visible()
            _loan_sections(page)
            _no_page_overflow(page)
            page.get_by_role("link", name="Add cost").click()
            cost_dialog = page.locator("#cost-dialog")
            expect(cost_dialog).to_be_visible()
            expect(cost_dialog.get_by_label("Name", exact=True)).to_be_visible()
            _no_page_overflow(page)
            cost_dialog.get_by_role("link", name="Cancel").click()
            expect(cost_dialog).to_be_hidden()

            page.goto(f"{base}/loans/{loan_id}/scenarios", wait_until="networkidle")
            _loan_sections(page)
            expect(page.locator("[data-scenario-comparison]")).to_be_visible()
            expect(page.locator("[data-scenario-card]")).to_have_count(3)
            expect(page.locator("[data-scenario-presets]").first).to_be_visible()
            expect(page.locator("[data-saved-delta]")).to_be_visible()
            _no_page_overflow(page)
            for index in range(3):
                box = page.locator("[data-scenario-card]").nth(index).bounding_box()
                assert box is not None
                assert box["x"] >= -1
                assert box["x"] + box["width"] <= 390 + 1
            scenario_scroll = page.locator("[data-scenario-table-scroll]").evaluate(
                "el => ({scrollWidth: el.scrollWidth, clientWidth: el.clientWidth})"
            )
            assert scenario_scroll["scrollWidth"] > scenario_scroll["clientWidth"]
            expect(page.locator('[data-chart="line"]')).to_have_attribute(
                "data-chart-ready", "true"
            )

            page.goto(f"{base}/loans/{loan_id}/payments", wait_until="networkidle")
            _loan_sections(page)
            expect(page.locator("[data-loan-payments-list]")).to_be_visible()
            expect(page.get_by_role("link", name="Record payment")).to_be_visible()
            expect(page.locator("[data-payment]")).to_have_count(1)
            expect(page.locator("[data-payment-variance]")).to_be_visible()
            expect(page.locator("[data-checkpoint]")).to_have_count(1)
            _no_page_overflow(page)
            phone_payment_display = page.locator("[data-payment]").first.evaluate(
                "el => getComputedStyle(el).display"
            )
            assert phone_payment_display == "grid"
            phone_checkpoint_display = page.locator("[data-checkpoint]").first.evaluate(
                "el => getComputedStyle(el).display"
            )
            assert phone_checkpoint_display == "grid"
            for selector in ("[data-payments-scroll]", "[data-checkpoints-scroll]"):
                scroll = page.locator(selector).evaluate(
                    "el => ({scrollWidth: el.scrollWidth, clientWidth: el.clientWidth})"
                )
                assert scroll["scrollWidth"] <= scroll["clientWidth"] + 1, (selector, scroll)

            page.goto(plan_url, wait_until="networkidle")
            expect(page.locator("[data-installment-plan-detail]")).to_be_visible()
            _no_page_overflow(page)
            _actions_under_the_tabs(page)
            page.goto(f"{base}/loans/installments", wait_until="networkidle")
            expect(page.locator("[data-installment-plan]")).to_have_count(1)
            _no_page_overflow(page)

            page.goto(f"{base}/loans/reference-rates", wait_until="networkidle")
            page.locator('[data-dialog-open="rate-dialog"]').first.click()
            rate_dialog = page.locator("#rate-dialog")
            expect(rate_dialog).to_be_visible()
            rate_dialog.get_by_label("Name", exact=True).fill("EURIBOR 6M")
            rate_dialog.get_by_role("button", name="Save reference rate").click()
            page.wait_for_load_state("networkidle")
            rate = page.locator("[data-reference-rate]").first
            expect(rate).to_be_visible()
            rate.locator("summary").click()
            rate.get_by_role("link", name="Add value").click()
            value_dialog = page.locator('[id^="value-dialog-"]').first
            expect(value_dialog).to_be_visible()
            value_dialog.get_by_label("Effective from").fill("2025-10-01")
            value_dialog.locator('input[name="value_percent"]').fill("4")
            value_dialog.get_by_role("button", name="Save value").click()
            page.wait_for_load_state("networkidle")
            rate = page.locator("[data-reference-rate]").first
            rate.locator("summary").click()
            rate.get_by_role("link", name="Add value").click()
            value_dialog = page.locator('[id^="value-dialog-"]').first
            value_dialog.get_by_label("Effective from").fill("2026-01-01")
            value_dialog.locator('input[name="value_percent"]').fill("4.5")
            value_dialog.get_by_role("button", name="Save value").click()
            page.wait_for_load_state("networkidle")
            rate = page.locator("[data-reference-rate]").first
            rate.locator("summary").click()
            expect(rate.locator("[data-reference-sparkline] svg")).to_be_visible()
            expect(rate.locator("[data-reference-current]")).to_contain_text("4.5%")
            expect(rate.locator("[data-reference-values]")).to_be_visible()
            expect(rate.locator("[data-reference-value]")).to_have_count(2)
            _no_page_overflow(page)
            page.set_viewport_size({"width": 1440, "height": 900})
            _no_page_overflow(page)

            assert page.evaluate("window.__cspViolations") == []
            browser.close()
