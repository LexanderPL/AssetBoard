"""The derived net-worth trend in Chromium (F5.9): chart, table, range and mobile width."""

from __future__ import annotations

import os
import re
import socket
import threading
import time
from datetime import date
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select

from assetboard.app import create_app
from assetboard.config import Settings
from assetboard.db.models.identity import Workspace
from assetboard.db.models.networth import ItemValuation, NetWorthCategory, NetWorthItem

pytestmark = pytest.mark.skipif(
    os.environ.get("ASSETBOARD_RUN_BROWSER_TESTS") != "1",
    reason="Set ASSETBOARD_RUN_BROWSER_TESTS=1 to run the Chromium tests",
)

VIOLATION_RECORDER = """
window.__cspViolations = [];
document.addEventListener("securitypolicyviolation", (event) => {
  window.__cspViolations.push(event.violatedDirective + " " + (event.blockedURI || "inline"));
});
"""
PASSWORD = "Correct Horse Battery!"


def _seed_net_worth(app) -> None:
    with app.state.session_factory() as session, session.begin():
        workspace = session.scalars(select(Workspace)).one()
        category = session.scalars(
            select(NetWorthCategory).where(
                NetWorthCategory.workspace_id == workspace.id,
                NetWorthCategory.name_key == "vehicles",
            )
        ).one()
        item = NetWorthItem(
            workspace_id=workspace.id,
            side="asset",
            category_id=category.id,
            name="Car",
            currency="EUR",
            notes="",
            valuation_mode="manual",
            include_in_net_worth=True,
            investable=False,
            estimate_between=True,
        )
        session.add(item)
        session.flush()
        session.add(
            ItemValuation(
                workspace_id=workspace.id,
                item_id=item.id,
                as_of=date(2025, 1, 15),
                value_minor=1_200_000,
                currency="EUR",
                note="",
            )
        )
        session.add(
            ItemValuation(
                workspace_id=workspace.id,
                item_id=item.id,
                as_of=date(2026, 6, 1),
                value_minor=900_000,
                currency="EUR",
                note="",
            )
        )


def test_the_derived_net_worth_trend_at_desktop_and_phone_width(tmp_path: Path) -> None:
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
            context = browser.new_context(
                color_scheme="light", viewport={"width": 1440, "height": 900}
            )
            context.add_init_script(VIOLATION_RECORDER)
            page = context.new_page()
            base = f"http://127.0.0.1:{port}"
            page.goto(f"{base}/", wait_until="networkidle")
            page.get_by_label("Setup token").fill(app.state.setup_token)
            page.get_by_label("Email").fill("admin@example.com")
            page.get_by_label("Display name").fill("Browser Admin")
            page.get_by_label("Password").fill(PASSWORD)
            page.get_by_label("Workspace name").fill("Browser Home")
            page.get_by_role("button", name="Create administrator").click()
            page.wait_for_url(f"{base}/")
            _seed_net_worth(app)

            page.goto(f"{base}/net-worth/summary", wait_until="networkidle")
            trend = page.locator("#net-worth-trend")
            expect(trend).to_be_visible()
            page.wait_for_function("document.querySelector('[data-chart-ready=\"true\"]') !== null")
            expect(page.locator("[data-chart-canvas]")).to_be_visible()

            table = page.locator("details[data-history-table]")
            assert table.evaluate("element => element.open") is False
            table.locator("summary").first.click()
            assert table.evaluate("element => element.open") is True
            expect(page.locator("[data-trend-row]").first).to_be_visible()

            page.locator('[data-trend-range] a[data-range="3y"]').click()
            expect(page).to_have_url(re.compile(r"/net-worth/summary\?range=3y#net-worth-trend"))
            expect(trend).to_be_visible()

            page.goto(f"{base}/net-worth/history?range=all", wait_until="networkidle")
            expect(page).to_have_url(re.compile(r"/net-worth/summary\?range=all#net-worth-trend"))
            expect(trend).to_be_visible()

            page.set_viewport_size({"width": 390, "height": 844})
            page.reload(wait_until="networkidle")
            expect(trend).to_be_visible()
            overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
            assert overflow <= 1, overflow
            assert page.evaluate("window.__cspViolations") == []
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
