from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZIP_BZIP2, ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from assetboard.db.base import Base, WorkspaceOwned
from assetboard.domain.archive import (
    ArchiveError,
    ArchiveRows,
    BudgetLineRow,
    CategoryRow,
    MonthRow,
    TransactionRow,
    parse_archive,
    write_archive,
)
from assetboard.services.archive import ARCHIVE_EXCLUDED_MODELS, ARCHIVE_INCLUDED_MODELS

FIXTURES = Path(__file__).parents[1] / "fixtures"
GOLDEN = FIXTURES / "workspace-archive-v1.1.zip"
GOLDEN_V1_0 = FIXTURES / "workspace-archive-v1.0.zip"
EXPORTED_AT = datetime(2026, 10, 18, 14, 30, tzinfo=UTC)


def _rows() -> ArchiveRows:
    category_id = "2b0e8c1e-5f7a-4c6e-9a51-3f1d2c7b9e01"
    transaction_id = "5c7d8e9f-0a1b-4c2d-9e3f-4a5b6c7d8e02"
    return ArchiveRows(
        workspace={
            "name": "Home",
            "base_currency": "EUR",
            "country_code": "PL",
            "timezone": "Europe/Warsaw",
            "locale": "pl",
            "auto_copy_plan": False,
            "taxes_enabled": True,
            "upcoming_days": 14,
        },
        categories=(CategoryRow(category_id, "expense", "Food", "regular", "#4f9d69", 0, None),),
        budget_lines=(BudgetLineRow("2026-09", "expense", "Food", "550.00", "EUR", category_id),),
        transactions=(
            TransactionRow(
                "2026-09-02",
                "expense",
                "4.20",
                "EUR",
                "Food",
                "Bakery, rolls",
                category_id,
                transaction_id,
                "2026-09-03T07:12:44Z",
            ),
        ),
        months=(MonthRow("2026-09", None, "2026-09-01T06:30:12Z"),),
    )


def _archive() -> bytes:
    return write_archive(
        _rows(),
        app_name="AssetBoard",
        app_version="0.4.0",
        exported_at=EXPORTED_AT,
        base_currency="EUR",
    )


def test_archive_matches_golden_and_is_deterministic() -> None:
    assert _archive() == GOLDEN.read_bytes()
    assert _archive() == _archive()


def test_released_v1_0_archive_still_reads_without_the_1_1_keys() -> None:
    parsed, errors = parse_archive(GOLDEN_V1_0.read_bytes())
    assert errors == ()
    assert parsed is not None and parsed.warnings == ()
    assert parsed.manifest["format_version"] == "1.0"
    assert "taxes_enabled" not in parsed.rows.workspace
    assert "upcoming_days" not in parsed.rows.workspace


def test_v1_1_workspace_carries_the_taxes_switch_and_upcoming_days() -> None:
    parsed, errors = parse_archive(GOLDEN.read_bytes())
    assert errors == ()
    assert parsed is not None and parsed.warnings == ()
    assert parsed.manifest["format_version"] == "1.1"
    assert parsed.rows.workspace["taxes_enabled"] is True
    assert parsed.rows.workspace["upcoming_days"] == 14


@pytest.mark.parametrize(
    ("key", "value", "valid"),
    [
        ("taxes_enabled", False, True),
        ("taxes_enabled", "yes", False),
        ("taxes_enabled", 1, False),
        ("upcoming_days", None, True),
        ("upcoming_days", 1, True),
        ("upcoming_days", 365, True),
        ("upcoming_days", 0, False),
        ("upcoming_days", 366, False),
        ("upcoming_days", True, False),
        ("upcoming_days", "14", False),
    ],
)
def test_reader_validates_the_1_1_workspace_keys(key: str, value: object, valid: bool) -> None:
    import json

    files = _files()
    workspace = json.loads(files["workspace.json"])
    workspace[key] = value
    files["workspace.json"] = json.dumps(workspace).encode()
    parsed, errors = parse_archive(_rewrite(files))
    assert (parsed is not None) is valid
    if not valid:
        assert errors == (ArchiveError("workspace.json", "invalid workspace settings"),)


