"""Bulk add of investment transactions (F6.2a part B): line splitting. Pure: no I/O, no clock.

One transaction per line, fields separated by ``;``, surrounding whitespace trimmed and empty
trailing fields optional::

    date; instrument; type; quantity; unit price; fee; fx; amounts currency

Blank lines and lines starting with ``#`` are ignored. This module checks the structure, the
date and the type; numbers (locale), instruments, FX and the amounts currency are resolved by
the service, which knows the workspace.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from assetboard.domain.networth import (
    MAX_BULK_BYTES,
    MAX_BULK_LINES,
    BulkTooLarge,
    TooManyBulkLines,
    check_bulk_limits,
    normalized_bulk_text,
)
from assetboard.domain.text import text_key

__all__ = [
    "MAX_BULK_BYTES",
    "MAX_BULK_LINES",
    "MIN_FIELDS",
    "MAX_FIELDS",
    "BulkTooLarge",
    "RawTransactionLine",
    "TooManyBulkLines",
    "TransactionLineProblem",
    "check_bulk_limits",
    "normalized_bulk_text",
    "parse_bulk_date",
    "parse_bulk_type",
    "split_transaction_lines",
]

MIN_FIELDS = 4
MAX_FIELDS = 8
MIN_YEAR = 1900
MAX_YEAR = 2199

# English source words plus the Polish aliases of the spec, matched case-insensitively.
TYPE_ALIASES = {
    "buy": "buy",
    "kupno": "buy",
    "zakup": "buy",
    "sell": "sell",
    "sprzedaż": "sell",
    "sprzedaz": "sell",
}

_ISO = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")


@dataclass(frozen=True, slots=True)
class RawTransactionLine:
    number: int
    text: str
    day: date
    instrument: str
    type: str
    quantity: str
    unit_price: str
    fee: str
    fx: str
    currency: str


@dataclass(frozen=True, slots=True)
class TransactionLineProblem:
    number: int
    text: str
    code: str


class InvalidBulkDate(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def parse_bulk_date(value: str, order: str, separator: str, today: date) -> date:
    """ISO ``YYYY-MM-DD`` or the locale's short date in ``order`` with a four-digit year.

    Codes: ``invalid_date`` (not a date or outside 1900-2199), ``future_date`` (after today).
    """
    text = value.strip()
    parts: dict[str, int] = {}
    if match := _ISO.fullmatch(text):
        parts = {"y": int(match.group(1)), "m": int(match.group(2)), "d": int(match.group(3))}
    else:
        pieces = text.split(separator) if separator else []
        if len(pieces) == 3 and len(order) == 3:
            for letter, piece in zip(order, pieces, strict=True):
                piece = piece.strip()
                limit = 4 if letter == "y" else 2
                if not piece.isascii() or not piece.isdigit() or len(piece) > limit:
                    raise InvalidBulkDate("invalid_date")
                if letter == "y" and len(piece) != 4:
                    # A two-digit year would guess the century; refuse it.
                    raise InvalidBulkDate("invalid_date")
                parts[letter] = int(piece)
    if set(parts) != {"y", "m", "d"}:
        raise InvalidBulkDate("invalid_date")
    try:
        day = date(parts["y"], parts["m"], parts["d"])
    except ValueError:
        raise InvalidBulkDate("invalid_date") from None
    if not MIN_YEAR <= day.year <= MAX_YEAR:
        raise InvalidBulkDate("invalid_date")
    if day > today:
        raise InvalidBulkDate("future_date")
    return day


def parse_bulk_type(value: str) -> str | None:
    """``buy`` or ``sell`` for a type word or its Polish alias; ``None`` when unknown."""
    return TYPE_ALIASES.get(text_key(value))


def split_transaction_lines(
    text: str, *, order: str, separator: str, today: date
) -> tuple[tuple[RawTransactionLine, ...], tuple[TransactionLineProblem, ...]]:
    """Split the pasted text into lines; structural, date and type errors are per line.

    Raises ``BulkTooLarge`` / ``TooManyBulkLines`` before any parsing.
    """
    check_bulk_limits(text)
    lines: list[RawTransactionLine] = []
    problems: list[TransactionLineProblem] = []
    for number, raw in enumerate(normalized_bulk_text(text).split("\n"), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = [field.strip() for field in raw.split(";")]
        while len(fields) > MIN_FIELDS and not fields[-1]:
            fields.pop()
        if not MIN_FIELDS <= len(fields) <= MAX_FIELDS:
            problems.append(TransactionLineProblem(number, stripped, "wrong_format"))
            continue
        fields += [""] * (MAX_FIELDS - len(fields))
        day_text, instrument, type_text, quantity, price, fee, fx, currency = fields
        if not day_text:
            problems.append(TransactionLineProblem(number, stripped, "date_required"))
            continue
        try:
            day = parse_bulk_date(day_text, order, separator, today)
        except InvalidBulkDate as exc:
            problems.append(TransactionLineProblem(number, stripped, exc.code))
            continue
        if not instrument:
            problems.append(TransactionLineProblem(number, stripped, "instrument_required"))
            continue
        kind = parse_bulk_type(type_text)
        if kind is None:
            problems.append(TransactionLineProblem(number, stripped, "invalid_type"))
            continue
        if not quantity:
            problems.append(TransactionLineProblem(number, stripped, "quantity_required"))
            continue
        lines.append(
            RawTransactionLine(
                number=number,
                text=stripped,
                day=day,
                instrument=instrument,
                type=kind,
                quantity=quantity,
                unit_price=price,
                fee=fee,
                fx=fx,
                currency=currency.upper(),
            )
        )
    return tuple(lines), tuple(problems)
