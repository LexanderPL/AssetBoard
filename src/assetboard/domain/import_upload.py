"""Pure classification and validation of untrusted import upload bytes (F4.2/F4.3)."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from assetboard.domain.archive import MAX_ERRORS, ArchiveError, ParsedArchive, parse_archive
from assetboard.domain.import_csv import (
    MONTHS,
    HeaderError,
    classify_header,
    detect_delimiter,
)

ZIP_LOCAL_FILE_SIGNATURE = b"PK\x03\x04"
MAX_IMPORT_ROWS = 1_000_000
MAX_IMPORT_COLUMNS = 64
# How often (in data rows) the CSV reader asks ``out_of_time``.
DEADLINE_CHECK_ROWS = 1000


class ParseTimeout(Exception):
    """``out_of_time`` answered true while the upload was being read."""


@dataclass(frozen=True, slots=True)
class ParsedUpload:
    source: str
    archive: ParsedArchive | None = None
    csv_header: tuple[str, ...] = ()
    csv_rows: tuple[tuple[str, ...], ...] = ()
    row_errors: tuple[ArchiveError, ...] = ()
    invalid_row_count: int = 0
    warnings: tuple[ArchiveError, ...] = ()
    delimiter: str = ","


def _never() -> bool:
    return False


def _looks_like_workspace_json(text: str) -> bool:
    stripped = text.lstrip()
    if not stripped.startswith("{"):
        return False
    try:
        value: object = json.loads(stripped)
    except (ValueError, RecursionError):
        return False
    if not isinstance(value, dict):
        return False
    mapping = cast("dict[object, object]", value)
    keys = {key for key in mapping if isinstance(key, str)}
    return {"name", "base_currency"} <= keys


def parse_upload(
    content: bytes,
    *,
    delimiter: str = "detect",
    out_of_time: Callable[[], bool] = _never,
) -> tuple[ParsedUpload | None, tuple[ArchiveError, ...]]:
    """Classify by content, then validate without extracting or writing anything.

    ``delimiter`` is ``detect`` (the lone-CSV default), ``,`` or ``;``. Archives always use the
    canonical comma format and ignore it. ``out_of_time`` is asked every
    ``DEADLINE_CHECK_ROWS`` CSV rows (and once after reading an archive, whose reader is bounded
    by its size limits); when it answers true the read stops with ``ParseTimeout``.
    """
    if content.startswith(ZIP_LOCAL_FILE_SIGNATURE):
        archive, errors = parse_archive(content)
        if out_of_time():
            raise ParseTimeout
        if archive is None:
            return None, errors
        return ParsedUpload("archive", archive=archive, warnings=archive.warnings), ()
    if delimiter not in {"detect", ",", ";"}:
        return None, (ArchiveError("upload.csv", "invalid delimiter option"),)

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        line = content.count(b"\n", 0, error.start) + 1
        return None, (ArchiveError("upload.csv", "invalid UTF-8", line=line),)
    if _looks_like_workspace_json(text):
        return None, (ArchiveError("upload.csv", "only part of a full restore", line=1),)

    first_line = text.split("\n", 1)[0].rstrip("\r")
    chosen = detect_delimiter(first_line) if delimiter == "detect" else delimiter
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=chosen, strict=True)
    try:
        header = next(reader)
    except StopIteration:
        return None, (ArchiveError("upload.csv", "file is empty", line=1),)
    except csv.Error:
        return None, (ArchiveError("upload.csv", "invalid CSV", line=reader.line_num or 1),)
    if not header or len(header) > MAX_IMPORT_COLUMNS:
        return None, (ArchiveError("upload.csv", "invalid column count", line=1),)
    try:
        classification = classify_header(tuple(header))
    except HeaderError as error:
        message = {
            "duplicate_column": "duplicate column name",
            "unknown_file": "unknown file",
        }.get(error.code, error.code)
        return None, (ArchiveError("upload.csv", message, line=1, column=error.column or None),)
    if classification.source == MONTHS:
        return None, (ArchiveError("upload.csv", "only part of a full restore", line=1),)
    if classification.missing_columns:
        column = classification.missing_columns[0]
        return None, (
            ArchiveError(
                "upload.csv",
                f"missing required column: {column}",
                line=1,
                column=column,
            ),
        )

    rows: list[tuple[str, ...]] = []
    row_errors: list[ArchiveError] = []
    invalid_row_count = 0
    data_row_count = 0
    try:
        for row in reader:
            data_row_count += 1
            if data_row_count % DEADLINE_CHECK_ROWS == 0 and out_of_time():
                raise ParseTimeout
            if data_row_count > MAX_IMPORT_ROWS:
                return None, (ArchiveError("upload.csv", "too many data rows"),)
            if len(row) != len(header):
                invalid_row_count += 1
                if len(row_errors) < MAX_ERRORS:
                    row_errors.append(
                        ArchiveError(
                            "upload.csv",
                            "row has a different number of columns",
                            line=reader.line_num,
                            column="row",
                        )
                    )
                continue
            rows.append(tuple(row))
    except csv.Error:
        return None, (ArchiveError("upload.csv", "invalid CSV", line=reader.line_num or 1),)

    warnings = tuple(
        ArchiveError("upload.csv", "unknown column ignored", line=1, column=column)
        for column in classification.unknown_columns
    )
    return (
        ParsedUpload(
            classification.source,
            csv_header=classification.columns,
            csv_rows=tuple(rows),
            row_errors=tuple(row_errors),
            invalid_row_count=invalid_row_count,
            warnings=warnings,
            delimiter=chosen,
        ),
        (),
    )
