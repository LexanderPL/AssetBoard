"""The category report in Chromium (F10.3): stacked bars, top-8 + Other, category colours, the
filter, the table toggle, CSP and phone width."""

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


def _seed(app) -> dict[str, str]:
    with app.state.session_factory() as session, session.begin():
        workspace = session.scalars(select(Workspace)).one()
        categories: dict[str, str] = {}
        for index in range(1, 11):
            name = f"Browser Category {index:02d}"
            category = Category(
                workspace_id=workspace.id,
                name=name,
                name_key=name.casefold(),
                kind="expense",
                treatment="regular",
                color="#123456" if index == 10 else "#16a34a",
                sort_order=index,
            )
            session.add(category)
            session.flush()
            categories[name] = str(category.id)
            session.add(
                Transaction(
                    workspace_id=workspace.id,
                    direction="expense",
                    date=date(2026, 1, 10),
                    amount_minor=index * 1_000,
                    currency="EUR",
                    category_id=category.id,
                    description=name,
                    description_key=name.casefold(),
                    source="manual",
                    created_by_user_id=None,
                )
            )
        savings = Category(
            workspace_id=workspace.id,
            name="Browser Category Savings",
            name_key="browser category savings",
            kind="expense",
            treatment="savings",
            color="#7c3aed",
            sort_order=11,
        )
        session.add(savings)
        session.flush()
        categories[savings.name] = str(savings.id)
        session.add(
            Transaction(
                workspace_id=workspace.id,
                direction="expense",
                date=date(2026, 1, 11),
                amount_minor=50_000,
                currency="EUR",
                category_id=savings.id,
                description="Investments",
                description_key="investments",
                source="manual",
                created_by_user_id=None,
            )
        )
    return categories


def _sign_in(page: Page, base: str) -> None:
    page.goto(f"{base}/login", wait_until="networkidle")
    page.get_by_label("Email").fill("admin@example.com")
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(f"{base}/")


def test_the_category_chart_filter_and_table_in_a_browser(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'browser-categories.db'}",
            secret_key="browser-categories-test-key",
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
        categories = _seed(app)
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

        page.goto(
            f"{base}/reports?from=2026&to=2026&step=month&savings=1",
            wait_until="networkidle",
        )
        section = page.locator("[data-report-category-section]")
        expect(section).to_be_visible()
        chart = section.locator('[data-chart="report-category-bars"]')
        expect(chart).to_have_attribute("data-chart-ready", "true")
        expect(chart.locator("[data-chart-canvas]")).to_be_visible()

        option = page.evaluate(
            """() => {
              const chart = document.querySelector('[data-chart="report-category-bars"]');
              const el = chart.querySelector("[data-chart-canvas]");
              const option = window.echarts.getInstanceByDom(el).getOption();
              return {
                series: option.series.map((s) => s.name),
                stacks: [...new Set(option.series.map((s) => s.stack))],
                legend: option.legend[0].data,
                other: option.series.find((s) => s.name === "Other").data[0],
                colour: option.series.find((s) => s.name === "Browser Category 10")
                  .itemStyle.color,
              };
            }"""
        )
        assert option["series"] == [
            "Browser Category Savings",
            "Browser Category 10",
            "Browser Category 09",
            "Browser Category 08",
            "Browser Category 07",
            "Browser Category 06",
            "Browser Category 05",
            "Browser Category 04",
            "Other",
        ]
        assert option["stacks"] == ["spending"]
        assert option["legend"] == option["series"]
        assert option["other"] == 6_000  # the three smallest: 3 000 + 2 000 + 1 000
        assert option["colour"] == "#123456"

        rows = section.locator("[data-report-category-row]")
        expect(rows).to_have_count(9)
        expect(
            rows.filter(has_text="Other").locator("[data-report-category-row-total]")
        ).to_have_text("€60.00")

        table = section.locator("[data-chart-table]")
        toggle = section.locator("[data-chart-table-toggle]")
        assert "sr-only" in (table.get_attribute("class") or "").split()
        expect(toggle).to_be_visible()
        toggle.click()
        expect(toggle).to_have_text("Hide table")
        assert "sr-only" not in (table.get_attribute("class") or "").split()

        # A filter narrows the chart to exactly the chosen categories and drops Other.
        filtered = (
            f"{base}/reports?from=2026&to=2026&step=month"
            f"&category={categories['Browser Category 01']}"
            f"&category={categories['Browser Category 03']}"
        )
        page.goto(filtered, wait_until="networkidle")
        expect(page.locator("[data-report-category-section]")).to_contain_text(
            "Showing the selected categories only."
        )
        filtered_option = page.evaluate(
            """() => {
              const chart = document.querySelector('[data-chart="report-category-bars"]');
              const el = chart.querySelector("[data-chart-canvas]");
              return window.echarts.getInstanceByDom(el).getOption().series.map((s) => s.name);
            }"""
        )
        assert filtered_option == ["Browser Category 03", "Browser Category 01"]

        page.set_viewport_size({"width": 390, "height": 844})
        page.reload(wait_until="networkidle")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        expect(page.locator("[data-report-category-section]")).to_be_visible()
        expect(page.locator('[data-chart="report-category-bars"]')).to_have_attribute(
            "data-chart-ready", "true"
        )

        assert page.evaluate("window.__cspViolations") == []
        assert console_errors == []
        context.close()
        browser.close()
