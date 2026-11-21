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
from assetboard.db.models.identity import User, Workspace
from assetboard.db.models.ledger import BudgetLine, Category, Transaction
from assetboard.services.context import Context, Role, UserRef, WorkspaceRef
from assetboard.services.networth import add_valuation, create_item
from assetboard.services.networth import create_category as create_networth_category

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


def _seed_net_worth(app) -> None:
    with app.state.session_factory() as session:
        user = session.scalars(select(User)).one()
        workspace = session.scalars(select(Workspace)).one()
        ctx = Context(
            user=UserRef(
                id=user.id,
                email=user.email,
                display_name=user.display_name,
                locale=user.locale,
                is_instance_admin=user.is_instance_admin,
            ),
            workspace=WorkspaceRef(
                id=workspace.id,
                name=workspace.name,
                base_currency=workspace.base_currency,
                locale=workspace.locale,
                timezone=workspace.timezone,
                country_code=workspace.country_code,
                taxes_enabled=workspace.taxes_enabled,
            ),
            role=Role.OWNER,
            clock=app.state.clock,
        )
    category = create_networth_category(
        app.state.session_factory, ctx, side="asset", name="Browser Assets", color="#16a34a"
    )
    item = create_item(
        app.state.session_factory,
        ctx,
        locale="en",
        side="asset",
        category_id=category.id,
        name="Browser Fund",
        value="1000",
        currency="EUR",
        include_in_net_worth=True,
        as_of=date(2025, 12, 31),
    )
    add_valuation(
        app.state.session_factory,
        ctx,
        item.id,
        locale="en",
        value="1500",
        as_of=date(2026, 6, 30),
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
            secret_key="test-key-5d364b22dfd4d0ff6f63873",
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

            # One multi-select category dropdown. The summary updates without a reload.
            picker = page.locator("[data-multi-select]")
            expect(picker).to_be_visible()
            expect(picker.locator("[data-multi-select-summary]")).to_have_text("All categories")
            picker.locator("summary").click()
            options = picker.locator("input[name=category]")
            expect(options.first).to_be_visible()
            options.first.check()
            expect(picker.locator("[data-multi-select-summary]")).to_have_text(
                "1 category selected"
            )
            options.first.uncheck()
            expect(picker.locator("[data-multi-select-summary]")).to_have_text("All categories")
            page.keyboard.press("Escape")
            expect(picker).not_to_have_attribute("open", "")
            expect(picker.locator("summary")).to_be_focused()

            # The primary action is still reachable while the picker is open: the panel is in
            # flow, so clicking Show report submits the selected category instead of checking
            # another checkbox under the submit button.
            category_value = options.first.get_attribute("value")
            picker.locator("summary").click()
            expect(options.first).to_be_visible()
            options.first.check()
            page.locator("#report-filters button[type=submit]").click()
            page.wait_for_url(f"**category={category_value}**")
            expect(page.locator("[data-multi-select-summary]")).to_have_text("1 category selected")
            page.goto(f"{base}/reports?from=2026&to=2026&step=month", wait_until="networkidle")

            # R-1: the filter grid fills the panel and top-aligns its controls. At 1280 px the
            # first row shares one top edge; with the currency selector hidden, Granularity
            # spans the remaining columns and reaches the panel's right edge.
            page.set_viewport_size({"width": 1280, "height": 900})
            page.reload(wait_until="networkidle")
            metrics = page.evaluate(
                """() => {
                  const rect = (sel) => {
                    const box = document.querySelector(sel).getBoundingClientRect();
                    return {top: box.top, right: box.right, bottom: box.bottom,
                            left: box.left, width: box.width, height: box.height};
                  };
                  return {
                    form: rect("#report-filters"),
                    from: rect("#report-from"),
                    to: rect("#report-to"),
                    step: rect("#report-step"),
                  };
                }"""
            )
            assert abs(metrics["from"]["top"] - metrics["to"]["top"]) <= 1
            assert abs(metrics["from"]["top"] - metrics["step"]["top"]) <= 1
            assert abs(metrics["step"]["right"] - metrics["form"]["right"]) <= 1

            # A field error appears below its control and does not move the first row.
            page.goto(f"{base}/reports?from=20x6&to=2026", wait_until="networkidle")
            expect(page.locator("#report-from-error")).to_be_visible()
            invalid = page.evaluate(
                """() => {
                  const rect = (sel) => {
                    const box = document.querySelector(sel).getBoundingClientRect();
                    return {top: box.top, right: box.right, bottom: box.bottom,
                            left: box.left, width: box.width, height: box.height};
                  };
                  return {
                    form: rect("#report-filters"),
                    from: rect("#report-from"),
                    to: rect("#report-to"),
                    step: rect("#report-step"),
                    error: rect("#report-from-error"),
                  };
                }"""
            )
            for name in ("from", "to", "step"):
                assert (
                    abs(
                        (invalid[name]["top"] - invalid["form"]["top"])
                        - (metrics[name]["top"] - metrics["form"]["top"])
                    )
                    <= 1
                )
            assert invalid["error"]["top"] >= invalid["from"]["bottom"] - 1
            summary = page.locator("[data-multi-select] summary").bounding_box()
            category_help = page.get_by_text(
                "Leave empty for all categories", exact=False
            ).bounding_box()
            assert summary is not None and category_help is not None
            assert category_help["y"] >= summary["y"] + summary["height"] - 1
            savings = page.locator('[name="savings"]').bounding_box()
            savings_help = page.get_by_text(
                "Saved and invested amounts get their own category series.", exact=True
            ).bounding_box()
            assert savings is not None and savings_help is not None
            assert savings_help["y"] >= savings["y"] + savings["height"] - 1

            # At 390 px the same form is one column and stays usable.
            page.set_viewport_size({"width": 390, "height": 844})
            page.goto(f"{base}/reports?from=2026&to=2026&step=month", wait_until="networkidle")
            phone = page.evaluate(
                """() => {
                  const rect = (sel) => {
                    const box = document.querySelector(sel).getBoundingClientRect();
                    return {top: box.top, left: box.left, width: box.width};
                  };
                  return {
                    from: rect("#report-from"),
                    to: rect("#report-to"),
                    step: rect("#report-step"),
                  };
                }"""
            )
            assert abs(phone["from"]["left"] - phone["to"]["left"]) <= 1
            assert abs(phone["from"]["width"] - phone["step"]["width"]) <= 1
            assert phone["from"]["top"] < phone["to"]["top"] < phone["step"]["top"]
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

            page.set_viewport_size({"width": 1440, "height": 900})
            page.goto(f"{base}/reports?from=2026&to=2026&step=month", wait_until="networkidle")

            # F9.2: the year grid has twelve heat-shaded month columns and its own year selector.
            grid = page.locator("[data-report-year-overview]")
            expect(grid).to_be_visible()
            expect(grid.locator("[data-report-grid-row]")).to_have_count(1)
            expect(grid.locator("[data-report-grid-cell]")).to_have_count(12)
            expect(grid.locator("[data-report-grid-cell]").first).to_have_text("€1,200.00")
            expect(grid).to_contain_text("Jan")
            expect(grid).to_contain_text("Dec")
            expect(grid.locator("[data-report-grid-so-far]")).to_be_visible()
            heat = page.evaluate(
                """() => {
                  const style = getComputedStyle(document.documentElement);
                  const rgb = (name) => {
                    const [r, g, b] = style
                      .getPropertyValue(`--color-${name}`)
                      .trim()
                      .split(/\\s+/);
                    return `rgb(${r}, ${g}, ${b})`;
                  };
                  return {
                    tint: rgb("danger-tint"),
                    cells: [...document.querySelectorAll("[data-report-grid-cell]")].map(
                      (cell) => ({
                        text: cell.textContent.trim(),
                        className: cell.className,
                        background: getComputedStyle(cell).backgroundColor,
                      })
                    ),
                  };
                }"""
            )
            tinted = next(cell for cell in heat["cells"] if cell["text"] == "€1,200.00")
            assert "bg-danger-tint" in tinted["className"].split()
            assert tinted["background"] == heat["tint"]
            assert all(
                cell["background"] == "rgba(0, 0, 0, 0)"
                for cell in heat["cells"]
                if cell["text"] == "€0.00"
            )
            assert all("heat-" not in cell["className"] for cell in heat["cells"])
            year_select = grid.locator("#report-year")
            expect(year_select).to_be_visible()
            expect(year_select).to_have_value("2026")
            expect(year_select).to_have_attribute("form", "report-filters")

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

            # Year-over-year has one row per year and compares the current partial year
            # with the same months of the previous one.
            expect(page.locator("[data-report-yoy-partial]")).to_be_visible()
            expect(page.locator("[data-report-yoy-row]")).to_have_count(1)
            expect(page.locator("[data-report-yoy-expense-percent]").last).to_contain_text(
                "+140.0%"
            )
            expect(page.locator("[data-report-yoy-so-far]")).to_be_visible()
            accuracy = page.locator("[data-report-accuracy-row]")
            expect(accuracy).to_have_count(1)
            expect(accuracy.locator("[data-report-accuracy-planned]")).to_have_text("2")
            expect(accuracy.locator("[data-report-accuracy-over]")).to_have_text("2")

            # Net worth/portfolio data tables are collapsed by default once the chart is drawn.
            _seed_net_worth(app)
            page.goto(f"{base}/reports?from=2026&to=2026&step=month", wait_until="networkidle")
            series = page.locator('[data-report-series="net-worth"]')
            expect(series).to_be_visible()
            chart_root = series.locator('[data-chart="line"]')
            expect(chart_root).to_have_attribute("data-chart-ready", "true")
            table = chart_root.locator("[data-chart-table]")
            assert "sr-only" in (table.get_attribute("class") or "").split()
            toggle = chart_root.locator("[data-chart-table-toggle]")
            expect(toggle).to_be_visible()
            expect(toggle).to_have_text("Show as table")
            toggle.click()
            expect(toggle).to_have_text("Hide table")
            assert "sr-only" not in (table.get_attribute("class") or "").split()
            chart_json = page.evaluate(
                """() => JSON.parse(
                  document.getElementById("reports-net-worth-data").textContent
                )"""
            )
            assert chart_json["links"] and "/m/2026-06" in chart_json["links"]
            chart_state = page.evaluate(
                """() => {
                  const root = document.querySelector('[data-report-series="net-worth"]');
                  const el = root.querySelector("[data-chart-canvas]");
                  const option = window.echarts.getInstanceByDom(el).getOption();
                  return {trigger: option.xAxis[0].triggerEvent};
                }"""
            )
            assert chart_state["trigger"] is True

            # Phone width: one column, no sideways page scroll.
            page.set_viewport_size({"width": 390, "height": 844})
            page.reload(wait_until="networkidle")
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            expect(page.locator("[data-report-section]")).to_be_visible()
            expect(page.locator('[data-chart="report-bars"]')).to_have_attribute(
                "data-chart-ready", "true"
            )
            expect(page.locator('[data-chart="report-bars"] [data-chart-canvas]')).to_be_visible()

            # The Report year selector inside the Year overview submits the filter form on
            # change, preserving the range and the other filters.
            page.set_viewport_size({"width": 1440, "height": 900})
            page.goto(
                f"{base}/reports?from=2025&to=2026&step=month&savings=1"
                f"&category={category_value}&trend=12",
                wait_until="networkidle",
            )
            year_select = page.locator("#report-year")
            expect(year_select).to_have_value("2026")
            year_select.select_option("2025")
            page.wait_for_url("**year=2025#year-overview")
            assert "savings=1" in page.url and "trend=12" in page.url
            expect(page.locator("#report-year")).to_have_value("2025")
            expect(page.locator("#year-overview")).to_be_in_viewport()

            # The Category trends window now lives in its panel and submits its own form; every
            # other filter survives the change.
            expect(page.locator("#report-filters #report-trend")).to_have_count(0)
            trend_select = page.locator("[data-report-trends] #report-trend")
            expect(trend_select).to_be_visible()
            expect(trend_select).to_have_value("12")
            trend_select.select_option("3")
            page.wait_for_url("**trend=3**")
            assert "from=2025" in page.url and "to=2026" in page.url
            assert "step=month" in page.url and "savings=1" in page.url
            assert f"category={category_value}" in page.url
            assert "year=2025" in page.url
            assert page.url.endswith("#category-trends")
            expect(page.locator("[data-report-trends] #report-trend")).to_have_value("3")
            expect(page.locator("#category-trends")).to_be_in_viewport()

            # A chart data point uses the same link as the table row.
            page.goto(f"{base}/reports?from=2026&to=2026&step=month", wait_until="networkidle")
            page.wait_for_function(
                """() => {
                  const chart = document.querySelector('[data-chart="report-bars"]');
                  return chart && chart.getAttribute("data-chart-ready") === "true";
                }"""
            )
            page.evaluate(
                """() => {
                  const chart = document.querySelector('[data-chart="report-bars"]');
                  const el = chart.querySelector("[data-chart-canvas]");
                  window.echarts.getInstanceByDom(el).trigger("click", {dataIndex: 0});
                }"""
            )
            page.wait_for_url(f"{base}/m/2026-01")

            assert page.evaluate("window.__cspViolations") == []
            assert console_errors == []
            context.close()
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=20)
