"""The Reports page UX in Chromium (F11.8): phone filters, section nav, series table toggle,
gain colours and the no-JavaScript table."""

from __future__ import annotations

import os
import socket
import threading
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import Page, expect, sync_playwright
from sqlalchemy import select

from assetboard.app import create_app
from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.db.models.identity import User, Workspace
from assetboard.db.models.ledger import Category, Transaction
from assetboard.db.repositories import PricePointRepository
from assetboard.services.context import Context, Role, UserRef, WorkspaceRef
from assetboard.services.investment_transactions import create_transaction
from assetboard.services.investments import create_asset_class, create_instrument
from assetboard.services.networth import create_category as create_networth_category
from assetboard.services.networth import create_item

pytestmark = pytest.mark.skipif(
    os.environ.get("ASSETBOARD_RUN_BROWSER_TESTS") != "1",
    reason="Set ASSETBOARD_RUN_BROWSER_TESTS=1 to run the Chromium tests",
)

PASSWORD = "Correct Horse Battery!"


def _context(app) -> Context:
    with app.state.session_factory() as session:
        user = session.scalars(select(User)).one()
        workspace = session.scalars(select(Workspace)).one()
        return Context(
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


def _seed(app) -> None:
    ctx = _context(app)
    with app.state.session_factory() as session, session.begin():
        workspace = session.scalars(select(Workspace)).one()
        categories = {}
        for key, name, kind in (
            ("food", "Browser Food", "expense"),
            ("salary", "Browser Salary", "income"),
            ("savings", "Browser Savings", "expense"),
        ):
            category = Category(
                workspace_id=workspace.id,
                name=name,
                name_key=name.casefold(),
                kind=kind,
                treatment="savings" if key == "savings" else "regular",
                color="#16a34a",
                sort_order=len(categories),
            )
            session.add(category)
            session.flush()
            categories[key] = category.id
        for month in range(1, 11):
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
    category = create_networth_category(
        app.state.session_factory, ctx, side="asset", name="Browser Assets", color="#16a34a"
    )
    create_item(
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
    asset_class = create_asset_class(
        app.state.session_factory, ctx, name="Browser Stocks", color="#2563eb", investable=True
    )
    instrument = create_instrument(
        app.state.session_factory,
        ctx,
        name="Browser ETF",
        symbol="",
        type="etf",
        quote_currency="EUR",
        unit_label="share",
        allocations=[(asset_class.id, 10_000)],
        locale="en",
    )
    create_transaction(
        app.state.session_factory,
        ctx,
        instrument_id=instrument.id,
        type_value="buy",
        date_value="2024-01-01",
        quantity_value="10",
        unit_price_value="100",
        fee_value="0",
        fx_value="",
        note_value="",
        locale="en",
    )
    for day, price in (
        ("2024-12-01", "50"),
        ("2025-12-01", "120"),
        ("2026-10-01", "130"),
    ):
        with app.state.session_factory.begin() as session:
            PricePointRepository(session, ctx).create(
                instrument_id=instrument.id,
                date=date.fromisoformat(day),
                price=Decimal(price),
                source="manual",
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


def _serve(app):
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        threading.Event().wait(0.01)
    assert server.started
    return port


def _app(tmp_path: Path):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'browser.db'}",
            secret_key="test-key-5d364b22dfd4d0ff6f63873",
        )
    )
    app.state.clock = FixedClock(datetime(2026, 10, 14, 12, tzinfo=UTC).isoformat())
    return app


