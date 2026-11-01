from __future__ import annotations

import io
import zipfile

import pytest

from assetboard.domain.archive import ArchiveError
from assetboard.domain.import_upload import parse_upload
from assetboard.services.imports import report_errors_csv


def test_csv_is_detected_by_content_and_bom_is_removed() -> None:
    content = b"\xef\xbb\xbfdate,direction,amount\r\n2026-10-01,expense,1.00\r\n"
    parsed, errors = parse_upload(content)

    assert errors == ()
    assert parsed is not None
    assert parsed.source == "transactions_csv"
    assert parsed.csv_header == ("date", "direction", "amount")
    assert parsed.csv_rows == (("2026-10-01", "expense", "1.00"),)


def test_semicolon_csv_preserves_decimal_comma_values() -> None:
    parsed, errors = parse_upload(
        b"date;direction;amount\n2026-10-01;expense;1,25\n", delimiter=";"
    )

    assert errors == ()
    assert parsed is not None
    assert parsed.csv_header == ("date", "direction", "amount")
    assert parsed.csv_rows == (("2026-10-01", "expense", "1,25"),)


@pytest.mark.parametrize(
    ("content", "line", "message"),
    [
        (b"date,amount\n2026-01-01,1\n\xff", 3, "invalid UTF-8"),
        (b'"unfinished', 1, "invalid CSV"),
    ],
)
def test_invalid_csv_reports_line(content: bytes, line: int, message: str) -> None:
    parsed, errors = parse_upload(content)

    assert parsed is None
    assert errors[0].line == line
    assert message in errors[0].message


def test_invalid_csv_rows_are_reported_while_valid_rows_are_kept() -> None:
    parsed, errors = parse_upload(b"date,amount\n2026-01-01,1\n2026-01-02\n2026-01-03,3\n")

    assert errors == ()
    assert parsed is not None
    assert parsed.csv_rows == (("2026-01-01", "1"), ("2026-01-03", "3"))
    assert len(parsed.row_errors) == 1
    assert parsed.row_errors[0].line == 3
    assert parsed.row_errors[0].column == "row"


def test_invalid_row_details_are_capped_without_losing_the_total_count() -> None:
    content = b"date,amount\n" + b"2026-01-01\n" * 1001
    parsed, errors = parse_upload(content)

    assert errors == ()
    assert parsed is not None
    assert parsed.invalid_row_count == 1001
    assert len(parsed.row_errors) == 1000


def test_zip_signature_selects_archive_reader() -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("bad.csv", b"not an archive")

    parsed, errors = parse_upload(output.getvalue())

    assert parsed is None
    assert errors == (ArchiveError("archive", "missing required members"),)


def test_non_zip_content_is_treated_as_csv_even_with_zip_name() -> None:
    parsed, errors = parse_upload(b"date,direction\n")

    assert errors == ()
    assert parsed is not None
    assert parsed.source == "transactions_csv"


def test_error_csv_escapes_spreadsheet_formulas() -> None:
    output = report_errors_csv(
        {
            "errors": [
                {
                    "file": "=IMPORT()",
                    "line": 1,
                    "column": "=A1",
                    "code": "invalid_utf8",
                    "message": "invalid UTF-8",
                }
            ],
            "skipped": [
                {
                    "file": "transactions.csv",
                    "line": 2,
                    "column": "amount",
                    "code": "closed_month",
                    "message": "month is closed",
                },
                {
                    "file": "transactions.csv",
                    "line": 3,
                    "column": "",
                    "code": "adapter_specific",
                    "message": "=HYPERLINK()",
                },
            ],
        }
    ).decode("utf-8")

    assert "'=IMPORT()" in output
    assert "'=A1" in output
    assert "Invalid UTF-8 encoding." in output
    assert "transactions.csv,2,amount,closed_month,The month is closed." in output
    # Unknown codes keep their own message (formula-guarded).
    assert "transactions.csv,3,,adapter_specific,'=HYPERLINK()" in output
