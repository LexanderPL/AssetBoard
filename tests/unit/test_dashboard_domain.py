"""Pure Home widget preference helpers (F9.1)."""

from __future__ import annotations

import pytest

from assetboard.domain.dashboard import (
    DEFAULT_NET_WORTH_RANGE,
    DEFAULT_PORTFOLIO_RANGE,
    DEFAULT_UPCOMING_LIMIT,
    DEFAULT_WIDGET_KEYS,
    NET_WORTH_RANGES,
    PORTFOLIO_RANGES,
    UPCOMING_LIMIT_MAX,
    UPCOMING_LIMIT_MIN,
    WIDGET_KEYS,
    clean_net_worth_range,
    clean_portfolio_range,
    clean_upcoming_limit,
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
    assert WIDGET_KEYS.index("portfolio") + 1 == WIDGET_KEYS.index("portfolio-history")
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


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (None, DEFAULT_UPCOMING_LIMIT),
        ("", DEFAULT_UPCOMING_LIMIT),
        ("not a number", DEFAULT_UPCOMING_LIMIT),
        (True, DEFAULT_UPCOMING_LIMIT),
        (-5, UPCOMING_LIMIT_MIN),
        (0, UPCOMING_LIMIT_MIN),
        ("1", 1),
        ("12", 12),
        (UPCOMING_LIMIT_MAX + 1, UPCOMING_LIMIT_MAX),
        ("999", UPCOMING_LIMIT_MAX),
    ],
)
def test_clean_upcoming_limit_parses_clamps_and_falls_back(stored: object, expected: int) -> None:
    assert clean_upcoming_limit(stored) == expected


@pytest.mark.parametrize("value", PORTFOLIO_RANGES)
def test_every_portfolio_range_is_accepted(value: str) -> None:
    assert clean_portfolio_range(value) == value


@pytest.mark.parametrize("stored", [None, "", "2y", "1Y", 6, True])
def test_clean_portfolio_range_falls_back_to_one_year(stored: object) -> None:
    assert clean_portfolio_range(stored) == DEFAULT_PORTFOLIO_RANGE == "1y"


@pytest.mark.parametrize("value", NET_WORTH_RANGES)
def test_every_net_worth_range_is_accepted(value: str) -> None:
    assert clean_net_worth_range(value) == value


@pytest.mark.parametrize("stored", [None, "", "2y", "1Y", "1m", 6, True])
def test_clean_net_worth_range_falls_back_to_one_year(stored: object) -> None:
    assert clean_net_worth_range(stored) == DEFAULT_NET_WORTH_RANGE == "1y"
