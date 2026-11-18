"""The pure personal-sidebar rules (F9.12): order, visibility, moves and stored JSON."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from assetboard.domain.navigation import (
    CANONICAL_KEYS,
    DEFAULT_SIDEBAR,
    HOME_KEY,
    SidebarItem,
    move_sidebar,
    normalize_sidebar,
    serialize_sidebar,
)


def ids(items: tuple[SidebarItem, ...]) -> list[str]:
    return [item.key for item in items]


def visible_keys(items: tuple[SidebarItem, ...]) -> dict[str, bool]:
    return {item.key: item.visible for item in items}


def test_default_sidebar_is_the_canonical_order_with_everything_visible() -> None:
    assert ids(DEFAULT_SIDEBAR) == list(CANONICAL_KEYS)
    assert all(item.visible for item in DEFAULT_SIDEBAR)
    assert normalize_sidebar(None) == DEFAULT_SIDEBAR
    assert normalize_sidebar([]) == DEFAULT_SIDEBAR


def test_custom_order_and_visibility_are_kept() -> None:
    raw = [
        {"key": "home", "visible": True},
        {"key": "investments", "visible": True},
        {"key": "month", "visible": False},
        {"key": "history", "visible": True},
    ]
    items = normalize_sidebar(raw)
    assert ids(items) == [
        "home",
        "investments",
        "month",
        "history",
        "transactions",
        "reports",
        "upcoming",
        "recurring",
        "income",
        "taxes",
        "net_worth",
        "loans",
        "fire",
    ]
    assert visible_keys(items)["month"] is False
    assert visible_keys(items)["investments"] is True


def test_hidden_items_keep_their_position() -> None:
    raw = [
        {"key": "home", "visible": True},
        {"key": "loans", "visible": False},
        {"key": "month", "visible": True},
    ]
    items = normalize_sidebar(raw)
    assert ids(items)[:3] == ["home", "loans", "month"]
    assert visible_keys(items)["loans"] is False


@pytest.mark.parametrize(
    "raw",
    [
        [{"key": "month", "visible": True}, {"key": "home", "visible": False}],
        [{"key": "month", "visible": True}, {"key": "home", "visible": True}],
        [{"key": "month", "visible": True}, {"key": "month", "visible": False}],
    ],
)
def test_home_is_forced_first_and_visible(raw: list[dict[str, object]]) -> None:
    items = normalize_sidebar(raw)
    assert items[0] == SidebarItem(HOME_KEY, True)
    assert ids(items).count("home") == 1


def test_unknown_keys_are_ignored() -> None:
    raw = [
        {"key": "home", "visible": True},
        {"key": "not_a_page", "visible": True},
        {"key": "loans", "visible": False},
    ]
    items = normalize_sidebar(raw)
    assert "not_a_page" not in ids(items)
    assert ids(items)[:2] == ["home", "loans"]


def test_duplicate_keys_first_wins() -> None:
    raw = [
        {"key": "home", "visible": True},
        {"key": "loans", "visible": False},
        {"key": "loans", "visible": True},
    ]
    assert visible_keys(normalize_sidebar(raw))["loans"] is False


def test_missing_keys_are_appended_visible_in_canonical_order() -> None:
    items = normalize_sidebar([{"key": "history", "visible": False}])
    assert ids(items) == [
        "home",
        "history",
        *[k for k in CANONICAL_KEYS if k not in {"home", "history"}],
    ]
    assert visible_keys(items)["taxes"] is True


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        "",
        "home",
        [1, 2, 3],
        [{"key": 1, "visible": True}],
        [{"key": "home", "visible": "yes"}],
        [{"key": "home", "visible": 1}],
        [{"visible": True}],
        [["home", True]],
    ],
)
def test_malformed_values_fall_back_to_defaults(raw: object) -> None:
    assert normalize_sidebar(raw) == DEFAULT_SIDEBAR


@pytest.mark.parametrize(
    ("key", "direction", "expected"),
    [
        ("loans", "up", ["home", "month", "transactions", "history", "reports", "upcoming",
                         "recurring", "income", "taxes", "net_worth", "loans", "investments",
                         "fire"]),
        ("loans", "down", ["home", "month", "transactions", "history", "reports", "upcoming",
                           "recurring", "income", "taxes", "net_worth", "investments", "fire",
                           "loans"]),
        ("month", "up", list(CANONICAL_KEYS)),  # Home blocks the move to the top
        ("fire", "down", list(CANONICAL_KEYS)),  # already last
        ("unknown", "up", list(CANONICAL_KEYS)),
        ("loans", "sideways", list(CANONICAL_KEYS)),
    ],
)  # fmt: skip
def test_move_boundaries_and_unknown_keys(key: str, direction: str, expected: list[str]) -> None:
    assert ids(move_sidebar(DEFAULT_SIDEBAR, key, direction)) == expected


def test_home_never_moves() -> None:
    assert move_sidebar(DEFAULT_SIDEBAR, HOME_KEY, "up") == DEFAULT_SIDEBAR
    assert move_sidebar(DEFAULT_SIDEBAR, HOME_KEY, "down") == DEFAULT_SIDEBAR


def test_serialize_round_trips_through_normalize() -> None:
    items = normalize_sidebar(
        [
            {"key": "home", "visible": True},
            {"key": "fire", "visible": False},
            {"key": "income", "visible": True},
        ]
    )
    stored = serialize_sidebar(items)
    assert stored == [
        {"key": "home", "visible": True},
        {"key": "fire", "visible": False},
        {"key": "income", "visible": True},
        {"key": "month", "visible": True},
        {"key": "transactions", "visible": True},
        {"key": "history", "visible": True},
        {"key": "reports", "visible": True},
        {"key": "upcoming", "visible": True},
        {"key": "recurring", "visible": True},
        {"key": "taxes", "visible": True},
        {"key": "net_worth", "visible": True},
        {"key": "investments", "visible": True},
        {"key": "loans", "visible": True},
    ]
    assert normalize_sidebar(stored) == items


@given(
    keys=st.lists(st.sampled_from(CANONICAL_KEYS), max_size=30),
    hidden=st.sets(st.sampled_from(CANONICAL_KEYS)),
)
def test_normalize_is_idempotent_and_always_has_home_first(
    keys: list[str], hidden: set[str]
) -> None:
    raw = [{"key": key, "visible": key not in hidden} for key in keys]
    items = normalize_sidebar(raw)
    assert items[0] == SidebarItem(HOME_KEY, True)
    assert ids(items) == [key for key in ids(items) if key in CANONICAL_KEYS]
    assert len(ids(items)) == len(set(ids(items)))
    assert set(ids(items)) == set(CANONICAL_KEYS)
    assert normalize_sidebar(serialize_sidebar(items)) == items


@given(key=st.sampled_from(CANONICAL_KEYS), direction=st.sampled_from(["up", "down"]))
def test_a_move_keeps_every_destination_and_home_first(key: str, direction: str) -> None:
    moved = move_sidebar(DEFAULT_SIDEBAR, key, direction)
    assert moved[0] == SidebarItem(HOME_KEY, True)
    assert set(ids(moved)) == set(CANONICAL_KEYS)
    assert len(moved) == len(CANONICAL_KEYS)
