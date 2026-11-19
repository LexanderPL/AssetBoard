"""The Reports page in Chromium (F10.1): the grouped bars, the table toggle, CSP and phone width."""

from __future__ import annotations

import os
import socket
import threading
import time
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import Page, expect, sync_playwright
from sqlalchemy import select

from assetboard.app import create_app
from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.db.models.identity import Workspace
from assetboard.db.models.ledger import BudgetLine, Category, Transaction

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


def _seed(app) -> None:
    with app.state.session_factory() as session, session.begin():
        workspace = session.scalars(select(Workspace)).one()
        categories = {}
        for key, name, kind, treatment in (
            ("food", "Browser Food", "expense", "regular"),
            ("salary", "Browser Salary", "income", "regular"),
            ("savings", "Browser Savings", "expense", "savings"),
            ("transfer", "Browser Transfer", "expense", "transfer"),
        ):
            category = Category(
                workspace_id=workspace.id,
                name=name,
                name_key=name.casefold(),
                kind=kind,
                treatment=treatment,
                color="#16a34a",
                sort_order=len(categories),
            )
            session.add(category)
            session.flush()
            categories[key] = category.id
        session.add(
            Transaction(
                workspace_id=workspace.id,
                direction="expense",
                date=date(2025, 1, 10),
                amount_minor=100_000,
                currency="EUR",
                category_id=categories["food"],
                description="Last year",
                description_key="last year",
                source="manual",
                created_by_user_id=None,
            )
        )
        for month, planned in ((1, 100_000), (2, 50_000)):
            session.add(
                BudgetLine(
                    workspace_id=workspace.id,
                    month=date(2026, month, 1),
                    category_id=categories["food"],
                    planned_minor=planned,
                    currency="EUR",
                )
            )
        for month in (1, 2):
            session.add(
                Transaction(
                    workspace_id=workspace.id,
                    direction="income",
                    date=date(2026, month, 5),
                    amount_minor=300_000,
                    currency="EUR",
                    category_id=categories["salary"],
                    description="Pay",
                    description_key="pay",
                    source="manual",
                    created_by_user_id=None,
                )
            )
            session.add(
                Transaction(
                    workspace_id=workspace.id,
                    direction="expense",
                    date=date(2026, month, 7),
                    amount_minor=120_000,
                    currency="EUR",
                    category_id=categories["food"],
                    description="Groceries",
                    description_key="groceries",
                    source="manual",
                    created_by_user_id=None,
                )
            )
            session.add(
                Transaction(
                    workspace_id=workspace.id,
                    direction="expense",
                    date=date(2026, month, 8),
                    amount_minor=50_000,
                    currency="EUR",
                    category_id=categories["savings"],
                    description="Savings",
                    description_key="savings",
                    source="manual",
                    created_by_user_id=None,
                )
            )
            session.add(
                Transaction(
                    workspace_id=workspace.id,
                    direction="expense",
                    date=date(2026, month, 9),
                    amount_minor=90_000,
                    currency="EUR",
                    category_id=categories["transfer"],
                    description="Transfer",
                    description_key="transfer",
                    source="manual",
                    created_by_user_id=None,
                )
            )


def _sign_in(page: Page, base: str, app) -> None:
    page.goto(f"{base}/", wait_until="networkidle")
    page.get_by_label("Setup token").fill(app.state.setup_token)
    page.get_by_label("Email").fill("admin@example.com")
    page.get_by_label("Display name").fill("Browser Admin")
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_label("Workspace name").fill("Browser Home")
    page.get_by_role("button", name="Create administrator").click()
    page.wait_for_url(f"{base}/")


