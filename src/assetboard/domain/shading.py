"""Amount shading: how strongly a Transactions page row is tinted (pure, no I/O).

Like a spreadsheet colour scale: a row's step depends on its absolute amount relative to the
largest amount of the same table (expense or income) and currency in the month. Integer
arithmetic only, so the thresholds are exact.
"""

from __future__ import annotations

from collections.abc import Iterable

from assetboard.domain.money import Money

# (numerator, denominator, step): the first threshold the amount reaches gives the step.
_THRESHOLDS = ((9, 10, 4), (3, 4, 3), (1, 2, 2), (1, 4, 1))
MAX_STEP = 4


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


def shade(amount: Money, top: dict[str, int]) -> int:
    """The step of ``amount`` against the maximum of its own currency in ``top``."""
    return shade_step(amount.amount_minor, top.get(amount.currency, 0))
