"""F2.7 in Chromium: the Home donuts render (both themes, no CSP violations, a category name
that looks like HTML stays text), the Transactions page's add and edit flow and month picker,
History's filter and export dialog, and the redirects from the old addresses."""

from __future__ import annotations

import os
import re
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import Dialog, Page, expect, sync_playwright
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
XSS = "<img src=x onerror=alert(1)>"
LEGEND_COLOUR_CHANGED = """colour => {
  const el = document.querySelector('[data-donut="expense"] [data-chart-canvas]');
  return window.echarts.getInstanceByDom(el).getOption().legend[0].textStyle.color !== colour;
}"""


def _add(page: Page, direction: str, day: str, amount: str, label: str, text: str) -> None:
    rows = page.locator(f"#ledger-{direction} tr[data-transaction]")
    count = rows.count()
    page.locator(f"#entry-{direction}-day").fill(day)
    page.locator(f"#entry-{direction}-amount").fill(amount)
    page.locator(f"#entry-{direction}-description").fill(text)
    page.locator(f"#entry-{direction}-category").select_option(label=label)
    page.locator(f"#entry-{direction}-amount").press("Enter")
    expect(rows).to_have_count(count + 1)


def _chart_colours(page: Page) -> list[str]:
    """The colours the tooltip and legend use (read from the tokens at draw time)."""
    return page.evaluate(
        """() => {
          const el = document.querySelector('[data-donut="expense"] [data-chart-canvas]');
          const chart = window.echarts.getInstanceByDom(el);
          const option = chart.getOption();
          return [option.legend[0].textStyle.color, option.tooltip[0].backgroundColor];
        }"""
    )


