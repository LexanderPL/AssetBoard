"""Installable-web-app constants shared by the routes and the templates (F9.10).

The colours are the design tokens from ``static/css/input.css``: ``primary`` for the theme
colour and ``canvas`` for the launch background (light and dark). ``tests/unit/test_pwa.py``
parses the stylesheet and fails if these no longer match the tokens.
"""

from __future__ import annotations

from pathlib import Path

MANIFEST_PATH = "/manifest.webmanifest"
SERVICE_WORKER_PATH = "/service-worker.js"
OFFLINE_PATH = "/offline"

# Theme colour ("primary") is the same token in both themes; the launch background follows the
# page canvas, which differs between light and dark.
LIGHT_THEME_COLOR = "#0277bd"
DARK_THEME_COLOR = "#0277bd"
LIGHT_BACKGROUND_COLOR = "#f1f3f4"
DARK_BACKGROUND_COLOR = "#111111"

APPLE_TOUCH_ICON = "apple-touch-icon.png"
# (file, pixel size, manifest purpose). Both sizes have a separate maskable variant: the plain
# icons keep the favicon's transparent corners, the maskable ones fill the whole canvas.
MANIFEST_ICONS = (
    ("icon-192.png", "192x192", "any"),
    ("icon-512.png", "512x512", "any"),
    ("icon-192-maskable.png", "192x192", "maskable"),
    ("icon-512-maskable.png", "512x512", "maskable"),
)
APPLE_TOUCH_ICON_SIZE = 180

STATIC_DIRECTORY = Path(__file__).resolve().parents[1] / "static"
SERVICE_WORKER_SOURCE = STATIC_DIRECTORY / "js" / "service-worker.js"
