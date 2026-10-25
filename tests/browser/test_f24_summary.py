"""The month summary in Chromium (F2.4): actual, difference and KPIs refresh without a reload
after a planned value or an entry (made on the Transactions page since F2.7) is saved, the sort
toggle reorders the rows and is kept, the F3.5 forecast toggle hides the forecast columns and is
kept, clicking a category opens the filtered Transactions page (part B), and there are no CSP
violations.
The test's categories are created directly ("Golden ..."); the suggested set that setup gives
every new workspace stays in the summary with nothing in it."""

from __future__ import annotations

import asyncio
import os
import re
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from playwright.sync_api import Page, expect, sync_playwright
from sqlalchemy import select

from assetboard.app import create_app
from assetboard.config import Settings
from assetboard.db.models.identity import Workspace
from assetboard.db.models.ledger import Category

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
MONTH = "2025-03"


class SlowPosts:
    """Delays every POST by ``delay`` seconds (network latency, without blocking the test)."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.delay = 0.0
        self.plan_delay = 0.0  # planned-value saves only

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope["method"] == "POST":
            delay = self.plan_delay if "/plan/" in scope["path"] else self.delay
            if delay:
                await asyncio.sleep(delay)
        await self.app(scope, receive, send)


def _order(page: Page) -> list[str]:
    """The expense rows of this test's categories, in page order (the seeded ones are there
    too, with nothing in them)."""
    names = page.locator(
        "#month-summary [data-plan-group='expense'] [data-plan-category]"
    ).all_text_contents()
    return [name for name in names if name.startswith("Golden ")]


def test_f24_summary_in_a_browser(tmp_path: Path) -> None:
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
            with app.state.session_factory() as session, session.begin():
                workspace = session.scalars(select(Workspace)).one()
                for order, (name, kind) in enumerate(
                    [("Golden Food", "expense"), ("Golden Travel", "expense"),
                     ("Golden Salary", "income")]
                ):  # fmt: skip
                    session.add(
                        Category(
                            workspace_id=workspace.id, name=name, name_key=name.casefold(),
                            kind=kind, treatment="regular", color="#16a34a", sort_order=order,
                        )
                    )  # fmt: skip

            page.goto(f"{base}/m/{MONTH}")
            food_row = page.locator("#month-summary tr[data-plan-row]", has_text="Golden Food")
            travel_row = page.locator("#month-summary tr[data-plan-row]", has_text="Golden Travel")
            kpis = page.locator("#summary-kpis [data-kpi]")
            # Nothing earned: no savings-rate caption, and an empty flow bar.
            expect(kpis).to_have_text(["€0.00", "€0.00", "€0.00", "€0.00"])
            expect(page.locator("[data-flow-segment='empty']")).to_have_count(1)

            # A planned value updates the difference, the totals and the bar text at once.
            page.get_by_label("Planned for Golden Food").fill("550")
            page.keyboard.press("Tab")
            expect(food_row.locator("[data-difference]")).to_contain_text("+€550.00")
            expect(page.locator("#summary-total-expense")).to_contain_text("€550.00")
            expect(page.locator("[data-summary-bar='expense']")).to_contain_text(
                "Spent €0.00 of €550.00 planned"
            )

            # F2.7: entries are made on the Transactions page; the Month summary shows them.
            def add(direction: str, day: str, amount: str, label: str) -> None:
                page.goto(f"{base}/transactions/{MONTH}")
                page.locator(f"#entry-{direction}-day").fill(day)
                page.locator(f"#entry-{direction}-amount").fill(amount)
                page.locator(f"#entry-{direction}-category").select_option(label=label)
                count = page.locator(f"#ledger-{direction} tr[data-transaction]").count()
                page.locator(f"#entry-{direction}-amount").press("Enter")
                expect(page.locator(f"#ledger-{direction} tr[data-transaction]")).to_have_count(
                    count + 1
                )
                page.goto(f"{base}/m/{MONTH}")

            add("expense", "3", "50", "Golden Food")
            expect(food_row.locator("[data-actual]")).to_have_text("€50.00")
            expect(food_row.locator("[data-difference]")).to_contain_text("+€500.00")
            expect(kpis.nth(1)).to_have_text("€50.00")
            add("income", "1", "1000", "Golden Salary")
            expect(kpis).to_have_text(["€1,000.00", "€50.00", "€0.00", "€950.00", "0.0%"])
            expect(page.locator("[data-savings-rate]")).to_have_text(
                "Saved / invested 0.0% of income"
            )
            expect(page.locator("[data-flow-segment]")).to_have_count(2)  # spent and left

            # Largest first by default; "Your order" keeps the categories' order, and is kept.
            add("expense", "4", "400", "Golden Travel")
            expect(travel_row.locator("[data-actual]")).to_have_text("€400.00")
            assert _order(page) == ["Golden Travel", "Golden Food"]
            page.get_by_role("button", name="Your order").click()
            expect(page.get_by_role("button", name="Your order")).to_have_attribute(
                "aria-pressed", "true"
            )
            # The focus stays on the pressed button's replacement (stable ids across the swap).
            expect(page.locator("#summary-sort-manual")).to_be_focused()
            assert _order(page) == ["Golden Food", "Golden Travel"]
            page.reload()
            assert _order(page) == ["Golden Food", "Golden Travel"]
            page.get_by_role("button", name="Largest first").click()
            expect(page.get_by_role("button", name="Largest first")).to_have_attribute(
                "aria-pressed", "true"
            )
            assert page.evaluate("document.activeElement.id") == "summary-sort-actual"
            assert _order(page) == ["Golden Travel", "Golden Food"]

            # F3.5: the forecast toggle hides the Expected and Forecast columns and the forecast
            # KPI line; the choice survives a row swap (planned value), a whole-summary swap
            # (sort) and a reload, and it comes back with a second click.
            forecast = page.get_by_role("button", name="Show forecast")
            visible_forecast = page.locator("#month-summary [data-forecast-column]:visible")
            expect(forecast).to_have_attribute("aria-pressed", "true")
            expect(food_row.locator("[data-forecast-column]:visible")).to_have_count(2)
            forecast.click()
            expect(forecast).to_have_attribute("aria-pressed", "false")
            expect(visible_forecast).to_have_count(0)
            page.get_by_label("Planned for Golden Travel").fill("5350")
            page.keyboard.press("Tab")
            expect(travel_row.locator("[data-difference]")).to_contain_text("+€4,950.00")
            expect(visible_forecast).to_have_count(0)
            page.get_by_role("button", name="Your order").click()
            expect(page.get_by_role("button", name="Your order")).to_have_attribute(
                "aria-pressed", "true"
            )
            expect(visible_forecast).to_have_count(0)
            expect(forecast).to_have_attribute("aria-pressed", "false")
            page.get_by_role("button", name="Largest first").click()
            expect(page.get_by_role("button", name="Largest first")).to_have_attribute(
                "aria-pressed", "true"
            )
            page.reload()
            expect(forecast).to_have_attribute("aria-pressed", "false")
            expect(visible_forecast).to_have_count(0)
            forecast.click()
            expect(forecast).to_have_attribute("aria-pressed", "true")
            expect(food_row.locator("[data-forecast-column]:visible")).to_have_count(2)
            assert _order(page) == ["Golden Travel", "Golden Food"]

            # The expense donut is drawn from the summary's actuals.
            expect(page.locator("[data-donut='expense'] [data-chart-canvas] canvas")).to_have_count(
                1
            )
            expect(page.locator("[data-donut='expense'] [data-chart-row]")).to_have_count(2)

            # Clicking a category opens the Transactions page with only its entries (and only
            # its table); its entry row preselects it.
            page.locator("#month-summary [data-plan-category]", has_text="Golden Food").click()
            page.wait_for_url(re.compile(r"/transactions/2025-03\?category=[0-9a-f-]+$"))
            expect(page.locator("[data-ledger-filter-bar]")).to_contain_text(
                "Showing Golden Food only (1 entry)."
            )
            expect(page.locator("#ledger-income")).to_have_count(0)
            expect(page.locator("#ledger-expense [data-description]")).to_have_count(1)
            expect(page.locator("#entry-expense-category option:checked")).to_have_text(
                "Golden Food"
            )
            page.locator("#entry-expense-day").fill("5")
            page.locator("#entry-expense-amount").fill("7")
            page.locator("#entry-expense-amount").press("Enter")
            expect(page.locator("[data-ledger-filter-bar]")).to_contain_text("(2 entries)")
            expect(page.locator("#ledger-expense tr[data-transaction]")).to_have_count(2)
            expect(page.locator("#entry-expense-category option:checked")).to_have_text(
                "Golden Food"
            )
            # "Show all" swaps the tables back, announces it and focuses the heading.
            page.locator("[data-ledger-show-all]").click()
            expect(page.locator("#ledger-income")).to_be_visible()
            expect(page.locator("#ledger-expense-heading")).to_be_focused()
            expect(page.locator("#ledger-status")).to_have_text("Showing all entries.")
            expect(page.locator("[data-ledger-filter-bar]")).to_have_count(0)
            page.go_back()
            expect(page.locator("[data-ledger-filter-bar]")).to_contain_text("(2 entries)")
            assert page.evaluate("localStorage.getItem('htmx-history-cache')") is None
            page.goto(f"{base}/m/{MONTH}")
            expect(food_row.locator("[data-actual]")).to_have_text("€57.00")
            expect(kpis.nth(1)).to_have_text("€457.00")

            assert page.evaluate("window.__cspViolations") == []
            assert not [e for e in console_errors if "Content Security Policy" in e]
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
