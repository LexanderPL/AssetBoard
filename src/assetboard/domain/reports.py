"""Ledger reports (F10.1): income, spending and savings per month or year.

Pure: no I/O, no clock. Amounts are integer minor units (``Money``) in the report's currency.
The treatment rules are F2.4's (docs/specs/00-overview.md §3.3): ``earned`` is income/regular,
``spent`` is expense/regular, ``saved`` is expense/savings; transfers are in nothing and a
withdrawal from savings (income/savings) is not earnings. ``net = earned - spent - saved``.

The series has **no gaps**: every month or year from ``start`` through ``end`` appears once, in
calendar order, with zeros when nothing happened. Entries outside the range or in another
currency are ignored here (the service counts the latter for its note); an entry whose category
is unknown raises ``ValueError``, as the month summary does.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from uuid import UUID

from assetboard.domain.categories import Kind, Treatment
from assetboard.domain.money import Money
from assetboard.domain.months import YearMonth


class ReportStep(StrEnum):
    MONTH = "month"
    YEAR = "year"


type PeriodKey = YearMonth | int


@dataclass(frozen=True, slots=True)
class ReportCategory:
    """The category fields the report needs (archived categories count like any other)."""

    id: UUID
    kind: str
    treatment: str


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One non-deleted transaction: its calendar day, category and amount."""

    day: date
    category_id: UUID
    amount: Money


@dataclass(frozen=True, slots=True)
class PeriodTotals:
    """One period's earned, spent and saved amounts and the resulting net.

    ``period`` is a ``YearMonth`` for ``ReportStep.MONTH`` and an ``int`` year for
    ``ReportStep.YEAR``. ``net = earned - spent - saved`` (withdrawals are not earnings).
    """

    period: PeriodKey
    earned: Money
    spent: Money
    saved: Money
    net: Money

    @property
    def empty(self) -> bool:
        """Whether nothing was earned, spent or saved in the period."""
        return (
            self.earned.amount_minor == 0
            and self.spent.amount_minor == 0
            and self.saved.amount_minor == 0
        )


@dataclass(frozen=True, slots=True)
class ReportTotals:
    earned: Money
    spent: Money
    saved: Money
    net: Money


def period_keys(start: date, end: date, step: ReportStep | str) -> tuple[PeriodKey, ...]:
    """Every period from ``start`` through ``end`` once, in calendar order."""
    wanted = ReportStep(step)
    if start > end:
        return ()
    first, last = YearMonth.from_date(start), YearMonth.from_date(end)
    if wanted is ReportStep.YEAR:
        return tuple(range(first.year, last.year + 1))
    keys: list[PeriodKey] = []
    current: YearMonth | None = first
    while current is not None and current <= last:
        keys.append(current)
        current = current.next()
    return tuple(keys)


def ledger_series(
    entries: Iterable[LedgerEntry],
    categories: Mapping[UUID, ReportCategory],
    *,
    start: date,
    end: date,
    step: ReportStep | str,
    currency: str,
) -> list[PeriodTotals]:
    """Income, spending and savings per month or year (see the module docstring).

    ``categories`` maps every category id a counted entry may use to its kind and treatment.
    Entries dated outside ``[start, end]`` or in another currency are ignored; the service
    proves and counts those before calling this function.
    """
    wanted = ReportStep(step)
    keys = period_keys(start, end, wanted)
    if not keys:
        return []
    earned: dict[PeriodKey, int] = {}
    spent: dict[PeriodKey, int] = {}
    saved: dict[PeriodKey, int] = {}
    for entry in entries:
        if entry.day < start or entry.day > end or entry.amount.currency != currency:
            continue
        category = categories.get(entry.category_id)
        if category is None:
            raise ValueError(f"unknown category {entry.category_id}")
        if category.treatment == Treatment.TRANSFER:
            continue
        if category.kind == Kind.EXPENSE:
            target = saved if category.treatment == Treatment.SAVINGS else spent
        elif category.kind == Kind.INCOME:
            if category.treatment == Treatment.SAVINGS:
                continue  # a withdrawal from savings is not earnings
            target = earned
        else:
            raise ValueError(f"unknown kind: {category.kind}")
        key: PeriodKey = (
            entry.day.year if wanted is ReportStep.YEAR else YearMonth.from_date(entry.day)
        )
        target[key] = target.get(key, 0) + entry.amount.amount_minor
    result: list[PeriodTotals] = []
    for key in keys:
        e, s, v = earned.get(key, 0), spent.get(key, 0), saved.get(key, 0)
        result.append(
            PeriodTotals(
                period=key,
                earned=Money(e, currency),
                spent=Money(s, currency),
                saved=Money(v, currency),
                net=Money(e - s - v, currency),
            )
        )
    return result


def totals_of(series: Iterable[PeriodTotals], *, currency: str) -> ReportTotals:
    """The exact integer sum of a series, as the table's total row."""
    earned = spent = saved = 0
    for period in series:
        earned += period.earned.amount_minor
        spent += period.spent.amount_minor
        saved += period.saved.amount_minor
    return ReportTotals(
        earned=Money(earned, currency),
        spent=Money(spent, currency),
        saved=Money(saved, currency),
        net=Money(earned - spent - saved, currency),
    )


__all__ = [
    "LedgerEntry",
    "PeriodKey",
    "PeriodTotals",
    "ReportCategory",
    "ReportStep",
    "ReportTotals",
    "ledger_series",
    "period_keys",
    "totals_of",
]