def test_the_reports_chart_and_table_in_a_browser(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'browser.db'}",
            secret_key="browser-test-key",
        )
    )
    app.state.clock = FixedClock(datetime(2026, 10, 14, 12, tzinfo=UTC).isoformat())
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
            page = context.new_page()
            page.on(
                "console",
                lambda message: (
                    console_errors.append(message.text) if message.type == "error" else None
                ),
            )
            base = f"http://127.0.0.1:{port}"
            _sign_in(page, base, app)
            _seed(app)

            page.goto(f"{base}/reports?from=2026&to=2026&step=month", wait_until="networkidle")
            # The current month row says "so far"; February is a closed past month. The rate
            # section has its own "so far" badge, so scope to the income/expenses section.
            expect(page.locator("[data-report-section] [data-report-so-far]")).to_have_count(1)
            chart = page.locator('[data-chart="report-bars"]')
            expect(chart.locator("[data-chart-empty]")).to_have_count(0)
            expect(chart).to_have_attribute("data-chart-ready", "true")
            canvas = chart.locator("[data-chart-canvas]")
            expect(canvas).to_be_visible()
            option = page.evaluate(
                """() => {
                  const chart = document.querySelector('[data-chart="report-bars"]');
                  const el = chart.querySelector("[data-chart-canvas]");
                  const option = window.echarts.getInstanceByDom(el).getOption();
                  return {series: option.series.map((s) => s.name), legend: option.legend[0].data};
                }"""
            )
            assert option["series"] == ["Earned", "Spent", "Saved"]
            assert option["legend"] == option["series"]
            # The legend hides a series (ECharts toggles it by name).
            hidden = page.evaluate(
                """() => {
                  const root = document.querySelector('[data-chart="report-bars"]');
                  const el = root.querySelector("[data-chart-canvas]");
                  const chart = window.echarts.getInstanceByDom(el);
                  chart.dispatchAction({type: "legendToggleSelect", name: "Saved"});
                  return chart.getOption().legend[0].selected.Saved;
                }"""
            )
            assert hidden is False

            # The accessible table is sr-only while the chart is drawn, and "Show as table"
            # reveals it without a page reload.
            table = chart.locator("[data-chart-table]")
            assert "sr-only" in (table.get_attribute("class") or "").split()
            toggle = chart.locator("[data-chart-table-toggle]")
            expect(toggle).to_be_visible()
            expect(toggle).to_have_text("Show as table")
            toggle.click()
            expect(toggle).to_have_text("Hide table")
            assert "sr-only" not in (table.get_attribute("class") or "").split()

            # A theme redraw must not hide a table the visitor revealed or desynchronise the
            # button's aria-expanded state.
            page.locator("[data-theme-toggle]").click()
            page.wait_for_function("document.documentElement.classList.contains('dark')")
            expect(chart).to_have_attribute("data-chart-ready", "true")
            expect(toggle).to_have_text("Hide table")
            expect(toggle).to_have_attribute("aria-expanded", "true")
            assert "sr-only" not in (table.get_attribute("class") or "").split()

            toggle.click()
            expect(toggle).to_have_text("Show as table")
            assert "sr-only" in (table.get_attribute("class") or "").split()

            # F9.2: the year grid has twelve heat-shaded month columns and a CSV link.
            grid = page.locator("[data-report-year-overview]")
            expect(grid).to_be_visible()
            expect(grid.locator("[data-report-grid-row]")).to_have_count(1)
            expect(grid.locator("[data-report-grid-cell]")).to_have_count(12)
            expect(grid.locator("[data-report-grid-cell]").first).to_have_text("€1,200.00")
            expect(grid).to_contain_text("Jan")
            expect(grid).to_contain_text("Dec")
            expect(grid.locator("[data-report-grid-so-far]")).to_be_visible()
            heat_classes = grid.locator("[data-report-grid-cell]").evaluate_all(
                "(cells) => cells.map((cell) => cell.className)"
            )
            assert any("heat-" in value for value in heat_classes)
            csv_link = grid.locator("[data-report-year-csv]")
            expect(csv_link).to_be_visible()
            assert "/reports/year-overview.csv" in (csv_link.get_attribute("href") or "")

            # The rolling-average chart reuses the report line module and reveals its table.
            trend = page.locator('[data-chart="report-trend"]')
            expect(trend).to_have_attribute("data-chart-ready", "true")
            expect(trend.locator("[data-chart-canvas]")).to_be_visible()
            trend_option = page.evaluate(
                """() => {
                  const root = document.querySelector('[data-chart="report-trend"]');
                  const el = root.querySelector("[data-chart-canvas]");
                  const option = window.echarts.getInstanceByDom(el).getOption();
                  return {series: option.series.map((s) => s.name)};
                }"""
            )
            assert trend_option["series"] == ["Browser Food"]

            # Year-over-year and plan accuracy read the section year and the plans.
            expect(page.locator("[data-report-yoy-partial]")).to_be_visible()
            expect(page.locator("[data-report-yoy-row]")).to_be_visible()
            expect(page.locator("[data-report-yoy-percent]")).to_contain_text("140.0%")
            accuracy = page.locator("[data-report-accuracy-row]")
            expect(accuracy).to_have_count(1)
            expect(accuracy.locator("[data-report-accuracy-planned]")).to_have_text("2")
            expect(accuracy.locator("[data-report-accuracy-over]")).to_have_text("2")

            # Phone width: one column, no sideways page scroll.
            page.set_viewport_size({"width": 390, "height": 844})
            page.reload(wait_until="networkidle")
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            expect(page.locator("[data-report-section]")).to_be_visible()
            expect(page.locator('[data-chart="report-bars"]')).to_have_attribute(
                "data-chart-ready", "true"
            )
            expect(page.locator('[data-chart="report-bars"] [data-chart-canvas]')).to_be_visible()

            assert page.evaluate("window.__cspViolations") == []
            assert console_errors == []
            context.close()
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=20)
