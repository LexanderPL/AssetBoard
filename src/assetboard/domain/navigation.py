"""The personal sidebar (F9.12): which destinations appear and in which order (pure).

The preference is a per-user, per-workspace-independent list stored on ``User.sidebar_items``
as JSON. It is navigation only: a hidden page stays reachable by its address, and permissions
are never derived from it. ``None`` means "never customised" and follows the canonical order,
so a destination added later appears at its canonical position; a customised list appends new
destinations at the end, visible, until the user arranges them.

Malformed stored data never raises: every entry that is not a mapping with a known string key
and a boolean ``visible`` is dropped. Home is always first and shown.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

# The sidebar's order in the canonical (never customised) list, after F2.7.
CANONICAL_KEYS: tuple[str, ...] = (
    "home",
    "month",
    "transactions",
    "history",
    "upcoming",
    "recurring",
    "income",
    "taxes",
    "net_worth",
    "investments",
    "loans",
    "fire",
)
HOME_KEY = "home"
DIRECTIONS = frozenset({"up", "down"})


@dataclass(frozen=True, slots=True)
class SidebarItem:
    """One destination: its stable key and whether it is shown in the sidebar."""

    key: str
    visible: bool


DEFAULT_SIDEBAR: tuple[SidebarItem, ...] = tuple(SidebarItem(key, True) for key in CANONICAL_KEYS)


def normalize_sidebar(raw: object) -> tuple[SidebarItem, ...]:
    """A valid list for ``raw`` (stored JSON), never raising.

    Unknown keys and malformed entries are ignored; the first entry of a duplicated key wins.
    Missing known keys are appended visible in canonical order. Home is forced first and
    visible. Anything that is not a list of mappings gives the default list.
    """
    if not isinstance(raw, list):
        return DEFAULT_SIDEBAR
    entries = cast(list[object], raw)
    seen: set[str] = set()
    items: list[SidebarItem] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        mapping = cast(Mapping[object, object], entry)
        key = mapping.get("key")
        visible = mapping.get("visible")
        if not isinstance(key, str) or key not in CANONICAL_KEYS:
            continue
        if not isinstance(visible, bool) or key in seen:
            continue
        seen.add(key)
        if key != HOME_KEY:
            items.append(SidebarItem(key, visible))
    for key in CANONICAL_KEYS:
        if key not in seen and key != HOME_KEY:
            items.append(SidebarItem(key, True))
    return (SidebarItem(HOME_KEY, True), *items)


def move_sidebar(
    items: tuple[SidebarItem, ...], key: str, direction: str
) -> tuple[SidebarItem, ...]:
    """Move ``key`` one place up or down; Home and boundary moves are no-ops."""
    if key == HOME_KEY or direction not in DIRECTIONS:
        return items
    positions = {item.key: index for index, item in enumerate(items)}
    index = positions.get(key)
    if index is None:
        return items
    target = index - 1 if direction == "up" else index + 1
    if target < 0 or target >= len(items) or items[target].key == HOME_KEY:
        return items
    moved = list(items)
    moved[index], moved[target] = moved[target], moved[index]
    return tuple(moved)


def serialize_sidebar(items: tuple[SidebarItem, ...]) -> list[dict[str, Any]]:
    """The JSON-ready stored form."""
    return [{"key": item.key, "visible": item.visible} for item in items]
