"""Income log editing, one-time payments and Skip in Chromium (UI batch 3): a payment row's
inline Edit opens focused, Escape cancels and restores the logged units, Save changes them; the
Log form prefills a month that has a log and says "Update"; Upcoming adds a one-time payment;
a recurring expected row in the month ledger is skipped (greyed out, with Undo) and comes back,
its Skip button lined up with Confirm. No CSP violations.

With ``ASSETBOARD_SHOTS_DIR`` set, screenshots of the three places are saved in light and
dark."""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import Browser, FloatRect, Page, expect, sync_playwright

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


def _box(page: Page, selector: str) -> FloatRect:
    box = page.locator(selector).first.bounding_box()
    assert box is not None, selector
    return box


def _sign_in(browser: Browser, base: str, scheme: str = "light") -> Page:
    context = browser.new_context(
        viewport={"width": 1280, "height": 900},
        color_scheme="dark" if scheme == "dark" else "light",
    )
    context.add_init_script(VIOLATION_RECORDER)
    page = context.new_page()
    page.goto(f"{base}/login", wait_until="networkidle")
    page.get_by_label("Email").fill("admin@example.com")
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(f"{base}/")
    return page


def _shots(browser: Browser, base: str, pages: dict[str, tuple[str, str]]) -> None:
    folder = os.environ.get("ASSETBOARD_SHOTS_DIR")
    if not folder:
        return
    Path(folder).mkdir(parents=True, exist_ok=True)
    for scheme in ("light", "dark"):
        page = _sign_in(browser, base, scheme)
        for name, (path, selector) in pages.items():
            page.goto(f"{base}{path}", wait_until="networkidle")
            target = page.locator(selector).first
            if name == "income-edit":
                target.locator("details[data-inline-edit] summary").first.click()
            if name == "upcoming-one-time":
                page.locator("[data-add-one-time] > summary").click()
            target.screenshot(path=str(Path(folder) / f"{name}-{scheme}.png"))
        page.context.close()


