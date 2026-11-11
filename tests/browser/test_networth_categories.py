"""Net worth category editing in Chromium: the Categories section stays open and the edited
row keeps its place after the HTMX swap."""

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

pytestmark = pytest.mark.skipif(
    os.environ.get("ASSETBOARD_RUN_BROWSER_TESTS") != "1",
    reason="Set ASSETBOARD_RUN_BROWSER_TESTS=1 to run the Chromium tests",
)

PASSWORD = "Correct Horse Battery!"


def test_editing_a_category_keeps_the_section_open_and_the_row_in_place(
    tmp_path: Path,
) -> None:
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
            page = browser.new_page(viewport={"width": 1200, "height": 700})
            base = f"http://127.0.0.1:{port}"
            page.goto(f"{base}/", wait_until="networkidle")
            page.get_by_label("Setup token").fill(app.state.setup_token)
            page.get_by_label("Email").fill("admin@example.com")
            page.get_by_label("Display name").fill("Browser Admin")
            page.get_by_label("Password").fill(PASSWORD)
            page.get_by_label("Workspace name").fill("Browser Home")
            page.get_by_role("button", name="Create administrator").click()
            page.wait_for_url(f"{base}/")

            page.goto(f"{base}/net-worth", wait_until="networkidle")
            # Adding an item is a header action that opens a dialog, not a form at the bottom.
            item_dialog = page.locator("#item-dialog")
            expect(item_dialog).to_be_hidden()
            page.locator("[data-add-item]").click()
            expect(item_dialog).to_be_visible()
            item_dialog.get_by_role("link", name="Cancel").click()
            expect(item_dialog).to_be_hidden()
            details = page.locator("[data-categories]")
            expect(details).to_be_visible()
            assert details.evaluate("element => element.open") is False

            page.locator("[data-categories] summary").click()
            assert details.evaluate("element => element.open") is True

            row = page.locator("li[data-category]", has_text="Vehicles").first
            row.scroll_into_view_if_needed()
            row.get_by_role("link", name="Edit").click()
            editing = page.locator("li[data-editing]")
            expect(editing).to_be_visible()
            expect(editing).to_be_in_viewport()
            assert details.evaluate("element => element.open") is True

            editing.get_by_label("Name").fill("Cars")
            editing.get_by_role("button", name="Save").click()
            expect(page.locator("[data-notice]")).to_have_text("Category saved.")
            renamed = page.locator("li[data-category]", has_text="Cars").first
            expect(renamed).to_be_visible()
            expect(renamed).to_be_in_viewport()
            assert details.evaluate("element => element.open") is True
            assert page.evaluate("window.scrollY") > 0
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
