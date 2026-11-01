"""Pure workspace archive format (F4.1): deterministic CSV/ZIP encoding and validation."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import stat
import zipfile
import zlib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import cast
from uuid import UUID

from assetboard.domain.categories import Kind, Treatment
from assetboard.domain.money import minor_unit_for

FORMAT = "workspace-archive"
FORMAT_VERSION = "1.1"
# Bounds of the optional ``upcoming_days`` key (1.1); mirror ``config.MIN/MAX_UPCOMING_DAYS``.
MIN_UPCOMING_DAYS = 1
MAX_UPCOMING_DAYS = 365
CSV_COLUMNS = {
    "categories.csv": ("id", "kind", "name", "treatment", "color", "sort_order", "archived_at"),
    "budget_lines.csv": ("month", "kind", "category", "planned", "currency", "category_id"),
    "transactions.csv": (
        "date",
        "direction",
        "amount",
        "currency",
        "category",
        "description",
        "category_id",
        "id",
        "deleted_at",
    ),
    "months.csv": ("month", "closed_at", "plan_started_at"),
}
FILE_ORDER = ("workspace.json", *CSV_COLUMNS)
REQUIRED_FILES = ("manifest.json", *FILE_ORDER)

# Reader limits for untrusted archives (F4.2 "ZIP safety"); the reader stops at each of them.
MAX_MEMBERS = 64
MAX_MEMBER_BYTES = 100 * 1024 * 1024
MAX_TOTAL_BYTES = 200 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
RATIO_EXEMPT_BYTES = 1024 * 1024
MAX_DATA_ROWS = 1_000_000
MAX_COLUMNS = 64
MAX_ERRORS = 1000


@dataclass(frozen=True, slots=True)
class CategoryRow:
    id: str
    kind: str
    name: str
    treatment: str
    color: str
    sort_order: int
    archived_at: str | None


@dataclass(frozen=True, slots=True)
class BudgetLineRow:
    month: str
    kind: str
    category: str
    planned: str
    currency: str
    category_id: str


@dataclass(frozen=True, slots=True)
class TransactionRow:
    date: str
    direction: str
    amount: str
    currency: str
    category: str
    description: str
    category_id: str
    id: str
    deleted_at: str | None


@dataclass(frozen=True, slots=True)
class MonthRow:
    month: str
    closed_at: str | None
    plan_started_at: str | None


@dataclass(frozen=True, slots=True)
class ArchiveRows:
    workspace: dict[str, object]
    categories: tuple[CategoryRow, ...]
    budget_lines: tuple[BudgetLineRow, ...]
    transactions: tuple[TransactionRow, ...]
    months: tuple[MonthRow, ...]


@dataclass(frozen=True, slots=True)
class ArchiveError:
    file: str
    message: str
    line: int | None = None
    column: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedArchive:
    manifest: dict[str, object]
    files: dict[str, bytes]
    rows: ArchiveRows
    warnings: tuple[ArchiveError, ...] = ()


def csv_bytes(filename: str, rows: tuple[object, ...]) -> bytes:
    """Serialize typed rows using the canonical header and RFC 4180 CRLF endings."""
    columns = CSV_COLUMNS[filename]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output, fieldnames=columns, lineterminator="\r\n", extrasaction="ignore"
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {key: "" if getattr(row, key) is None else getattr(row, key) for key in columns}
        )
    return output.getvalue().encode("utf-8")


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def _csv_data_rows(data: bytes) -> int:
    return max(
        0,
        sum(1 for _ in csv.reader(io.StringIO(data.decode("utf-8-sig"), newline=""), strict=True))
        - 1,
    )


def _string_keyed_object(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, object] = {}
    for key, item in cast(dict[object, object], value).items():
        if not isinstance(key, str):
            return None
        result[key] = item
    return result


def build_files(rows: ArchiveRows) -> dict[str, bytes]:
    """Return the five data files, in canonical order."""
    return {
        "workspace.json": _json_bytes(rows.workspace),
        "categories.csv": csv_bytes("categories.csv", rows.categories),
        "budget_lines.csv": csv_bytes("budget_lines.csv", rows.budget_lines),
        "transactions.csv": csv_bytes("transactions.csv", rows.transactions),
        "months.csv": csv_bytes("months.csv", rows.months),
    }


def manifest_bytes(
    files: dict[str, bytes],
    *,
    app_name: str,
    app_version: str,
    exported_at: datetime,
    base_currency: str,
) -> bytes:
    """Build a manifest with a checksum and data-row count for every member."""
    listed: list[dict[str, object]] = []
    for path in FILE_ORDER:
        data = files[path]
        row_count = None if path == "workspace.json" else _csv_data_rows(data)
        listed.append(
            {
                "path": path,
                "sha256": hashlib.sha256(data).hexdigest(),
                "rows": row_count,
                "bytes": len(data),
            }
        )
    return _json_bytes(
        {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "app": {"name": app_name, "version": app_version},
            "exported_at": timestamp_text(exported_at),
            "base_currency": base_currency,
            "files": listed,
        }
    )


def timestamp_text(value: datetime | None) -> str | None:
    """Format a timestamp as the archive's UTC text (naive values are UTC)."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return (
        value.astimezone(UTC)
        .isoformat(timespec="microseconds" if value.microsecond else "seconds")
        .replace("+00:00", "Z")
    )


