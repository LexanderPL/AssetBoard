"""F9.9 in Chromium at 390 × 844: the Month summary fits without sideways scroll (and keeps
Category, Plan and Actual visible and editable with a six-digit amount, with the Difference
amount labelled under Actual), the Transactions entry row has 44 px tap targets and no page
overflow, Upcoming confirms and skips in one tap, and every main page stays within the phone
viewport. No CSP violations."""

from __future__ import annotations

import os
import socket
import threading
import time
from datetime import date
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import Browser, Page, expect, sync_playwright
from sqlalchemy import select

from assetboard.app import create_app
from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.db.models.identity import InstanceSettings, User, Workspace
from assetboard.services.auth import run_initial_setup
from assetboard.services.context import Context, Role, UserRef, WorkspaceRef
from assetboard.services.demo import build_demo_workspace, plan_for_workspace
from tests.conftest import FakeMailer

pytestmark = pytest.mark.skipif(
    os.environ.get("ASSETBOARD_RUN_BROWSER_TESTS") != "1",
    reason="Set ASSETBOARD_RUN_BROWSER_TESTS=1 to run the Chromium tests",
)

PASSWORD = "Correct Horse Battery!"
REFERENCE = date(2026, 11, 20)
MONTH = "2026-11"
VIOLATION_RECORDER = """
window.__cspViolations = [];
document.addEventListener("securitypolicyviolation", (event) => {
  window.__cspViolations.push(event.violatedDirective + " " + (event.blockedURI || "inline"));
});
"""


def _start(tmp_path: Path):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'browser.db'}",
            secret_key="test-key-5d364b22dfd4d0ff6f63873",
            smtp_host="smtp.test",
            smtp_from="budget@example.com",
            base_url="http://testserver",
        ),
        mailer=FakeMailer(),
        mail_delay=lambda: 0.0,
    )
    clock = FixedClock(f"{REFERENCE.isoformat()}T12:00:00+00:00")
    app.state.clock = clock
    app.state.session_factory.configure(info={"clock": clock})
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

    run_initial_setup(
        app.state.session_factory,
        email="admin@example.com",
        display_name="Browser Admin",
        password=PASSWORD,
        workspace_name="Browser Home",
        settings=app.state.settings,
        clock=clock,
    )
    with app.state.session_factory.begin() as session:
        user = session.scalars(select(User)).one()
        workspace = session.scalars(select(Workspace)).one()
        workspace.taxes_enabled = True
        instance = session.get(InstanceSettings, 1)
        assert instance is not None
        instance.allow_registration = True
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
                taxes_enabled=True,
            ),
            role=Role.OWNER,
            clock=clock,
        )
        plan = plan_for_workspace(ctx, REFERENCE)
    build_demo_workspace(
        app.state.session_factory,
        ctx,
        plan,
        data_dir=app.state.settings.data_dir,
        horizon_months=app.state.settings.recurrence_horizon_months,
    )
    return app, server, thread, port


def _sign_in(browser: Browser, base: str, *, phone: bool = False) -> Page:
    context = browser.new_context(
        viewport={"width": 390, "height": 844} if phone else {"width": 1440, "height": 900},
        color_scheme="light",
    )
    context.add_init_script(VIOLATION_RECORDER)
    page = context.new_page()
    page.goto(f"{base}/login", wait_until="networkidle")
    page.get_by_label("Email").fill("admin@example.com")
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(f"{base}/")
    return page


def _no_page_overflow(page: Page, label: str) -> None:
    sizes = page.evaluate(
        """() => ({
          document: document.documentElement.scrollWidth,
          body: document.body.scrollWidth,
          viewport: window.innerWidth,
        })"""
    )
    assert sizes["document"] <= sizes["viewport"] + 1, (label, sizes)
    assert sizes["body"] <= sizes["viewport"] + 1, (label, sizes)


