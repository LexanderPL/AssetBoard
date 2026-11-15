"""F8.10 view/edit modes in Chromium: live preview, suggestions, dirty guard and save."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect, sync_playwright

from assetboard.app import create_app
from assetboard.config import Settings
from assetboard.db.models.fire import FireScenario
from assetboard.db.repositories import CategoryRepository, TransactionRepository
from assetboard.domain.fire import trailing_window
from assetboard.services.auth import run_initial_setup
from assetboard.services.context import Context, Role, UserRef, WorkspaceRef
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


def _context(app, admin, workspace) -> Context:
    return Context(
        user=UserRef(
            id=admin.id,
            email=admin.email,
            display_name=admin.display_name,
            locale="en",
            is_instance_admin=True,
        ),
        workspace=WorkspaceRef(
            id=workspace.id,
            name=workspace.name,
            base_currency=workspace.base_currency,
            locale=workspace.locale,
            timezone=workspace.timezone,
            country_code=workspace.country_code,
        ),
        role=Role.OWNER,
        clock=app.state.clock,
    )


def _months(window) -> list[date]:
    months: list[date] = []
    year, month = window.first_day.year, window.first_day.month
    for _ in range(12):
        months.append(date(year, month, 1))
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return months


def _seed_ledger(app, admin, workspace) -> None:
    ctx = _context(app, admin, workspace)
    today = app.state.clock.today(workspace.timezone)
    window = trailing_window(today)
    with app.state.session_factory.begin() as session:
        income = CategoryRepository(session, ctx).create(
            name="Salary browser",
            name_key="salary browser",
            kind="income",
            treatment="regular",
            color="#16a34a",
            sort_order=0,
        )
        expenses = CategoryRepository(session, ctx).create(
            name="Food browser",
            name_key="food browser",
            kind="expense",
            treatment="regular",
            color="#dc2626",
            sort_order=0,
        )
        for day in _months(window):
            TransactionRepository(session, ctx).create(
                direction="income",
                day=day,
                amount_minor=500_000,
                currency="EUR",
                category_id=income.id,
                description="Synthetic salary",
                description_key="synthetic salary",
                source="manual",
                created_by_user_id=admin.id,
            )
            TransactionRepository(session, ctx).create(
                direction="expense",
                day=day,
                amount_minor=200_000,
                currency="EUR",
                category_id=expenses.id,
                description="Synthetic food",
                description_key="synthetic food",
                source="manual",
                created_by_user_id=admin.id,
            )


def test_view_and_edit_modes_in_a_browser(tmp_path: Path) -> None:
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
        admin, workspace = run_initial_setup(
            app.state.session_factory,
            email="admin@example.com",
            display_name="Browser Admin",
            password=PASSWORD,
            workspace_name="Browser Home",
            settings=app.state.settings,
            clock=app.state.clock,
        )
        _seed_ledger(app, admin, workspace)
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

            # Create a plan through the wizard; suggestions are shown but not used.
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

            assert page.url == f"{base}/fire?notice=created"
            expect(page.locator("[data-fire-view]")).to_be_visible()
            expect(page.locator("[data-fire-edit]")).to_be_visible()
            expect(page.locator("[data-fire-form]")).to_have_count(0)
            expect(page.locator('input[name="income_manual"]')).to_have_count(0)
            expect(page.locator("[data-fire-plan-values]")).to_be_visible()
            expect(page.locator("[data-fire-adjustments-view]")).to_be_visible()
            expect(page.locator("[data-fire-loans]")).to_be_visible()
            expect(
                page.locator('[data-fire-chart="net-worth"] [data-chart="line"]')
            ).to_have_attribute("data-chart-ready", "true")

            # Open the separate editor.
            with page.expect_navigation(wait_until="networkidle"):
                page.locator("[data-fire-edit]").click()
            assert page.url == f"{base}/fire/edit"
            expect(page.locator("[data-fire-form]")).to_have_attribute("data-fire-dirty", "0")
            expect(page.locator('[data-fire-suggestion="income"]')).to_be_visible()
            expect(page.locator('[data-fire-suggestion="expenses"]')).to_be_visible()
            expect(page.locator("[data-fire-suggestion-categories]")).to_be_visible()
            assert page.locator('input[name="income_manual"]').input_value() == "50000.00"
            edit_url = page.url

            # Every change recalculates server-side in place and never navigates or writes.
            expenses = page.locator('input[name="expenses_manual"]')
            before_fire_age = page.locator("[data-fire-fire-age]").get_attribute(
                "data-fire-fire-age-value"
            )
            expenses.fill("80000")
            expect(page.locator("[data-fire-preview-note]")).to_be_visible()
            assert page.url == edit_url
            assert page.evaluate("document.activeElement && document.activeElement.name") == (
                "expenses_manual"
            )
            expect(page.locator("[data-fire-fire-age]")).not_to_have_attribute(
                "data-fire-fire-age-value", before_fire_age
            )
            expect(page.locator("[data-fire-form]")).to_have_attribute("data-fire-dirty", "1")
            expect(page.locator("[data-fire-dirty-status]")).to_be_visible()
            page.reload(wait_until="networkidle")
            assert page.locator('input[name="expenses_manual"]').input_value() == "30000.00"

            # Use suggested value copies the server-formatted suggestion and previews.
            page.locator('[data-use-suggestion="expenses_manual"]').click()
            assert page.locator('input[name="expenses_manual"]').input_value() == "24000.00"
            expect(page.locator("[data-fire-preview-note]")).to_be_visible()

            # Leaving with unsaved changes asks for confirmation; dismissing stays.
            page.locator('input[name="expenses_manual"]').fill("70000")
            expect(page.locator("[data-fire-dirty-status]")).to_be_visible()
            page.once("dialog", lambda dialog: dialog.dismiss())
            page.locator("[data-fire-view-plan]").click()
            page.wait_for_timeout(150)
            assert page.url == edit_url
            # Accepting leaves and the view still shows the saved amount.
            page.once("dialog", lambda dialog: dialog.accept())
            page.locator("[data-fire-view-plan]").click()
            page.wait_for_url(f"{base}/fire")
            expect(page.locator("[data-fire-plan-values]")).to_contain_text("€30,000.00")

            # Discard reloads the saved values without a prompt and shows a notice.
            page.goto(f"{base}/fire/edit", wait_until="networkidle")
            page.locator('input[name="expenses_manual"]').fill("70000")
            expect(page.locator("[data-fire-dirty-status]")).to_be_visible()
            page.locator("[data-fire-discard]").click()
            page.wait_for_url(f"{base}/fire/edit?discarded=1")
            assert page.locator('input[name="expenses_manual"]').input_value() == "30000.00"
            expect(page.locator("[data-action-notice]")).to_contain_text(
                "Unsaved changes discarded."
            )

            # Save persists and returns to the read-only view.
            page.locator('input[name="expenses_manual"]').fill("80000")
            with page.expect_navigation(wait_until="networkidle"):
                page.locator("[data-fire-save]").click()
            assert page.url == f"{base}/fire?notice=saved"
            expect(page.locator("[data-action-notice]")).to_be_visible()
            expect(page.locator("[data-fire-plan-values]")).to_contain_text("€80,000.00")

            # The editor still works when the saved projection is blocked; preview recovers.
            from sqlalchemy import select

            with app.state.session_factory() as session:
                plan_id = session.scalars(
                    select(FireScenario.id).where(FireScenario.is_live.is_(True))
                ).one()
            current_age = 41
            with app.state.session_factory() as session, session.begin():
                session.get_one(FireScenario, plan_id).horizon_age = current_age
            page.goto(f"{base}/fire/edit", wait_until="networkidle")
            expect(page.locator("[data-fire-blocked]")).to_be_visible()
            assert page.locator("script[src*='charts.js']").count() == 1
            page.get_by_label("Horizon age").fill("90")
            expect(
                page.locator('[data-fire-chart="net-worth"] [data-chart="line"]')
            ).to_have_attribute("data-chart-ready", "true")

            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_timeout(200)
            _no_page_overflow(page)
            page.goto(f"{base}/fire", wait_until="networkidle")
            page.wait_for_timeout(200)
            _no_page_overflow(page)
            page.set_viewport_size({"width": 1440, "height": 900})

            assert page.evaluate("window.__cspViolations") == []
            browser.close()
