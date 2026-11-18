#!/usr/bin/env python3
"""Generate the PWA icons from the SVG favicon (F9.10).

The plain icons keep the favicon's transparent rounded corners. The maskable variants and the
Apple touch icon fill the whole square with the brand colour and keep the mark inside the
maskable safe zone, because phone launchers crop those images to their own shape.

Playwright is already a test dependency; no runtime dependency is added. Chromium must be
installed for Python Playwright once:

    uv run playwright install chromium

Regenerate the committed outputs with:

    make icons
"""

from __future__ import annotations

import base64
import struct
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
FAVICON = ROOT / "src" / "assetboard" / "static" / "img" / "favicon.svg"
OUTPUT_DIRECTORY = ROOT / "src" / "assetboard" / "static" / "img"
BRAND_COLOR = "#0277bd"
MASKABLE_SCALE = 80
# file name -> (pixel size, fill the square with the brand colour)
SIZES = {
    "icon-192.png": (192, False),
    "icon-512.png": (512, False),
    "icon-192-maskable.png": (192, True),
    "icon-512-maskable.png": (512, True),
    "apple-touch-icon.png": (180, True),
}


def png_dimensions(path: Path) -> tuple[int, int]:
    """Width and height from the PNG IHDR chunk, without an image library."""
    header = path.read_bytes()[:24]
    if header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError(f"{path} is not a PNG file")
    return struct.unpack(">II", header[16:24])


def render(page: Page, size: int, filled: bool, output: Path) -> None:
    page.set_viewport_size({"width": size, "height": size})
    encoded = base64.b64encode(FAVICON.read_bytes()).decode("ascii")
    page.set_content(
        f"""
        <!doctype html>
        <html>
          <head>
            <style>
              html, body {{ margin: 0; width: 100%; height: 100%; overflow: hidden; }}
              body {{
                background: {BRAND_COLOR if filled else "transparent"};
                display: flex;
                align-items: center;
                justify-content: center;
              }}
              img {{
                display: block;
                width: {MASKABLE_SCALE if filled else 100}%;
                height: auto;
              }}
            </style>
          </head>
          <body><img alt="" src="data:image/svg+xml;base64,{encoded}"></body>
        </html>
        """
    )
    page.screenshot(path=str(output), omit_background=not filled, type="png")


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            context = browser.new_context(device_scale_factor=1)
            page = context.new_page()
            for file_name, (size, filled) in SIZES.items():
                output = OUTPUT_DIRECTORY / file_name
                render(page, size, filled=filled, output=output)
                width, height = png_dimensions(output)
                if (width, height) != (size, size):
                    raise SystemExit(f"{output}: expected {size}x{size}, got {width}x{height}")
                print(f"wrote {output.relative_to(ROOT)} ({width}x{height})")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