def test_the_phone_filters_nav_and_series_toggle(tmp_path: Path) -> None:
    app = _app(tmp_path)
    port = _serve(app)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(color_scheme="light", viewport={"width": 390, "height": 844})
        page = context.new_page()
        base = f"http://127.0.0.1:{port}"
        _sign_in(page, base, app)
        _seed(app)

        page.goto(f"{base}/reports?from=2024&to=2026&step=year", wait_until="networkidle")
        panel = page.locator("details[data-filter-disclosure]")
        expect(panel).not_to_have_attribute("open", "")
        expect(panel.locator(":scope > summary")).to_be_visible()
        # UX-07: the first data section starts inside the phone viewport.
        first_section = page.locator("[data-report-section]").first
        expect(first_section).to_be_visible()
        box = first_section.bounding_box()
        assert box is not None and box["y"] < 844

        # UX-10: the nav reaches the last section; the heading lands in view.
        nav = page.locator("[data-report-nav]")
        expect(nav).to_be_visible()
        nav.get_by_role("link", name="Portfolio value over time").click()
        heading = page.locator("#reports-portfolio-heading")
        expect(heading).to_be_in_viewport()

        # UX-09: the series table is sr-only after the chart draws; the toggle reveals it.
        series = page.locator('[data-report-series="portfolio"]')
        table = series.locator("[data-chart-table]")
        expect(series.locator("[data-chart-canvas]")).to_be_visible()
        assert "sr-only" in (table.get_attribute("class") or "").split()
        toggle = series.locator("[data-chart-table-toggle]")
        expect(toggle).to_be_visible()
        toggle.click()
        assert "sr-only" not in (table.get_attribute("class") or "").split()
        expect(toggle).to_have_text("Hide table")

        # UX-13: the 2024 loss is danger, the 2026 gain is success.
        rows = series.locator("tr[data-report-series-row]")
        expect(rows.nth(0).locator("td").last).to_have_class(
            "whitespace-nowrap py-1.5 pr-2 text-right tabular-nums text-danger"
        )
        expect(rows.nth(2).locator("td").last).to_have_class(
            "whitespace-nowrap py-1.5 pr-2 text-right tabular-nums text-success-strong"
        )
        context.close()
        browser.close()


def test_invalid_filters_stay_open_with_their_error_on_a_phone(tmp_path: Path) -> None:
    app = _app(tmp_path)
    port = _serve(app)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(color_scheme="light", viewport={"width": 390, "height": 844})
        page = context.new_page()
        base = f"http://127.0.0.1:{port}"
        _sign_in(page, base, app)
        _seed(app)
        page.goto(f"{base}/reports?from=2026&to=2025", wait_until="networkidle")
        panel = page.locator("details[data-filter-disclosure]")
        expect(panel).to_have_attribute("open", "")
        expect(page.locator("[data-report-invalid]")).to_be_visible()
        expect(panel.locator(":scope > summary")).to_contain_text("1 filter")
        context.close()
        browser.close()


def test_a_donut_table_stays_in_the_accessibility_tree(tmp_path: Path) -> None:
    app = _app(tmp_path)
    port = _serve(app)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(color_scheme="light", viewport={"width": 1440, "height": 900})
        page = context.new_page()
        base = f"http://127.0.0.1:{port}"
        _sign_in(page, base, app)
        _seed(app)
        page.goto(f"{base}/m/2026-01", wait_until="networkidle")
        donut = page.locator('[data-chart="donut"]').first
        expect(donut).to_have_attribute("data-chart-ready", "true")
        table = donut.locator("[data-chart-table]")
        assert "sr-only" in (table.get_attribute("class") or "").split()
        assert table.evaluate("el => el.inert") is False
        context.close()
        browser.close()


def test_the_series_table_is_visible_without_javascript(tmp_path: Path) -> None:
    app = _app(tmp_path)
    port = _serve(app)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(
            color_scheme="light",
            viewport={"width": 1440, "height": 900},
            java_script_enabled=False,
        )
        page = context.new_page()
        base = f"http://127.0.0.1:{port}"
        _sign_in(page, base, app)
        _seed(app)
        page.goto(f"{base}/reports?from=2024&to=2026&step=year", wait_until="networkidle")
        series = page.locator('[data-report-series="net-worth"]')
        expect(series.locator("[data-chart-table]")).to_be_visible()
        expect(series.locator("[data-chart-table-toggle]")).to_be_hidden()
        context.close()
        browser.close()
