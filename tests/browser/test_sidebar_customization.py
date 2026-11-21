"""F9.12 in Chromium: the Profile customiser at 1440 px and 390 px, arrows reordering rows in
place without saving, the Save button persisting the order, the desktop and mobile navigation
reflecting the saved order, checkbox + save, reset, one action row, no horizontal overflow, and
a JavaScript-disabled fallback that still saves and moves."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import parse_qs

import pytest
from playwright.sync_api import Page, expect, sync_playwright
from sqlalchemy import select

from assetboard.app import create_app
from assetboard.config import Settings
from assetboard.db.models.identity import User
from assetboard.services.auth import run_initial_setup
from tests.conftest import live_server

pytestmark = pytest.mark.skipif(
    os.environ.get("ASSETBOARD_RUN_BROWSER_TESTS") != "1",
    reason="Set ASSETBOARD_RUN_BROWSER_TESTS=1 to run the Chromium tests",
)

PASSWORD = "Correct Horse Battery!"
NAV_KEYS = """() => Array.from(
  document.querySelectorAll('nav[aria-label="Main navigation"] a.nav-link')
).map((a) => a.getAttributeNames().find(
  (name) => name.startsWith("data-") && name.endsWith("-link")
).slice(5, -5))"""
NO_OVERFLOW = "document.documentElement.scrollWidth <= window.innerWidth"
FORM_KEYS = """() => Array.from(
  document.querySelectorAll('[data-sidebar-list] > [data-sidebar-item]')
).map((row) => row.dataset.sidebarItem)"""


def _app(tmp_path: Path):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'browser-sidebar.db'}",
            secret_key="test-key-cb204ffa891a6a56e860db7",
        )
    )
    return app


def _setup(app) -> None:
    run_initial_setup(
        app.state.session_factory,
        email="admin@example.com",
        display_name="Browser Admin",
        password=PASSWORD,
        workspace_name="Browser Home",
        settings=app.state.settings,
        clock=app.state.clock,
    )


def _sign_in(page: Page, base: str) -> None:
    page.goto(f"{base}/login", wait_until="networkidle")
    page.get_by_label("Email").fill("admin@example.com")
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(f"{base}/")


def _nav(page: Page) -> list[str]:
    return page.evaluate(NAV_KEYS)


def _form_keys(page: Page) -> list[str]:
    return page.evaluate(FORM_KEYS)


def _stored_sidebar(app) -> list[dict[str, object]] | None:
    with app.state.session_factory() as session:
        user = session.scalars(select(User).where(User.email == "admin@example.com")).one()
        return user.sidebar_items


def _actions_are_inline(page: Page) -> None:
    save = page.locator("[data-sidebar-save]").bounding_box()
    reset = page.locator("[data-sidebar-reset]").bounding_box()
    assert save is not None and reset is not None
    assert abs(save["y"] - reset["y"]) <= 1, (save, reset)
    assert save["x"] < reset["x"], (save, reset)


def _open_menu(page: Page) -> None:
    menu = page.locator("[data-sidebar-toggle]")
    if menu.is_visible():
        menu.click()
        expect(page.locator("[data-home-link]")).to_be_visible()


def test_the_sidebar_customiser_at_desktop_and_phone_widths(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with live_server(app) as base, sync_playwright() as playwright:
        _setup(app)
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        _sign_in(page, base)

        page.goto(f"{base}/account", wait_until="networkidle")
        settings = page.locator("[data-sidebar-settings]")
        expect(settings).to_be_visible()
        expect(page.locator('[data-sidebar-item="home"] [data-sidebar-visible="home"]')
               ).to_be_checked()  # fmt: skip
        expect(page.locator('[data-sidebar-item="home"] [data-sidebar-visible="home"]')
               ).to_be_disabled()  # fmt: skip
        expect(page.locator("[data-sidebar-save]")).to_have_text("Save sidebar")
        assert page.locator("[data-sidebar-nojs-note]").count() == 0
        expect(page.locator('[data-sidebar-move="income:up"]')).to_have_attribute("type", "button")
        _actions_are_inline(page)
        assert _stored_sidebar(app) is None

        # Click "Move up" for Income at desktop width: the row moves in place, the page does
        # not reload and nothing is saved until Save sidebar is pressed.
        page.locator('[data-sidebar-move="income:up"]').click()
        assert page.url == f"{base}/account"
        assert _form_keys(page) == [
            "home", "month", "transactions", "history", "reports", "upcoming", "income",
            "recurring", "taxes", "net_worth", "investments", "loans", "fire",
        ]  # fmt: skip
        assert _nav(page).index("income") > _nav(page).index("recurring")
        assert page.locator("[data-sidebar-notice]").count() == 0
        assert page.evaluate("document.activeElement?.dataset?.sidebarMove") == "income:up"
        # The local move has not written the preference.
        assert _stored_sidebar(app) is None
        assert page.evaluate(NO_OVERFLOW)
        with page.expect_request(
            lambda request: request.method == "POST" and request.url.endswith("/account/sidebar")
        ) as save_request:
            page.locator("[data-sidebar-save]").click()
        fields = parse_qs(save_request.value.post_data or "")
        assert "move" not in fields
        assert fields["item"] == [
            "home", "month", "transactions", "history", "reports", "upcoming", "income",
            "recurring", "taxes", "net_worth", "investments", "loans", "fire",
        ]  # fmt: skip
        page.wait_for_url(f"{base}/account?sidebar=saved#sidebar-settings")
        expect(settings.locator('[role="status"]')).to_contain_text("Your sidebar is saved.")
        assert _nav(page).index("income") < _nav(page).index("recurring")
        assert [entry["key"] for entry in _stored_sidebar(app) or []] == fields["item"]
        assert page.evaluate(NO_OVERFLOW)
        _actions_are_inline(page)

        # Keyboard move at phone width: Income goes back down in place, then Save persists
        # the canonical list again.
        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(f"{base}/account", wait_until="networkidle")
        move_down = page.locator('[data-sidebar-move="income:down"]')
        move_down.focus()
        page.keyboard.press("Enter")
        assert page.url == f"{base}/account"
        assert _form_keys(page) == [
            "home", "month", "transactions", "history", "reports", "upcoming", "recurring",
            "income", "taxes", "net_worth", "investments", "loans", "fire",
        ]  # fmt: skip
        assert page.evaluate("document.activeElement?.dataset?.sidebarMove") == "income:down"
        # The local keyboard move has not changed the order saved above.
        assert [entry["key"] for entry in _stored_sidebar(app) or []] == [
            "home", "month", "transactions", "history", "reports", "upcoming", "income",
            "recurring", "taxes", "net_worth", "investments", "loans", "fire",
        ]  # fmt: skip
        _actions_are_inline(page)
        page.locator("[data-sidebar-save]").click()
        page.wait_for_url(f"{base}/account?sidebar=saved#sidebar-settings")
        assert _stored_sidebar(app) is None  # the canonical list is stored as NULL
        page.goto(f"{base}/", wait_until="networkidle")
        _open_menu(page)
        assert _nav(page).index("income") > _nav(page).index("recurring")
        assert page.evaluate(NO_OVERFLOW)

        # Checkbox + save at phone width: History disappears from the mobile nav, while its
        # address keeps working.
        page.goto(f"{base}/account", wait_until="networkidle")
        page.locator('[data-sidebar-visible="history"]').uncheck()
        page.locator("[data-sidebar-save]").click()
        page.wait_for_url(f"{base}/account?sidebar=saved#sidebar-settings")
        page.goto(f"{base}/", wait_until="networkidle")
        _open_menu(page)
        assert "history" not in _nav(page)
        assert page.evaluate(NO_OVERFLOW)
        page.goto(f"{base}/history", wait_until="networkidle")
        expect(page.locator("h1")).to_be_visible()

        # Reset at phone width: History is back, in the canonical order, in both navs.
        page.goto(f"{base}/account", wait_until="networkidle")
        page.locator("[data-sidebar-reset]").click()
        page.wait_for_url(f"{base}/account?sidebar=reset#sidebar-settings")
        expect(settings.locator('[role="status"]')).to_contain_text(
            "Your sidebar is back to the default order."
        )
        page.goto(f"{base}/", wait_until="networkidle")
        _open_menu(page)
        assert _nav(page) == [
            "home", "month", "transactions", "history", "reports", "upcoming", "recurring",
            "income", "net-worth", "investments", "loans", "fire",
        ]  # fmt: skip
        assert page.evaluate(NO_OVERFLOW)
        page.set_viewport_size({"width": 1440, "height": 900})
        page.goto(f"{base}/", wait_until="networkidle")
        assert _nav(page) == [
            "home", "month", "transactions", "history", "reports", "upcoming", "recurring",
            "income", "net-worth", "investments", "loans", "fire",
        ]  # fmt: skip

        # Polish labels are longer: the two actions still share one row at phone width.
        with app.state.session_factory() as session, session.begin():
            user = session.scalars(select(User).where(User.email == "admin@example.com")).one()
            user.locale = "pl"
        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(f"{base}/account", wait_until="networkidle")
        expect(page.locator("[data-sidebar-save]")).to_have_text("Zapisz pasek")
        _actions_are_inline(page)
        assert page.evaluate(NO_OVERFLOW)
        page.set_viewport_size({"width": 1440, "height": 900})
        _actions_are_inline(page)
        assert page.evaluate(NO_OVERFLOW)
        browser.close()


def test_the_taxes_note_keeps_the_arrows_on_the_label_line(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with live_server(app) as base, sync_playwright() as playwright:
        _setup(app)
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        _sign_in(page, base)
        page.goto(f"{base}/account", wait_until="networkidle")

        for width in (1280, 390):
            page.set_viewport_size({"width": width, "height": 900 if width == 1280 else 844})
            page.goto(f"{base}/account", wait_until="networkidle")
            taxes = page.locator('[data-sidebar-item="taxes"]')
            transactions = page.locator('[data-sidebar-item="transactions"]')
            label = taxes.locator('label[for="sidebar-visible-taxes"]').bounding_box()
            note = taxes.locator("[data-sidebar-taxes-note]").bounding_box()
            up = taxes.locator('[data-sidebar-move="taxes:up"]').bounding_box()
            down = taxes.locator('[data-sidebar-move="taxes:down"]').bounding_box()
            row = taxes.bounding_box()
            other_up = transactions.locator('[data-sidebar-move="transactions:up"]').bounding_box()
            other_row = transactions.bounding_box()
            assert label and note and up and down and row and other_up and other_row
            # Both arrows share the label's line instead of wrapping below the note.
            assert abs(up["y"] - down["y"]) <= 1, (width, up, down)
            assert up["y"] <= label["y"] + 2, (width, up, label)
            assert up["y"] < note["y"], (width, up, note)
            # The note wraps inside the middle column, never under the arrows.
            assert note["x"] >= label["x"] - 1
            assert note["x"] + note["width"] <= up["x"] + 1
            if width == 390:
                assert note["height"] > label["height"]
            # The arrows sit at the same offset from the row top as on a row without a note.
            assert abs((up["y"] - row["y"]) - (other_up["y"] - other_row["y"])) <= 1
            assert page.evaluate(NO_OVERFLOW)
        browser.close()


def test_keyboard_moves_at_the_boundaries_keep_focus(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with live_server(app) as base, sync_playwright() as playwright:
        _setup(app)
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        _sign_in(page, base)
        page.goto(f"{base}/account", wait_until="networkidle")

        # Up into the first non-Home slot: Up becomes disabled; focus moves to the other arrow
        # of the same row instead of being dropped to <body>.
        page.locator('[data-sidebar-move="transactions:up"]').focus()
        page.keyboard.press("Enter")
        assert _form_keys(page).index("transactions") == 1
        expect(page.locator('[data-sidebar-move="transactions:up"]')).to_be_disabled()
        assert page.evaluate("document.activeElement?.dataset?.sidebarMove") == "transactions:down"
        assert page.locator("[data-sidebar-notice]").count() == 0
        assert _stored_sidebar(app) is None

        # Down into the last slot: Down becomes disabled; focus moves to the other arrow.
        page.reload(wait_until="networkidle")
        page.locator('[data-sidebar-move="fire:up"]').click()
        page.locator('[data-sidebar-move="fire:down"]').focus()
        page.keyboard.press("Enter")
        assert _form_keys(page)[-1] == "fire"
        expect(page.locator('[data-sidebar-move="fire:down"]')).to_be_disabled()
        assert page.evaluate("document.activeElement?.dataset?.sidebarMove") == "fire:up"
        assert page.locator("[data-sidebar-notice]").count() == 0
        assert _stored_sidebar(app) is None
        browser.close()


def test_the_customiser_works_without_javascript(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with live_server(app) as base, sync_playwright() as playwright:
        _setup(app)
        browser = playwright.chromium.launch()
        context = browser.new_context(
            java_script_enabled=False, viewport={"width": 390, "height": 844}
        )
        page = context.new_page()
        _sign_in(page, base)

        page.goto(f"{base}/account", wait_until="networkidle")
        expect(page.locator("[data-sidebar-form]")).to_be_visible()
        expect(page.locator("[data-sidebar-nojs-note]")).to_contain_text(
            "Without JavaScript, moving a page saves the new order immediately."
        )
        # Fallback: without JavaScript the arrow is still a submit button, so it applies the
        # move on the server and reloads. With JavaScript the move stays local until Save.
        expect(page.locator('[data-sidebar-move="loans:up"]')).to_have_attribute("type", "submit")
        page.locator('[data-sidebar-move="loans:up"]').click()
        page.wait_for_url(f"{base}/account?sidebar=saved#sidebar-settings")
        page.goto(f"{base}/", wait_until="networkidle")
        # Without JavaScript the phone navigation stays unfolded.
        expect(page.locator("[data-home-link]")).to_be_visible()
        assert _nav(page).index("loans") < _nav(page).index("investments")
        assert page.evaluate(NO_OVERFLOW)

        page.goto(f"{base}/account", wait_until="networkidle")
        page.locator("[data-sidebar-reset]").click()
        page.wait_for_url(f"{base}/account?sidebar=reset#sidebar-settings")
        page.goto(f"{base}/", wait_until="networkidle")
        assert _nav(page) == [
            "home", "month", "transactions", "history", "reports", "upcoming", "recurring",
            "income", "net-worth", "investments", "loans", "fire",
        ]  # fmt: skip
        assert page.evaluate(NO_OVERFLOW)
        context.close()
        browser.close()
