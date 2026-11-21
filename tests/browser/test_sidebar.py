"""The desktop sidebar in Chromium: its footer (version, dark mode) stays at the bottom of the
viewport on long pages, before and after scrolling, whatever the window height (the nav scrolls
inside the sidebar instead); on a phone the sidebar is a bar above the content whose Menu button
unfolds the navigation (Escape folds it again). The Home item marks the dashboard as the current
page. The user menu sits at the top of the page. Every destination icon is a 20 px Heroicons
outline SVG, hidden from assistive technology and centred with its label."""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from playwright.sync_api import Page, expect, sync_playwright
from sqlalchemy import select

from assetboard.app import create_app
from assetboard.config import Settings
from assetboard.db.models.identity import User

pytestmark = pytest.mark.skipif(
    os.environ.get("ASSETBOARD_RUN_BROWSER_TESTS") != "1",
    reason="Set ASSETBOARD_RUN_BROWSER_TESTS=1 to run the Chromium tests",
)

PASSWORD = "Correct Horse Battery!"
MENU_BOX = """() => {
  const box = document.querySelector('[data-sidebar-footer]').getBoundingClientRect();
  return {top: box.top, bottom: box.bottom, height: innerHeight};
}"""
ICON_METRICS = """() => Array.from(
  document.querySelectorAll('[data-sidebar] nav a.nav-link')
).map((link) => {
  const svg = link.querySelector('svg');
  const box = svg.getBoundingClientRect();
  const linkBox = link.getBoundingClientRect();
  const key = link.getAttributeNames().find(
    (name) => name.startsWith('data-') && name.endsWith('-link')
  );
  return {
    key: key ? key.slice(5, -5) : null,
    width: box.width,
    height: box.height,
    iconCenter: box.y + box.height / 2,
    linkCenter: linkBox.y + linkBox.height / 2,
    ariaHidden: svg.getAttribute('aria-hidden'),
    stroke: svg.getAttribute('stroke'),
    strokeWidth: svg.getAttribute('stroke-width'),
    paths: Array.from(svg.querySelectorAll('path')).map((path) => path.getAttribute('d')),
  };
})"""


def _icon_metrics(page: Page) -> list[dict[str, Any]]:
    return page.evaluate(ICON_METRICS)


def _assert_sidebar_icons(page: Page) -> None:
    metrics = _icon_metrics(page)
    assert len(metrics) == 12  # every default destination except Taxes
    assert len({tuple(metric["paths"]) for metric in metrics}) == 12
    for metric in metrics:
        assert metric["width"] == pytest.approx(20, abs=0.5), metric
        assert metric["height"] == pytest.approx(20, abs=0.5), metric
        assert metric["ariaHidden"] == "true", metric
        assert metric["stroke"] == "currentColor", metric
        assert metric["strokeWidth"] == "1.5", metric
        assert abs(metric["iconCenter"] - metric["linkCenter"]) <= 1, metric


def _menu_in_viewport(page: Page) -> None:
    box = page.evaluate(MENU_BOX)
    assert box["top"] >= 0 and box["bottom"] <= box["height"], box


