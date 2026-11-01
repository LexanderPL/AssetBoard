"""F3.4/F3.7 "Edit anyway" in Chromium: a received month is read-only on the Income page (inline
log Edit and the Log form) and in the work calendar until "Edit anyway"; saving shows the
received payment against the new amount, and "Update the transaction" changes it. No CSP
violations."""

from __future__ import annotations

import os
import socket
import threading
import time
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select

from assetboard.app import create_app
from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.db.models.ledger import Category, Transaction
from assetboard.db.repositories import ScheduledOccurrenceRepository
from assetboard.services import income
from assetboard.services.auth import run_initial_setup
from assetboard.services.context import Context, Role, UserRef, WorkspaceRef
from assetboard.services.occurrences import post_occurrence

pytestmark = pytest.mark.skipif(
    os.environ.get("ASSETBOARD_RUN_BROWSER_TESTS") != "1",
    reason="Set ASSETBOARD_RUN_BROWSER_TESTS=1 to run the Chromium tests",
)

PASSWORD = "Correct Horse Battery!"
AUGUST = date(2026, 8, 1)
VIOLATION_RECORDER = """
window.__cspViolations = [];
document.addEventListener("securitypolicyviolation", (event) => {
  window.__cspViolations.push(event.violatedDirective + " " + (event.blockedURI || "inline"));
});
"""


def _seed(app) -> tuple[UUID, UUID, UUID, UUID]:
    """Two sources whose August work (2 days at 100.00) was received on 5 September (today):
    one logged per month, one from calendar days. Returns their ids and transaction ids."""
    factory = app.state.session_factory
    admin, workspace = run_initial_setup(
        factory,
        email="admin@example.com",
        display_name="Browser Admin",
        password=PASSWORD,
        workspace_name="Browser Home",
        settings=app.state.settings,
        clock=app.state.clock,
    )
    ctx = Context(
        user=UserRef(admin.id, admin.email, admin.display_name, "en", True),
        workspace=WorkspaceRef(
            workspace.id,
            workspace.name,
            workspace.base_currency,
            workspace.locale,
            workspace.timezone,
            workspace.country_code,
        ),
        role=Role.OWNER,
        clock=app.state.clock,
    )
    with factory() as session:
        category_id = session.scalars(
            select(Category.id).where(
                Category.workspace_id == workspace.id, Category.kind == "income"
            )
        ).first()
    assert category_id is not None
    ids: list[UUID] = []
    for name in ("Monthly log", "Calendar days"):
        source_id = income.create_source(
            factory,
            ctx,
            name=name,
            kind="variable",
            category_id=category_id,
            fixed_minor=None,
            rate_minor=10_000,
            unit_label="day",
            pay_day_value=10,
            pay_lag_months=1,
            expected_mode="manual",
            expected_units_value=None,
            average_months=3,
            active_from=date(2026, 1, 1),
            active_to=None,
        )
        if name == "Monthly log":
            income.log_work(factory, ctx, source_id, AUGUST, Decimal(2))
        else:
            income.toggle_work_day(factory, ctx, source_id, date(2026, 8, 3))
            income.toggle_work_day(factory, ctx, source_id, date(2026, 8, 4))
        with factory() as session:
            occurrence = ScheduledOccurrenceRepository(session, ctx).for_income_work_month(
                source_id, AUGUST
            )
            assert occurrence is not None
            occurrence_id = occurrence.id
        transaction_id = post_occurrence(
            factory, ctx, occurrence_id, amount_minor=20_000, day=date(2026, 9, 5)
        )
        ids += [source_id, transaction_id]
    return ids[0], ids[1], ids[2], ids[3]


def _amount(app, transaction_id: UUID) -> int:
    with app.state.session_factory() as session:
        return session.get_one(Transaction, transaction_id).amount_minor


def test_edit_anyway_in_a_browser(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'browser.db'}",
            secret_key="browser-test-key",
        )
    )
    app.state.clock = FixedClock("2026-09-05T12:00:00+00:00")
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
        monthly, monthly_tx, days, days_tx = _seed(app)
        base = f"http://127.0.0.1:{port}"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context()
            context.add_init_script(VIOLATION_RECORDER)
            page = context.new_page()
            page.goto(f"{base}/login", wait_until="networkidle")
            page.get_by_label("Email").fill("admin@example.com")
            page.get_by_label("Password").fill(PASSWORD)
            page.get_by_role("button", name="Sign in").click()
            page.wait_for_url(f"{base}/")

            # Income page, Log form: a received month is read-only until "Edit anyway".
            page.goto(f"{base}/income", wait_until="networkidle")
            section = page.locator(f"#income-source-{monthly}")
            section.locator("[data-log-work] > summary").click()
            log_form = section.locator("form[data-log-form]")
            log_form.locator("select[name='work_month_month']").select_option("8")
            units = log_form.locator("[data-log-units]")
            expect(units).to_be_disabled()
            expect(log_form.locator("[data-log-hint]")).to_contain_text("already received")
            log_form.locator("[data-log-edit-anyway-button]").click()
            expect(units).to_be_enabled()
            expect(units).to_have_value("2")
            expect(log_form.locator("[data-log-override]")).to_have_value("1")
            # Choosing another month drops the override again.
            log_form.locator("select[name='work_month_month']").select_option("9")
            expect(log_form.locator("[data-log-override]")).to_have_value("")
            expect(units).to_be_enabled()
            section.locator("[data-log-work] > summary").click()

            # Income page, inline Edit anyway on the received row.
            row = section.locator('details[data-log-edit-anyway="2026-08"]')
            expect(section.locator("[data-log-locked]").first).to_have_text("received")
            row.locator("summary").click()
            row.locator("[data-inline-edit-input]").fill("3")
            row.get_by_role("button", name="Save").click()
            page.wait_for_load_state("networkidle")
            notice = page.locator("[data-posted-change]")
            expect(notice).to_contain_text("The received payment was")
            expect(notice).to_contain_text("200.00")
            expect(notice).to_contain_text("300.00")
            assert _amount(app, monthly_tx) == 20_000
            notice.locator("[data-update-transaction]").click()
            page.wait_for_load_state("networkidle")
            expect(page.locator("[data-transaction-updated]")).to_be_visible()
            assert _amount(app, monthly_tx) == 30_000

            # Work calendar: read-only, then "Edit anyway" reveals the editable days.
            page.goto(f"{base}/income/{days}/calendar?month=2026-08", wait_until="networkidle")
            expect(page.locator("[data-calendar-received]")).to_be_visible()
            expect(page.locator('button[data-day="2026-08-05"]')).to_have_count(0)
            page.locator("[data-edit-anyway]").click()
            expect(page.locator("[data-calendar-overriding]")).to_be_visible()
            page.locator('button[data-day="2026-08-05"]').click()
            change = page.locator("[data-posted-change]")
            expect(change).to_contain_text("300.00")
            expect(page.locator("[data-calendar-total]")).to_contain_text("3")
            assert _amount(app, days_tx) == 20_000
            change.locator("[data-update-transaction]").click()
            page.wait_for_load_state("networkidle")
            expect(page.locator("[data-transaction-updated]")).to_be_visible()
            assert _amount(app, days_tx) == 30_000
            assert page.evaluate("window.__cspViolations") == []
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
