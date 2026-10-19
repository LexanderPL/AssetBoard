"""Transactions as CSV (F2.6), in the columns of M4's ``transactions.csv``. Pure: no I/O.

F2.6's export writes the columns of the workspace archive's ``transactions.csv`` (ADR-0016,
docs/specs/M4-ledger-export-import.md "Format reference v1.0") in the same order, without
``deleted_at``. With the default options a file is a valid ``transactions.csv``: UTF-8 without
BOM, comma delimiter, RFC 4180 quoting, CRLF line ends, dates ``YYYY-MM-DD``, English enum
tokens, amounts as plain decimals with ``.`` and exactly the currency's minor digits. The
options change only what M4's lone-CSV upload also accepts (``;`` with a decimal comma) or what
it removes (a BOM). Nothing depends on the user's locale.

The formula guard (owner decision, 2026-10-19: opt-in, off by default) prefixes ``'`` to text
cells that a spreadsheet would read as a formula. It changes those cells, so a guarded file no
longer re-imports them exactly.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from assetboard.domain.money import Money

TRANSACTION_EXPORT_COLUMNS = (
    "date",
    "direction",
    "amount",
    "currency",
    "category",
    "description",
    "category_id",
    "id",
)
DELIMITERS = {"comma": ",", "semicolon": ";"}
DECIMAL_SEPARATORS = {"dot": ".", "comma": ","}
# Owner decision (2026-10-19): the guard is offered, off unless the user ticks it.
FORMULA_GUARD_DEFAULT = False
# Cells starting with these are formulas (or become one) in common spreadsheet programs.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
BOM = "﻿"


class CsvOptionError(ValueError):
    """An option, or a combination of options, the export does not offer; ``code`` names it."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class CsvOptions:
    delimiter: str = "comma"
    decimal: str = "dot"
    bom: bool = False
    formula_guard: bool = FORMULA_GUARD_DEFAULT

    def __post_init__(self) -> None:
        if self.delimiter not in DELIMITERS:
            raise CsvOptionError("invalid_delimiter")
        if self.decimal not in DECIMAL_SEPARATORS:
            raise CsvOptionError("invalid_decimal")
        # As for M4's upload options: a decimal comma only with the semicolon delimiter.
        if self.decimal == "comma" and self.delimiter != "semicolon":
            raise CsvOptionError("decimal_comma_needs_semicolon")


@dataclass(frozen=True, slots=True)
class TransactionCsvRow:
    date: date
    direction: str
    amount_minor: int
    currency: str
    category: str
    description: str
    category_id: UUID
    id: UUID


def plain_amount(amount_minor: int, currency: str, *, decimal: str = "dot") -> str:
    """``12.50`` (EUR), ``1200`` (JPY): exactly the minor digits, no grouping, no sign."""
    if amount_minor < 0:
        raise ValueError("amounts are positive")
    text = format(Money(amount_minor, currency).to_decimal(), "f")
    return text.replace(".", DECIMAL_SEPARATORS[decimal])


def guard_formula(value: str) -> str:
    """``value`` with a leading ``'`` when a spreadsheet would treat it as a formula."""
    return f"'{value}" if value.startswith(FORMULA_PREFIXES) else value


def _cells(row: TransactionCsvRow, options: CsvOptions) -> list[str]:
    category, description = row.category, row.description
    if options.formula_guard:
        category, description = guard_formula(category), guard_formula(description)
    return [
        row.date.isoformat(),
        row.direction,
        plain_amount(row.amount_minor, row.currency, decimal=options.decimal),
        row.currency,
        category,
        description,
        str(row.category_id),
        str(row.id),
    ]


def transactions_csv(rows: Iterable[TransactionCsvRow], options: CsvOptions) -> Iterator[str]:
    """The file as text chunks: the BOM (if chosen) with the header, then one chunk per row."""
    buffer = io.StringIO()
    writer = csv.writer(
        buffer,
        delimiter=DELIMITERS[options.delimiter],
        quotechar='"',
        quoting=csv.QUOTE_MINIMAL,
        lineterminator="\r\n",
    )

    def take() -> str:
        text = buffer.getvalue()
        buffer.seek(0)
        buffer.truncate()
        return text

    writer.writerow(TRANSACTION_EXPORT_COLUMNS)
    yield (BOM if options.bom else "") + take()
    for row in rows:
        writer.writerow(_cells(row, options))
        yield take()
