"""Taxes in Chromium (ADR-0022): switch Taxes on in Workspace settings, create a tax with the
radio cards, watch the example panel follow the kind and the rate, and see its card on the
Taxes page. No CSP violations. Set ASSETBOARD_TAX_SCREENSHOTS to a folder to save screenshots."""

from __future__ import annotations

import os
import re
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


def test_new_tax_flow_in_a_browser(tmp_path: Path) -> None:
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

            shots = os.environ.get("ASSETBOARD_TAX_SCREENSHOTS")
            page.goto(f"{base}/workspace/settings", wait_until="networkidle")
            switch = page.locator("[data-taxes-switch]")
            expect(switch).to_have_attribute("aria-pressed", "false")
            switch.click()
            page.wait_for_load_state("networkidle")
            expect(page.locator("[data-taxes-switch]")).to_have_attribute("aria-pressed", "true")
            expect(page.locator("[data-taxes-link]")).to_be_visible()

            page.goto(f"{base}/taxes/new", wait_until="networkidle")
            example = page.locator("[data-tax-example]")
            schedule = page.locator("[data-tax-schedule]")
            page.get_by_label("Name").fill("Income tax")
            page.get_by_label("I do, from my income").check()
            page.get_by_label("Rate in %").fill("12")
            page.get_by_label("Rate in %").dispatch_event("change")
            expect(example).to_contain_text("Income tax (to pay later)")
            expect(example).to_contain_text("8,800.00 is really yours")
            expect(page.get_by_label("Amount per payment (EUR)")).to_be_hidden()
            page.get_by_label("A fixed amount each time").check()
            expect(page.get_by_label("Amount per payment (EUR)")).to_be_visible()
            expect(page.get_by_label("Rate in %")).to_be_hidden()
            page.get_by_label("A percentage").check()
            page.get_by_label("The payer takes it off before paying me").check()
            expect(schedule).to_be_hidden()
            expect(example).to_contain_text("nothing is planned")
            page.get_by_label("I do. It is added on top of what I charge").check()
            expect(schedule).to_be_visible()
            expect(page.locator("[data-tax-base]")).to_be_hidden()
            expect(example.locator("[data-example-receive]")).to_have_text("€11,200.00")
            page.get_by_label("I do, from my income").check()
            page.get_by_label("On day").fill("25")
            page.get_by_label("On day").dispatch_event("change")
            category = page.get_by_label("Record it in category")
            assert category.evaluate("s => s.selectedOptions[0].text") == "Taxes"
            expect(example).to_contain_text("8,800.00 is really yours")
            # The example pays on the 25th of a month that depends on today's date.
            expect(example).to_contain_text(re.compile(r"on [A-Z][a-z]{2} 25, \d{4}"))
            if shots:
                page.screenshot(path=os.path.join(shots, "taxes-a-new.png"), full_page=True)
            page.get_by_role("button", name="Save tax").click()
            page.wait_for_load_state("networkidle")
            card = page.locator("[data-tax-rule]")
            expect(card).to_contain_text("Income tax")
            expect(card).to_contain_text("12%")
            expect(card).to_contain_text("Paid on the 25th of the month after you are paid")
            page.get_by_text("Add another tax").click()
            page.get_by_label("Name", exact=True).fill("Real estate tax")
            page.get_by_label("Amount (EUR)").fill("1,240.00")
            page.get_by_role("button", name="Save", exact=True).click()
            page.wait_for_load_state("networkidle")
            expect(page.locator("[data-other-tax]")).to_contain_text("Real estate tax")
            if shots:
                page.screenshot(path=os.path.join(shots, "taxes-a-list.png"), full_page=True)
            assert page.evaluate("window.__cspViolations") == []
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
