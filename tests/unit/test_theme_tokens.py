"""Colour tokens: WCAG AA contrast in both themes, and templates use only tokens."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
INPUT_CSS = ROOT / "src/assetboard/static/css/input.css"
APP_CSS = ROOT / "src/assetboard/static/css/app.css"
TEMPLATES = ROOT / "src/assetboard/web/templates"

TEXT_AA = 4.5
NON_TEXT = 3.0  # WCAG 1.4.11: borders that identify a form control

# (foreground, background) pairs that the templates actually render as text.
TEXT_PAIRS = [
    *[("ink", bg) for bg in ("canvas", "surface", "subtle", "field")],
    *[("muted", bg) for bg in ("canvas", "surface", "subtle", "field", "primary-soft")],
    *[("primary-ink", bg) for bg in ("canvas", "surface", "subtle", "primary-soft")],
    ("on-primary", "primary"),
    ("on-primary", "primary-hover"),
    ("on-accent", "accent"),
    *[("success", bg) for bg in ("canvas", "surface", "subtle")],
    *[("danger", bg) for bg in ("canvas", "surface", "subtle", "danger-soft")],
    ("success-strong", "success-soft"),
    ("danger-strong", "danger-soft"),
    ("warning-strong", "warning-soft"),
    ("warning-strong", "warning-line"),
    ("on-inverse", "inverse"),
    ("inverse-link", "inverse"),
]
NON_TEXT_PAIRS = [
    ("line-strong", "field"),
    ("line-strong", "surface"),
    ("primary-ink", "canvas"),  # focus outline
    ("primary-ink", "surface"),
]


def _theme(selector: str) -> dict[str, tuple[int, int, int]]:
    css = INPUT_CSS.read_text(encoding="utf-8")
    block = re.search(re.escape(selector) + r"\s*\{(.*?)\}", css, re.S)
    assert block is not None, selector
    return {
        name: (int(r), int(g), int(b))
        for name, r, g, b in re.findall(
            r"--color-([a-z-]+):\s*(\d+)\s+(\d+)\s+(\d+);", block.group(1)
        )
    }


def _luminance(rgb: tuple[int, int, int]) -> float:
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


THEMES = {"light": ":root", "dark": ":root.dark"}


def test_contrast_formula_matches_known_values() -> None:
    assert contrast((0, 0, 0), (255, 255, 255)) == pytest.approx(21.0)
    # White on #03a9f4 is the shade the owner suggested; it fails AA, so buttons use #0277bd.
    assert contrast((255, 255, 255), (3, 169, 244)) == pytest.approx(2.63, abs=0.01)


def test_both_themes_define_the_same_tokens() -> None:
    light, dark = _theme(":root"), _theme(":root.dark")
    assert light.keys() == dark.keys()
    assert len(light) >= 25


@pytest.mark.parametrize("theme", sorted(THEMES))
@pytest.mark.parametrize(("fg", "bg"), TEXT_PAIRS)
def test_text_pairs_meet_wcag_aa(theme: str, fg: str, bg: str) -> None:
    tokens = _theme(THEMES[theme])
    ratio = contrast(tokens[fg], tokens[bg])
    assert ratio >= TEXT_AA, f"{theme}: {fg} on {bg} is {ratio:.2f}"


@pytest.mark.parametrize("theme", sorted(THEMES))
@pytest.mark.parametrize(("fg", "bg"), NON_TEXT_PAIRS)
def test_control_borders_and_focus_meet_non_text_contrast(theme: str, fg: str, bg: str) -> None:
    tokens = _theme(THEMES[theme])
    ratio = contrast(tokens[fg], tokens[bg])
    assert ratio >= NON_TEXT, f"{theme}: {fg} on {bg} is {ratio:.2f}"


def test_palette_matches_the_approved_values() -> None:
    light, dark = _theme(":root"), _theme(":root.dark")
    assert light["canvas"] == (0xFA, 0xFA, 0xFA) and dark["canvas"] == (0x11, 0x11, 0x11)
    assert light["surface"] == (0xFF, 0xFF, 0xFF) and dark["surface"] == (0x1C, 0x1C, 0x1C)
    assert light["ink"] == (0x21, 0x21, 0x21) and dark["ink"] == (0xE1, 0xE1, 0xE1)
    assert light["primary"] == dark["primary"] == (0x02, 0x77, 0xBD)
    assert light["accent"] == dark["accent"] == (0xFF, 0x98, 0x00)


def test_built_css_contains_both_themes() -> None:
    css = APP_CSS.read_text(encoding="utf-8")
    assert ":root.dark{" in css and "--color-canvas:17 17 17" in css
    assert "--color-canvas:250 250 250" in css


LEGACY = re.compile(
    r"(?<![\w-])(?:[a-z-]+:)*(?:bg|text|border|divide|ring|outline|from|to)-"
    r"(?:slate|gray|zinc|neutral|stone|emerald|green|rose|red|amber|yellow|sky|blue|white|"
    r"accent-dark)\b"
)
DARK_COLOUR = re.compile(r"(?<![\w-])dark:(?:[a-z-]+:)*(?:bg|text|border|divide|ring|outline)-")


@pytest.mark.parametrize("path", sorted(TEMPLATES.rglob("*.html")), ids=lambda p: p.name)
def test_templates_use_only_colour_tokens(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert not LEGACY.findall(text), f"{path.name}: use a theme token"
    assert not DARK_COLOUR.findall(text), f"{path.name}: tokens switch with the theme"
    # Orange is a background colour only; as text it fails AA on light backgrounds.
    assert not re.search(r"(?<![\w-])(?:[a-z-]+:)*text-accent\b", text), path.name


def test_every_select_uses_the_shared_select_class() -> None:
    for path in TEMPLATES.rglob("*.html"):
        for tag in re.findall(r"<select\b[^>]*>", path.read_text(encoding="utf-8"), re.S):
            classes = re.search(r'class="([^"]*)"', tag)
            assert classes is not None and "form-select" in classes.group(1).split(), path
            # Horizontal padding on the select itself would cover the chevron's space.
            assert not re.search(r"(?<![\w-])(px|pr)-", classes.group(1)), path
