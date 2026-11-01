"""Pure classification and validation of untrusted import upload bytes (F4.2)."""

from __future__ import annotations

import csv
import io
from collections.abc import Callable
from dataclasses import dataclass

from assetboard.domain.archive import MAX_ERRORS, ArchiveError, ParsedArchive, parse_archive

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


def _never() -> bool:
    return False


def parse_upload(
    content: bytes, *, delimiter: str = ",", out_of_time: Callable[[], bool] = _never
) -> tuple[ParsedUpload | None, tuple[ArchiveError, ...]]:
    """Classify by content, then validate without extracting or writing anything.

    ``out_of_time`` is asked every ``DEADLINE_CHECK_ROWS`` CSV rows (and once after reading an
    archive, whose reader is bounded by its size limits); when it answers true the read stops
    with ``ParseTimeout``. The caller owns the clock, so this module stays pure.
    """
    if content.startswith(ZIP_LOCAL_FILE_SIGNATURE):
        archive, errors = parse_archive(content)
        if out_of_time():
            raise ParseTimeout
        if archive is None:
            return None, errors
        return ParsedUpload("archive", archive=archive, warnings=archive.warnings), ()
    if delimiter not in {",", ";"}:
        return None, (ArchiveError("upload.csv", "invalid delimiter option"),)

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        line = content.count(b"\n", 0, error.start) + 1
        return None, (ArchiveError("upload.csv", "invalid UTF-8", line=line),)

    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
    try:
        header = next(reader)
    except StopIteration:
        return None, (ArchiveError("upload.csv", "file is empty", line=1),)
    except csv.Error:
        return None, (ArchiveError("upload.csv", "invalid CSV", line=reader.line_num or 1),)
    if not header or len(header) > MAX_IMPORT_COLUMNS:
        return None, (ArchiveError("upload.csv", "invalid column count", line=1),)

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

    header_tuple = tuple(header)
    if header_tuple[:2] == ("month", "kind"):
        source = "budget_lines_csv"
    elif header_tuple[:2] == ("id", "kind"):
        source = "categories_csv"
    else:
        source = "transactions_csv"
    return (
        ParsedUpload(
            source,
            csv_header=header_tuple,
            csv_rows=tuple(rows),
            row_errors=tuple(row_errors),
            invalid_row_count=invalid_row_count,
        ),
        (),
    )
