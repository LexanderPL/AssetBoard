"""Transactions on the month page in Chromium (F2.2 part B): the keyboard flow of the entry row,
autocomplete, inline edit, delete with the undo toast, a closed month, a viewer, and no CSP
violations."""

from __future__ import annotations

import os
import re
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import Page, expect, sync_playwright
from sqlalchemy import select

from assetboard.app import create_app
from assetboard.config import Settings
from assetboard.db.models.identity import Membership, User, Workspace
from assetboard.services.auth import argon2_hash

pytestmark = pytest.mark.skipif(
    os.environ.get("ASSETBOARD_RUN_BROWSER_TESTS") != "1",
    reason="Set ASSETBOARD_RUN_BROWSER_TESTS=1 to run the Chromium tests",
)

# Records every CSP violation the page reports (inline styles, inline scripts, eval).
VIOLATION_RECORDER = """
window.__cspViolations = [];
document.addEventListener("securitypolicyviolation", (event) => {
  window.__cspViolations.push(event.violatedDirective + " " + (event.blockedURI || "inline"));
});
"""
PASSWORD = "Correct Horse Battery!"
MONTH = "2025-03"


def _descriptions(page: Page) -> list[str]:
    return page.locator("#ledger-expense [data-description]").all_text_contents()


def _sign_in(page: Page, base: str, email: str) -> None:
    page.goto(f"{base}/login")
    page.get_by_label("Email").fill(email)
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(f"{base}/")