def test_reader_checks_checksums() -> None:
    parsed, errors = parse_archive(GOLDEN.read_bytes())
    assert errors == ()
    assert parsed is not None
    assert parsed.manifest["base_currency"] == "EUR"

    with ZipFile(GOLDEN) as source:
        files = {name: source.read(name) for name in source.namelist()}
    files["categories.csv"] = files["categories.csv"].replace(b"Food", b"Meal")
    corrupted = io.BytesIO()
    with ZipFile(corrupted, "w", ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    parsed, errors = parse_archive(corrupted.getvalue())
    assert parsed is None
    assert any(error.message == "checksum mismatch" for error in errors)


def test_every_workspace_owned_model_is_archived_or_excluded() -> None:
    owned = {
        mapper.class_
        for mapper in Base.registry.mappers
        if issubclass(mapper.class_, WorkspaceOwned)
    }
    owned_names = {model.__name__ for model in owned}
    assert ARCHIVE_INCLUDED_MODELS | set(ARCHIVE_EXCLUDED_MODELS) == owned_names
    assert not (ARCHIVE_INCLUDED_MODELS & set(ARCHIVE_EXCLUDED_MODELS))


def _rewrite(members: dict[str, bytes], **manifest_changes: object) -> bytes:
    """Re-sign intentional edits so tests exercise validation beyond the checksum."""
    import json

    from assetboard.domain.archive import manifest_bytes

    manifest = json.loads(
        manifest_bytes(
            members,
            app_name="AssetBoard",
            app_version="0.4.0",
            exported_at=EXPORTED_AT,
            base_currency="EUR",
        )
    )
    manifest.update(manifest_changes)
    members = {"manifest.json": json.dumps(manifest).encode(), **members}
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return output.getvalue()


def _files() -> dict[str, bytes]:
    from assetboard.domain.archive import build_files

    return build_files(_rows())


def test_reader_checks_counts_currency_and_versions() -> None:
    import json

    from assetboard.domain.archive import manifest_bytes

    files = _files()
    manifest = json.loads(
        manifest_bytes(
            files,
            app_name="AssetBoard",
            app_version="0.4.0",
            exported_at=EXPORTED_AT,
            base_currency="EUR",
        )
    )
    manifest["files"][1]["rows"] += 1
    parsed, errors = parse_archive(_rewrite(files, files=manifest["files"]))
    assert parsed is None and any(e.message == "row count mismatch" for e in errors)
    for changes, message in [
        ({"base_currency": "PLN"}, "base currency differs from manifest"),
        ({"format_version": "2.0"}, "unsupported archive version"),
    ]:
        parsed, errors = parse_archive(_rewrite(files, **changes))
        assert parsed is None and any(e.message == message for e in errors)


def test_reader_accepts_reordered_columns_bom_defaults_and_future_minor() -> None:
    files = _files()
    files["categories.csv"] = (
        "\ufeffname,id,kind,future\nFood,2b0e8c1e-5f7a-4c6e-9a51-3f1d2c7b9e01,expense,x\n"
    ).encode()
    parsed, errors = parse_archive(_rewrite(files, format_version="1.9", future=True))
    assert not errors and parsed is not None
    assert parsed.rows.categories[0].treatment == "regular"
    assert parsed.rows.categories[0].sort_order == 0
    assert sorted(w.message for w in parsed.warnings) == [
        "newer minor version: 1.9",
        "unknown column: future",
        "unknown key: future",
    ]


def test_reader_validates_fields_and_references() -> None:
    cases = [
        ("transactions.csv", b"4.20", b"4.201"),
        ("transactions.csv", b"4.20", b"-4.20"),
        ("transactions.csv", b"4.20", b"0.00"),
        ("transactions.csv", b"2026-09-02", b"2026-02-30"),
        ("transactions.csv", b"Food", b"Missing"),
        ("transactions.csv", b"expense", b"income"),
        ("transactions.csv", b"EUR", b"PLN"),
        ("transactions.csv", b"2026-09-03T07:12:44Z", b"2026-09-03T07:12:44"),
        ("categories.csv", b"regular", b"invalid"),
        ("categories.csv", b"#4f9d69", b"red"),
        ("categories.csv", b",0,", b",-1,"),
        ("categories.csv", b"2b0e8c1e-5f7a-4c6e-9a51-3f1d2c7b9e01", b"invalid"),
    ]
    for file, old, new in cases:
        files = _files()
        files[file] = files[file].replace(old, new)
        parsed, errors = parse_archive(_rewrite(files))
        assert parsed is None and errors, (file, new)


def test_reader_normalizes_timestamps_and_accepts_trailing_decimal_zeros() -> None:
    files = _files()
    files["transactions.csv"] = (
        files["transactions.csv"]
        .replace(b"4.20", b"4.2000")
        .replace(b"2026-09-03T07:12:44Z", b"2026-09-03T09:12:44+02:00")
    )
    parsed, errors = parse_archive(_rewrite(files))
    assert not errors and parsed is not None
    assert parsed.rows.transactions[0].deleted_at == "2026-09-03T07:12:44Z"


def test_reader_refuses_unsafe_and_invalid_archives() -> None:
    for data in (b"", b"not a zip"):
        assert parse_archive(data)[1]
    files = _files()
    files["../secret"] = b"x"
    assert parse_archive(_rewrite(files))[1] == (ArchiveError("archive", "unsafe member"),)
    files = _files()
    files["months.csv"] += files["months.csv"].splitlines(keepends=True)[1]
    assert parse_archive(_rewrite(files))[1] == (ArchiveError("months.csv", "duplicate month"),)


def test_filtered_csv_columns_match_archive() -> None:
    from assetboard.domain.archive import CSV_COLUMNS
    from assetboard.domain.ledger_csv import TRANSACTION_EXPORT_COLUMNS

    assert CSV_COLUMNS["transactions.csv"] == (*TRANSACTION_EXPORT_COLUMNS, "deleted_at")


def test_zip_metadata_and_lossless_text() -> None:
    from dataclasses import replace

    rows = _rows()
    text = '=SUM(1,2)\r\nZażółć "gęślą"'
    rows = replace(rows, transactions=(replace(rows.transactions[0], description=text),))
    data = write_archive(
        rows,
        app_name="AssetBoard",
        app_version="0.4.0",
        exported_at=EXPORTED_AT,
        base_currency="EUR",
    )
    parsed, errors = parse_archive(data)
    assert not errors and parsed is not None
    assert parsed.rows.transactions[0].description == text
    with ZipFile(io.BytesIO(data)) as archive:
        for member in archive.infolist():
            assert member.date_time == (2026, 10, 18, 14, 30, 0)
            assert member.compress_type == ZIP_DEFLATED
            assert member.extra == b""


def test_reader_warns_about_unknown_files() -> None:
    import hashlib
    import json

    from assetboard.domain.archive import manifest_bytes

    members = _files()
    manifest = json.loads(
        manifest_bytes(
            members,
            app_name="AssetBoard",
            app_version="0.4.0",
            exported_at=EXPORTED_AT,
            base_currency="EUR",
        )
    )
    extra = b'{"new":true}'
    members["future.json"] = extra
    manifest["files"].append(
        {
            "path": "future.json",
            "bytes": len(extra),
            "rows": None,
            "sha256": hashlib.sha256(extra).hexdigest(),
        }
    )
    # Build directly: the v1 writer intentionally only writes the current files.
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for name, data in members.items():
            archive.writestr(name, data)
    parsed, errors = parse_archive(output.getvalue())
    assert not errors and parsed is not None
    assert parsed.warnings[0].file == "future.json"


@given(st.text(alphabet='abcąćęłńóśźż0123=+-@,;"\r\n ', max_size=200))
def test_csv_text_round_trip(description: str) -> None:
    from dataclasses import replace

    rows = _rows()
    rows = replace(rows, transactions=(replace(rows.transactions[0], description=description),))
    encoded = write_archive(
        rows,
        app_name="AssetBoard",
        app_version="0.4.0",
        exported_at=EXPORTED_AT,
        base_currency="EUR",
    )
    parsed, errors = parse_archive(encoded)
    assert not errors and parsed is not None
    assert parsed.rows.transactions[0].description == description


def _zip(members: list[tuple[ZipInfo | str, bytes]], *, compression: int = ZIP_DEFLATED) -> bytes:
    output = io.BytesIO()
    with ZipFile(output, "w", compression) as archive:
        for name, data in members:
            archive.writestr(name, data)
    return output.getvalue()


def _signed_members() -> list[tuple[ZipInfo | str, bytes]]:
    with ZipFile(io.BytesIO(_rewrite(_files()))) as archive:
        return [(name, archive.read(name)) for name in archive.namelist()]


def _special(name: str, *, mode: int = 0) -> ZipInfo:
    info = ZipInfo(name)
    info.external_attr = mode << 16
    return info


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ((_special("link", mode=0o120777), b"/etc/passwd"), "unsafe member"),
        ((_special("dir", mode=0o040755), b""), "unsafe member"),
        (("C:evil", b"x"), "unsafe member"),
        (("sub\\evil", b"x"), "unsafe member"),
        (("zeros", b"\0" * (2 * 1024 * 1024)), "uncompressed size or ratio limit"),
    ],
    ids=["symlink", "directory", "drive", "backslash", "ratio"],
)
def test_reader_refuses_each_unsafe_member(
    extra: tuple[ZipInfo | str, bytes], message: str
) -> None:
    assert parse_archive(_zip([*_signed_members(), extra]))[1] == (
        ArchiveError("archive", message),
    )


