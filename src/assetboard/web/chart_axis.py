"""Shared nice-step axes for the server-rendered charts.

The browser only looks up integer tick labels; every tick is formatted here with the project's
locale-aware formatters. Keeping the helpers beside the charts avoids a second rounding or
grouping rule in JavaScript.
"""

from __future__ import annotations

from collections.abc import Iterable

from assetboard.domain.money import Money
from assetboard.i18n.numbers import format_money, format_rate_tenths


def nice_step(span: int) -> int:
    """A 1/2/5-times-a-power-of-ten step covering ``span`` in about five ticks."""
    raw = max(1, span // 5)
    magnitude = 10 ** (len(str(raw)) - 1)
    for factor in (1, 2, 5, 10):
        step = factor * magnitude
        if step >= raw:
            return step
    return 10 * magnitude


def _bounds(values: Iterable[int]) -> tuple[int, int, int]:
    """Nice integer bounds and step, always including zero."""
    numbers = list(values)
    low_value = min((0, *numbers))
    high_value = max((0, *numbers))
    if high_value == low_value:
        high_value = low_value + 1
    step = nice_step(high_value - low_value)
    low = (low_value // step) * step
    high = -((-high_value) // step) * step
    if high == low:
        high = low + step
    return low, high, step


def money_axis(
    values: Iterable[int], currency: str, locale: str
) -> tuple[int, int, int, dict[str, str]]:
    """Integer bounds, tick step and formatted tick labels for an amount chart."""
    low, high, step = _bounds(values)
    labels = {
        str(tick): format_money(Money(tick, currency), locale)
        for tick in range(low, high + 1, step)
    }
    return low, high, step, labels


def rate_axis(values: Iterable[int], locale: str) -> tuple[int, int, int, dict[str, str]]:
    """Bounds, tick step and labels for a chart whose values are tenths of a percent.

    The bounds always include zero and go below it when a value is negative, so a negative
    savings rate is drawn under the zero line rather than clipped.
    """
    low, high, step = _bounds(values)
    labels = {str(tick): format_rate_tenths(tick, locale) for tick in range(low, high + 1, step)}
    return low, high, step, labels
