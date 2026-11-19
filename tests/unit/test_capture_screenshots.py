"""The screenshot capture fails loudly instead of committing an error page (F9.7)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from scripts.capture_screenshots import _capture


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status
        self.ok = status < 400


class _Page:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.shots: list[Path] = []

    def goto(self, url: str, *, wait_until: str) -> _Response:
        assert wait_until == "networkidle"
        return _Response(self.status)

    def locator(self, selector: str) -> _Page:
        assert selector == "main"
        return self

    def wait_for(self, *, state: str) -> None:
        assert state == "visible"

    def wait_for_timeout(self, milliseconds: int) -> None:
        assert milliseconds > 0

    def screenshot(self, *, path: Path, **options: Any) -> None:
        self.shots.append(Path(path))
        Path(path).write_bytes(b"jpeg")
        assert options["type"] == "jpeg"


def test_a_successful_page_is_captured(tmp_path: Path) -> None:
    page = _Page()
    _capture(page, "http://127.0.0.1", tmp_path, month="2026-11")  # type: ignore[arg-type]
    assert len(page.shots) == 10
    assert all(path.is_file() for path in page.shots)


def test_an_error_page_stops_the_capture(tmp_path: Path) -> None:
    page = _Page(status=404)
    with pytest.raises(SystemExit, match="HTTP 404"):
        _capture(page, "http://127.0.0.1", tmp_path, month="2026-11")  # type: ignore[arg-type]
    assert page.shots == []
