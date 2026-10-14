"""Category rules (F2.1): names, name keys, colours, ordering and the seed set. Pure: no I/O."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

MAX_CATEGORY_NAME_LENGTH = 80
# casefold() can lengthen a string (for example "ß" -> "ss"); the key column allows for it.
MAX_NAME_KEY_LENGTH = 255
_REJECTED_NAME_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Co", "Cn"})
_COLOR = re.compile(r"#[0-9a-f]{6}")


def _(message: str) -> str:
    """Marks a message for extraction; it is translated where it is used."""
    return message


class Kind(StrEnum):
    EXPENSE = "expense"
    INCOME = "income"


class Treatment(StrEnum):
    REGULAR = "regular"
    SAVINGS = "savings"
    TRANSFER = "transfer"


class InvalidCategoryName(ValueError):
    code = "invalid_name"


class InvalidColor(ValueError):
    code = "invalid_color"


def clean_name(value: str) -> str:
    """Trim, collapse inner whitespace, refuse control/invisible characters and bad lengths."""
    stripped = value.strip()
    if any(unicodedata.category(character) in _REJECTED_NAME_CATEGORIES for character in stripped):
        raise InvalidCategoryName
    cleaned = " ".join(stripped.split())
    if not cleaned or len(cleaned) > MAX_CATEGORY_NAME_LENGTH:
        raise InvalidCategoryName
    return cleaned


def name_key(name: str) -> str:
    """The case-insensitive identity of a name: NFKC, Unicode casefold, whitespace collapsed.

    SQLite's ``lower()`` folds ASCII only, so uniqueness uses this stored key instead.
    """
    folded = unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", name).casefold())
    return " ".join(folded.split())


def clean_color(value: str) -> str:
    """A colour as ``#rrggbb`` in lowercase (what ``<input type="color">`` sends)."""
    color = value.strip().lower()
    if not _COLOR.fullmatch(color):
        raise InvalidColor
    return color


Direction = Literal["up", "down"]


def move[T](items: Sequence[T], item: T, direction: Direction) -> list[T]:
    """``items`` with ``item`` swapped with its neighbour; unchanged at the edges."""
    order = list(items)
    index = order.index(item)
    other = index - 1 if direction == "up" else index + 1
    if 0 <= other < len(order):
        order[index], order[other] = order[other], order[index]
    return order


@dataclass(frozen=True, slots=True)
class SeedCategory:
    name: str  # message id; translated when the seed runs
    kind: Kind
    treatment: Treatment
    color: str


_PALETTE = (
    "#16a34a",
    "#f97316",
    "#e11d48",
    "#0ea5e9",
    "#8b5cf6",
    "#64748b",
    "#eab308",
    "#14b8a6",
    "#ec4899",
    "#2563eb",
    "#a16207",
    "#475569",
)


def _seed(names: Sequence[str], kind: Kind, treatment: Treatment) -> list[SeedCategory]:
    return [
        SeedCategory(name, kind, treatment, _PALETTE[index % len(_PALETTE)])
        for index, name in enumerate(names)
    ]


SEED: tuple[SeedCategory, ...] = (
    *_seed(
        (
            _("Food"),
            _("Eating out"),
            _("Gifts"),
            _("Health"),
            _("Housing"),
            _("Transport"),
            _("Cosmetics"),
            _("Hobbies"),
            _("Utilities"),
            _("Travel"),
            _("Entertainment"),
            _("Education"),
            _("Taxes"),
            _("Clothes"),
            _("Other"),
        ),
        Kind.EXPENSE,
        Treatment.REGULAR,
    ),
    *_seed((_("Savings"), _("Investments")), Kind.EXPENSE, Treatment.SAVINGS),
    *_seed(
        (_("Salary"), _("Bonus"), _("Interest"), _("Sale of items"), _("Other")),
        Kind.INCOME,
        Treatment.REGULAR,
    ),
    *_seed((_("Savings withdrawal"),), Kind.INCOME, Treatment.SAVINGS),
)