def test_f27_navigation_in_a_browser(tmp_path: Path) -> None:
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
            console_errors: list[str] = []
            dialogs: list[str] = []
            page = context.new_page()
            page.on(
                "console",
                lambda message: (
                    console_errors.append(message.text) if message.type == "error" else None
                ),
            )

            def on_dialog(dialog: Dialog) -> None:
                dialogs.append(dialog.message)
                dialog.dismiss()

            page.on("dialog", on_dialog)
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
                session.add(
                    Category(
                        workspace_id=workspace.id, name=XSS, name_key=XSS.casefold(),
                        kind="expense", treatment="regular", color="#7c3aed", sort_order=99,
                    )
                )  # fmt: skip
            # Today's month in the workspace's time zone (the real clock; Home shows it).
            today = page.locator("[data-home]").get_attribute("data-month") or ""
            assert re.fullmatch(r"\d{4}-\d{2}", today)

            # Home without data: empty states, no chart.
            expect(page.locator("[data-chart-empty]")).to_have_count(2)
            expect(page.locator("[data-home-link]")).to_have_attribute("aria-current", "page")

            # Plain /transactions opens this month's Transactions page; add and edit there.
            page.locator("[data-transactions-link]").first.click()
            page.wait_for_url(f"{base}/transactions/{today}")
            expect(page.locator("[data-transactions-link]")).to_have_attribute(
                "aria-current", "page"
            )
            expect(page.locator("[data-summary-kpis]")).to_have_count(0)
            _add(page, "expense", "1", "40", "Food", "Groceries")
            _add(page, "expense", "1", "25.5", XSS, "Odd category")
            _add(page, "income", "1", "1000", "Salary", "Pay")
            groceries = page.locator("#ledger-expense tr[data-transaction]", has_text="Groceries")
            groceries.locator("[data-edit]").click()
            amount = page.locator("#ledger-expense [data-edit-row] input[name='amount']")
            amount.fill("60")
            amount.press("Enter")
            expect(groceries.locator("[data-amount]")).to_have_text("€60.00")
            # The month picker opens on its trigger; a month link opens that month.
            page.locator("[data-month-picker-toggle]").click()
            target = f"{today[:4]}-01"
            page.locator(f'[data-month-option="{target}"]').click()
            page.wait_for_url(f"{base}/transactions/{target}")
            page.go_back()

            # Home: the flow bar and both donuts are drawn; the tables become sr-only.
            page.goto(f"{base}/")
            for key in ("expense", "income"):
                donut = page.locator(f"[data-donut='{key}']")
                expect(donut.locator("[data-chart]")).to_have_attribute("data-chart-ready", "true")
                expect(donut.locator("[data-chart-canvas] canvas")).to_have_count(1)
                expect(donut.locator("[data-chart-canvas]")).to_have_attribute(
                    "aria-hidden", "true"
                )
                expect(donut.locator("[data-chart-table]")).to_have_class(re.compile("sr-only"))
            expect(page.locator("[data-donut='expense'] [data-chart-row]")).to_have_count(2)
            expect(page.locator("[data-donut='expense'] [data-chart-row]").first).to_contain_text(
                "Food"
            )
            # The HTML-looking category name stays text: in the table, the legend and the
            # canvas tooltip (richText), no element is injected and no dialog opens.
            expect(page.locator("[data-donut='expense'] tbody")).to_contain_text(XSS)
            assert page.locator("img[src='x']").count() == 0
            canvas = page.locator("[data-donut='expense'] [data-chart-canvas] canvas")
            box = canvas.bounding_box()
            assert box is not None
            for dx in (0.3, 0.5, 0.7):  # hover the ring to raise the tooltip
                page.mouse.move(box["x"] + box["width"] * dx, box["y"] + box["height"] * 0.25)
                page.wait_for_timeout(150)
            assert page.locator("img[src='x']").count() == 0
            assert dialogs == []
            light = _chart_colours(page)
            page.locator("[data-theme-toggle]").click()
            expect(page.locator("html")).to_have_class(re.compile(r"\bdark\b"))
            page.wait_for_function(LEGEND_COLOUR_CHANGED, arg=light[0])
            assert _chart_colours(page) != light  # redrawn with the dark tokens
            page.locator("[data-theme-toggle]").click()

            # Month: a summary with the expense and income donuts, no entry form.
            page.locator("[data-month-link]").click()
            page.wait_for_url(f"{base}/m/{today}")
            expect(page.locator("#month-summary")).to_be_visible()
            expect(page.locator("[data-donut]")).to_have_count(2)
            expect(page.locator("[data-donut='expense'] canvas")).to_have_count(1)
            expect(page.locator("[data-donut='income'] canvas")).to_have_count(1)
            expect(page.locator("[data-entry-form]")).to_have_count(0)

            # History: filters and the export dialog.
            page.locator("[data-history-link]").first.click()
            page.wait_for_url(f"{base}/history")
            page.get_by_label("Description contains").fill("groc")
            expect(page.locator("[data-description]")).to_have_text(["Groceries"])
            page.locator("[data-filters-open]").click()
            page.get_by_role("button", name="Show entries").click()
            expect(page).to_have_url(re.compile(r"/history\?.*q=groc"))
            expect(page.locator("[data-description]")).to_have_text(["Groceries"])
            page.locator("[data-export-open]").click()
            dialog = page.locator("#export-dialog")
            expect(dialog).to_be_visible()
            with page.expect_download() as info:
                dialog.get_by_role("button", name="Download CSV").click()
            assert info.value.suggested_filename.endswith(".csv")

            # Old addresses land on the new pages.
            page.goto(f"{base}/transactions?q=pay")
            expect(page).to_have_url(f"{base}/history?q=pay")
            expect(page.locator("[data-description]")).to_have_text(["Pay"])
            page.goto(f"{base}/m/{today}?category=00000000-0000-0000-0000-000000000000")
            expect(page).to_have_url(
                f"{base}/transactions/{today}?category=00000000-0000-0000-0000-000000000000"
            )

            assert page.evaluate("window.__cspViolations") == []
            assert not [e for e in console_errors if "Content Security Policy" in e]
            assert dialogs == []
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
