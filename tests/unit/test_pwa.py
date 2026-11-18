"""F9.10 installable web app: manifest, icons, service worker and offline page."""

from __future__ import annotations

import re
import struct
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from assetboard.branding import APP_NAME
from assetboard.web.pwa import (
    APPLE_TOUCH_ICON,
    APPLE_TOUCH_ICON_SIZE,
    DARK_BACKGROUND_COLOR,
    DARK_THEME_COLOR,
    LIGHT_BACKGROUND_COLOR,
    LIGHT_THEME_COLOR,
    STATIC_DIRECTORY,
)

if TYPE_CHECKING:
    from conftest import OwnerApp, WebHarness

ROOT = Path(__file__).parents[2]
INPUT_CSS = ROOT / "src" / "assetboard" / "static" / "css" / "input.css"
IMAGE_DIRECTORY = STATIC_DIRECTORY / "img"


def png_dimensions(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    assert header[:8] == b"\x89PNG\r\n\x1a\n" and header[12:16] == b"IHDR", path
    return struct.unpack(">II", header[16:24])


def theme_tokens(selector: str) -> dict[str, tuple[int, int, int]]:
    css = INPUT_CSS.read_text(encoding="utf-8")
    block = re.search(re.escape(selector) + r"\s*\{(.*?)\}", css, re.S)
    assert block is not None, selector
    return {
        name: (int(r), int(g), int(b))
        for name, r, g, b in re.findall(
            r"--color-([a-z0-9-]+):\s*(\d+)\s+(\d+)\s+(\d+);", block.group(1)
        )
    }


def hex_colour(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def test_manifest_is_served_with_the_installable_fields(web: WebHarness) -> None:
    app = web.app()
    with TestClient(app) as client:
        response = client.get("/manifest.webmanifest", headers={"accept-language": "pl-PL"})

    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0] == "application/manifest+json"
    manifest = response.json()
    assert manifest["id"] == manifest["start_url"] == manifest["scope"] == "/"
    assert manifest["name"] == manifest["short_name"] == APP_NAME
    assert manifest["lang"] == "pl"
    assert manifest["display"] == "standalone"
    assert manifest["theme_color"] == LIGHT_THEME_COLOR
    assert manifest["background_color"] == LIGHT_BACKGROUND_COLOR
    assert manifest["color_scheme_dark"] == {
        "theme_color": DARK_THEME_COLOR,
        "background_color": DARK_BACKGROUND_COLOR,
    }
    assert response.headers["cache-control"] == "no-store"


def test_manifest_colours_are_the_design_tokens() -> None:
    light = theme_tokens(":root")
    dark = theme_tokens(":root.dark")

    assert hex_colour(light["primary"]) == LIGHT_THEME_COLOR
    assert hex_colour(dark["primary"]) == DARK_THEME_COLOR
    assert hex_colour(light["canvas"]) == LIGHT_BACKGROUND_COLOR
    assert hex_colour(dark["canvas"]) == DARK_BACKGROUND_COLOR


def test_every_manifest_icon_exists_with_its_declared_size(web: WebHarness) -> None:
    app = web.app()
    with TestClient(app) as client:
        manifest = client.get("/manifest.webmanifest").json()

    icons = manifest["icons"]
    assert {icon["purpose"] for icon in icons} == {"any", "maskable"}
    assert {icon["sizes"] for icon in icons} == {"192x192", "512x512"}
    assert len(icons) == 4
    for icon in icons:
        assert icon["type"] == "image/png"
        path = urlsplit(icon["src"]).path
        assert path.startswith("/static/img/"), path
        image = IMAGE_DIRECTORY / Path(path).name
        assert image.is_file(), image
        width, height = png_dimensions(image)
        assert f"{width}x{height}" == icon["sizes"]


def test_apple_touch_icon_exists_and_is_linked(owner: OwnerApp) -> None:
    image = IMAGE_DIRECTORY / APPLE_TOUCH_ICON
    assert png_dimensions(image) == (APPLE_TOUCH_ICON_SIZE, APPLE_TOUCH_ICON_SIZE)

    page = owner.client.get("/").text
    assert re.search(
        rf'<link rel="apple-touch-icon" sizes="180x180" '
        rf'href="/static/img/{APPLE_TOUCH_ICON}\?v=[^"]+"',
        page,
    )
    assert '<link rel="manifest" href="/manifest.webmanifest"' in page


def test_signed_out_pages_link_the_manifest_and_apple_icon(owner: OwnerApp) -> None:
    owner.client.cookies.clear()
    page = owner.client.get("/login")
    assert page.status_code == 200
    matching = (
        '<link rel="manifest" href="/manifest.webmanifest"',
        '<link rel="apple-touch-icon" sizes="180x180"',
        'name="theme-color" media="(prefers-color-scheme: light)"',
        'name="theme-color" media="(prefers-color-scheme: dark)"',
        'name="apple-mobile-web-app-capable" content="yes"',
    )
    for snippet in matching:
        assert snippet in page.text, snippet


def test_manifest_and_links_respect_the_root_path(web: WebHarness) -> None:
    app = web.app()
    with TestClient(app, root_path="/money") as client:
        web.complete_setup(app)
        manifest_response = client.get("/manifest.webmanifest")
        login = client.get("/login")
        worker = client.get("/service-worker.js")
        offline = client.get("/offline")

    assert manifest_response.status_code == 200
    manifest = manifest_response.json()
    assert manifest["id"] == manifest["start_url"] == manifest["scope"] == "/money/"
    assert all(icon["src"].startswith("/money/static/img/") for icon in manifest["icons"])
    assert worker.headers["service-worker-allowed"] == "/money/"
    assert 'href="/money/manifest.webmanifest"' in login.text
    assert 'href="/money/static/img/apple-touch-icon.png?v=' in login.text
    assert 'href="/money/offline"' in offline.text
    assert 'href="/money/"' in offline.text


def test_service_worker_is_root_scoped_and_allowed_by_csp(web: WebHarness) -> None:
    app = web.app()
    with TestClient(app) as client:
        response = client.get("/service-worker.js")

    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0] == "text/javascript"
    assert response.headers["service-worker-allowed"] == "/"
    assert "worker-src 'self'" in response.headers["content-security-policy"]
    assert "assetboard-pwa-" in response.text
    assert "caches.open" in response.text
    assert response.headers["cache-control"] == "no-store"


def test_offline_page_is_translated_and_contains_no_workspace_data(web: WebHarness) -> None:
    app = web.app()
    with TestClient(app) as client:
        web.complete_setup(app)
        response = client.get("/offline", headers={"accept-language": "pl"})
        english = client.get("/offline", headers={"accept-language": "en"})

    assert response.status_code == 200
    assert "Jesteś offline" in response.text
    assert "Sprawdź połączenie i spróbuj ponownie" in response.text
    assert "Spróbuj ponownie" in response.text
    assert "Otwórz stronę główną" in response.text
    # The one cached HTML page: no navigation, no workspace name, no financial data, no forms.
    for forbidden in ("data-sidebar", "Home", "<form", "csrf-token"):
        assert forbidden not in response.text, forbidden
    assert "You are offline" in english.text
    assert "worker-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
