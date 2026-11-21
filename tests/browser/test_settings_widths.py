"""User-menu pages in Chromium: Profile and Account security share one settings width, every
Instance settings panel fills the same max-w-4xl wrapper, representative settings/list/import
pages share one frame, and every user-menu page fits a 390 px phone."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from playwright.sync_api import Page, sync_playwright

from assetboard.app import create_app
from assetboard.config import Settings
from assetboard.services.auth import run_initial_setup
from tests.conftest import live_server

pytestmark = pytest.mark.skipif(
    os.environ.get("ASSETBOARD_RUN_BROWSER_TESTS") != "1",
    reason="Set ASSETBOARD_RUN_BROWSER_TESTS=1 to run the Chromium tests",
)

PASSWORD = "Correct Horse Battery!"
NO_OVERFLOW = "document.documentElement.scrollWidth <= window.innerWidth"

USER_MENU_PAGES = [
    ("categories", "/categories"),
    ("workspace settings", "/workspace/settings"),
    ("workspace members", "/workspace/members"),
    ("workspace audit", "/audit?scope=workspace"),
    ("export and import", "/workspace/data"),
    ("notifications", "/notifications"),
    ("profile", "/account"),
    ("account security", "/account/password"),
    ("users", "/admin/users"),
    ("instance settings", "/admin/settings"),
    ("instance audit", "/audit?scope=instance"),
]

REPRESENTATIVE_PAGES = [
    ("settings", "/workspace/settings", "[data-settings-menu]"),
    ("categories", "/categories", '[data-section="expense"]'),
    ("users", "/admin/users", "section.panel"),
    ("import", "/workspace/data", "section.panel"),
]


def _app(tmp_path: Path):
    return create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'browser-settings-widths.db'}",
            secret_key="test-key-3b1e5a4d3f0b4c9e8a7d6f5e",
        )
    )


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


def _box(page: Page, selector: str):
    box = page.locator(selector).first.bounding_box()
    assert box is not None, selector
    return box


def test_profile_and_account_security_share_one_width(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with live_server(app) as base, sync_playwright() as playwright:
        _setup(app)
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        _sign_in(page, base)

        for width in (1280, 390):
            page.set_viewport_size({"width": width, "height": 900 if width == 1280 else 844})
            page.goto(f"{base}/account", wait_until="networkidle")
            profile = _box(page, '[data-page-width="4xl"]')
            page.goto(f"{base}/account/password", wait_until="networkidle")
            security = _box(page, '[data-page-width="4xl"]')
            assert abs(profile["x"] - security["x"]) <= 1, (width, profile, security)
            assert abs(profile["width"] - security["width"]) <= 1, (width, profile, security)
            if width == 1280:
                assert 880 <= profile["width"] <= 897, (width, profile)
            else:
                assert profile["width"] <= width, (width, profile)
            assert security["x"] >= 0 and security["x"] + security["width"] <= width
            assert page.evaluate(NO_OVERFLOW)
        browser.close()


def test_instance_settings_panels_share_one_width(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with live_server(app) as base, sync_playwright() as playwright:
        _setup(app)
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        _sign_in(page, base)

        for width in (1280, 390):
            page.set_viewport_size({"width": width, "height": 900 if width == 1280 else 844})
            page.goto(f"{base}/admin/settings", wait_until="networkidle")
            wrapper = _box(page, '[data-page-width="4xl"]')
            settings = _box(page, "[data-instance-settings]")
            backups = _box(page, "[data-backup-panel]")
            for panel in (settings, backups):
                assert abs(panel["x"] - wrapper["x"]) <= 1, (width, panel, wrapper)
                assert abs(panel["width"] - wrapper["width"]) <= 1, (width, panel, wrapper)
                assert abs(panel["x"] - backups["x"]) <= 1, (width, panel, backups)
            assert wrapper["x"] >= 0 and wrapper["x"] + wrapper["width"] <= width
            assert page.evaluate(NO_OVERFLOW)
        browser.close()


def test_representative_user_menu_pages_share_one_frame(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with live_server(app) as base, sync_playwright() as playwright:
        _setup(app)
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        _sign_in(page, base)

        for width in (1280, 390):
            page.set_viewport_size({"width": width, "height": 900 if width == 1280 else 844})
            frames = []
            for label, path, panel_selector in REPRESENTATIVE_PAGES:
                page.goto(f"{base}{path}", wait_until="networkidle")
                frame = _box(page, '[data-page-width="4xl"]')
                panel = _box(page, panel_selector)
                assert abs(panel["x"] - frame["x"]) <= 1, (label, width, panel, frame)
                assert abs(panel["width"] - frame["width"]) <= 1, (label, width, panel, frame)
                assert frame["x"] >= 0 and frame["x"] + frame["width"] <= width, (
                    label,
                    width,
                    frame,
                )
                if width == 1280:
                    assert 880 <= frame["width"] <= 897, (label, width, frame)
                assert page.evaluate(NO_OVERFLOW), (label, width)
                frames.append(frame)

            first = frames[0]
            for label, frame in zip(("categories", "users", "import"), frames[1:], strict=True):
                assert abs(frame["x"] - first["x"]) <= 1, (width, label, frame, first)
                assert abs(frame["width"] - first["width"]) <= 1, (width, label, frame, first)
        browser.close()


def test_every_user_menu_page_fits_a_phone(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with live_server(app) as base, sync_playwright() as playwright:
        _setup(app)
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        _sign_in(page, base)

        for label, path in USER_MENU_PAGES:
            response = page.goto(f"{base}{path}", wait_until="networkidle")
            assert response is not None and response.ok, (label, response and response.status)
            assert page.evaluate(NO_OVERFLOW), label

        page.goto(f"{base}/audit?scope=workspace", wait_until="networkidle")
        assert (
            page.eval_on_selector(
                'section[aria-label="Audit log"]',
                "element => getComputedStyle(element).overflowX",
            )
            == "auto"
        )

        page.goto(f"{base}/admin/users", wait_until="networkidle")
        assert (
            page.eval_on_selector(
                'section[aria-label="Users"]',
                "element => getComputedStyle(element).overflowX",
            )
            == "auto"
        )
        browser.close()