def test_reader_refuses_an_encrypted_member() -> None:
    data = bytearray(_zip([*_signed_members(), ("secret", b"x")]))
    # zipfile cannot write encrypted members: set bit 0 of the general-purpose flags in the
    # central directory entry of the last member (signature PK\x01\x02, flags at offset 8).
    entry = data.rindex(b"PK\x01\x02")
    data[entry + 8] |= 1
    assert parse_archive(bytes(data))[1] == (ArchiveError("archive", "unsafe member"),)


def test_reader_refuses_other_methods_duplicates_and_too_many_members() -> None:
    bz = _zip([*_signed_members(), ("notes.txt", b"x")], compression=ZIP_BZIP2)
    assert parse_archive(bz)[1] == (ArchiveError("archive", "unsafe member"),)
    members = _signed_members()
    with pytest.warns(UserWarning, match="Duplicate name"):
        duplicated = _zip([*members, members[-1]])
    assert parse_archive(duplicated)[1] == (ArchiveError("archive", "duplicate member names"),)
    many = _zip([*members, *((f"extra{n}.txt", b"x") for n in range(60))])
    assert parse_archive(many)[1] == (ArchiveError("archive", "too many members"),)


@pytest.mark.parametrize("limit", ["MAX_MEMBER_BYTES", "MAX_TOTAL_BYTES"])
def test_reader_stops_at_the_size_limits(monkeypatch: pytest.MonkeyPatch, limit: str) -> None:
    import assetboard.domain.archive as archive

    monkeypatch.setattr(archive, limit, 100)
    data = _zip(_signed_members(), compression=ZIP_STORED)
    assert parse_archive(data)[1] == (ArchiveError("archive", "uncompressed size or ratio limit"),)


