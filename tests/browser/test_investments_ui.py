"""Investments forms in Chromium: the add form opens from its button with no allocation warning,
a successful add shows the notice in view (and a fresh form still has no warning), editing opens
in the instrument's row and cancels back to it, and a pre-filled stored FX rate follows the
transaction date (re-filled with the new date's stored rate, cleared when none is stored) while a
typed rate is kept. No CSP violations."""

from __future__ import annotations

import os
import socket
import threading
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select

from assetboard.app import create_app
from assetboard.config import Settings
from assetboard.db.models.identity import Workspace
from assetboard.db.models.investments import Instrument
from assetboard.db.models.prices import FxRate
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


def test_instrument_add_and_inline_edit_in_a_browser(tmp_path: Path) -> None:
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
            context = browser.new_context(viewport={"width": 1280, "height": 720})
            context.add_init_script(VIOLATION_RECORDER)
            page = context.new_page()
            page.goto(f"{base}/login", wait_until="networkidle")
            page.get_by_label("Email").fill("admin@example.com")
            page.get_by_label("Password").fill(PASSWORD)
            page.get_by_role("button", name="Sign in").click()
            page.wait_for_url(f"{base}/")

            page.goto(f"{base}/investments/instruments", wait_until="networkidle")
            add = page.locator("[data-add-instrument]")
            create_form = add.locator("[data-instrument-form]")
            warning = create_form.locator("[data-allocation-warning]")
            expect(create_form).to_be_hidden()
            add.locator("summary").click()
            expect(create_form).to_be_visible()
            # A fresh form is not an error: no warning until a weight is typed.
            expect(warning).to_be_hidden()
            create_form.get_by_label("Name", exact=True).fill("World ETF")
            stocks = create_form.get_by_label("Weight for Stocks")
            stocks.fill("50")
            expect(warning).to_be_visible()
            stocks.fill("100")
            expect(warning).to_be_hidden()
            create_form.get_by_role("button", name="Save instrument").click()

            notice = page.locator("[data-notice]")
            expect(notice).to_have_text("Instrument added.")
            expect(notice).to_be_in_viewport()
            expect(page.locator("[data-instrument-name='World ETF']")).to_be_visible()
            expect(add.locator("[data-instrument-form]")).to_be_hidden()  # closed again
            add.locator("summary").click()
            expect(add.locator("[data-allocation-warning]")).to_be_hidden()
            add.get_by_role("button", name="Cancel").click()

            row = page.locator("[data-instrument-name='World ETF']")
            row.locator("[data-edit]").click()
            editing = page.locator("[data-instrument-name='World ETF'][data-editing]")
            expect(editing).to_be_visible()
            expect(editing.locator("[data-instrument-form]")).to_be_in_viewport()
            expect(editing.get_by_label("Name", exact=True)).to_have_value("World ETF")
            expect(editing.locator("[data-allocation-warning]")).to_be_hidden()
            editing.get_by_role("link", name="Cancel").click()
            expect(page.locator("[data-editing]")).to_have_count(0)
            expect(page.locator("[data-instrument-name='World ETF'] [data-edit]")).to_be_visible()

            # A foreign-quoted instrument and an old stored rate: the new transaction's FX field
            # starts with it, a date change re-fills it untouched with that date's stored rate (or
            # clears it when none is stored), and a typed rate survives.
            add.locator("summary").click()
            create_form = add.locator("[data-instrument-form]")
            create_form.get_by_label("Name", exact=True).fill("US fund")
            create_form.get_by_label("Quote currency").select_option("USD")
            create_form.get_by_label("Weight for Stocks").fill("100")
            create_form.get_by_role("button", name="Save instrument").click()
            expect(page.locator("[data-instrument-name='US fund']")).to_be_visible()
            with app.state.session_factory.begin() as session:
                workspace_id = session.scalars(select(Workspace.id)).one()
                instrument_id = session.scalars(
                    select(Instrument.id).where(Instrument.name == "US fund")
                ).one()
                for day, rate in ((date(2000, 1, 3), "0.85"), (date(2001, 2, 1), "0.9")):
                    session.add(
                        FxRate(
                            workspace_id=workspace_id,
                            base_currency="EUR",
                            quote_currency="USD",
                            date=day,
                            rate=Decimal(rate),
                            source="ecb",
                        )
                    )
            page.goto(f"{base}/investments/instruments/{instrument_id}", wait_until="networkidle")
            fx = page.locator("#transaction-fx")
            hint = page.locator("[data-fx-hint]")
            expect(fx).to_have_value("0.9")
            expect(hint).to_be_visible()
            page.locator("#transaction-date").fill("2000-06-05")
            expect(fx).to_have_value("0.85")
            expect(hint).to_be_visible()
            expect(hint).to_have_text("Stored rate for Jun 5, 2000")
            page.locator("#transaction-date").fill("1999-06-01")
            expect(fx).to_have_value("")
            expect(hint).to_be_hidden()
            page.reload(wait_until="networkidle")
            expect(fx).to_have_value("0.9")
            fx.fill("0.95")
            expect(hint).to_be_hidden()
            page.locator("#transaction-date").fill("2001-02-05")
            expect(fx).to_have_value("0.95")

            assert page.evaluate("window.__cspViolations") == []
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
