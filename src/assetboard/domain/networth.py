"""Net-worth rules (F5.1): sides, names, seeds, valuation order, bulk lines, sparklines.

Pure: no I/O, database or clock access. Services pass "today" in from the workspace's ``Clock``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from uuid import UUID

from assetboard.domain.categories import (
    PALETTE,
    Direction,
    InvalidCategoryName,
    clean_color,
    clean_name,
    move,
    name_key,
)
from assetboard.domain.months import MAX_YEAR, MIN_YEAR
from assetboard.domain.text import collapse_whitespace, has_rejected_characters, text_key

MAX_ITEM_NAME_LENGTH = 200
MAX_NOTES_LENGTH = 200
MAX_BULK_LINES = 500
MAX_BULK_BYTES = 64 * 1024
SPARKLINE_WIDTH = 120
SPARKLINE_HEIGHT = 28
SPARKLINE_PADDING = 2


class Side(StrEnum):
    ASSET = "asset"
    LIABILITY = "liability"


@dataclass(frozen=True, slots=True)
class Unavailable:
    """A linked value could not be resolved for the requested date."""


class InvalidItemName(ValueError):
    code = "invalid_name"


class InvalidNotes(ValueError):
    code = "invalid_notes"


class BulkTooLarge(ValueError):
    code = "bulk_too_large"


class TooManyBulkLines(ValueError):
    code = "too_many_lines"


def clean_item_name(value: str) -> str:
    """Trim, collapse whitespace, refuse control/invisible characters and bad lengths."""
    if has_rejected_characters(value.strip()):
        raise InvalidItemName
    cleaned = collapse_whitespace(value)
    if not cleaned or len(cleaned) > MAX_ITEM_NAME_LENGTH:
        raise InvalidItemName
    return cleaned


def clean_notes(value: str) -> str:
    """Trim and collapse whitespace; notes are single-line and at most 200 characters."""
    if has_rejected_characters(value.strip()):
        raise InvalidNotes
    cleaned = collapse_whitespace(value)
    if len(cleaned) > MAX_NOTES_LENGTH:
        raise InvalidNotes
    return cleaned


def clean_valuation_note(value: str) -> str:
    return clean_notes(value)


def valid_day(day: date) -> bool:
    return MIN_YEAR <= day.year <= MAX_YEAR


def category_name(value: str) -> str:
    return clean_name(value)


def category_name_key(value: str) -> str:
    return name_key(value)


def category_color(value: str) -> str:
    return clean_color(value)


def category_direction(value: str) -> Direction:
    if value not in {"up", "down"}:
        raise ValueError("invalid direction")
    return "up" if value == "up" else "down"


def move_category[T](items: Sequence[T], item: T, direction: Direction) -> list[T]:
    return move(items, item, direction)


@dataclass(frozen=True, slots=True)
class SeedNetWorthCategory:
    name: str  # message id; translated when the seed runs
    side: Side
    color: str


def _seed(names: Sequence[str], side: Side) -> tuple[SeedNetWorthCategory, ...]:
    return tuple(
        SeedNetWorthCategory(name, side, PALETTE[index % len(PALETTE)])
        for index, name in enumerate(names)
    )


def _(message: str) -> str:
    """Marks a message for extraction; it is translated where it is used."""
    return message


# The original asset list, kept so a workspace seeded before the 2026-11 refresh is still
# recognised as untouched by the archive restore check.
ASSET_SEED_V1: tuple[SeedNetWorthCategory, ...] = _seed(
    (
        _("Vehicles"),
        _("Electronics"),
        _("Audio equipment"),
        _("Instruments"),
        _("Appliances"),
        _("Jewelry & watches"),
        _("Photography"),
        _("Other"),
    ),
    Side.ASSET,
)
ASSET_SEED: tuple[SeedNetWorthCategory, ...] = _seed(
    (
        _("Vehicles"),
        _("Electronics"),
        _("Appliances"),
        _("Furniture"),
        _("Jewelry & watches"),
        _("Art & collectibles"),
        _("Tools"),
        _("Other"),
    ),
    Side.ASSET,
)
ASSET_SEEDS: tuple[tuple[SeedNetWorthCategory, ...], ...] = (ASSET_SEED_V1, ASSET_SEED)
LIABILITY_SEED: tuple[SeedNetWorthCategory, ...] = _seed(
    (_("Installments"), _("Mortgage"), _("Other")),
    Side.LIABILITY,
)
SEED: tuple[SeedNetWorthCategory, ...] = (*ASSET_SEED, *LIABILITY_SEED)


def seed_for(side: Side) -> tuple[SeedNetWorthCategory, ...]:
    return ASSET_SEED if side is Side.ASSET else LIABILITY_SEED


@dataclass(frozen=True, slots=True)
class ValuationPoint:
    """The fields valuation ordering needs; services build these from their rows."""

    id: UUID
    as_of: date
    created_at: datetime
    value_minor: int


def valuation_order_key(value: ValuationPoint) -> tuple[date, datetime, str]:
    return (value.as_of, value.created_at, str(value.id))


def ordered_valuations(values: Sequence[ValuationPoint]) -> list[ValuationPoint]:
    """Oldest first: ``as_of``, then entry order, then id (a total order)."""
    return sorted(values, key=valuation_order_key)


def current_valuation(values: Sequence[ValuationPoint], today: date) -> ValuationPoint | None:
    """The latest valuation effective on ``today``; future-dated rows are ignored."""
    effective = [value for value in values if value.as_of <= today]
    return max(effective, key=valuation_order_key) if effective else None


@dataclass(frozen=True, slots=True)
class HistoryPoint:
    as_of: date
    value_minor: int


def history_points(values: Sequence[ValuationPoint], today: date) -> tuple[HistoryPoint, ...]:
    """One point per effective date (the latest entry on a date wins), oldest first."""
    latest: dict[date, tuple[tuple[date, datetime, str], ValuationPoint]] = {}
    for value in values:
        if value.as_of > today:
            continue
        key = valuation_order_key(value)
        previous = latest.get(value.as_of)
        if previous is None or key > previous[0]:
            latest[value.as_of] = (key, value)
    return tuple(
        HistoryPoint(day, value.value_minor) for day, (_key, value) in sorted(latest.items())
    )


def sparkline_polyline(
    values: Sequence[int | Decimal],
    *,
    width: int = SPARKLINE_WIDTH,
    height: int = SPARKLINE_HEIGHT,
    padding: int = SPARKLINE_PADDING,
) -> str:
    """SVG ``points`` for a tiny trend line, in a 0..width/0..height viewBox.

    A single value (or a flat series) draws a horizontal line in the middle, so a one-point
    history still has a visible sparkline. No float: coordinates are Decimal strings.
    """
    if not values:
        return ""
    if width <= 2 * padding or height <= 2 * padding:
        raise ValueError("sparkline dimensions must leave room for padding")
    middle = Decimal(height - 1) / 2
    if len(values) == 1:
        return f"{padding},{middle:.2f} {width - padding},{middle:.2f}"

    low, high = min(values), max(values)
    usable_width = Decimal(width - 2 * padding)
    usable_height = Decimal(height - 2 * padding)

    points: list[str] = []
    for index, value in enumerate(values):
        x = Decimal(padding) + (usable_width * index / (len(values) - 1))
        if high == low:
            y = middle
        else:
            y = Decimal(padding) + (Decimal(high - value) / Decimal(high - low)) * usable_height
        x_text = x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        y_text = y.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        points.append(f"{x_text},{y_text}")
    return " ".join(points)


@dataclass(frozen=True, slots=True)
class RawBulkLine:
    number: int
    name: str
    value: str
    category: str


@dataclass(frozen=True, slots=True)
class BulkProblem:
    number: int
    code: str


def normalized_bulk_text(text: str) -> str:
    return text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def check_bulk_limits(text: str) -> int:
    """Refuse oversized input before any line parsing; returns the non-blank line count."""
    if len(text.encode("utf-8")) > MAX_BULK_BYTES:
        raise BulkTooLarge
    lines = [line for line in normalized_bulk_text(text).split("\n") if line.strip()]
    if len(lines) > MAX_BULK_LINES:
        raise TooManyBulkLines
    return len(lines)


def split_bulk_lines(text: str) -> tuple[tuple[RawBulkLine, ...], tuple[BulkProblem, ...]]:
    """Split ``name; value; category`` lines; structural and text errors are returned per line.

    Blank lines are ignored. A line must have exactly two semicolons; names and categories use
    the same text rules as the forms. Amount parsing (locale) and category lookup happen in the
    service.
    """
    check_bulk_limits(text)
    raw_lines: list[RawBulkLine] = []
    problems: list[BulkProblem] = []
    for number, raw in enumerate(normalized_bulk_text(text).split("\n"), start=1):
        if not raw.strip():
            continue
        parts = raw.split(";")
        if len(parts) != 3:
            problems.append(BulkProblem(number, "wrong_format"))
            continue
        name_text, value_text, category_text = (part.strip() for part in parts)
        if not name_text:
            problems.append(BulkProblem(number, "name_required"))
            continue
        if not value_text:
            problems.append(BulkProblem(number, "value_required"))
            continue
        if not category_text:
            problems.append(BulkProblem(number, "category_required"))
            continue
        try:
            name = clean_item_name(name_text)
        except InvalidItemName:
            problems.append(BulkProblem(number, "invalid_name"))
            continue
        try:
            category = category_name(category_text)
        except InvalidCategoryName:
            problems.append(BulkProblem(number, "invalid_category"))
            continue
        raw_lines.append(RawBulkLine(number, name, value_text, category))
    return tuple(raw_lines), tuple(problems)


def duplicate_name_lines(lines: Sequence[RawBulkLine]) -> frozenset[int]:
    """Line numbers whose name (case-insensitive) already appeared earlier in the batch."""
    seen: set[str] = set()
    duplicates: set[int] = set()
    for line in lines:
        key = text_key(line.name)
        if key in seen:
            duplicates.add(line.number)
        seen.add(key)
    return frozenset(duplicates)