def _summary_fits(page: Page) -> None:
    panes = page.locator("[data-money-block]")
    assert panes.count() >= 1
    for index in range(panes.count()):
        widths = panes.nth(index).evaluate("el => [el.clientWidth, el.scrollWidth]")
        if widths[1] > widths[0] + 1:
            details = panes.nth(index).evaluate(
                """el => {
                  const table = el.querySelector("table");
                  return {
                    panel: [el.clientWidth, el.scrollWidth],
                    table: [table.offsetWidth, table.scrollWidth],
                    rect: table.getBoundingClientRect().width,
                    cells: [...table.querySelectorAll("thead th")].map((c) => [
                      c.textContent.trim(), c.getBoundingClientRect().width,
                    ]),
                    wide: [...table.querySelectorAll("th,td")]
                      .map((c) => ({
                        text: c.innerText.slice(0, 40),
                        width: c.getBoundingClientRect().width,
                        scroll: c.scrollWidth,
                        client: c.clientWidth,
                      }))
                      .filter((c) => c.scroll > c.client + 0.5),
                  };
                }"""
            )
            raise AssertionError((widths, details))
    for heading in ("Category", "Plan", "Actual"):
        cell = page.get_by_role("columnheader", name=heading, exact=True).first
        expect(cell).to_be_visible()
        box = cell.bounding_box()
        assert box is not None
        assert box["x"] >= -1 and box["x"] + box["width"] <= 391, (heading, box)
    # F9.9's fallback: at phone width Difference is a line under Actual, not a column.
    expect(page.get_by_role("columnheader", name="Difference", exact=True).first).to_be_hidden()
    expect(page.locator("[data-difference-phone]").first).to_be_visible()


