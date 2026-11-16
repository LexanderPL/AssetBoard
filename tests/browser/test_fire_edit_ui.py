"""F8.10 view/edit modes in Chromium: live preview, suggestions, dirty guard and save."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from playwright.sync_api import Page, expect, sync_playwright

from assetboard.app import create_app
from assetboard.config import Settings
from assetboard.db.models.fire import FireScenario
from assetboard.db.repositories import (
    CategoryRepository,
    ItemValuationRepository,
    NetWorthCategoryRepository,
    NetWorthItemRepository,
    TransactionRepository,
)
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


def _seed_portfolio(app, admin, workspace) -> None:
    ctx = _context(app, admin, workspace)
    today = app.state.clock.today(workspace.timezone)
    with app.state.session_factory.begin() as session:
        category = NetWorthCategoryRepository(session, ctx).create(
            name="Savings browser",
            name_key="savings browser",
            side="asset",
            color="#0ea5e9",
            sort_order=0,
        )
        manual = NetWorthItemRepository(session, ctx).create(
            side="asset",
            category_id=category.id,
            name="Cash browser",
            currency="EUR",
            valuation_mode="manual",
            link_type=None,
            link_id=None,
            include_in_net_worth=True,
            investable=True,
            estimate_between=False,
        )
        ItemValuationRepository(session, ctx).create(
            item_id=manual.id,
            as_of=today.replace(day=1),
            value_minor=5_000_000,
            currency="EUR",
        )
        NetWorthItemRepository(session, ctx).create(
            side="asset",
            category_id=category.id,
            name="Linked browser item with a long name for wrapping",
            currency="EUR",
            valuation_mode="linked",
            link_type="portfolio_asset_class",
            link_id=uuid4(),
            include_in_net_worth=True,
            investable=True,
            estimate_between=False,
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
        _seed_portfolio(app, admin, workspace)
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
            plan_inputs = page.locator("#plan-inputs")
            expect(plan_inputs).to_have_attribute("data-fire-plan-inputs", "")
            assert plan_inputs.evaluate("(element) => element.open") is False
            expect(page.locator("[data-fire-plan-values]")).to_be_hidden()
            expect(page.locator("[data-fire-adjustments-view]")).to_be_hidden()
            expect(page.locator("[data-fire-loans]")).to_be_hidden()
            plan_inputs.locator("summary").focus()
            page.keyboard.press("Enter")
            assert plan_inputs.evaluate("(element) => element.open") is True
            expect(page.locator("[data-fire-plan-values]")).to_be_visible()
            expect(page.locator("[data-fire-adjustments-view]")).to_be_visible()
            expect(page.locator("[data-fire-loans]")).to_be_visible()
            expect(page.locator("[data-fire-sensitivity-grid]")).to_be_visible()
            expect(page.locator("[data-fire-sensitivity-refresh]")).to_have_count(0)
            expect(page.locator("[data-fire-progress='plan']")).to_be_visible()
            expect(page.locator("[data-fire-progress-bar]")).to_be_visible()
            expect(page.locator("[data-fire-progress-starting]")).to_contain_text("€")
            expect(page.locator("[data-fire-progress-target]")).to_be_visible()
            expect(page.locator("[data-fire-progress='plan']")).to_contain_text("Net worth at FIRE")
            expect(page.locator("[data-fire-progress='today']")).to_have_count(0)
            expect(page.locator("[data-fire-basis-toggle]")).to_have_count(0)
            expect(page.get_by_text("Plan progress")).to_be_visible()
            expect(page.get_by_text("Sheet progress")).to_have_count(0)
            fire_number = page.locator("[data-fire-number]")
            fire_year = fire_number.get_attribute("data-fire-number-year")
            assert fire_year
            expect(fire_number).to_contain_text(f"({fire_year})")
            expect(page.get_by_text("Today's money")).to_have_count(0)
            expect(
                page.locator('[data-fire-chart="net-worth"] [data-chart="line"]')
            ).to_have_attribute("data-chart-ready", "true")

            # Open the separate editor.
            with page.expect_navigation(wait_until="networkidle"):
                page.locator("[data-fire-edit]").click()
            assert page.url == f"{base}/fire/edit"
            expect(page.locator("[data-fire-form]")).to_have_attribute("data-fire-dirty", "0")
            for section_id in ("plan", "assumptions", "amounts", "adjustments", "loans"):
                assert page.locator(f"#{section_id}").evaluate("(element) => element.open") is False
            # The editor reveals Refresh grid; the grid itself is server-rendered and the
            # control re-fetches the saved plan's fragment rather than unsaved form values.
            refresh = page.locator("[data-fire-sensitivity-refresh]")
            expect(refresh).to_be_visible()
            with page.expect_response(
                lambda response: "/fire/sensitivity" in response.url
            ) as refresh_response:
                refresh.click()
            assert refresh_response.value.ok
            expect(page.locator("[data-fire-sensitivity-grid]")).to_be_visible()
            expect(page.locator('[data-fire-suggestion="income"]')).to_be_hidden()
            # The sections are an exclusive native accordion; the fallback follows the same rule.
            page.locator("#plan > summary").click()
            assert page.locator("#plan").evaluate("(element) => element.open") is True
            page.locator("#assumptions > summary").click()
            assert page.locator("#assumptions").evaluate("(element) => element.open") is True
            assert page.locator("#plan").evaluate("(element) => element.open") is False
            # Keyboard disclosure: Space closes the current section, Enter opens it again.
            page.locator("#assumptions > summary").focus()
            page.keyboard.press("Space")
            assert page.locator("#assumptions").evaluate("(element) => element.open") is False
            page.keyboard.press("Enter")
            assert page.locator("#assumptions").evaluate("(element) => element.open") is True
            page.locator("#amounts > summary").click()
            assert page.locator("#assumptions").evaluate("(element) => element.open") is False
            expect(page.locator('[data-fire-suggestion="income"]')).to_be_visible()
            expect(page.locator('[data-fire-suggestion="expenses"]')).to_be_visible()
            expect(
                page.locator('[data-fire-suggestion="income"] [data-fire-suggestion-categories]')
            ).to_be_visible()
            expect(
                page.locator('[data-fire-suggestion="expenses"] [data-fire-suggestion-categories]')
            ).to_be_visible()
            assert page.locator('input[name="income_manual"]').input_value() == "50000.00"
            edit_url = page.url

            # The portfolio card lists skipped sources one click away and without an amount.
            page.locator('[data-fire-suggestion="portfolio"] summary').click()
            skipped = page.locator("[data-fire-suggestion-skipped]")
            expect(skipped).to_be_visible()
            expect(skipped).to_contain_text("Linked browser item with a long name for wrapping")
            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_timeout(200)
            _no_page_overflow(page)
            page.set_viewport_size({"width": 1440, "height": 900})

            # The withdrawal rate stays in the layout; it is only disabled and dimmed for the
            # other rule. It never gets the hidden attribute or class.
            page.locator("#assumptions > summary").click()
            assert page.locator("#assumptions").evaluate("(element) => element.open") is True
            swr_wrapper = page.locator("[data-fire-swr-field]")
            swr_input = page.locator('input[name="swr"]')
            expect(swr_wrapper).to_be_visible()
            expect(swr_input).to_be_disabled()
            assert "hidden" not in (swr_wrapper.get_attribute("class") or "")
            expect(swr_wrapper).to_have_attribute("data-fire-swr-inactive", "1")
            expect(page.locator("[data-fire-swr-note]")).to_be_visible()
            page.locator('select[name="rule"]').select_option("swr")
            expect(swr_wrapper).to_be_visible()
            expect(swr_input).to_be_enabled()
            expect(swr_wrapper).to_have_attribute("data-fire-swr-inactive", "0")
            swr_input.fill("4")
            expect(page.locator("[data-fire-preview-note]")).to_be_visible()
            expect(page.locator("[data-fire-preview-error]")).to_have_count(0)
            page.locator('select[name="rule"]').select_option("returns_cover_expenses")
            expect(swr_wrapper).to_be_visible()
            expect(swr_input).to_be_disabled()
            assert "hidden" not in (swr_wrapper.get_attribute("class") or "")
            expect(page.locator("[data-fire-preview-note]")).to_be_visible()
            expect(page.locator("[data-fire-preview-error]")).to_have_count(0)

            expect(page.locator("[data-fire-basis-toggle]")).to_have_count(0)
            expect(page.get_by_text("Return above inflation (%)")).to_be_visible()
            expect(page.get_by_text("Real return")).to_have_count(0)

            # Every change recalculates server-side in place and never navigates or writes.
            page.locator("#amounts > summary").click()
            assert page.locator("#amounts").evaluate("(element) => element.open") is True
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

            # A preview error opens the section that owns the invalid field, even after the
            # user collapsed it; the form is not swapped.
            page.locator("#plan > summary").click()
            page.get_by_label("Horizon age").fill("41")
            expect(page.locator("[data-fire-preview-error]")).to_be_visible()
            page.locator("#assumptions > summary").click()
            assert page.locator("#plan").evaluate("(element) => element.open") is False
            page.locator('input[name="inflation"]').fill("2.6")
            expect(page.locator("[data-fire-preview-error]")).to_be_visible()
            expect(page.locator("#plan")).to_have_attribute("open", "")
            page.get_by_label("Horizon age").fill("90")
            expect(page.locator("[data-fire-preview-error]")).to_have_count(0)

            page.reload(wait_until="networkidle")
            assert page.locator('input[name="expenses_manual"]').input_value() == "30000.00"
            page.locator("#amounts > summary").click()

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
            page.locator("#plan-inputs > summary").click()
            expect(page.locator("[data-fire-plan-values]")).to_contain_text("€30,000.00")

            # Discard reloads the saved values without a prompt and shows a notice.
            page.goto(f"{base}/fire/edit", wait_until="networkidle")
            page.locator("#amounts > summary").click()
            page.locator('input[name="expenses_manual"]').fill("70000")
            expect(page.locator("[data-fire-dirty-status]")).to_be_visible()
            page.locator("[data-fire-discard]").click()
            page.wait_for_url(f"{base}/fire/edit?discarded=1")
            assert page.locator('input[name="expenses_manual"]').input_value() == "30000.00"
            expect(page.locator("[data-action-notice]")).to_contain_text(
                "Unsaved changes discarded."
            )

            # Save persists and returns to the read-only view.
            page.locator("#amounts > summary").click()
            page.locator('input[name="expenses_manual"]').fill("80000")
            with page.expect_navigation(wait_until="networkidle"):
                page.locator("[data-fire-save]").click()
            assert page.url == f"{base}/fire?notice=saved"
            expect(page.locator("[data-action-notice]")).to_be_visible()
            page.locator("#plan-inputs > summary").click()
            expect(page.locator("[data-fire-plan-values]")).to_contain_text("€80,000.00")

            # F8.16: a plan whose values were last reviewed in an earlier year opens Starting
            # amounts on the editor; the view banner link and #amounts do the same.
            from sqlalchemy import select, update

            with app.state.session_factory() as session:
                plan_id = session.scalars(
                    select(FireScenario.id).where(FireScenario.is_live.is_(True))
                ).one()
            with app.state.session_factory() as session, session.begin():
                session.execute(
                    update(FireScenario)
                    .where(FireScenario.id == plan_id)
                    .values(updated_at=datetime(start_year - 1, 1, 1, tzinfo=UTC))
                )
            page.goto(f"{base}/fire", wait_until="networkidle")
            expect(page.locator("[data-fire-year-review]")).to_be_visible()
            with page.expect_navigation(wait_until="networkidle"):
                page.locator("[data-fire-year-review] a").click()
            assert page.url == f"{base}/fire/edit#amounts"
            assert page.locator("#amounts").evaluate("(element) => element.open") is True
            page.goto(f"{base}/fire/edit", wait_until="networkidle")
            expect(page.locator("[data-fire-year-review]")).to_be_visible()
            assert page.locator("#amounts").evaluate("(element) => element.open") is True

            # The editor still works when the saved projection is blocked; preview recovers.
            current_age = 41
            with app.state.session_factory() as session, session.begin():
                session.get_one(FireScenario, plan_id).horizon_age = current_age
            page.goto(f"{base}/fire/edit", wait_until="networkidle")
            expect(page.locator("[data-fire-blocked]")).to_be_visible()
            assert page.locator("script[src*='charts.js']").count() == 1
            page.locator("#plan > summary").click()
            page.get_by_label("Horizon age").fill("90")
            expect(
                page.locator('[data-fire-chart="net-worth"] [data-chart="line"]')
            ).to_have_attribute("data-chart-ready", "true")
            # Restore the saved plan so the no-JavaScript grid and save checks use a valid plan.
            with app.state.session_factory() as session, session.begin():
                session.get_one(FireScenario, plan_id).horizon_age = 90

            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_timeout(200)
            _no_page_overflow(page)
            page.goto(f"{base}/fire/edit", wait_until="networkidle")
            for section_id in ("plan", "assumptions", "amounts", "adjustments", "loans"):
                page.locator(f"#{section_id} > summary").click()
                page.wait_for_timeout(100)
                _no_page_overflow(page)
            page.goto(f"{base}/fire", wait_until="networkidle")
            page.locator("#plan-inputs > summary").click()
            page.wait_for_timeout(200)
            _no_page_overflow(page)
            page.set_viewport_size({"width": 1440, "height": 900})

            # Without JavaScript the withdrawal-rate field is still visible and enabled, so a
            # no-JS user can choose the rule and enter a rate. Closed sections still submit.
            storage = context.storage_state()
            no_js_context = browser.new_context(
                viewport={"width": 1440, "height": 900},
                java_script_enabled=False,
                storage_state=storage,
            )
            no_js_page = no_js_context.new_page()
            no_js_page.goto(f"{base}/fire/edit", wait_until="load")
            no_js_page.locator("#assumptions > summary").click()
            expect(no_js_page.locator("[data-fire-swr-field]")).to_be_visible()
            expect(no_js_page.locator('input[name="swr"]')).to_be_enabled()
            expect(no_js_page.locator("[data-fire-sensitivity-grid]")).to_be_visible()
            expect(no_js_page.locator("[data-fire-sensitivity-refresh]")).to_be_hidden()
            no_js_page.locator("#amounts > summary").click()
            expect(no_js_page.locator('input[name="income_manual"]')).to_be_visible()
            no_js_page.locator('input[name="income_manual"]').fill("51000")
            with no_js_page.expect_navigation(wait_until="load"):
                no_js_page.locator("[data-fire-save]").click()
            assert "/fire?notice=saved" in no_js_page.url
            no_js_page.locator("#plan-inputs > summary").click()
            expect(no_js_page.locator("[data-fire-plan-values]")).to_contain_text("€51,000.00")
            no_js_page.set_viewport_size({"width": 390, "height": 844})
            _no_page_overflow(no_js_page)
            no_js_context.close()

            assert page.evaluate("window.__cspViolations") == []
            browser.close()