def test_f22_transactions_in_a_browser(tmp_path: Path) -> None:
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
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(color_scheme="light")
            context.add_init_script(VIOLATION_RECORDER)
            console_errors: list[str] = []
            page = context.new_page()
            page.on(
                "console",
                lambda message: (
                    console_errors.append(message.text) if message.type == "error" else None
                ),
            )
            base = f"http://127.0.0.1:{port}"
            page.goto(f"{base}/", wait_until="networkidle")
            page.get_by_label("Setup token").fill(app.state.setup_token)
            page.get_by_label("Email").fill("admin@example.com")
            page.get_by_label("Display name").fill("Browser Admin")
            page.get_by_label("Password").fill(PASSWORD)
            page.get_by_label("Workspace name").fill("Browser Home")
            page.get_by_role("button", name="Create administrator").click()
            page.wait_for_url(f"{base}/")
            page.goto(f"{base}/categories")
            page.locator("[data-seed]").click()
            expect(page.locator("li[data-category]").first).to_be_visible()

            page.goto(f"{base}/m/{MONTH}")
            day = page.locator("#entry-expense-day")
            amount = page.locator("#entry-expense-amount")
            description = page.locator("#entry-expense-description")
            category = page.locator("#entry-expense-category")

            # Type, Tab between the fields, Enter on the category select saves; the new entry
            # row is empty and the focus is back in its day field.
            day.click()
            page.keyboard.type("7")
            page.keyboard.press("Tab")
            expect(amount).to_be_focused()
            page.keyboard.type("12.50")
            page.keyboard.press("Tab")
            expect(description).to_be_focused()
            page.keyboard.type("Żabka")
            page.keyboard.press("Tab")
            expect(category).to_be_focused()
            category.select_option(label="Food")
            page.keyboard.press("Enter")
            expect(page.locator("#ledger-expense [data-description]")).to_have_text(["Żabka"])
            expect(day).to_be_focused()
            expect(day).to_have_value("")
            expect(page.locator("#entry-expense-category")).to_have_value("")
            expect(page.locator("#ledger-status")).to_have_text("Entry added.")
            expect(page.locator("#ledger-expense [data-amount]")).to_have_text(["€12.50"])

            # Enter in a text field saves too.
            page.keyboard.type("3")
            page.keyboard.press("Tab")
            page.keyboard.type("4")
            page.keyboard.press("Tab")
            page.keyboard.type("Bread")
            category.select_option(label="Food")
            description.press("Enter")
            expect(page.locator("#ledger-expense [data-description]")).to_have_text(
                ["Bread", "Żabka"]
            )
            expect(day).to_be_focused()

            # A refused entry shows the message and focuses the first invalid field; Esc clears.
            page.keyboard.type("40")
            page.keyboard.press("Enter")
            expect(page.locator("#entry-expense-day-error")).to_contain_text(
                "Enter a day of the month (1-31)"
            )
            expect(page.locator("#entry-expense-category-error")).to_be_visible()
            expect(day).to_be_focused()
            expect(day).to_have_attribute("aria-invalid", "true")
            page.keyboard.press("Escape")
            expect(day).to_have_value("")
            expect(page.locator("[data-field-error]")).to_have_count(0)
            expect(day).not_to_have_attribute("aria-invalid", "true")
            expect(day).to_be_focused()

            # Autocomplete: the suggestion fills the description and pre-selects its category.
            page.keyboard.type("9")
            page.keyboard.press("Tab")
            page.keyboard.type("2")
            page.keyboard.press("Tab")
            page.keyboard.type("ża")
            listbox = page.locator("#entry-expense-suggestions")
            expect(listbox).to_be_visible()
            expect(listbox.get_by_role("option")).to_have_text(["Żabka"])
            expect(description).to_have_attribute("aria-expanded", "true")
            page.keyboard.press("ArrowDown")
            expect(listbox.get_by_role("option").first).to_have_attribute("aria-selected", "true")
            page.keyboard.press("Enter")
            expect(description).to_have_value("Żabka")
            expect(listbox).to_be_hidden()
            expect(category.locator("option:checked")).to_have_text("Food")
            page.keyboard.press("Enter")
            expect(page.locator("#ledger-expense [data-description]")).to_have_text(
                ["Bread", "Żabka", "Żabka"]
            )
            expect(day).to_be_focused()
            # Esc closes an open list before it clears the row.
            page.keyboard.press("Tab")
            page.keyboard.press("Tab")
            page.keyboard.type("Br")
            expect(listbox).to_be_visible()
            page.keyboard.press("Escape")
            expect(listbox).to_be_hidden()
            expect(description).to_have_value("Br")
            page.keyboard.press("Escape")
            expect(description).to_have_value("")

            # Inline edit: Esc cancels and returns the focus to the Edit button; Enter saves.
            bread = page.locator("#ledger-expense tr[data-transaction]", has_text="Bread")
            bread.locator("[data-edit]").click()
            edit_row = page.locator("#ledger-expense tr[data-edit-row]")
            expect(edit_row).to_be_visible()
            edit_day = edit_row.locator("input[name='day']")
            expect(edit_day).to_be_focused()
            expect(edit_day).to_have_value("3")
            page.keyboard.press("Escape")
            expect(edit_row).to_have_count(0)
            expect(bread.locator("[data-edit]")).to_be_focused()
            bread.locator("[data-edit]").click()
            amount_field = edit_row.locator("input[name='amount']")
            expect(amount_field).to_have_value("4.00")
            amount_field.fill("13")
            amount_field.press("Enter")
            expect(edit_row).to_have_count(0)
            expect(bread.locator("[data-amount]")).to_have_text("€13.00")
            expect(bread.locator("[data-edit]")).to_be_focused()

            # Delete: the strict undo toast counts down; Undo restores the row.
            bread.locator("[data-delete]").click()
            expect(page.locator("#ledger-expense [data-description]")).to_have_text(
                ["Żabka", "Żabka"]
            )
            toast = page.locator("#toast-container [data-toast-strict]")
            expect(toast).to_contain_text("Entry deleted.")
            expect(toast.locator("[data-toast-countdown]")).to_have_text(re.compile(r"^(10|9|8)$"))
            expect(day).to_be_focused()
            toast.hover()  # an undo toast does not pause on hover
            toast.get_by_role("button", name="Undo").click()
            expect(toast).to_have_count(0)
            expect(page.locator("#ledger-expense [data-description]")).to_have_text(
                ["Bread", "Żabka", "Żabka"]
            )
            expect(page.locator("#ledger-status")).to_have_text("Entry restored.")

            # Without Undo the toast disappears after ten seconds.
            bread.locator("[data-delete]").click()
            expect(toast).to_have_count(1)
            toast.hover()
            expect(toast).to_have_count(0, timeout=12_000)

            # A closed month is read-only.
            page.locator("[data-month-close]").click()
            page.get_by_role("button", name="Continue").click()
            page.wait_for_url(f"{base}/m/{MONTH}?notice=closed")
            expect(page.locator("#entry-expense-row")).to_have_count(0)
            expect(page.locator("[data-edit], [data-delete]")).to_have_count(0)
            assert _descriptions(page) == ["Żabka", "Żabka"]

            assert page.evaluate("window.__cspViolations") == []
            assert not [e for e in console_errors if "Content Security Policy" in e]

            # A viewer sees the tables without entry rows or row actions.
            with app.state.session_factory() as session, session.begin():
                workspace = session.scalars(select(Workspace)).one()
                viewer = User(
                    email="viewer@example.com",
                    display_name="Viewer",
                    password_hash=argon2_hash(PASSWORD),
                )
                session.add(viewer)
                session.flush()
                session.add(Membership(workspace_id=workspace.id, user_id=viewer.id, role="viewer"))
            viewer_page = browser.new_context(color_scheme="light").new_page()
            _sign_in(viewer_page, base, "viewer@example.com")
            for month in (MONTH, "2025-04"):
                viewer_page.goto(f"{base}/m/{month}")
                expect(viewer_page.locator("#ledger-expense")).to_be_visible()
                expect(viewer_page.locator("[data-entry-row]")).to_have_count(0)
                expect(viewer_page.locator("[data-edit], [data-delete]")).to_have_count(0)
            viewer_page.goto(f"{base}/m/{MONTH}")
            assert _descriptions(viewer_page) == ["Żabka", "Żabka"]
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
