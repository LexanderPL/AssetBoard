""" "Add income" in Chromium: the four steps with conditional fields, the live payday sentence
and the tax example, then the Income page cards. No CSP violations. Set
ASSETBOARD_INCOME_SCREENSHOTS to a folder to save screenshots."""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright

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


def test_add_income_wizard_in_a_browser(tmp_path: Path) -> None:
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
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context()
            context.add_init_script(VIOLATION_RECORDER)
            page = context.new_page()
            page.goto(f"{base}/login", wait_until="networkidle")
            page.get_by_label("Email").fill("admin@example.com")
            page.get_by_label("Password").fill(PASSWORD)
            page.get_by_role("button", name="Sign in").click()
            page.wait_for_url(f"{base}/")

            shots = os.environ.get("ASSETBOARD_INCOME_SCREENSHOTS")
            page.set_viewport_size({"width": 1280, "height": 900})
            page.goto(f"{base}/workspace/settings", wait_until="networkidle")
            page.locator("[data-taxes-switch]").click()
            page.wait_for_load_state("networkidle")
            for name, kind, rate, day in (
                ("VAT", "I do. It is added on top of what I charge", "23", "25"),
                ("Income tax", "I do, from my income", "12", "20"),
            ):
                page.goto(f"{base}/taxes/new", wait_until="networkidle")
                page.get_by_label("Name").fill(name)
                page.get_by_label(kind).check()
                page.get_by_label("Rate in %").fill(rate)
                page.get_by_label("On day").fill(day)
                page.get_by_role("button", name="Save tax").click()
                page.wait_for_load_state("networkidle")

            def add_fixed(name: str, amount: str) -> None:
                page.goto(f"{base}/income/new", wait_until="networkidle")
                page.get_by_label("Name").fill(name)
                page.get_by_role("button", name="Next").click()
                page.get_by_label("Amount you are paid each month", exact=False).fill(amount)
                page.get_by_role("button", name="Next").click()
                page.get_by_role("button", name="Next").click()
                page.get_by_role("button", name="Save income").click()
                page.wait_for_load_state("networkidle")

            add_fixed("Salary", "8500")
            add_fixed("Flat rental", "3000")

            page.goto(f"{base}/income", wait_until="networkidle")
            page.locator("[data-add-income]").click()
            page.wait_for_load_state("networkidle")
            expect(page.locator("[data-wizard-steps] li")).to_have_count(4)
            page.get_by_label("Name").fill("Contract work")
            page.get_by_label("Per day or hour").check()
            if shots:
                page.screenshot(path=os.path.join(shots, "income-b-step1.png"), full_page=True)
            page.get_by_role("button", name="Next").click()
            page.wait_for_load_state("networkidle")
            # Only the field of the chosen estimate is shown.
            units = page.get_by_label("Units a month")
            months = page.get_by_label("Number of months")
            expect(units).to_be_hidden()
            expect(months).to_be_hidden()
            estimate = page.get_by_label("Until you log the real number", exact=False)
            estimate.select_option("fixed")
            expect(units).to_be_visible()
            estimate.select_option("average")
            expect(months).to_be_visible()
            expect(units).to_be_hidden()
            estimate.select_option("working_days")
            page.get_by_label("Rate", exact=False).fill("1200")
            page.get_by_role("button", name="Next").click()
            page.wait_for_load_state("networkidle")
            sentence = page.locator("[data-payday-sentence]")
            picker = page.locator("[data-month-picker='active_from']")
            picker.get_by_label("Month").select_option("10")
            picker.get_by_label("Year").select_option("2026")
            expect(sentence).to_have_text("Work done in October is paid on November 10.")
            page.get_by_label("Which month").select_option("0")
            expect(sentence).to_have_text("Work done in October is paid on October 10.")
            page.get_by_label("Which month").select_option("1")
            expect(sentence).to_contain_text("November 10")
            page.get_by_role("button", name="Next").click()
            page.wait_for_load_state("networkidle")
            example = page.locator("[data-income-example]")
            expect(example).to_contain_text("No tax payments are planned.")
            page.get_by_label("VAT 23%").check()
            expect(example).to_contain_text("You receive €12,300.00.")
            page.get_by_label("Income tax 12%").check()
            expect(example).to_contain_text("Income tax €1,200.00 to pay on")
            if shots:
                page.screenshot(path=os.path.join(shots, "income-b-step4.png"), full_page=True)
            page.get_by_role("button", name="Save income").click()
            page.wait_for_load_state("networkidle")

            card = page.locator("[data-income-source]", has_text="Contract work")
            expect(card).to_contain_text("VAT 23% · added on top")
            expect(card).to_contain_text("Income tax 12% · paid by you")
            expect(card.locator("th", has_text="Tax to pay")).to_be_visible()
            rental = page.locator("[data-income-source]", has_text="Flat rental")
            expect(rental).to_contain_text("No taxes on this income.")
            rental.get_by_role("link", name="Add taxes").click()
            page.wait_for_load_state("networkidle")
            expect(
                page.locator("[data-income-source]", has_text="Flat rental").locator(
                    "[data-income-taxes]"
                )
            ).to_be_visible()
            page.goto(f"{base}/income", wait_until="networkidle")
            if shots:
                page.screenshot(path=os.path.join(shots, "income-b-list.png"), full_page=True)
            # Phone width: the payments table scrolls inside its box, not the page.
            page.set_viewport_size({"width": 375, "height": 800})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            assert page.evaluate("window.__cspViolations") == []
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
