"""Work calendar in Chromium (F3.7): toggle days with the mouse and the keyboard, edit a day
value, fill the working days and clear the month (confirmation dialog); then a one-time
recurring item, whose repeat fields hide. No CSP violations."""

from __future__ import annotations

import os
import socket
import threading
import time
from datetime import date, timedelta
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


def test_work_calendar_in_a_browser(tmp_path: Path) -> None:
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

            page.goto(f"{base}/income", wait_until="networkidle")
            page.get_by_label("Name").first.fill("Consulting")
            page.get_by_label("Rate per unit", exact=False).first.fill("100")
            page.get_by_role("button", name="Create income source").click()
            page.wait_for_load_state("networkidle")
            page.locator("[data-work-calendar-link]").click()
            page.wait_for_load_state("networkidle")

            # A fixed month: August 2026 starts on a Saturday and has 21 weekdays.
            page.goto(page.url.split("?")[0] + "?month=2026-08", wait_until="networkidle")
            total = page.locator("[data-calendar-total]")
            expect(total).to_contain_text("Month total: 0")
            expect(page.locator('[data-day="2026-08-01"]')).to_have_attribute("data-weekend", "")

            monday = page.locator('[data-day="2026-08-03"]')
            monday.click()
            expect(page.locator('[data-day="2026-08-03"]')).to_have_attribute(
                "aria-pressed", "true"
            )
            expect(total).to_contain_text("Month total: 1 (day)")

            # Keyboard: focus a day button and press Space.
            page.locator('[data-day="2026-08-04"]').focus()
            page.keyboard.press("Space")
            expect(page.locator('[data-day="2026-08-04"]')).to_have_attribute(
                "aria-pressed", "true"
            )
            expect(total).to_contain_text("Month total: 2 (day)")

            day_value = page.locator('[data-day-value="2026-08-03"]')
            day_value.get_by_role("textbox").fill("0.5")
            day_value.get_by_role("button", name="Save").click()
            expect(total).to_contain_text("Month total: 1.5 (day)")

            page.locator("[data-calendar-fill]").click()
            expect(total).to_contain_text("Month total: 20.5 (day)")

            page.locator("[data-calendar-clear]").click()
            confirm = page.locator("#confirm-dialog")
            expect(confirm).to_contain_text("Clear every day of August 2026?")
            page.get_by_role("button", name="Continue").click()
            expect(total).to_contain_text("Month total: 0")
            expect(page.locator('[data-day="2026-08-03"]')).to_have_attribute(
                "aria-pressed", "false"
            )

            page.locator("[data-calendar-next]").click()
            expect(page.locator("[data-calendar-month]")).to_have_text("September 2026")

            # A one-time item: choosing "One time" hides the repeat fields.
            page.goto(f"{base}/recurring/new", wait_until="networkidle")
            repeat_fields = page.locator('[data-hidden-when="freq=once"]')
            expect(repeat_fields).to_be_visible()
            page.get_by_label("Name").fill("Wedding gift")
            page.get_by_label("Amount", exact=True).fill("120")
            page.get_by_label("Start date").fill((date.today() + timedelta(days=30)).isoformat())
            page.get_by_label("Repeat").select_option("once")
            expect(repeat_fields).to_be_hidden()
            page.get_by_role("button", name="Save recurring item").click()
            page.wait_for_load_state("networkidle")
            expect(page.locator("[data-one-time]")).to_contain_text("One time on")
            assert page.evaluate("window.__cspViolations") == []
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