def lineage_id(row_id: UUID, import_key: str | None) -> UUID:
    """Preserve origin IDs across restore/export cycles; content keys use the local ID."""
    return UUID(import_key[3:]) if import_key and import_key.startswith("id:") else row_id


def zip_bytes(files: dict[str, bytes], *, exported_at: datetime) -> bytes:
    """Create a deterministic deflated ZIP with fixed entry order and timestamps."""
    stamp = exported_at.astimezone(UTC)
    year = max(1980, min(stamp.year, 2107))
    zip_time = (year, stamp.month, stamp.day, stamp.hour, stamp.minute, stamp.second // 2 * 2)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in REQUIRED_FILES:
            info = zipfile.ZipInfo(name, zip_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0
            archive.writestr(info, files[name])
    return output.getvalue()


def write_archive(
    rows: ArchiveRows,
    *,
    app_name: str,
    app_version: str,
    exported_at: datetime,
    base_currency: str,
) -> bytes:
    files = build_files(rows)
    files["manifest.json"] = manifest_bytes(
        files,
        app_name=app_name,
        app_version=app_version,
        exported_at=exported_at,
        base_currency=base_currency,
    )
    return zip_bytes(files, exported_at=exported_at)


def parse_archive(data: bytes) -> tuple[ParsedArchive | None, tuple[ArchiveError, ...]]:
    """Read an in-memory archive and verify its member set, checksums and row counts."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                return None, (ArchiveError("archive", "duplicate member names"),)
            if not set(REQUIRED_FILES) <= set(names):
                return None, (ArchiveError("archive", "missing required members"),)
            # F4.2's documented limits also bound this shared reader before upload exists.
            if len(names) > MAX_MEMBERS:
                return None, (ArchiveError("archive", "too many members"),)
            files: dict[str, bytes] = {}
            total = 0
            for info in archive.infolist():
                mode = info.external_attr >> 16
                if (
                    any(part in info.filename for part in ("/", "\\", "..", ":"))
                    or (stat.S_IFMT(mode) not in (0, stat.S_IFREG))
                    or info.flag_bits & 1
                    or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                ):
                    return None, (ArchiveError("archive", "unsafe member"),)
                with archive.open(info) as member:
                    content = member.read(MAX_MEMBER_BYTES + 1)
                total += len(content)
                if (
                    len(content) > MAX_MEMBER_BYTES
                    or total > MAX_TOTAL_BYTES
                    or (
                        len(content) >= RATIO_EXEMPT_BYTES
                        and len(content) > info.compress_size * MAX_COMPRESSION_RATIO
                    )
                ):
                    return None, (ArchiveError("archive", "uncompressed size or ratio limit"),)
                files[info.filename] = content
    except (
        zipfile.BadZipFile,
        OSError,
        RuntimeError,
        NotImplementedError,
        EOFError,
        zlib.error,
    ) as exc:
        return None, (ArchiveError("archive", str(exc)),)
    try:
        loaded_manifest: object = json.loads(files["manifest.json"])
    except (ValueError, RecursionError):  # also over-long integers and deep nesting
        return None, (ArchiveError("manifest.json", "invalid JSON"),)
    manifest = _string_keyed_object(loaded_manifest)
    if manifest is None:
        return None, (ArchiveError("manifest.json", "invalid JSON object"),)
    if manifest.get("format") != FORMAT:
        return None, (ArchiveError("manifest.json", "unsupported archive format"),)
    version = manifest.get("format_version")
    if not isinstance(version, str) or not re.fullmatch(r"1\.[0-9]{1,4}", version):
        return None, (ArchiveError("manifest.json", "unsupported archive version"),)
    warnings = [
        ArchiveError(name, "unknown file ignored") for name in names if name not in REQUIRED_FILES
    ]
    if int(version.partition(".")[2]) > int(FORMAT_VERSION.partition(".")[2]):
        warnings.append(ArchiveError("manifest.json", f"newer minor version: {version}"))
    warnings.extend(
        ArchiveError("manifest.json", f"unknown key: {key}")
        for key in manifest.keys()
        - {"format", "format_version", "app", "exported_at", "base_currency", "files"}
    )
    try:
        _parse_timestamp(str(manifest.get("exported_at", "")))
        currency = manifest.get("base_currency")
        if not isinstance(currency, str) or currency != currency.upper():
            raise ValueError
        minor_unit_for(currency)
        app = _string_keyed_object(manifest.get("app"))
        if app is None or not all(isinstance(app.get(key), str) for key in ("name", "version")):
            raise ValueError
    except (TypeError, ValueError):
        return None, (ArchiveError("manifest.json", "invalid metadata"),)
    entries_value = manifest.get("files")
    if not isinstance(entries_value, list):
        return None, (ArchiveError("manifest.json", "files must be a list"),)
    entries = cast(list[object], entries_value)
    expected: dict[str, dict[str, object]] = {}
    for value in entries:
        entry = _string_keyed_object(value)
        if entry is None:
            return None, (ArchiveError("manifest.json", "invalid file entry"),)
        path = entry.get("path")
        if not isinstance(path, str):
            return None, (ArchiveError("manifest.json", "invalid file path"),)
        expected[path] = entry
    # Unlisted extra members are only warned about above (ADR-0016: readers ignore what they
    # do not know); every listed file must exist and every data file must be listed.
    if (
        len(expected) != len(entries)
        or "manifest.json" in expected
        or not set(FILE_ORDER) <= set(expected)
        or not set(expected) <= set(files)
    ):
        return None, (ArchiveError("manifest.json", "invalid file list"),)
    errors: list[ArchiveError] = []
    data_rows = 0
    for name, entry in expected.items():
        raw = files[name]
        if entry.get("sha256") != hashlib.sha256(raw).hexdigest():
            errors.append(ArchiveError(name, "checksum mismatch"))
        if entry.get("bytes") != len(raw):
            errors.append(ArchiveError(name, "byte count mismatch"))
        if not name.endswith(".csv"):
            if entry.get("rows") is not None:
                errors.append(ArchiveError(name, "JSON row count must be null"))
        else:
            try:
                rows = _csv_data_rows(raw)
            except (UnicodeDecodeError, csv.Error):
                errors.append(ArchiveError(name, "invalid UTF-8 or CSV"))
            else:
                if entry.get("rows") != rows:
                    errors.append(ArchiveError(name, "row count mismatch"))
                data_rows += rows
    if errors:
        return None, tuple(errors)
    if data_rows > MAX_DATA_ROWS:
        return None, (ArchiveError("archive", "too many data rows"),)
    try:
        workspace = _string_keyed_object(json.loads(files["workspace.json"]))
    except (ValueError, RecursionError):
        workspace = None
    if workspace is None:
        return None, (ArchiveError("workspace.json", "invalid JSON object"),)
    if workspace.get("base_currency") != currency:
        return None, (ArchiveError("workspace.json", "base currency differs from manifest"),)
    if (
        not all(
            isinstance(workspace.get(key), str) and workspace[key]
            for key in ("name", "timezone", "locale")
        )
        or type(workspace.get("auto_copy_plan")) is not bool
        or (
            workspace.get("country_code") is not None
            and not re.fullmatch(r"[A-Z]{2}", str(workspace["country_code"]))
        )
        or ("taxes_enabled" in workspace and type(workspace["taxes_enabled"]) is not bool)
        or not _valid_upcoming_days(workspace.get("upcoming_days"))
    ):
        return None, (ArchiveError("workspace.json", "invalid workspace settings"),)
    warnings.extend(
        ArchiveError("workspace.json", f"unknown key: {key}")
        for key in workspace.keys()
        - {
            "name",
            "base_currency",
            "country_code",
            "timezone",
            "locale",
            "auto_copy_plan",
            "taxes_enabled",
            "upcoming_days",
        }
    )
    parsed = _read_rows(files, workspace, currency, errors, warnings)
    if errors:
        return None, tuple(errors)
    return ParsedArchive(manifest, files, parsed, tuple(warnings)), ()


def _valid_upcoming_days(value: object) -> bool:
    """``upcoming_days`` (1.1) is optional: absent or ``null`` means the instance default."""
    return value is None or (type(value) is int and MIN_UPCOMING_DAYS <= value <= MAX_UPCOMING_DAYS)


def _parse_timestamp(value: str) -> str:
    if not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})",
        value,
    ):
        raise ValueError("invalid timestamp")
    try:
        result = timestamp_text(datetime.fromisoformat(value))
    except OverflowError:  # an offset that moves year 1 or 9999 out of range
        raise ValueError("invalid timestamp") from None
    assert result is not None
    return result


def _validate_cell(column: str, value: str, currency: str) -> str:
    if column in ("id", "category_id"):
        return str(UUID(value)) if value else ""
    if column in ("kind", "direction"):
        return Kind(value).value if value else ""
    if column == "treatment":
        return Treatment(value or "regular").value
    if column in ("archived_at", "deleted_at", "closed_at", "plan_started_at"):
        return _parse_timestamp(value) if value else ""
    if column in ("date", "month"):
        pattern = r"[0-9]{4}-[0-9]{2}" + (r"-[0-9]{2}" if column == "date" else "")
        if not re.fullmatch(pattern, value):
            raise ValueError("invalid date or month")
        date.fromisoformat(value if column == "date" else value + "-01")
    if column in ("amount", "planned"):
        if column == "planned" and not value:
            return "0"
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
            raise ValueError("invalid unsigned decimal")
        whole, _, fractional = value.partition(".")
        digits = minor_unit_for(currency)
        if fractional[digits:].strip("0") or (
            column == "amount" and not (whole + fractional).strip("0")
        ):
            raise ValueError("invalid precision or zero amount")
    if column == "currency":
        if value and value != currency:
            raise ValueError("currency differs from workspace")
        return currency
    if column in ("category", "name") and not 1 <= len(value) <= 80:
        raise ValueError("invalid category name")
    if column == "description" and len(value) > 200:
        raise ValueError("description too long")
    if column == "color":
        if value and not re.fullmatch(r"#[0-9a-f]{6}", value):
            raise ValueError("invalid color")
        return value or "#16a34a"
    if column == "sort_order" and not re.fullmatch(r"[0-9]{1,9}", value):
        raise ValueError("invalid sort order")
    return value


_REQUIRED_COLUMNS = {
    "categories.csv": {"id", "kind", "name"},
    "transactions.csv": {"date", "direction", "amount", "category", "category_id"},
    "budget_lines.csv": {"month", "category", "planned", "category_id"},
    "months.csv": {"month"},
}


def _read_rows(
    files: dict[str, bytes],
    workspace: dict[str, object],
    currency: str,
    errors: list[ArchiveError],
    warnings: list[ArchiveError],
) -> ArchiveRows:
    tables: dict[str, list[dict[str, str]]] = {}
    for name, columns in CSV_COLUMNS.items():
        rows: list[dict[str, str]] = []
        tables[name] = rows
        try:
            reader = csv.reader(
                io.StringIO(files[name].decode("utf-8-sig"), newline=""), strict=True
            )
            header = next(reader, list[str]())
            if (
                len(header) > MAX_COLUMNS
                or len(header) != len(set(header))
                or not _REQUIRED_COLUMNS[name] <= set(header)
            ):
                errors.append(ArchiveError(name, "invalid or missing header", 1))
                continue
            warnings.extend(
                ArchiveError(name, f"unknown column: {key}", 1, key)
                for key in header
                if key not in columns
            )
            for index, cells in enumerate(reader):
                if len(errors) >= MAX_ERRORS:
                    break
                line = reader.line_num
                if len(cells) != len(header):
                    errors.append(ArchiveError(name, "wrong number of cells", line))
                    continue
                raw = dict(zip(header, cells, strict=True))
                row: dict[str, str] = {}
                for column in columns:
                    value = raw.get(column, "")
                    if column == "sort_order" and not value:
                        value = str(index)
                    try:
                        if column in _REQUIRED_COLUMNS[name] and column != "planned" and not value:
                            raise ValueError("required value missing")
                        row[column] = _validate_cell(column, value, currency)
                    except ValueError as exc:
                        errors.append(ArchiveError(name, str(exc), line, column))
                if len(row) == len(columns):
                    rows.append(row)
        except (UnicodeDecodeError, csv.Error) as exc:
            errors.append(ArchiveError(name, str(exc)))
    if len(errors) >= MAX_ERRORS:
        # The rows are incomplete, so cross-file checks would only add noise.
        errors.append(ArchiveError("archive", "too many errors; later rows were not checked"))
        return ArchiveRows(workspace, (), (), (), ())
    categories = tables["categories.csv"]
    by_id = {row["id"]: row for row in categories}
    if len(by_id) != len(categories):
        errors.append(ArchiveError("categories.csv", "duplicate category id"))
    for name in ("transactions.csv", "budget_lines.csv"):
        seen: set[object] = set()
        for row in tables[name]:
            category = by_id.get(row["category_id"])
            kind = row.get("direction", row.get("kind", ""))
            if (
                category is None
                or (kind and kind != category["kind"])
                or row["category"] != category["name"]
            ):
                errors.append(ArchiveError(name, "category reference mismatch"))
            key = (row["month"], row["category_id"]) if name == "budget_lines.csv" else row["id"]
            if key and key in seen:
                errors.append(ArchiveError(name, "duplicate row identity"))
            seen.add(key)
    months = tables["months.csv"]
    if len({row["month"] for row in months}) != len(months):
        errors.append(ArchiveError("months.csv", "duplicate month"))
    return ArchiveRows(
        workspace=workspace,
        categories=tuple(
            CategoryRow(
                row["id"],
                row["kind"],
                row["name"],
                row["treatment"],
                row["color"],
                int(row["sort_order"]),
                row["archived_at"] or None,
            )
            for row in categories
        ),
        budget_lines=tuple(BudgetLineRow(**row) for row in tables["budget_lines.csv"]),
        transactions=tuple(
            TransactionRow(
                row["date"],
                row["direction"],
                row["amount"],
                row["currency"],
                row["category"],
                row["description"],
                row["category_id"],
                row["id"],
                row["deleted_at"] or None,
            )
            for row in tables["transactions.csv"]
        ),
        months=tuple(
            MonthRow(row["month"], row["closed_at"] or None, row["plan_started_at"] or None)
            for row in months
        ),
    )
