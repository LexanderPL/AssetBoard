"""WP5 in Chromium: Profile and Account security share one settings width, and every Instance
settings panel fills the same max-w-3xl wrapper at desktop and phone widths."""

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
            profile = _box(page, 'div[class="max-w-3xl space-y-6"]')
            page.goto(f"{base}/account/password", wait_until="networkidle")
            security = _box(page, 'div[class="max-w-3xl space-y-6"]')
            assert abs(profile["x"] - security["x"]) <= 1, (width, profile, security)
            assert abs(profile["width"] - security["width"]) <= 1, (width, profile, security)
            assert profile["width"] <= 769, (width, profile)
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
            wrapper = _box(page, 'div[class="max-w-3xl space-y-6"]')
            settings = _box(page, "[data-instance-settings]")
            backups = _box(page, "[data-backup-panel]")
            for panel in (settings, backups):
                assert abs(panel["x"] - wrapper["x"]) <= 1, (width, panel, wrapper)
                assert abs(panel["width"] - wrapper["width"]) <= 1, (width, panel, wrapper)
                assert abs(panel["x"] - backups["x"]) <= 1, (width, panel, backups)
            assert wrapper["x"] >= 0 and wrapper["x"] + wrapper["width"] <= width
            assert page.evaluate(NO_OVERFLOW)
        browser.close()
