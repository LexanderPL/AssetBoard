"""Amount shading: how strongly a Transactions page row is tinted (pure, no I/O).

Only genuinely large entries stand out. A month needs at least three entries of the same
direction and currency before anything is compared; only amounts at or above twice their
median can be tinted. Eligible rows then follow a spreadsheet colour scale relative to the
largest amount of that direction and currency. Integer arithmetic only, so the thresholds are
exact.
"""

from __future__ import annotations

from collections.abc import Iterable

from assetboard.domain.money import Money

# (numerator, denominator, step): the first threshold the amount reaches gives the step.
_THRESHOLDS = ((9, 10, 4), (3, 4, 3), (1, 2, 2), (1, 4, 1))
MAX_STEP = 4
# Fewer entries than this in one direction and currency: no tint at all.
MIN_GROUP_SIZE = 3
# An entry is eligible for a tint from this multiple of the group's median amount.
OUTLIER_MULTIPLE = 2


def shade_step(amount_minor: int, max_minor: int) -> int:
    """The tint step 0-4 of ``|amount_minor|`` against ``|max_minor|``.

    Under 25% of the maximum: 0 (no tint); from 25%, 50%, 75% and 90%: 1, 2, 3 and 4. A zero
    maximum gives 0.
    """
    amount, top = abs(amount_minor), abs(max_minor)
    if top == 0:
        return 0
    for numerator, denominator, step in _THRESHOLDS:
        if amount * denominator >= top * numerator:
            return step
    return 0


def maxima(amounts: Iterable[Money]) -> dict[str, int]:
    """The largest absolute amount in minor units per currency."""
    result: dict[str, int] = {}
    for money in amounts:
        value = abs(money.amount_minor)
        if value > result.get(money.currency, -1):
            result[money.currency] = value
    return result


def large_gates(amounts: Iterable[Money]) -> dict[str, int]:
    """The smallest tintable amount per currency: twice the median absolute amount.

    A currency with fewer than ``MIN_GROUP_SIZE`` amounts has no gate and gets no tints. The
    gate is exact integer arithmetic: for an even count it is the sum of the two middle
    values, which is twice their average, so no half minor units are rounded.
    """
    grouped: dict[str, list[int]] = {}
    for money in amounts:
        grouped.setdefault(money.currency, []).append(abs(money.amount_minor))
    gates: dict[str, int] = {}
    for currency, values in grouped.items():
        if len(values) < MIN_GROUP_SIZE:
            continue
        values.sort()
        middle = len(values) // 2
        if len(values) % 2:
            gates[currency] = OUTLIER_MULTIPLE * values[middle]
        else:
            gates[currency] = values[middle - 1] + values[middle]
    return gates


def shade(amount: Money, top: dict[str, int], gates: dict[str, int]) -> int:
    """The step of ``amount`` against the maximum and median gate of its own currency.

    An amount below its currency's gate, or a currency with no gate, is never tinted.
    """
    gate = gates.get(amount.currency)
    if gate is None or abs(amount.amount_minor) < gate:
        return 0
    return shade_step(amount.amount_minor, top.get(amount.currency, 0))