def test_log_edit_one_time_payment_and_skip(tmp_path: Path) -> None:
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
        today = app.state.clock.today("UTC")
        previous = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        following = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = _sign_in(browser, base)

            # A per-day income paid on the 10th of the next month.
            page.goto(f"{base}/income", wait_until="networkidle")
            page.locator("[data-add-income]").click()
            page.wait_for_load_state("networkidle")
            page.get_by_label("Name").fill("Consulting")
            page.get_by_label("Per day or hour").check()
            page.get_by_role("button", name="Next").click()
            page.get_by_label("Rate", exact=False).fill("100")
            page.get_by_role("button", name="Next").click()
            picker = page.locator("[data-month-picker='active_from']")
            picker.get_by_label("Month").select_option(str(previous[1]))
            picker.get_by_label("Year").select_option(str(previous[0]))
            page.get_by_role("button", name="Save income").click()
            page.wait_for_load_state("networkidle")

            # Log this month (the form's default month): 5 days.
            log = page.locator("details[data-log-work]")
            log.locator("summary").click()
            form = log.locator("form[data-log-form]")
            expect(form.locator("[data-log-submit]")).to_have_text("Save work log")
            form.locator("[data-log-units]").fill("5")
            form.locator("[data-log-submit]").click()
            page.wait_for_load_state("networkidle")

            # The payments row has Edit: it opens focused; Escape cancels and restores 5.
            month_key = f"{today:%Y-%m}"
            edit = page.locator(f"details[data-log-edit='{month_key}']")
            units = edit.locator("[data-inline-edit-input]")
            edit.locator("summary").click()
            expect(edit).to_have_attribute("open", "")
            expect(units).to_be_focused()
            expect(units).to_have_value("5")
            units.fill("9")
            page.keyboard.press("Escape")
            expect(edit).not_to_have_attribute("open", "")
            expect(edit.locator("summary")).to_be_focused()
            expect(units).to_have_value("5")
            # Cancel does the same.
            edit.locator("summary").click()
            units.fill("8")
            edit.get_by_role("button", name="Cancel").click()
            expect(edit).not_to_have_attribute("open", "")
            expect(units).to_have_value("5")
            # Save changes the log.
            edit.locator("summary").click()
            units.fill("7")
            edit.get_by_role("button", name="Save").click()
            page.wait_for_load_state("networkidle")
            row_units = page.locator(f"details[data-log-edit='{month_key}']").locator(
                "xpath=ancestor::td[1]"
            )
            expect(row_units.locator("[data-logged-units]")).to_contain_text("7")

            # The Log form prefills a month with a log and says "Update"; another month not.
            log = page.locator("details[data-log-work]")
            log.locator("summary").click()
            form = log.locator("form[data-log-form]")
            month_select = form.locator("select[name='work_month_month']")
            year_select = form.locator("select[name='work_month_year']")
            expect(form.locator("[data-log-units]")).to_have_value("7")
            expect(form.locator("[data-log-submit]")).to_have_text("Update")
            year_select.select_option(str(previous[0]))
            month_select.select_option(str(previous[1]))
            expect(form.locator("[data-log-units]")).to_have_value("")
            expect(form.locator("[data-log-submit]")).to_have_text("Save work log")
            year_select.select_option(str(today.year))
            month_select.select_option(str(today.month))
            expect(form.locator("[data-log-units]")).to_have_value("7")
            expect(form.locator("[data-log-submit]")).to_have_text("Update")

            # Upcoming: add a one-time payment.
            page.goto(f"{base}/upcoming", wait_until="networkidle")
            page.locator("[data-add-one-time] > summary").click()
            one_time = page.locator("form[data-one-time-form]")
            one_time.get_by_label("Description").fill("Car insurance")
            one_time.get_by_label("Amount", exact=False).fill("450")
            one_time.get_by_label("Date").fill(f"{following[0]:04d}-{following[1]:02d}-05")
            one_time.get_by_label("Category").select_option(index=1)
            one_time.get_by_role("button", name="Add payment").click()
            page.wait_for_load_state("networkidle")
            expect(page.locator("[data-upcoming-notice]")).to_contain_text("One-time payment")

            # The income is expected next month: Skip it in that month's ledger, then Undo.
            month_path = f"/transactions/{following[0]:04d}-{following[1]:02d}"
            page.goto(f"{base}{month_path}", wait_until="networkidle")
            pending = page.locator("#ledger-income tr[data-pending-occurrence]").first
            expect(pending.locator("[data-skip]")).to_be_visible()
            confirm_box = _box(page, "#ledger-income [data-pending-actions] summary")
            skip_box = _box(page, "#ledger-income [data-pending-actions] [data-skip]")
            assert confirm_box["height"] == pytest.approx(skip_box["height"], abs=0.5)
            assert confirm_box["y"] == pytest.approx(skip_box["y"], abs=0.5)
            pending.locator("[data-skip]").click()
            page.wait_for_load_state("networkidle")
            skipped = page.locator("#ledger-income tr[data-skipped-occurrence]")
            expect(skipped).to_have_count(1)
            expect(skipped).to_contain_text("Skipped")
            expect(page.locator("#ledger-income tr[data-pending-occurrence]")).to_have_count(0)
            assert page.evaluate("window.__cspViolations") == []

            _shots(
                browser,
                base,
                {
                    "income-edit": ("/income", "section[data-income-source]"),
                    "upcoming-one-time": ("/upcoming", "main"),
                    "ledger-skipped": (month_path, "#ledger-income"),
                },
            )

            skipped.locator("[data-unskip]").click()
            page.wait_for_load_state("networkidle")
            expect(page.locator("#ledger-income tr[data-skipped-occurrence]")).to_have_count(0)
            expect(page.locator("#ledger-income tr[data-pending-occurrence]")).to_have_count(1)

            assert page.evaluate("window.__cspViolations") == []
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
