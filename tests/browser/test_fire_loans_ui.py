"""F8.12 live loans in Chromium: view, toggle, preview, save and mobile width."""

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
from assetboard.domain.money import Money
from assetboard.services import loans
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


def _create_loan(app, admin, workspace) -> str:
    ctx = _context(app, admin, workspace)
    loan_id = loans.create_loan(
        app.state.session_factory,
        ctx,
        entry_path="original",
        name="Browser mortgage",
        kind="annuity",
        principal_original=Money(5_000_000, "EUR"),
        current_balance=None,
        balance_date=None,
        origination_date=date(2026, 1, 1),
        first_payment_date=date(2026, 2, 1),
        term_months=24,
        payment_day=1,
        rate_mode="fixed",
        fixed_rate_bp=600,
        reference_rate_id=None,
        margin_bp=None,
        high_rate_confirmed=False,
        installment_override=None,
        overpayment_mode="none",
        overpayment_amount=None,
        overpayment_strategy="reduce_term",
        drawdown_mode="full_at_origination",
        interest_only_months=0,
    )
    return str(loan_id)


def test_live_loans_in_view_and_editor(tmp_path: Path) -> None:
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

            loan_id = _create_loan(app, admin, workspace)
            page.goto(f"{base}/fire", wait_until="networkidle")
            page.locator("#plan-inputs > summary").click()
            expect(page.locator(f'[data-fire-loan="{loan_id}"]')).to_be_visible()
            expect(page.locator("[data-fire-loan-annual]")).to_contain_text("€")
            expect(page.locator("[data-fire-loan-effect]")).to_contain_text("Removed")
            expect(page.locator("[data-fire-loan-payoff-badge]")).to_be_visible()
            _no_page_overflow(page)

            page.goto(f"{base}/fire/edit", wait_until="networkidle")
            page.locator("#loans > summary").click()
            loan = page.locator(f'[data-fire-loan="{loan_id}"]')
            loan.locator('input[value="0"]').check()
            page.wait_for_timeout(800)
            expect(page.locator("[data-fire-loans-preview]")).to_have_count(0)
            expect(page.locator("[data-fire-loans-expense-warning]")).to_have_count(0)
            with page.expect_navigation(wait_until="networkidle"):
                page.locator("[data-fire-save]").click()
            page.locator("#plan-inputs > summary").click()
            expect(page.locator(f'[data-fire-loan="{loan_id}"]')).to_have_attribute(
                "data-fire-loan-state", "ignored"
            )
            expect(page.locator("[data-fire-loan-effect]")).to_contain_text("Stays in expenses")
            _no_page_overflow(page)

            page.set_viewport_size({"width": 390, "height": 844})
            page.goto(f"{base}/fire", wait_until="networkidle")
            page.locator("#plan-inputs > summary").click()
            _no_page_overflow(page)
            page.goto(f"{base}/fire/edit", wait_until="networkidle")
            page.locator("#loans > summary").click()
            _no_page_overflow(page)
            assert page.evaluate("window.__cspViolations") == []
            context.close()
            browser.close()
