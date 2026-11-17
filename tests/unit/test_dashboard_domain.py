"""Pure Home widget preference helpers (F9.1)."""

from __future__ import annotations

import pytest

from assetboard.domain.dashboard import (
    DEFAULT_WIDGET_KEYS,
    WIDGET_KEYS,
    clean_widgets,
    complete_order,
    move_widget,
    selected_widgets,
)

KNOWN = ("a", "b", "c", "d")


def test_default_keys_are_the_catalogue_in_order() -> None:
    assert DEFAULT_WIDGET_KEYS == WIDGET_KEYS
    assert len(set(WIDGET_KEYS)) == len(WIDGET_KEYS)
    assert WIDGET_KEYS[0] == "month-in-out"
    assert "missing-data" in WIDGET_KEYS


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (None, ()),
        ("not a list", ()),
        ({"a": True}, ()),
        ([], ()),
        (["a"], ("a",)),
        (["c", "a"], ("c", "a")),
        (["a", "a", "b"], ("a", "b")),
        (["unknown", "b", 3, None], ("b",)),
    ],
)
def test_clean_widgets_ignores_unknown_duplicate_and_non_string_values(
    stored: object, expected: tuple[str, ...]
) -> None:
    assert clean_widgets(stored, KNOWN) == expected


def test_complete_order_keeps_saved_order_and_appends_new_widgets() -> None:
    assert complete_order(("c", "a"), KNOWN) == ("c", "a", "b", "d")
    assert complete_order(("a", "a", "unknown"), KNOWN) == ("a", "b", "c", "d")
    assert complete_order((), KNOWN) == KNOWN


def test_move_widget_moves_one_place_and_stops_at_the_edges() -> None:
    assert move_widget(("a", "b", "c", "d"), "b", "up", KNOWN) == ("b", "a", "c", "d")
    assert move_widget(("a", "b", "c", "d"), "c", "down", KNOWN) == ("a", "b", "d", "c")
    assert move_widget(("a", "b", "c", "d"), "a", "up", KNOWN) == ("a", "b", "c", "d")
    assert move_widget(("a", "b", "c", "d"), "d", "down", KNOWN) == ("a", "b", "c", "d")
    assert move_widget(("a", "b", "c", "d"), "unknown", "up", KNOWN) == KNOWN
    assert move_widget(("a", "b"), "b", "up", KNOWN) == ("b", "a", "c", "d")


def test_move_widget_rejects_an_unknown_direction() -> None:
    with pytest.raises(ValueError):
        move_widget(("a", "b"), "a", "left", KNOWN)


def test_selected_widgets_keeps_only_enabled_keys_in_order() -> None:
    assert selected_widgets(("c", "a", "b", "d"), {"a", "c", "d"}, KNOWN) == (
        "c",
        "a",
        "d",
    )
    assert selected_widgets(("a", "b", "c", "d"), {"b", "unknown"}, KNOWN) == ("b",)
    assert selected_widgets(("b", "a"), {"a", "b"}, KNOWN) == ("b", "a")
    assert selected_widgets((), set(), KNOWN) == ()
