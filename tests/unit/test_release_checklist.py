"""The v1.0 release checklist names the repeatable checks and keeps its links honest (F9.8)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHECKLIST = ROOT / "docs" / "release-checklist.md"
SCRIPTS = ROOT / "scripts"


def text() -> str:
    return CHECKLIST.read_text(encoding="utf-8")


def test_the_checklist_covers_the_release_steps_in_order() -> None:
    document = text()
    headings = re.findall(r"^## (\d+)\. (.+)$", document, re.MULTILINE)
    assert [int(number) for number, _ in headings] == list(range(11))
    lowered = document.lower()
    for required in (
        "fresh install",
        "upgrade",
        "migration",
        "backup",
        "multi-arch",
        "security",
        "sbom",
        "tag and publish",
    ):
        assert required in lowered, required


def test_the_checklist_names_the_repeatable_scripts() -> None:
    document = text()
    for script in ("scripts/release_checks.sh", "scripts/verify_release_tags.py"):
        assert script in document, script
        assert (ROOT / script).is_file(), script


def test_the_checklist_names_every_published_tag() -> None:
    assert "`1.0.0`, `1.0`, `1`, and `latest`" in text()


def test_every_local_markdown_link_exists() -> None:
    for target in re.findall(r"\]\((?!https?://|#)([^)#]+)", text()):
        assert (CHECKLIST.parent / target).resolve().exists(), target
