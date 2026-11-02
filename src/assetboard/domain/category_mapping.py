"""Category mapping rules for lone CSV imports (F4.3). Pure: no DB, no clock."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal
from uuid import UUID

from assetboard.domain.categories import (
    PALETTE,
    Kind,
    Treatment,
    clean_color,
    clean_name,
    name_key,
)

MappingAction = Literal["map", "create", "ignore"]
_DATA = Path(__file__).resolve().parents[1] / "data" / "savings-words.json"


class MappingError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class CategorySnapshot:
    id: UUID
    kind: str
    name: str
    name_key: str
    treatment: str
    color: str
    sort_order: int
    archived: bool


@dataclass(frozen=True, slots=True)
class SavedMapping:
    kind: str
    source_name_key: str
    action: str
    category_id: UUID | None


@dataclass(frozen=True, slots=True)
class MappingChoice:
    kind: str
    source_name_key: str
    action: MappingAction
    category_id: UUID | None = None
    create_name: str = ""
    create_treatment: str = "regular"
    create_color: str = ""


@dataclass(frozen=True, slots=True)
class ResolvedMapping:
    kind: str
    source_name: str
    source_name_key: str
    action: MappingAction
    category_id: UUID | None
    create_name: str = ""
    create_treatment: str = "regular"
    create_color: str = ""

    @property
    def is_create(self) -> bool:
        return self.action == "create"


@lru_cache(maxsize=1)
def savings_words() -> frozenset[str]:
    """The union of the English and Polish savings words, as name keys."""
    raw = json.loads(_DATA.read_text(encoding="utf-8"))
    return frozenset(
        name_key(word) for language in raw.values() for word in language if word.strip()
    )


def is_savings_name(source_name_key: str) -> bool:
    return source_name_key in savings_words()


def default_treatment(source_name_key: str) -> str:
    return Treatment.SAVINGS.value if is_savings_name(source_name_key) else Treatment.REGULAR.value


def next_color(kind: str, categories: list[CategorySnapshot], offset: int = 0) -> str:
    """The next palette colour for a new category of ``kind``, deterministically.

    Existing colours are avoided first; when all palette colours are used the palette cycles
    from the count of existing categories of that kind. ``offset`` is the create choice's
    position in source order.
    """
    used = {category.color.casefold() for category in categories if category.kind == kind}
    start = sum(category.kind == kind for category in categories) + offset
    for step in range(len(PALETTE)):
        color = PALETTE[(start + step) % len(PALETTE)]
        if color.casefold() not in used:
            return color
    return PALETTE[start % len(PALETTE)]


def _find(categories: list[CategorySnapshot], category_id: UUID) -> CategorySnapshot | None:
    return next((category for category in categories if category.id == category_id), None)


def _mapped(
    *,
    kind: str,
    source_name: str,
    source_name_key: str,
    category: CategorySnapshot | None,
) -> ResolvedMapping:
    if category is None:
        raise MappingError("mapping_category_missing")
    if category.kind != kind:
        raise MappingError("mapping_kind_mismatch")
    return ResolvedMapping(
        kind=kind,
        source_name=source_name,
        source_name_key=source_name_key,
        action="map",
        category_id=category.id,
    )


def _created(
    *,
    kind: str,
    source_name: str,
    source_name_key: str,
    create_name: str,
    create_treatment: str,
    create_color: str,
    categories: list[CategorySnapshot],
    created_ids: dict[tuple[str, str], UUID],
    create_offset: int = 0,
) -> ResolvedMapping:
    existing_id = created_ids.get((kind, source_name_key))
    if existing_id is not None:
        return ResolvedMapping(
            kind=kind,
            source_name=source_name,
            source_name_key=source_name_key,
            action="map",
            category_id=existing_id,
        )
    if not create_name.strip():
        raise MappingError("invalid_category_name")
    try:
        cleaned = clean_name(create_name)
    except ValueError:
        raise MappingError("invalid_category_name") from None
    key = name_key(cleaned)
    clash = next(
        (
            category
            for category in categories
            if category.kind == kind and category.name_key == key and not category.archived
        ),
        None,
    )
    if clash is not None:
        raise MappingError("category_exists")
    try:
        treatment = Treatment(create_treatment or "regular").value
    except ValueError:
        raise MappingError("invalid_treatment") from None
    try:
        color = (
            clean_color(create_color)
            if create_color
            else next_color(kind, categories, create_offset)
        )
    except ValueError:
        raise MappingError("invalid_color") from None
    return ResolvedMapping(
        kind=kind,
        source_name=source_name,
        source_name_key=source_name_key,
        action="create",
        category_id=None,
        create_name=cleaned,
        create_treatment=treatment,
        create_color=color,
    )


def resolve_mapping(
    *,
    kind: str,
    source_name: str,
    source_name_key: str,
    categories: list[CategorySnapshot],
    saved: SavedMapping | None,
    choice: MappingChoice | None,
    created_ids: dict[tuple[str, str], UUID] | None = None,
    create_offset: int = 0,
) -> ResolvedMapping:
    """Resolve one source category: choice -> saved mapping -> active same name -> create."""
    if kind not in {Kind.EXPENSE.value, Kind.INCOME.value}:
        raise MappingError("mapping_kind_mismatch")
    created_ids = created_ids or {}

    if choice is not None:
        if choice.action == "ignore":
            return ResolvedMapping(kind, source_name, source_name_key, "ignore", None)
        if choice.action == "map":
            if choice.category_id is None:
                raise MappingError("mapping_category_missing")
            return _mapped(
                kind=kind,
                source_name=source_name,
                source_name_key=source_name_key,
                category=_find(categories, choice.category_id),
            )
        return _created(
            kind=kind,
            source_name=source_name,
            source_name_key=source_name_key,
            create_name=choice.create_name,
            create_treatment=choice.create_treatment,
            create_color=choice.create_color,
            categories=categories,
            created_ids=created_ids,
            create_offset=create_offset,
        )

    if saved is not None:
        if saved.action == "ignore":
            return ResolvedMapping(kind, source_name, source_name_key, "ignore", None)
        return _mapped(
            kind=kind,
            source_name=source_name,
            source_name_key=source_name_key,
            category=_find(categories, saved.category_id) if saved.category_id else None,
        )

    same_name = next(
        (
            category
            for category in categories
            if category.kind == kind
            and category.name_key == source_name_key
            and not category.archived
        ),
        None,
    )
    if same_name is not None:
        return _mapped(
            kind=kind,
            source_name=source_name,
            source_name_key=source_name_key,
            category=same_name,
        )

    return _created(
        kind=kind,
        source_name=source_name,
        source_name_key=source_name_key,
        create_name=source_name,
        create_treatment=default_treatment(source_name_key),
        create_color=next_color(kind, categories, create_offset),
        categories=categories,
        created_ids=created_ids,
        create_offset=create_offset,
    )


def validate_create_uniqueness(mappings: list[ResolvedMapping]) -> None:
    """Two create choices in one batch may not share a kind and name key."""
    seen: set[tuple[str, str]] = set()
    for mapping in mappings:
        if mapping.action != "create":
            continue
        key = (mapping.kind, name_key(mapping.create_name))
        if key in seen:
            raise MappingError("duplicate_create_name")
        seen.add(key)


def saved_kind_for_name(source_name_key: str, saved: list[SavedMapping]) -> str | None:
    """The one kind a saved mapping has for this name, or ``None`` when absent/ambiguous."""
    kinds = {mapping.kind for mapping in saved if mapping.source_name_key == source_name_key}
    return next(iter(kinds)) if len(kinds) == 1 else None