def test_reader_warns_about_unlisted_files() -> None:
    parsed, errors = parse_archive(_zip([*_signed_members(), ("README.txt", b"notes")]))
    assert not errors and parsed is not None
    assert parsed.warnings == (ArchiveError("README.txt", "unknown file ignored"),)


def test_reader_refuses_listed_files_that_are_missing() -> None:
    import json

    members = dict(_signed_members())
    manifest = json.loads(members["manifest.json"])
    manifest["files"].append({"path": "gone.csv", "sha256": "0", "rows": 0, "bytes": 0})
    members["manifest.json"] = json.dumps(manifest).encode()
    errors = parse_archive(_zip(list(members.items())))[1]
    assert errors == (ArchiveError("manifest.json", "invalid file list"),)


@pytest.mark.parametrize(
    ("file", "old", "new"),
    [
        ("categories.csv", b",0,", b"," + b"9" * 5000 + b","),
        ("categories.csv", b",0,", b",1234567890,"),
        ("transactions.csv", b"2026-09-03T07:12:44Z", b"0001-01-01T00:00:00+01:00"),
        ("transactions.csv", b"2026-09-03T07:12:44Z", b"9999-12-31T23:59:59-01:00"),
        ("workspace.json", b'"auto_copy_plan"', b'"x":' + b"9" * 5000 + b',"auto_copy_plan"'),
        ("workspace.json", b"{", b'{"x":' + b"[" * 100_000 + b"]" * 100_000 + b","),
    ],
    ids=["huge-sort", "long-sort", "early-ts", "late-ts", "huge-int", "deep-json"],
)
def test_reader_reports_hostile_values_as_errors(file: str, old: bytes, new: bytes) -> None:
    files = _files()
    assert old in files[file]
    files[file] = files[file].replace(old, new, 1)
    parsed, errors = parse_archive(_rewrite(files))
    assert parsed is None and errors


@pytest.mark.parametrize(
    "manifest",
    [
        b"[" * 100_000 + b"]" * 100_000,
        b'{"format":' + b"1" * 5000 + b"}",
        b'{"format":"workspace-archive","format_version":"1.0",'
        b'"exported_at":"9999-12-31T23:59:59-01:00"}',
    ],
    ids=["deep", "huge-int", "late-ts"],
)
def test_reader_reports_a_hostile_manifest_as_an_error(manifest: bytes) -> None:
    members = dict(_signed_members())
    members["manifest.json"] = manifest
    parsed, errors = parse_archive(_zip(list(members.items())))
    assert parsed is None and errors and errors[0].file == "manifest.json"


def test_reader_caps_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    import assetboard.domain.archive as archive

    monkeypatch.setattr(archive, "MAX_ERRORS", 5)
    files = _files()
    files["months.csv"] = b"month\r\n" + b"bad\r\n" * 50
    parsed, errors = parse_archive(_rewrite(files))
    assert parsed is None
    assert len(errors) == 6
    assert errors[-1] == ArchiveError("archive", "too many errors; later rows were not checked")


def test_reader_caps_data_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    import assetboard.domain.archive as archive

    monkeypatch.setattr(archive, "MAX_DATA_ROWS", 4)
    assert parse_archive(_archive())[1] == ()
    monkeypatch.setattr(archive, "MAX_DATA_ROWS", 3)
    assert parse_archive(_archive())[1] == (ArchiveError("archive", "too many data rows"),)