def test_the_sidebar_footer_stays_at_the_bottom_of_the_viewport(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'browser.db'}",
            secret_key="test-key-5d364b22dfd4d0ff6f63873",
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
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            base = f"http://127.0.0.1:{port}"
            page.goto(f"{base}/", wait_until="networkidle")
            page.get_by_label("Setup token").fill(app.state.setup_token)
            page.get_by_label("Email").fill("admin@example.com")
            page.get_by_label("Display name").fill("Browser Admin")
            page.get_by_label("Password").fill(PASSWORD)
            page.get_by_label("Workspace name").fill("Browser Home")
            page.get_by_role("button", name="Create administrator").click()
            page.wait_for_url(f"{base}/")
            expect(page.locator("[data-home-link]")).to_have_attribute("aria-current", "page")

            # Long pages: the suggested categories and the month summary are taller than 900px.
            for height in (900, 560):  # 560: the nav no longer fits and scrolls inside
                page.set_viewport_size({"width": 1440, "height": height})
                for path in ("/categories", "/m/2026-09"):
                    page.goto(f"{base}{path}", wait_until="networkidle")
                    assert page.evaluate(
                        "document.documentElement.scrollHeight > window.innerHeight * 1.5"
                    )
                    _menu_in_viewport(page)
                    page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
                    assert page.evaluate("window.scrollY > 0")
                    _menu_in_viewport(page)
                    expect(page.locator("[data-home-link]")).not_to_have_attribute(
                        "aria-current", "page"
                    )
                    page.evaluate("window.scrollTo(0, 0)")
                    page.locator("[data-user-menu] summary").click()
                    expect(page.locator("[data-user-menu-panel]")).to_be_in_viewport()
                    page.locator("[data-profile-link]").scroll_into_view_if_needed()
                    expect(page.locator("[data-profile-link]")).to_be_visible()
                    page.keyboard.press("Escape")

            # Phone: the sidebar sits above the content, and nothing overflows sideways.
            page.set_viewport_size({"width": 375, "height": 800})
            page.goto(f"{base}/categories", wait_until="networkidle")
            sidebar = page.locator("[data-sidebar]").bounding_box()
            main = page.locator("#main-content").bounding_box()
            assert sidebar is not None and main is not None
            assert sidebar["y"] + sidebar["height"] <= main["y"] + 1
            assert page.evaluate("document.documentElement.scrollWidth") <= 375
            # The navigation and the footer are folded away behind the Menu button.
            menu = page.locator("[data-sidebar-toggle]")
            expect(menu).to_have_attribute("aria-expanded", "false")
            expect(page.locator("[data-home-link]")).to_be_hidden()
            expect(page.locator("[data-sidebar-footer]")).to_be_hidden()
            assert sidebar["height"] < 80, sidebar
            menu.click()
            expect(menu).to_have_attribute("aria-expanded", "true")
            expect(page.locator("[data-home-link]")).to_be_visible()
            expect(page.locator("[data-sidebar-footer]")).to_be_visible()
            page.keyboard.press("Escape")
            expect(menu).to_have_attribute("aria-expanded", "false")
            expect(menu).to_be_focused()
            expect(page.locator("[data-home-link]")).to_be_hidden()
            # Desktop: no Menu button, the navigation always shows.
            page.set_viewport_size({"width": 1280, "height": 800})
            expect(menu).to_be_hidden()
            expect(page.locator("[data-home-link]")).to_be_visible()

            # Icons are 20 px decorative SVGs, centred with their labels. Screenshots at the
            # two review widths; the Polish pass proves they do not shrink when labels wrap.
            page.set_viewport_size({"width": 390, "height": 844})
            page.goto(f"{base}/", wait_until="networkidle")
            menu = page.locator("[data-sidebar-toggle]")
            menu.click()
            expect(page.locator("[data-home-link]")).to_be_visible()
            _assert_sidebar_icons(page)
            page.screenshot(path=tmp_path / "sidebar-390.png")
            page.set_viewport_size({"width": 1440, "height": 900})
            page.goto(f"{base}/", wait_until="networkidle")
            _assert_sidebar_icons(page)
            page.screenshot(path=tmp_path / "sidebar-1440.png")
            with app.state.session_factory.begin() as session:
                user = session.scalars(select(User).where(User.email == "admin@example.com")).one()
                user.locale = "pl"
            page.set_viewport_size({"width": 390, "height": 844})
            page.goto(f"{base}/", wait_until="networkidle")
            menu = page.locator("[data-sidebar-toggle]")
            if menu.is_visible():
                menu.click()
                expect(page.locator("[data-home-link]")).to_be_visible()
            _assert_sidebar_icons(page)
            assert (tmp_path / "sidebar-390.png").is_file()
            assert (tmp_path / "sidebar-1440.png").is_file()
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
