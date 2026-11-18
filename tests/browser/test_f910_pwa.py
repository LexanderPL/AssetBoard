"""F9.10 in Chromium: registration, cache privacy and the offline page."""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import Page, expect, sync_playwright

from assetboard.app import create_app
from assetboard.branding import APP_NAME
from assetboard.config import Settings

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


def _start(tmp_path: Path):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'browser.db'}",
            secret_key="browser-test-key",
        )
    )
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
    return app, server, thread, port


def _setup(page: Page, base: str, app) -> None:
    page.goto(f"{base}/", wait_until="networkidle")
    page.get_by_label("Setup token").fill(app.state.setup_token)
    page.get_by_label("Email").fill("admin@example.com")
    page.get_by_label("Display name").fill("Browser Admin")
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_label("Workspace name").fill("Browser Home")
    page.get_by_role("button", name="Create administrator").click()
    page.wait_for_url(f"{base}/")


def _wait_for_worker(page: Page) -> None:
    page.wait_for_function(
        """async () => {
            if (!("serviceWorker" in navigator)) return false;
            const registration = await navigator.serviceWorker.ready;
            return registration.active !== null && navigator.serviceWorker.controller !== null;
        }"""
    )


def _cache_contents(page: Page) -> tuple[list[str], list[str]]:
    result = page.evaluate(
        """async () => {
            const names = await caches.keys();
            const pwa = names.filter((name) => name.startsWith("assetboard-pwa-"));
            if (pwa.length === 0) return { names: [], urls: [] };
            const cache = await caches.open(pwa[0]);
            const keys = await cache.keys();
            return { names: pwa, urls: keys.map((request) => {
                const url = new URL(request.url);
                return url.pathname + url.search;
            }) };
        }"""
    )
    return result["names"], result["urls"]


def test_the_app_installs_offline_and_never_caches_workspace_data(tmp_path: Path) -> None:
    app, server, thread, port = _start(tmp_path)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(
                color_scheme="light", viewport={"width": 390, "height": 844}
            )
            context.add_init_script(VIOLATION_RECORDER)
            page = context.new_page()
            base = f"http://127.0.0.1:{port}"
            _setup(page, base, app)

            # The manifest is linked from the page and served with the installable fields.
            href = page.locator('link[rel="manifest"]').get_attribute("href")
            assert href == "/manifest.webmanifest"
            response = page.request.get(base + href)
            assert response.status == 200
            assert response.headers["content-type"].split(";")[0] == "application/manifest+json"
            manifest = response.json()
            assert manifest["name"] == APP_NAME
            assert manifest["display"] == "standalone"
            assert manifest["start_url"] == manifest["scope"] == "/"

            # app.js registers the worker; the cache name carries the running app version.
            _wait_for_worker(page)
            assert page.evaluate("window.__cspViolations") == []
            names, urls = _cache_contents(page)
            assert len(names) == 1 and app.version in names[0]

            # Versioned static assets are cached, including the generated icons.
            css = f"/static/css/app.css?v={app.version}"
            icon = manifest["icons"][0]["src"]
            assert css in urls
            assert icon in urls
            assert "/offline" in urls

            # A page, an HTMX fragment and an export are fetched through the worker but never
            # stored. Only static assets and the one offline page may appear in the cache.
            page.goto(f"{base}/categories", wait_until="networkidle")
            fragment = page.evaluate(
                """async () => {
                    const response = await fetch("/dashboard/widgets/month-in-out", {
                        headers: {"HX-Request": "true"},
                    });
                    return {status: response.status, text: await response.text()};
                }"""
            )
            assert fragment["status"] == 200
            assert "data-summary-kpis" in fragment["text"]
            export_status = page.evaluate(
                """async () => {
                    const token = document.querySelector('meta[name="csrf-token"]').content;
                    const response = await fetch("/workspace/data/export", {
                        method: "POST",
                        headers: {"X-CSRF-Token": token},
                        body: new URLSearchParams(),
                    });
                    return response.status;
                }"""
            )
            assert export_status == 200
            _, urls = _cache_contents(page)
            assert urls
            assert all(url.startswith("/static/") or url == "/offline" for url in urls), urls
            for forbidden in (
                "/",
                "/categories",
                "/dashboard/widgets/month-in-out",
                "/workspace/data/export",
            ):
                assert forbidden not in urls

            # Offline navigation shows the translated page with no workspace data and no CSP
            # violation; the cached CSS and icons make it render as the app.
            context.set_offline(True)
            try:
                page.goto(f"{base}/categories", wait_until="domcontentloaded")
                expect(page.get_by_role("heading", name="You are offline")).to_be_visible()
                expect(
                    page.get_by_text(
                        "Check your connection, then try again. "
                        "This page contains no workspace data."
                    )
                ).to_be_visible()
                offline_shell = "[data-sidebar], [data-widget], [data-app-version]"
                assert page.locator(offline_shell).count() == 0
                expect(page.get_by_role("link", name="Try again")).to_be_visible()
                expect(page.get_by_role("link", name="Go to Home")).to_be_visible()
                assert page.evaluate("window.__cspViolations") == []
            finally:
                context.set_offline(False)

            # "Try again" reloads the URL the worker substituted, so it reaches the real page
            # once the connection is back rather than the cached offline notice.
            page.get_by_role("link", name="Try again").click()
            page.wait_for_url(f"{base}/categories")
            expect(page.get_by_role("heading", name="Categories", exact=True)).to_be_visible()

            # The version footer and normal navigation work again after the release.
            page.goto(f"{base}/", wait_until="networkidle")
            expect(page.locator("[data-sidebar-footer] [data-app-version]")).to_have_text(
                f"v{app.version}"
            )
            assert page.evaluate("window.__cspViolations") == []
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
