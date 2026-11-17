"""Home widget catalogue keys and pure preference helpers (F9.1).

The keys are stable: stored preferences and tests refer to them, and a later release adds its
widget to :data:`WIDGET_KEYS` (followed by the presentation registry in ``web.dashboard``). The
order here is the catalogue order, which is also the default Home order for a user who never
customised Home.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from typing import cast

WIDGET_KEYS: tuple[str, ...] = (
    "month-in-out",
    "month-expenses",
    "month-income",
    "month-plan",
    "upcoming",
    "net-worth",
    "portfolio",
    "portfolio-changes",
    "loans",
    "fire",
    "missing-data",
)

# Every shipped widget is on by default (F9.1: today's Home followed by each module's widget).
DEFAULT_WIDGET_KEYS: tuple[str, ...] = WIDGET_KEYS

_DIRECTIONS = frozenset({"up", "down"})


def clean_widgets(stored: object, known: Collection[str]) -> tuple[str, ...]:
    """The known widget keys of ``stored``, in order, without duplicates.

    Anything that is not a list of strings, an unknown key or a repeated key is ignored. A
    corrupted stored value therefore never breaks Home.
    """
    if not isinstance(stored, list | tuple):
        return ()
    known_set = set(known)
    result: list[str] = []
    for value in cast("Sequence[object]", stored):
        if isinstance(value, str) and value in known_set and value not in result:
            result.append(value)
    return tuple(result)


def complete_order(order: Sequence[str], known: Sequence[str]) -> tuple[str, ...]:
    """``order`` restricted to known keys, followed by the keys it did not mention."""
    known_set = set(known)
    result: list[str] = []
    for key in order:
        if key in known_set and key not in result:
            result.append(key)
    for key in known:
        if key not in result:
            result.append(key)
    return tuple(result)


def move_widget(
    order: Sequence[str], key: str, direction: str, known: Sequence[str]
) -> tuple[str, ...]:
    """Move ``key`` one place up or down in a complete order; no-op at an end or if unknown."""
    if direction not in _DIRECTIONS:
        raise ValueError("direction must be 'up' or 'down'")
    keys = list(complete_order(order, known))
    try:
        index = keys.index(key)
    except ValueError:
        return tuple(keys)
    target = index - 1 if direction == "up" else index + 1
    if 0 <= target < len(keys):
        keys[index], keys[target] = keys[target], keys[index]
    return tuple(keys)


def selected_widgets(
    order: Sequence[str], enabled: Iterable[str], known: Sequence[str]
) -> tuple[str, ...]:
    """The enabled known keys in ``order``, followed by enabled keys missing from ``order``."""
    known_set = set(known)
    enabled_set = {value for value in enabled if value in known_set}
    result: list[str] = []
    for key in complete_order(order, known):
        if key in enabled_set and key not in result:
            result.append(key)
    for key in known:
        if key in enabled_set and key not in result:
            result.append(key)
    return tuple(result)
