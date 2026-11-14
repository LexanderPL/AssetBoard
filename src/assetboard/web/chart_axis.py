"""A shared nice-step money axis for the server-rendered charts.

The browser only looks up integer tick labels; every tick is formatted here with the project's
locale-aware money formatter. Keeping the helper beside the charts avoids a second rounding or
grouping rule in JavaScript.
"""

from __future__ import annotations

from collections.abc import Iterable

from assetboard.domain.money import Money
from assetboard.i18n.numbers import format_money


def nice_step(span: int) -> int:
    """A 1/2/5-times-a-power-of-ten step covering ``span`` in about five ticks."""
    raw = max(1, span // 5)
    magnitude = 10 ** (len(str(raw)) - 1)
    for factor in (1, 2, 5, 10):
        step = factor * magnitude
        if step >= raw:
            return step
    return 10 * magnitude


def money_axis(
    values: Iterable[int], currency: str, locale: str
) -> tuple[int, int, int, dict[str, str]]:
    """Integer bounds, tick step and formatted tick labels for an amount chart."""
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
    labels = {
        str(tick): format_money(Money(tick, currency), locale)
        for tick in range(low, high + 1, step)
    }
    return low, high, step, labels
