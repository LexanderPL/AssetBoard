"""Popovers and dialogs in Chromium (UI batch 3): a press outside an open popover closes it,
Escape closes the innermost one and returns the focus to its summary, opening one closes the
others; the History page's Export and Filters dialogs close on a backdrop click, the confirm
dialog does not; inline forms in <details> stay open. Also the planned rows' Confirm and Delete
buttons line up. No CSP violations."""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import FloatRect, Page, expect, sync_playwright

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


def _add_planned(page: Page, label: str) -> None:
    rows = page.locator("#ledger-expense tr[data-pending-occurrence]")
    count = rows.count()
    page.locator("#entry-expense-day").fill("20")
    page.locator("#entry-expense-amount").fill("250")
    page.locator("#entry-expense-description").fill(label)
    page.locator("#entry-expense-category").select_option(index=1)
    page.locator("#entry-expense-amount").press("Enter")
    expect(rows).to_have_count(count + 1)


def _box(page: Page, selector: str) -> FloatRect:
    box = page.locator(selector).first.bounding_box()
    assert box is not None, selector
    return box


def test_popovers_and_dialogs_close_from_outside_and_with_escape(tmp_path: Path) -> None:
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
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            context.add_init_script(VIOLATION_RECORDER)
            page = context.new_page()
            page.goto(f"{base}/login", wait_until="networkidle")
            page.get_by_label("Email").fill("admin@example.com")
            page.get_by_label("Password").fill(PASSWORD)
            page.get_by_role("button", name="Sign in").click()
            page.wait_for_url(f"{base}/")

            # A planned (future) entry next month: its row has Confirm and Delete.
            today = app.state.clock.today("UTC")
            year, month = (
                (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
            )
            transactions = f"{base}/transactions/{year:04d}-{month:02d}"
            page.goto(transactions, wait_until="networkidle")
            _add_planned(page, "Planned insurance")

            # Confirm and Delete line up: same height and the same vertical centre.
            confirm_box = _box(page, "[data-pending-actions] summary")
            delete_box = _box(page, "[data-pending-actions] > form[data-confirm] button")
            assert confirm_box["height"] == pytest.approx(delete_box["height"], abs=0.5)
            assert confirm_box["y"] == pytest.approx(delete_box["y"], abs=0.5)

            confirm = page.locator("[data-pending-actions] details[data-popover]").first
            user_menu = page.locator("[data-user-menu] details[data-popover]")
            picker = page.locator("details[data-month-picker]")

            # Filter-like popover: the Confirm form. A press outside closes it.
            confirm.locator("summary").click()
            expect(confirm).to_have_attribute("open", "")
            page.locator("h1").click()
            expect(confirm).not_to_have_attribute("open", "")
            # Pressing inside (the amount field) keeps it open.
            confirm.locator("summary").click()
            confirm.locator("input[name=amount]").click()
            expect(confirm).to_have_attribute("open", "")
            # Escape closes it and puts the focus back on its summary.
            page.keyboard.press("Escape")
            expect(confirm).not_to_have_attribute("open", "")
            expect(confirm.locator("summary")).to_be_focused()

            # Opening one popover closes the others (Confirm, then the user menu, then the
            # month picker).
            confirm.locator("summary").click()
            user_menu.locator("summary").click()
            expect(user_menu).to_have_attribute("open", "")
            expect(confirm).not_to_have_attribute("open", "")
            picker.locator("summary").click()
            expect(picker).to_have_attribute("open", "")
            expect(user_menu).not_to_have_attribute("open", "")
            page.keyboard.press("Escape")
            expect(picker).not_to_have_attribute("open", "")
            expect(picker.locator("summary")).to_be_focused()

            # The user menu: outside press and Escape.
            user_menu.locator("summary").click()
            expect(user_menu.get_by_role("link", name="Profile")).to_be_visible()
            page.locator("main").click(position={"x": 5, "y": 5})
            expect(user_menu).not_to_have_attribute("open", "")
            user_menu.locator("summary").click()
            page.keyboard.press("Escape")
            expect(user_menu).not_to_have_attribute("open", "")
            expect(user_menu.locator("summary")).to_be_focused()

            # Still works after an htmx swap replaced the table (a second planned entry).
            _add_planned(page, "Planned gym")
            swapped = page.locator("[data-pending-actions] details[data-popover]").last
            swapped.locator("summary").click()
            expect(swapped).to_have_attribute("open", "")
            page.locator("h1").click()
            expect(swapped).not_to_have_attribute("open", "")

            # The destructive confirm dialog does not close on a backdrop click.
            page.locator("[data-pending-actions] form[data-confirm] button").first.click()
            dialog = page.locator("#confirm-dialog")
            expect(dialog).to_be_visible()
            page.mouse.click(5, 5)
            expect(dialog).to_be_visible()
            page.keyboard.press("Escape")
            expect(dialog).to_be_hidden()

            # An inline form in a <details> (Upcoming: Mark paid or received) stays open.
            page.goto(f"{base}/upcoming", wait_until="networkidle")
            # Its summaries line up with the plain buttons.
            summary_box = _box(page, "[data-occurrence-actions] details summary")
            skip_box = _box(page, "[data-occurrence-actions] > form button")
            assert summary_box["height"] == pytest.approx(skip_box["height"], abs=0.5)
            assert summary_box["y"] == pytest.approx(skip_box["y"], abs=0.5)
            inline = page.locator("[data-occurrence-actions] details").first
            inline.locator("summary").click()
            page.locator("h1").click()
            expect(inline).to_have_attribute("open", "")

            # History: Export and Filters close on a backdrop click and with Escape.
            page.goto(f"{base}/history", wait_until="networkidle")
            for opener, dialog_id in (
                ("[data-export-open]", "#export-dialog"),
                ("[data-filters-open]", "#filters-dialog"),
            ):
                modal = page.locator(dialog_id)
                page.locator(opener).click()
                expect(modal).to_be_visible()
                modal.locator("h2").click()  # a press inside keeps it
                expect(modal).to_be_visible()
                page.mouse.click(5, 5)
                expect(modal).to_be_hidden()
                page.locator(opener).click()
                expect(modal).to_be_visible()
                page.keyboard.press("Escape")
                expect(modal).to_be_hidden()

            assert page.evaluate("window.__cspViolations") == []
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
