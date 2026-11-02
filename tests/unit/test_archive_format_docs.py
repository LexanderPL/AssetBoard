"""F4.5: docs/archive-format.md stays in sync with ``domain.archive``.

The documented examples must read cleanly with the real reader, and every column and key the
real writer emits must be documented. The user guide must carry the privacy note and the
conversion prompt.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from assetboard.domain.archive import (
    CSV_COLUMNS,
    FILE_ORDER,
    manifest_bytes,
    parse_archive,
    zip_bytes,
)
from assetboard.domain.import_csv import TEMPLATE_FILES, parse_decimal
from assetboard.domain.import_upload import parse_upload
from assetboard.services.archive import export_workspace
from tests.tenant.workspace_db import WorkspaceDb, workspace_db

DOCS = Path(__file__).resolve().parents[2] / "docs"
FORMAT_DOC = DOCS / "archive-format.md"
GUIDE_DOC = DOCS / "import-export.md"
EXPORTED_AT = datetime(2026, 10, 18, 14, 30, tzinfo=UTC)
_BLOCK = re.compile(r"```(csv|json|text)\n(.*?)```", re.DOTALL)
_SOURCES = {
    "categories.csv": "categories_csv",
    "budget_lines.csv": "budget_lines_csv",
    "transactions.csv": "transactions_csv",
}


def _sections(text: str) -> dict[str, str]:
    """``## `file` `` sections of the reference, keyed by file name."""
    sections: dict[str, str] = {}
    parts = re.split(r"^## ", text, flags=re.MULTILINE)
    for part in parts[1:]:
        heading, _, body = part.partition("\n")
        match = re.match(r"`([a-z_]+\.(?:csv|json))`", heading)
        if match:
            sections[match.group(1)] = body
    return sections


def _blocks(body: str, language: str) -> list[str]:
    return [content for kind, content in _BLOCK.findall(body) if kind == language]


def _crlf(text: str) -> bytes:
    return ("\r\n".join(text.rstrip("\n").split("\n")) + "\r\n").encode()


def _documented(body: str) -> set[str]:
    """Column or key names listed in the section's table (``| `name` | ...``)."""
    return set(re.findall(r"^\| `([a-z_]+)` \|", body, flags=re.MULTILINE))


@pytest.fixture(scope="module")
def reference() -> dict[str, str]:
    return _sections(FORMAT_DOC.read_text(encoding="utf-8"))


@pytest.fixture
def db(tmp_path: Path) -> Iterator[WorkspaceDb]:
    for prepared in workspace_db(tmp_path):
        prepared.add_member("own", "owner")
        yield prepared


def test_every_archive_file_has_a_section(reference: dict[str, str]) -> None:
    assert set(FILE_ORDER) | {"manifest.json"} <= set(reference)


def test_documented_archive_examples_read_cleanly(reference: dict[str, str]) -> None:
    files: dict[str, bytes] = {
        "workspace.json": _blocks(reference["workspace.json"], "json")[0].encode()
    }
    for name, columns in CSV_COLUMNS.items():
        archive_examples = [
            block
            for block in _blocks(reference[name], "csv")
            if block.split("\n", 1)[0] == ",".join(columns)
        ]
        assert archive_examples, f"{name} has no example with the archive's exact header"
        files[name] = _crlf(archive_examples[0])
    files["manifest.json"] = manifest_bytes(
        files,
        app_name="AssetBoard",
        app_version="0.0.0",
        exported_at=EXPORTED_AT,
        base_currency="EUR",
    )

    parsed, errors = parse_archive(zip_bytes(files, exported_at=EXPORTED_AT))

    assert errors == ()
    assert parsed is not None
    assert parsed.warnings == ()
    assert len(parsed.rows.categories) == 4
    assert len(parsed.rows.transactions) == 3
    assert len(parsed.rows.budget_lines) == 2
    assert len(parsed.rows.months) == 2


def test_documented_hand_made_examples_read_as_lone_csv(reference: dict[str, str]) -> None:
    checked = 0
    for name, source in _SOURCES.items():
        canonical = ",".join(CSV_COLUMNS[name])
        for block in _blocks(reference[name], "csv"):
            if block.split("\n", 1)[0] == canonical:
                continue
            parsed, errors = parse_upload(block.encode(), delimiter="detect")
            assert errors == (), (name, errors)
            assert parsed is not None
            assert parsed.source == source
            assert parsed.row_errors == ()
            assert parsed.warnings == ()
            if parsed.delimiter == ";":
                amount = parsed.csv_header.index("amount")
                for row in parsed.csv_rows:
                    parse_decimal(row[amount], decimal="comma")
            checked += 1
    assert checked == 3  # minimal transactions, its semicolon variant, minimal budget lines


def test_template_downloads_match_the_documented_examples(reference: dict[str, str]) -> None:
    for name, template in TEMPLATE_FILES.items():
        documented = [_crlf(block) for block in _blocks(reference[name], "csv")]
        assert template in documented, name


def test_every_written_column_and_key_is_documented(
    db: WorkspaceDb, reference: dict[str, str]
) -> None:
    content = export_workspace(db.factory, db.ctx("own", "owner")).content
    parsed, errors = parse_archive(content)
    assert errors == () and parsed is not None

    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for name in CSV_COLUMNS:
            header = archive.read(name).decode().split("\r\n", 1)[0].split(",")
            missing = set(header) - _documented(reference[name])
            assert not missing, f"{name}: {sorted(missing)} are written but not documented"
    missing_keys = set(parsed.rows.workspace) - _documented(reference["workspace.json"])
    assert not missing_keys, f"workspace.json: {sorted(missing_keys)} not documented"


def test_user_guide_has_the_privacy_note_and_the_prompt() -> None:
    guide = GUIDE_DOC.read_text(encoding="utf-8")
    section = guide.split("## Converting a spreadsheet", 1)[1]
    note = " ".join(section.split("```text", 1)[0].replace(">", " ").split())
    for phrase in (
        "never sends your data anywhere",
        "your own choice",
        "that service's terms",
        "local model",
        "dry-run totals",
    ):
        assert phrase in note, phrase
    prompt = _blocks(section, "text")[0]
    assert "Header, exactly: date,direction,amount,category,description" in prompt
    assert "Header, exactly: month,kind,category,planned" in prompt
    assert '"Check table"' in prompt
