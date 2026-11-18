"""The savings-rate report in Chromium (F10.2): the saved/earned line with a zero and a gap,
the rolling line, the table toggle, CSP and phone width."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect, sync_playwright
from sqlalchemy import select

from assetboard.app import create_app
from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.db.models.identity import Workspace
from assetboard.db.models.ledger import Category, Transaction
from assetboard.services.auth import run_initial_setup
from tests.conftest import live_server

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
            ("food", "Browser Rate Food", "expense", "regular"),
            ("savings", "Browser Rate Savings", "expense", "savings"),
            ("salary", "Browser Rate Salary", "income", "regular"),
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
        for month in range(1, 13):
            session.add(
                Transaction(
                    workspace_id=workspace.id,
                    direction="income",
                    date=date(2025, month, 5),
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
                    date=date(2025, month, 6),
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
                    date=date(2025, month, 7),
                    amount_minor=180_000,
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
                direction="income",
                date=date(2026, 1, 5),
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
                date=date(2026, 1, 6),
                amount_minor=420_000,
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
                date=date(2026, 2, 6),
                amount_minor=10_000,
                currency="EUR",
                category_id=categories["food"],
                description="Groceries without pay",
                description_key="groceries without pay",
                source="manual",
                created_by_user_id=None,
            )
        )


def _sign_in(page: Page, base: str) -> None:
    page.goto(f"{base}/login", wait_until="networkidle")
    page.get_by_label("Email").fill("admin@example.com")
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(f"{base}/")


def test_the_savings_rate_chart_and_table_in_a_browser(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'browser-rate.db'}",
            secret_key="browser-rate-test-key",
        )
    )
    app.state.clock = FixedClock(datetime(2026, 10, 14, 12, tzinfo=UTC).isoformat())

    with live_server(app) as base, sync_playwright() as playwright:
        run_initial_setup(
            app.state.session_factory,
            email="admin@example.com",
            display_name="Browser Admin",
            password=PASSWORD,
            workspace_name="Browser Home",
            settings=app.state.settings,
            clock=app.state.clock,
        )
        _seed(app)
        browser = playwright.chromium.launch()
        context = browser.new_context(color_scheme="light", viewport={"width": 1440, "height": 900})
        context.add_init_script(VIOLATION_RECORDER)
        console_errors: list[str] = []
        page = context.new_page()
        page.on(
            "console",
            lambda message: (
                console_errors.append(message.text) if message.type == "error" else None
            ),
        )
        _sign_in(page, base)
        page.goto(f"{base}/reports?from=2025&to=2026&step=month", wait_until="networkidle")

        section = page.locator("[data-report-rate-section]")
        expect(section).to_be_visible()
        chart = section.locator('[data-chart="report-rate"]')
        expect(chart).to_have_attribute("data-chart-ready", "true")
        expect(chart.locator("[data-chart-canvas]")).to_be_visible()

        option = page.evaluate(
            """() => {
              const chart = document.querySelector('[data-chart="report-rate"]');
              const el = chart.querySelector("[data-chart-canvas]");
              const option = window.echarts.getInstanceByDom(el).getOption();
              return {
                series: option.series.map((s) => s.name),
                types: option.series.map((s) => s.type),
                connect: option.series.map((s) => s.connectNulls),
                values: option.series[0].data,
                rolling: option.series[1].data,
                yMin: option.yAxis[0].min,
                legend: option.legend[0].data,
              };
            }"""
        )
        assert option["series"] == ["Savings rate", "12-month rolling rate"]
        assert option["legend"] == option["series"]
        assert option["types"] == ["line", "line"]
        assert option["connect"] == [False, False]
        assert option["values"][12] == 0  # January 2026 saves nothing
        assert option["values"][13] is None  # February 2026: nothing earned
        assert option["rolling"][:11] == [None] * 11
        assert option["rolling"][11] == 600  # December 2025: the first 12-month point

        january = section.locator("[data-report-rate-row]").filter(has_text="January 2026")
        expect(january.locator("[data-report-rate]")).to_have_text("0.0%")
        february = section.locator("[data-report-rate-row]").filter(has_text="February 2026")
        expect(february.locator("[data-report-rate]")).to_contain_text(
            "Not available: nothing earned"
        )
        first = section.locator("[data-report-rate-row]").first
        expect(first.locator("[data-report-rolling-rate]")).to_contain_text(
            "Not available: fewer than 12 months of data."
        )

        table = section.locator("[data-chart-table]")
        toggle = section.locator("[data-chart-table-toggle]")
        assert "sr-only" in (table.get_attribute("class") or "").split()
        expect(toggle).to_be_visible()
        toggle.click()
        expect(toggle).to_have_text("Hide table")
        assert "sr-only" not in (table.get_attribute("class") or "").split()

        page.set_viewport_size({"width": 390, "height": 844})
        page.reload(wait_until="networkidle")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        expect(page.locator("[data-report-rate-section]")).to_be_visible()
        expect(page.locator('[data-chart="report-rate"]')).to_have_attribute(
            "data-chart-ready", "true"
        )

        assert page.evaluate("window.__cspViolations") == []
        assert console_errors == []
        context.close()
        browser.close()
