"""The Recurring page in Chromium (F3.1/F3.2): the guided form shows the fields of the chosen
"How often", offers only the categories of the chosen kind and updates its "Next dates" panel
while typing; the saved item is listed under Expenses with its schedule sentence; the list does
not scroll the page sideways on a phone. No CSP violations. Set ASSETBOARD_RECURRING_SCREENSHOTS
to a folder to save light and dark screenshots of the list and the form."""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import Page, expect, sync_playwright

from assetboard.app import create_app
from assetboard.config import Settings
from assetboard.services.auth import run_initial_setup

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


def _shoot(page: Page, folder: str | None, name: str) -> None:
    if not folder:
        return
    Path(folder).mkdir(parents=True, exist_ok=True)
    for scheme in ("light", "dark"):
        page.emulate_media(color_scheme=scheme)
        # theme.js applies the scheme on load; switch the class for this page as it is.
        page.evaluate(
            "(dark) => document.documentElement.classList.toggle('dark', dark)", scheme == "dark"
        )
        page.screenshot(path=str(Path(folder) / f"{name}-{scheme}.png"), full_page=True)
    page.emulate_media(color_scheme="light")
    page.evaluate("document.documentElement.classList.remove('dark')")


def test_guided_recurring_form_in_a_browser(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'browser.db'}",
            secret_key="browser-test-key",
        )
    )
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    server_thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    server_thread.start()
    deadline = time.monotonic() + 10
    while not server.started and server_thread.is_alive() and time.monotonic() < deadline:
        threading.Event().wait(0.01)

    try:
        assert server.started
        run_initial_setup(
            app.state.session_factory,
            email="admin@example.com",
            display_name="Browser Admin",
            password=PASSWORD,
            workspace_name="Browser Home",
            settings=app.state.settings,
            clock=app.state.clock,
        )
        base = f"http://127.0.0.1:{port}"
        shots = os.environ.get("ASSETBOARD_RECURRING_SCREENSHOTS")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(
                color_scheme="light", viewport={"width": 1280, "height": 900}
            )
            context.add_init_script(VIOLATION_RECORDER)
            page = context.new_page()
            page.goto(f"{base}/login", wait_until="networkidle")
            page.get_by_label("Email").fill("admin@example.com")
            page.get_by_label("Password").fill(PASSWORD)
            page.get_by_role("button", name="Sign in").click()
            page.wait_for_url(f"{base}/")

            page.goto(f"{base}/recurring", wait_until="networkidle")
            empty = page.locator("[data-recurring-empty]")
            expect(empty).to_be_visible()
            empty.get_by_role("link", name="New recurring item").click()
            page.wait_for_url(f"{base}/recurring/new")
            page.wait_for_load_state("networkidle")

            sentence = page.locator("[data-schedule-sentence]")
            dates = page.locator("[data-preview-dates] time")
            weekly = page.locator('[data-repeat-fields="weekly"]')
            monthly = page.locator('[data-repeat-fields="monthly"]')
            every = page.locator('[data-repeat-fields="every_months"]')
            yearly = page.locator('[data-repeat-fields="yearly"]')

            # Monthly is the default: only the day of the month applies.
            expect(monthly).to_be_visible()
            for hidden in (weekly, every, yearly):
                expect(hidden).to_be_hidden()
            expect(sentence).to_contain_text("Monthly on the 1st")
            expect(dates).to_have_count(5)

            page.get_by_label("Name").fill("Rent")
            page.locator("#amount").fill("1200")

            # The category list follows Expense or Income.
            category = page.get_by_label("Category")
            income_group = page.locator('#category_id optgroup[data-kind="income"]')
            expense_group = page.locator('#category_id optgroup[data-kind="expense"]')
            expect(income_group).to_have_js_property("disabled", True)
            expect(expense_group).to_have_js_property("disabled", False)
            category.select_option(label="Housing")
            page.locator('[data-direction-option="income"]').click()
            expect(category).to_have_value("")
            expect(income_group).to_have_js_property("disabled", False)
            expect(expense_group).to_have_js_property("disabled", True)
            page.locator('[data-direction-option="expense"]').click()
            category.select_option(label="Housing")

            page.get_by_label("On which day of the month?").select_option("10")
            expect(sentence).to_have_text("Monthly on the 10th")

            page.locator('[data-repeat-option="weekly"]').click()
            expect(weekly).to_be_visible()
            for hidden in (monthly, every, yearly):
                expect(hidden).to_be_hidden()
            weekly.get_by_label("Monday").check()
            expect(sentence).to_contain_text("Weekly on Monday")
            expect(dates).to_have_count(5)

            page.locator('[data-repeat-option="every_months"]').click()
            expect(every).to_be_visible()
            expect(monthly).to_be_visible()
            expect(weekly).to_be_hidden()
            page.get_by_label("Every how many months?").fill("3")
            expect(sentence).to_contain_text("Every 3 months on the 10th")

            page.locator('[data-repeat-option="yearly"]').click()
            expect(yearly).to_be_visible()
            expect(monthly).to_be_hidden()
            yearly.get_by_label("Month").select_option(label="March")
            yearly.get_by_label("Day").select_option("31")
            expect(sentence).to_contain_text("Yearly on 31 March")

            # The end date field shows only for "On a date".
            end_date = page.locator("#end_date")
            expect(end_date).to_be_hidden()
            page.get_by_label("On a date").check()
            expect(end_date).to_be_visible()
            page.get_by_label("Never").check()

            page.locator('[data-repeat-option="monthly"]').click()
            expect(sentence).to_contain_text("Monthly on the 10th")
            _shoot(page, shots, "form")
            page.get_by_role("button", name="Save recurring item").click()
            page.wait_for_url(f"{base}/recurring")

            expenses = page.locator('[data-recurring-section="expense"]')
            expect(expenses.get_by_role("heading", name="Expenses")).to_be_visible()
            expect(expenses.locator("[data-schedule]")).to_have_text("Monthly on the 10th")
            expect(expenses.locator("[data-category]")).to_contain_text("Housing")
            expect(page.locator('[data-recurring-section="income"]')).to_contain_text(
                "No recurring income yet."
            )
            _shoot(page, shots, "list")

            # Phone width: no sideways scrolling of the page.
            page.set_viewport_size({"width": 375, "height": 800})
            page.reload(wait_until="networkidle")
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            _shoot(page, shots, "list-phone")
            page.goto(f"{base}/recurring/new", wait_until="networkidle")
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            assert page.evaluate("window.__cspViolations") == []
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
