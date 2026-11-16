"""F8.11 unified adjustments in Chromium: add/edit/remove, archive history and mobile."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect, sync_playwright
from sqlalchemy import select

from assetboard.app import create_app
from assetboard.config import Settings
from assetboard.db.models.fire import FireEvent, FireScenario
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


def _plan_id(app) -> object:
    with app.state.session_factory() as session:
        return session.scalars(select(FireScenario.id).where(FireScenario.is_live.is_(True))).one()


def _add_archived(app, plan_id, workspace_id) -> object:
    with app.state.session_factory.begin() as session:
        row = FireEvent(
            workspace_id=workspace_id,
            scenario_id=plan_id,
            year=2020,
            kind="cash",
            amount_minor=100_000,
            label="Old bonus",
        )
        session.add(row)
        session.flush()
        return row.id


def test_unified_adjustments_in_a_browser(tmp_path: Path) -> None:
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
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            context.add_init_script(VIOLATION_RECORDER)
            page = context.new_page()
            page.goto(f"{base}/login", wait_until="networkidle")
            page.get_by_label("Email").fill("admin@example.com")
            page.get_by_label("Password").fill(PASSWORD)
            page.get_by_role("button", name="Sign in").click()
            page.wait_for_url(f"{base}/")

            page.goto(f"{base}/fire", wait_until="networkidle")
            page.get_by_label("Birth year").fill(str(start_year - 41))
            page.get_by_label("Horizon age").fill("90")
            page.get_by_label("Income stops at age").fill("55")
            with page.expect_navigation(wait_until="networkidle"):
                page.get_by_role("button", name="Next").click()
            page.locator('input[name="income_manual"]').fill("50000")
            page.locator('input[name="expenses_manual"]').fill("30000")
            with page.expect_navigation(wait_until="networkidle"):
                page.get_by_role("button", name="Next").click()
            page.locator('input[name="portfolio_manual"]').fill("1000000")
            with page.expect_navigation(wait_until="networkidle"):
                page.get_by_role("button", name="Next").click()
            with page.expect_navigation(wait_until="networkidle"):
                page.locator("[data-fire-create]").click()

            page.locator("#plan-inputs > summary").click()
            expect(page.locator("[data-fire-adjustments-view]")).to_be_visible()
            expect(page.locator("[data-fire-adjustments-empty]")).to_be_visible()
            expect(page.locator("[data-fire-adjustments-empty]")).to_contain_text(
                "No adjustments yet"
            )
            expect(page.locator('input[name="archived_remove_1"]')).to_have_count(0)

            with page.expect_navigation(wait_until="networkidle"):
                page.locator("[data-fire-edit]").click()
            page.locator("#adjustments > summary").click()
            expect(page.locator("[data-fire-adjustments]")).to_be_visible()
            expect(page.locator("[data-fire-adjustments] textarea")).to_have_count(0)
            expect(page.locator("[data-fire-archived-adjustments]")).to_have_count(0)
            expect(page.locator("[data-fire-adjustments-empty]")).to_be_visible()

            # Add one row, with focus and an announcement. The empty hint is for the blank editor
            # only and disappears as soon as a row exists.
            page.locator("[data-add-adjustment]").click()
            expect(page.locator("[data-adjustment-status]")).to_have_text("Adjustment added.")
            expect(page.locator("[data-fire-adjustments-empty]")).to_be_hidden()
            assert page.evaluate("document.activeElement && document.activeElement.name") == (
                "event_year_1"
            )
            assert page.evaluate(
                """() => {
                  const rows = [...document.querySelectorAll("[data-adjustment-row]")];
                  return rows.at(-1).hasAttribute("data-new-adjustment-row");
                }"""
            )
            rows = page.locator("[data-adjustment-row]:not([data-new-adjustment-row])")
            row = rows.nth(0)
            row.locator('input[name^="event_year_"]').fill("2028")
            row.locator('select[name^="event_kind_"]').select_option("cash")
            row.locator('input[name^="event_amount_"]').fill("1000")
            row.locator('input[name^="event_label_"]').fill("Bonus")
            expect(page.locator("[data-fire-preview-note]")).to_be_visible()
            expect(page.locator("[data-fire-dirty-status]")).to_be_visible()

            # Add the other three kinds individually.
            for year, kind, amount, label in (
                ("2029", "income_change", "500", "Rent"),
                ("2030", "expense_change", "-200", "Loan repaid"),
                ("2031", "saving_override", "-3000", "Sabbatical"),
            ):
                page.locator("[data-add-adjustment]").click()
                new_row = rows.last
                new_row.locator('input[name^="event_year_"]').fill(year)
                new_row.locator('select[name^="event_kind_"]').select_option(kind)
                new_row.locator('input[name^="event_amount_"]').fill(amount)
                new_row.locator('input[name^="event_label_"]').fill(label)
            expect(rows).to_have_count(4)

            # An unsaved row is removed from the DOM immediately, without a marker.
            page.locator("[data-add-adjustment]").click()
            expect(rows).to_have_count(5)
            rows.last.locator("[data-adjustment-remove-text]").click()
            expect(rows).to_have_count(4)
            expect(page.locator("[data-adjustment-status]")).to_have_text("Adjustment removed.")
            assert page.locator('input[name^="event_remove_"]').count() == 4

            with page.expect_navigation(wait_until="networkidle"):
                page.locator("[data-fire-save]").click()
            assert page.url == f"{base}/fire?notice=saved"
            page.locator("#plan-inputs > summary").click()
            for label in ("Bonus", "Rent", "Loan repaid", "Sabbatical"):
                expect(page.locator("[data-fire-adjustments-view]")).to_contain_text(label)

            # The populated view and editor stay inside a 390px viewport.
            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_timeout(200)
            _no_page_overflow(page)
            with page.expect_navigation(wait_until="networkidle"):
                page.locator("[data-fire-edit]").click()
            page.wait_for_timeout(200)
            _no_page_overflow(page)
            page.set_viewport_size({"width": 1440, "height": 900})

            # Edit one row in place.
            page.locator("#adjustments > summary").click()
            rows = page.locator("[data-adjustment-row]:not([data-new-adjustment-row])")
            first = rows.filter(has=page.locator('input[name^="event_label_"][value="Bonus"]'))
            first.locator('input[name^="event_amount_"]').fill("1500")
            first.locator('input[name^="event_label_"]').fill("Bonus updated")
            expect(page.locator("[data-fire-preview-note]")).to_be_visible()
            with page.expect_navigation(wait_until="networkidle"):
                page.locator("[data-fire-save]").click()
            page.locator("#plan-inputs > summary").click()
            expect(page.locator("[data-fire-adjustments-view]")).to_contain_text("Bonus updated")
            expect(page.locator("[data-fire-adjustments-view]")).to_contain_text("€1,500.00")

            # Remove one saved row with its visible Remove button, not DOM deletion. The hidden
            # checkbox is focusable; Space toggles it and the label becomes Undo.
            with page.expect_navigation(wait_until="networkidle"):
                page.locator("[data-fire-edit]").click()
            page.locator("#adjustments > summary").click()
            rows = page.locator("[data-adjustment-row]:not([data-new-adjustment-row])")
            removed = rows.filter(has=page.locator('input[name^="event_label_"][value="Rent"]'))
            checkbox = removed.locator("[data-adjustment-remove]")
            checkbox.focus()
            page.keyboard.press("Space")
            expect(removed.locator("[data-adjustment-remove-text]")).to_have_text("Undo")
            expect(checkbox).to_have_attribute("aria-label", "Undo removal of this adjustment")
            expect(removed.locator("[data-adjustment-removed-note]")).to_be_visible()
            expect(removed).to_have_attribute("data-removed", "1")
            expect(page.locator("[data-fire-dirty-status]")).to_be_visible()
            page.keyboard.press("Space")
            expect(removed.locator("[data-adjustment-remove-text]")).to_have_text("Remove")
            expect(checkbox).to_have_attribute("aria-label", "Remove this adjustment")
            expect(removed.locator("[data-adjustment-removed-note]")).to_be_hidden()
            removed.locator("[data-adjustment-remove-text]").click()
            expect(removed.locator("[data-adjustment-remove-text]")).to_have_text("Undo")
            with page.expect_navigation(wait_until="networkidle"):
                page.locator("[data-fire-save]").click()
            page.locator("#plan-inputs > summary").click()
            expect(page.locator("[data-fire-adjustments-view]")).not_to_contain_text("Rent")

            # Archived history is read-only and removed only by its visible Remove control.
            plan_id = _plan_id(app)
            with app.state.session_factory() as session:
                workspace_id = session.scalars(
                    select(FireScenario.workspace_id).where(FireScenario.id == plan_id)
                ).one()
            _add_archived(app, plan_id, workspace_id)
            page.goto(f"{base}/fire/edit", wait_until="networkidle")
            page.locator("#adjustments > summary").click()
            page.locator("[data-fire-archived-adjustments] summary").click()
            archived = page.locator("[data-fire-archived-adjustment]").filter(has_text="Old bonus")
            expect(archived).to_be_visible()

            # Remove every active row; the saved archived history stays and the editor says so.
            removals = page.locator("[data-adjustment-row] [data-adjustment-remove-text]")
            expect(removals).to_have_count(3)
            for index in range(removals.count()):
                removals.nth(index).click()
            with page.expect_navigation(wait_until="networkidle"):
                page.locator("[data-fire-save]").click()
            page.locator("#plan-inputs > summary").click()
            expect(page.locator("[data-fire-adjustments-empty]")).to_contain_text(
                "No current adjustments"
            )
            expect(page.locator("[data-fire-archived-adjustments]")).to_have_count(1)

            with page.expect_navigation(wait_until="networkidle"):
                page.locator("[data-fire-edit]").click()
            page.locator("#adjustments > summary").click()
            expect(page.locator("[data-fire-adjustments-empty]")).to_contain_text(
                "No current adjustments"
            )
            page.locator("[data-fire-archived-adjustments] summary").click()
            archived = page.locator("[data-fire-archived-adjustment]").filter(has_text="Old bonus")
            archived.locator("[data-adjustment-remove-text]").click()
            expect(archived.locator("[data-adjustment-remove-text]")).to_have_text("Undo")
            expect(archived.locator("[data-adjustment-removed-note]")).to_be_visible()
            with page.expect_navigation(wait_until="networkidle"):
                page.locator("[data-fire-save]").click()
            page.locator("#plan-inputs > summary").click()
            expect(page.locator("[data-fire-archived-adjustments]")).to_have_count(0)
            expect(page.locator("[data-fire-adjustments-empty]")).to_contain_text(
                "No adjustments yet"
            )

            # Without JavaScript the visible Remove control still toggles its wrapped checkbox,
            # and Save changes applies the removal.
            with page.expect_navigation(wait_until="networkidle"):
                page.locator("[data-fire-edit]").click()
            page.locator("#adjustments > summary").click()
            page.locator("[data-add-adjustment]").click()
            no_js_row = page.locator("[data-adjustment-row]:not([data-new-adjustment-row])").last
            no_js_row.locator('input[name^="event_year_"]').fill("2032")
            no_js_row.locator('select[name^="event_kind_"]').select_option("cash")
            no_js_row.locator('input[name^="event_amount_"]').fill("250")
            no_js_row.locator('input[name^="event_label_"]').fill("No JS")
            with page.expect_navigation(wait_until="networkidle"):
                page.locator("[data-fire-save]").click()
            page.locator("#plan-inputs > summary").click()
            expect(page.locator("[data-fire-adjustments-view]")).to_contain_text("No JS")

            storage = context.storage_state()
            no_js_context = browser.new_context(
                viewport={"width": 1440, "height": 900},
                java_script_enabled=False,
                storage_state=storage,
            )
            no_js_page = no_js_context.new_page()
            no_js_page.goto(f"{base}/fire/edit", wait_until="load")
            no_js_page.locator("#adjustments > summary").click()
            no_js_row = no_js_page.locator("[data-adjustment-row]").filter(
                has=no_js_page.locator('input[name^="event_label_"][value="No JS"]')
            )
            no_js_row.locator("[data-adjustment-remove-text]").click()
            with no_js_page.expect_navigation(wait_until="load"):
                no_js_page.locator("[data-fire-save]").click()
            no_js_page.locator("#plan-inputs > summary").click()
            expect(no_js_page.locator("[data-fire-adjustments-view]")).not_to_contain_text("No JS")
            no_js_context.close()

            # Mobile layout stays inside the viewport in both modes.
            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_timeout(200)
            _no_page_overflow(page)
            page.goto(f"{base}/fire/edit", wait_until="networkidle")
            page.locator("#adjustments > summary").click()
            page.wait_for_timeout(200)
            _no_page_overflow(page)
            overflow = page.evaluate(
                """() => {
                  const holder = document.querySelector("[data-fire-adjustments] .overflow-x-auto");
                  return holder ? {scroll: holder.scrollWidth, client: holder.clientWidth} : null;
                }"""
            )
            assert overflow is not None and overflow["scroll"] > overflow["client"], overflow
            page.goto(f"{base}/fire", wait_until="networkidle")
            page.locator("#plan-inputs > summary").click()
            page.wait_for_timeout(200)
            _no_page_overflow(page)
            page.set_viewport_size({"width": 1440, "height": 900})

            assert page.evaluate("window.__cspViolations") == []
            browser.close()