def test_phone_layout(tmp_path: Path) -> None:
    app, server, thread, port = _start(tmp_path)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            base = f"http://127.0.0.1:{port}"
            page = _sign_in(browser, base)
            page.set_viewport_size({"width": 390, "height": 844})

            # The summary's phone rules: the currency once in the header, no symbol in the
            # amount cells, a smaller table type step and the narrow planned input.
            page.goto(f"{base}/m/{MONTH}", wait_until="networkidle")
            _no_page_overflow(page, "month")
            _summary_fits(page)
            expect(page.locator("[data-summary-currency]")).to_be_visible()
            assert page.locator("[data-summary-currency]").inner_text().strip() == "(EUR)"
            summary = page.locator("#month-summary")
            assert summary.evaluate(
                "el => getComputedStyle(el.querySelector('table')).fontSize"
            ) == ("12px")
            first_cell = summary.locator("tr[data-plan-row] td[data-actual-cell]").first
            assert first_cell.evaluate("el => getComputedStyle(el).paddingTop") == "4px"

            # A long demo category name and a six-digit planned value stay editable at 390 px.
            row = summary.locator("tr[data-plan-row]", has_text="Entertainment").first
            planned = row.locator("[data-plan-input]")
            expect(planned).to_be_visible()
            assert planned.get_attribute("size") == "7"
            planned.fill("123456.78")
            planned.press("Enter")
            expect(row.locator("[data-plan-input]")).to_have_value("123456.78")
            _no_page_overflow(page, "month with a six-digit plan")
            _summary_fits(page)

            # The Transactions entry row: 44 px targets, a decimal keypad and no page overflow.
            page.goto(f"{base}/transactions/{MONTH}", wait_until="networkidle")
            _no_page_overflow(page, "transactions")
            entry = page.locator("#entry-expense-row")
            expect(entry).to_be_visible()
            for field in (
                "#entry-expense-day",
                "#entry-expense-amount",
                "#entry-expense-description",
                "#entry-expense-category",
            ):
                box = entry.locator(field).bounding_box()
                assert box is not None and box["height"] >= 43.5, (field, box)
            add = entry.locator("[data-entry-add]")
            add_box = add.bounding_box()
            assert add_box is not None and add_box["height"] >= 43.5, add_box
            assert entry.locator("#entry-expense-amount").get_attribute("inputmode") == "decimal"
            assert entry.locator("#entry-expense-day").get_attribute("inputmode") == "numeric"
            _no_page_overflow(page, "transactions with the entry row")

            # A six-digit actual through the phone entry row, so Actual and Difference carry
            # six digits at 390 px too.
            entry.locator("#entry-expense-day").fill("5")
            entry.locator("#entry-expense-amount").fill("123456.78")
            entry.locator("#entry-expense-category").select_option(label="Entertainment")
            entry.locator("#entry-expense-amount").press("Enter")
            expect(
                page.locator("#ledger-expense td[data-amount]", has_text="123,456.78")
            ).to_have_count(1)
            page.goto(f"{base}/m/{MONTH}", wait_until="networkidle")
            _summary_fits(page)
            actual = row.locator("[data-actual-phone]")
            expect(actual).to_be_visible()
            assert actual.inner_text().strip().startswith("123,")
            expect(row.locator("[data-difference-phone]")).to_be_visible()
            _no_page_overflow(page, "month with a six-digit actual")
            assert page.evaluate("window.__cspViolations") == []

            # Pending amounts shorten to "+amount" on phones, while the screen-reader text
            # keeps the full wording with "pending".
            phone_pending = summary.locator("[data-pending-phone]").first
            expect(phone_pending).to_be_visible()
            visible_pending = phone_pending.locator("span[aria-hidden='true']").inner_text().strip()
            assert visible_pending.startswith("+")
            assert "pending" not in visible_pending
            assert "€" not in visible_pending

            # Upcoming: Mark paid and Skip each work with one tap at 390 px.
            page.goto(f"{base}/upcoming", wait_until="networkidle")
            _no_page_overflow(page, "upcoming")
            article = page.locator("article[data-occurrence]").first
            expect(article).to_be_visible()
            post = article.locator("[data-phone-post] button")
            skip = article.locator("[data-phone-skip] button")
            for button in (post, skip):
                box = button.bounding_box()
                assert box is not None and box["height"] >= 43.5, box
            post.click()
            page.wait_for_load_state("networkidle")
            expect(page.locator("[data-post-undo]")).to_be_visible()
            page.locator("[data-post-undo] button").click()
            page.wait_for_load_state("networkidle")
            article = page.locator("article[data-occurrence]").first
            article.locator("[data-phone-skip] button").click()
            page.wait_for_load_state("networkidle")
            expect(page.locator("[data-upcoming-notice]")).to_be_visible()
            page.locator("[data-skip-undo] button").click()
            page.wait_for_load_state("networkidle")

            # Every other page stays inside the 390 px viewport (F9.9 review).
            page.goto(f"{base}/income", wait_until="networkidle")
            calendar_link = page.locator("[data-work-calendar-link]").first
            calendar_path = calendar_link.get_attribute("href") if calendar_link.count() else None
            pages = {
                "home": "/",
                "month": f"/m/{MONTH}",
                "transactions": f"/transactions/{MONTH}",
                "history": "/history",
                "upcoming": "/upcoming",
                "recurring": "/recurring",
                "income": "/income",
                "taxes": "/taxes",
                "categories": "/categories",
                "account": "/account",
                "account security": "/account/password",
                "workspace settings": "/workspace/settings",
                "workspace data": "/workspace/data",
                "audit": "/audit?scope=workspace",
            }
            assert calendar_path
            pages["work calendar"] = calendar_path
            for label, path in pages.items():
                response = page.goto(f"{base}{path}", wait_until="networkidle")
                assert response is not None and response.ok, (label, response and response.status)
                _no_page_overflow(page, label)

            # Auth pages use their own shell, without the workspace navigation.
            context = browser.new_context(viewport={"width": 390, "height": 844})
            context.add_init_script(VIOLATION_RECORDER)
            auth = context.new_page()
            for label, path in (
                ("login", "/login"),
                ("forgot", "/password/forgot"),
                ("register", "/register"),
            ):
                response = auth.goto(f"{base}{path}", wait_until="networkidle")
                assert response is not None and response.ok, (label, response and response.status)
                _no_page_overflow(auth, label)
            assert auth.evaluate("window.__cspViolations") == []
            assert page.evaluate("window.__cspViolations") == []
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
