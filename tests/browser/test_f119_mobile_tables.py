"""F11.9 in Chromium at 390 × 844 and 1440 × 900: the Transactions header add action, the
Month phone difference and page identity, the admin users card rows, labelled scroll regions,
the Upcoming skip anchor, the skip link, the named confirm dialog and Home's header Customise.
No CSP violations and no page-level horizontal overflow."""

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
from assetboard.branding import APP_NAME
from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.db.models.identity import InstanceSettings, User, Workspace
from assetboard.services.auth import argon2_hash, run_initial_setup
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
            secret_key="browser-test-key",
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
        session.add(
            User(
                email="second@example.com",
                display_name="Second User",
                password_hash=argon2_hash(PASSWORD),
            )
        )
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


def _sign_in(browser: Browser, base: str) -> Page:
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="light")
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


def test_f119_mobile_tables_and_page_identity(tmp_path: Path) -> None:
    app, server, thread, port = _start(tmp_path)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            base = f"http://127.0.0.1:{port}"
            page = _sign_in(browser, base)
            console_errors: list[str] = []
            page.on(
                "console",
                lambda message: (
                    console_errors.append(message.text) if message.type == "error" else None
                ),
            )

            # The skip link is the first focusable element and focuses <main>.
            page.evaluate("window.scrollTo(0, 0)")
            page.keyboard.press("Tab")
            expect(page.locator("[data-skip-link]")).to_be_focused()
            page.keyboard.press("Enter")
            expect(page.locator("#main-content")).to_be_focused()

            # Home: Customise is a header action in the first viewport, not at the bottom.
            customise = page.locator("[data-customise-link]")
            expect(customise).to_be_visible()
            customise_box = customise.bounding_box()
            assert customise_box is not None and customise_box["y"] < 900
            assert page.locator("[data-home-actions]").count() == 0

            # The generic confirm dialog has an accessible name.
            page.goto(f"{base}/m/{MONTH}", wait_until="networkidle")
            page.locator("[data-month-close]").click()
            dialog = page.get_by_role("dialog", name="Are you sure?")
            expect(dialog).to_be_visible()
            page.keyboard.press("Escape")
            expect(dialog).to_be_hidden()

            # Month names the destination and keeps the period as its subtitle and title detail.
            expect(page.locator("h1")).to_have_text("Month")
            expect(page.locator("[data-month-subtitle]")).to_have_text("November 2026")
            expect(page).to_have_title(f"Month: November 2026 · {APP_NAME}")

            # Phone layout.
            page.set_viewport_size({"width": 390, "height": 844})
            page.goto(f"{base}/", wait_until="networkidle")
            expect(page.locator("[data-customise-link]")).to_be_visible()
            phone_customise_box = page.locator("[data-customise-link]").bounding_box()
            assert phone_customise_box is not None and phone_customise_box["y"] < 844
            page.goto(f"{base}/m/{MONTH}", wait_until="networkidle")
            _no_page_overflow(page, "month")
            taxes = page.locator("#month-summary tr[data-plan-row]", has_text="Taxes").first
            expect(taxes.locator("[data-difference-phone]")).to_be_visible()
            expect(taxes.locator("[data-difference-phone]")).to_contain_text("vs plan")
            expect(page.get_by_role("columnheader", name="Difference", exact=True)).to_be_hidden()

            # Transactions: the header Add entry is in the first viewport and focuses the Day
            # field of the existing chronological row, at both widths.
            for width, height in ((390, 844), (1440, 900)):
                page.set_viewport_size({"width": width, "height": height})
                page.goto(f"{base}/transactions/{MONTH}", wait_until="networkidle")
                add = page.locator("[data-add-entry='expense']")
                expect(add).to_be_visible()
                add_box = add.bounding_box()
                assert add_box is not None and add_box["y"] < height
                add.click()
                expect(page.locator("#entry-expense-day")).to_be_focused()
                row = page.locator("#entry-expense-row").bounding_box()
                assert row is not None and 0 <= row["y"] < height, (width, row)
                expect(page.locator("[data-add-entry='income']")).to_be_visible()

            # Wide tables: a named, focusable scroll region with a visible phone hint; the
            # keyboard can scroll it sideways.
            page.set_viewport_size({"width": 390, "height": 844})
            page.goto(f"{base}/transactions/{MONTH}", wait_until="networkidle")
            region = page.locator("#ledger-expense [data-table-scroll]")
            expect(region).to_have_attribute("role", "region")
            expect(region).to_have_attribute("aria-label", "Expenses")
            expect(region).to_have_attribute("tabindex", "0")
            expect(page.locator("#ledger-expense [data-table-scroll-hint]")).to_be_visible()
            region.focus()
            before = region.evaluate("el => el.scrollLeft")
            page.keyboard.press("ArrowRight")
            after = region.evaluate("el => el.scrollLeft")
            assert after > before, (
                before,
                after,
                region.evaluate(
                    "el => [el.scrollWidth, el.clientWidth, el.tabIndex,"
                    " getComputedStyle(el).overflowX]"
                ),
            )

            page.goto(f"{base}/history", wait_until="networkidle")
            history_region = page.locator("[data-results] [data-table-scroll]").first
            expect(history_region).to_have_attribute("role", "region")
            expect(page.locator("[data-results] [data-table-scroll-hint]").first).to_be_visible()

            page.goto(f"{base}/reports", wait_until="networkidle")
            year_region = page.locator("#year-overview [data-table-scroll]")
            expect(year_region).to_have_attribute("role", "region")
            expect(page.locator("#year-overview [data-table-scroll-hint]")).to_be_visible()

            # Admin users: card rows at 390, no page overflow, status and actions reachable.
            page.goto(f"{base}/admin/users", wait_until="networkidle")
            _no_page_overflow(page, "admin users")
            second = page.locator('[data-user="second@example.com"]')
            expect(second.get_by_text("Status", exact=True)).to_be_visible()
            expect(second).to_be_visible()
            history = second.locator("[data-audit-history]")
            expect(history).to_be_visible()
            history_box = history.bounding_box()
            assert history_box is not None and history_box["x"] + history_box["width"] <= 391
            second.locator("[data-row-menu] summary").click()
            menu = second.locator("[data-row-menu] .row-menu")
            expect(menu).to_be_visible()
            menu_box = menu.bounding_box()
            assert menu_box is not None and menu_box["x"] >= 0
            assert menu_box["x"] + menu_box["width"] <= 391

            # Upcoming: skip a row in a week with another row; the redirect keeps that week in
            # view and the notice stays pinned in the viewport with its Undo.
            page.goto(f"{base}/upcoming", wait_until="networkidle")
            target = page.evaluate(
                """() => {
                  const weeks = [...document.querySelectorAll('section[aria-labelledby^="week-"]')];
                  for (let i = weeks.length - 1; i >= 0; i--) {
                    const articles = [...weeks[i].querySelectorAll('article[data-occurrence]')];
                    const skips = articles.filter(
                      (article) => article.querySelector('[data-phone-skip] button'));
                    if (articles.length > 1 && skips.length) {
                      return {
                        article: skips[skips.length - 1].id,
                        week: weeks[i].getAttribute('aria-labelledby'),
                      };
                    }
                  }
                  return null;
                }"""
            )
            assert target is not None, "no skippable occurrence in a multi-row week"
            skip = page.locator(f"#{target['article']} [data-phone-skip] button")
            skip.scroll_into_view_if_needed()
            assert page.evaluate("window.scrollY") > 0
            with page.expect_response(
                lambda response: "/skip" in response.url and response.request.method == "POST"
            ) as response_info:
                skip.click()
            location = response_info.value.headers["location"]
            assert "at=week-" in location and "#week-" in location
            page.wait_for_load_state("networkidle")
            assert page.evaluate("window.scrollY") > 0
            assert f"at={target['week']}" in page.url
            week_box = page.locator(f"#{target['week']}").bounding_box()
            assert week_box is not None and 0 <= week_box["y"] < 844
            notice = page.locator("[data-upcoming-notice]")
            expect(notice).to_be_visible()
            assert notice.evaluate("el => getComputedStyle(el).position") == "fixed"
            notice_box = notice.bounding_box()
            assert notice_box is not None and notice_box["y"] + notice_box["height"] <= 845
            page.locator("[data-skip-undo] button").click()
            page.wait_for_load_state("networkidle")

            assert page.evaluate("window.__cspViolations") == []
            assert not [error for error in console_errors if "Content Security Policy" in error]
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
