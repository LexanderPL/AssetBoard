"""F8.9 setup wizard and reset in Chromium: steps, Use buttons, reset, CSP and phone width."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect, sync_playwright

from assetboard.app import create_app
from assetboard.config import Settings
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


def _months(window) -> list[date]:
    months: list[date] = []
    year, month = window.first_day.year, window.first_day.month
    for _ in range(12):
        months.append(date(year, month, 1))
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return months


def test_setup_wizard_and_reset_in_a_browser(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'browser.db'}",
            secret_key="browser-test-key",
        )
    )
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

            page.goto(f"{base}/fire", wait_until="networkidle")
            expect(page.locator("[data-fire-wizard]")).to_have_attribute("data-fire-wizard", "1")
            expect(page.locator('[aria-current="step"]')).to_contain_text("About you")
            page.get_by_label("Birth year").fill("1985")
            page.get_by_label("Horizon age").fill("90")
            page.get_by_label("Income stops at age").fill("55")
            with page.expect_navigation(wait_until="networkidle"):
                page.get_by_role("button", name="Next").click()

            expect(page.locator("[data-fire-wizard]")).to_have_attribute("data-fire-wizard", "2")
            expect(page.locator('[data-fire-suggestion="income"]')).to_be_visible()
            expect(page.locator('[data-fire-suggestion="expenses"]')).to_be_visible()
            expect(page.locator("[data-fire-suggestion-categories]")).to_be_visible()
            with page.expect_navigation(wait_until="networkidle"):
                page.locator('[data-use-suggestion="income_manual"]').click()
            assert page.locator('input[name="income_manual"]').input_value() == "60000.00"
            with page.expect_navigation(wait_until="networkidle"):
                page.locator('[data-use-suggestion="expenses_manual"]').click()
            assert page.locator('input[name="expenses_manual"]').input_value() == "24000.00"
            page.set_viewport_size({"width": 390, "height": 844})
            _no_page_overflow(page)
            page.set_viewport_size({"width": 1440, "height": 900})
            with page.expect_navigation(wait_until="networkidle"):
                page.get_by_role("button", name="Next").click()

            expect(page.locator("[data-fire-wizard]")).to_have_attribute("data-fire-wizard", "3")
            page.locator('input[name="portfolio_manual"]').fill("1000000")
            with page.expect_navigation(wait_until="networkidle"):
                page.get_by_role("button", name="Next").click()

            expect(page.locator("[data-fire-wizard]")).to_have_attribute("data-fire-wizard", "4")
            expect(page.locator("[data-fire-review]")).to_be_visible()
            expect(page.locator("[data-fire-wizard-loans]")).to_be_visible()
            page.locator('input[name="inflation"]').fill("3.5")
            with page.expect_navigation(wait_until="networkidle"):
                page.locator("[data-wizard-review]").click()
            expect(page.locator("[data-fire-review]")).to_contain_text("Inflation 3.5%")
            page.set_viewport_size({"width": 390, "height": 844})
            _no_page_overflow(page)
            page.set_viewport_size({"width": 1440, "height": 900})
            with page.expect_navigation(wait_until="networkidle"):
                page.locator("[data-fire-create]").click()

            assert "/fire?notice=created" in page.url
            expect(page.locator("[data-fire-overview]")).to_be_visible()
            expect(page.locator("[data-fire-reset]")).to_be_visible()
            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_timeout(200)
            _no_page_overflow(page)
            page.set_viewport_size({"width": 1440, "height": 900})

            with page.expect_navigation(wait_until="networkidle"):
                page.locator("[data-fire-reset]").click()
            expect(page.locator("[data-fire-reset-page]")).to_be_visible()
            page.set_viewport_size({"width": 390, "height": 844})
            _no_page_overflow(page)
            page.locator("#fire-reset-confirm").fill("reset")
            with page.expect_navigation(wait_until="networkidle"):
                page.locator("[data-fire-reset-submit]").click()
            assert "/fire?notice=reset" in page.url
            expect(page.locator("[data-fire-wizard]")).to_have_attribute("data-fire-wizard", "1")
            expect(page.locator("[data-action-notice]")).to_be_visible()
            _no_page_overflow(page)

            assert page.evaluate("window.__cspViolations") == []
            browser.close()
