"""Ledger reports (F10.1–F10.3): income, spending and savings per month or year, the
savings-rate trend, and spending by category over time.

Pure: no I/O, no clock. Amounts are integer minor units (``Money``) in the report's currency.
The treatment rules are F2.4's (docs/specs/00-overview.md §3.3): ``earned`` is income/regular,
``spent`` is expense/regular, ``saved`` is expense/savings; transfers are in nothing and a
withdrawal from savings (income/savings) is not earnings. ``net = earned - spent - saved``.

The series has **no gaps**: every month or year from ``start`` through ``end`` appears once, in
calendar order, with zeros when nothing happened. Entries outside the range or in another
currency are ignored here (the service counts the latter for its note); an entry whose category
is unknown raises ``ValueError``, as the month summary does.

**Savings rate (F10.2).** The report's rate is ``saved / earned``, the same measure as F2.4's
Month-page KPI: the share of earnings moved to savings and investments. It is kept as integer
basis points and tenths of a percent, each rounded **once** from the exact ratio with F2.4's
half-away-from-zero rule; a period with ``earned = 0`` has no rate. A yearly rate is computed
from the year's summed integers, never averaged from monthly rates.

**Expenses by category (F10.3).** ``category_series`` splits the same ``spent`` (and, with
``include_savings``, ``saved``) figures by regular expense category per period. The rows are
ordered by their range total, largest first; the service keeps the eight largest and lets
``top_categories`` combine the rest into one unnamed "Other" row. A category filter selects
exactly the chosen categories (zeros included). Archived categories are ordinary data here:
their history keeps counting and the caller supplies their current name and colour.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from uuid import UUID

from assetboard.domain.money import Money
from assetboard.domain.months import YearMonth
from assetboard.domain.summary import block_of, savings_rate_bp, savings_rate_tenths


class ReportStep(StrEnum):
    MONTH = "month"
    YEAR = "year"


type PeriodKey = YearMonth | int


@dataclass(frozen=True, slots=True)
class ReportCategory:
    """The category fields the reports need (archived categories count like any other).

    ``name`` and ``color`` are display data: the category reports echo them into their rows.
    The ledger series (F10.1/F10.2) needs only the kind and treatment.
    """

    id: UUID
    kind: str
    treatment: str
    name: str = ""
    color: str = ""


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One non-deleted transaction: its calendar day, category and amount."""

    day: date
    category_id: UUID
    amount: Money


@dataclass(frozen=True, slots=True)
class SavingsRate:
    """A report savings rate, rounded once in each representation.

    ``bp`` is basis points (1 bp = 0.01%), ``tenths`` tenths of a percent (for the "43.3%"
    display). Both are ``None`` when nothing was earned, because the ratio is undefined.
    """

    bp: int | None
    tenths: int | None

    @property
    def available(self) -> bool:
        return self.bp is not None


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

    @property
    def savings_rate(self) -> SavingsRate:
        """``saved / earned`` from this period's exact integer sums."""
        return savings_rate(self.earned.amount_minor, self.saved.amount_minor)


@dataclass(frozen=True, slots=True)
class ReportTotals:
    earned: Money
    spent: Money
    saved: Money
    net: Money


TOP_CATEGORIES = 8


@dataclass(frozen=True, slots=True)
class CategorySpend:
    """One category's spending per period.

    ``category_id`` is ``None`` for the combined remainder that ``top_categories`` builds; the
    UI names that row "Other" and gives it a theme colour. ``name`` and ``color`` are copied from
    ``ReportCategory`` (empty on the remainder), so the view model needs no second lookup.
    """

    category_id: UUID | None
    name: str
    color: str
    amounts: tuple[Money, ...]
    total: Money

    @property
    def other(self) -> bool:
        """Whether this is the unnamed combined remainder row."""
        return self.category_id is None


@dataclass(frozen=True, slots=True)
class CategorySeries:
    """Spending per category and period, one row per category, in range-total order.

    ``periods`` has no gaps, as in ``ledger_series``. ``totals`` is the exact sum of the rows for
    each period (equal to ``spent`` and, with savings, ``saved`` when nothing was filtered out);
    ``total`` is the range grand total.
    """

    periods: tuple[PeriodKey, ...]
    rows: tuple[CategorySpend, ...]
    totals: tuple[Money, ...]
    total: Money


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


_LEDGER_BLOCKS = frozenset({"expense", "saved", "income"})


@dataclass(slots=True)
class _Accumulated:
    """The integer sums of one pass over the entries; the series are built from this."""

    earned: dict[PeriodKey, int]
    spent: dict[PeriodKey, int]
    saved: dict[PeriodKey, int]
    amounts: dict[UUID, list[int]]


def _period_key(day: date, step: ReportStep) -> PeriodKey:
    return day.year if step is ReportStep.YEAR else YearMonth.from_date(day)


def _accumulate(
    entries: Iterable[LedgerEntry],
    categories: Mapping[UUID, ReportCategory],
    *,
    keys: tuple[PeriodKey, ...],
    step: ReportStep,
    start: date,
    end: date,
    currency: str,
    include_savings: bool,
    selected: Collection[UUID] | None,
    with_ledger: bool,
    with_categories: bool,
) -> _Accumulated:
    """One loop over the entries for the ledger totals and/or the category breakdown.

    The service uses both flags together, so a page request still reads and walks the ledger
    once. ``block_of`` runs once per entry, not once per report.
    """
    position_of = {key: index for index, key in enumerate(keys)}
    wanted_blocks = {"expense", "saved"} if include_savings else {"expense"}
    wanted_ids = set(selected) if selected else None
    state = _Accumulated({}, {}, {}, {})
    for entry in entries:
        if entry.day < start or entry.day > end or entry.amount.currency != currency:
            continue
        category = categories.get(entry.category_id)
        if category is None:
            raise ValueError(f"unknown category {entry.category_id}")
        block = block_of(category.kind, category.treatment)
        ledger_row = with_ledger and block in _LEDGER_BLOCKS
        category_row = (
            with_categories
            and block in wanted_blocks
            and (wanted_ids is None or category.id in wanted_ids)
        )
        if not (ledger_row or category_row):
            continue
        key = _period_key(entry.day, step)
        position = position_of.get(key)
        if position is None:
            continue
        if ledger_row:
            # ``block`` is in _LEDGER_BLOCKS here, so it is expense, saved or income.
            target = state.earned
            if block == "expense":
                target = state.spent
            elif block == "saved":
                target = state.saved
            target[key] = target.get(key, 0) + entry.amount.amount_minor
        if category_row:
            values = state.amounts.get(category.id)
            if values is None:
                values = state.amounts[category.id] = [0] * len(keys)
            values[position] += entry.amount.amount_minor
    return state


def _period_totals(
    state: _Accumulated, keys: tuple[PeriodKey, ...], currency: str
) -> list[PeriodTotals]:
    result: list[PeriodTotals] = []
    for key in keys:
        e = state.earned.get(key, 0)
        s = state.spent.get(key, 0)
        v = state.saved.get(key, 0)
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
    state = _accumulate(
        entries,
        categories,
        keys=keys,
        step=wanted,
        start=start,
        end=end,
        currency=currency,
        include_savings=False,
        selected=None,
        with_ledger=True,
        with_categories=False,
    )
    return _period_totals(state, keys, currency)


def _selected_rows(
    amounts: Mapping[UUID, list[int]],
    categories: Mapping[UUID, ReportCategory],
    *,
    keys: tuple[PeriodKey, ...],
    currency: str,
    include_savings: bool,
    selected: Collection[UUID] | None,
) -> list[CategorySpend]:
    """The category rows of one range, zeros included for explicitly selected categories."""
    wanted_blocks = {"expense", "saved"} if include_savings else {"expense"}
    wanted_ids = set(selected) if selected else None
    rows: list[CategorySpend] = []
    for category_id, values in amounts.items():
        if wanted_ids is not None and category_id not in wanted_ids:
            continue
        if wanted_ids is None and not any(values):
            continue
        category = categories[category_id]
        rows.append(
            CategorySpend(
                category_id=category_id,
                name=category.name,
                color=category.color,
                amounts=tuple(Money(value, currency) for value in values),
                total=Money(sum(values), currency),
            )
        )
    if wanted_ids is not None:
        # A chosen category with no entries still gets a row of zeros: the chart shows exactly
        # the chosen categories, not a silently smaller subset.
        for category_id in wanted_ids:
            if category_id in amounts:
                continue
            category = categories.get(category_id)
            if category is None or block_of(category.kind, category.treatment) not in wanted_blocks:
                continue
            zeros = tuple(Money(0, currency) for _ in keys)
            rows.append(
                CategorySpend(
                    category_id=category_id,
                    name=category.name,
                    color=category.color,
                    amounts=zeros,
                    total=Money(0, currency),
                )
            )
    rows.sort(key=lambda row: (-row.total.amount_minor, row.name.casefold(), str(row.category_id)))
    return rows


def _category_result(
    rows: Iterable[CategorySpend],
    keys: tuple[PeriodKey, ...],
    currency: str,
) -> CategorySeries:
    """The category series from already-summed rows and the full period list."""
    materialized = tuple(rows)
    totals = tuple(
        Money(sum(row.amounts[index].amount_minor for row in materialized), currency)
        for index in range(len(keys))
    )
    return CategorySeries(
        periods=keys,
        rows=materialized,
        totals=totals,
        total=Money(sum(total.amount_minor for total in totals), currency),
    )


def category_series(
    entries: Iterable[LedgerEntry],
    categories: Mapping[UUID, ReportCategory],
    *,
    start: date,
    end: date,
    step: ReportStep | str,
    currency: str,
    include_savings: bool = False,
    selected: Collection[UUID] | None = None,
) -> CategorySeries:
    """Spending per regular expense category per period (F10.3).

    Every counted entry's category must be in ``categories`` (an unknown one raises
    ``ValueError``, as in ``ledger_series``). Only ``expense/regular`` counts, plus
    ``expense/savings`` when ``include_savings`` is true; income, transfers and withdrawals from
    savings never count. Entries outside the range or in another currency are ignored.

    Without ``selected`` the rows are all categories with a non-zero total in the range. With a
    non-empty ``selected`` the rows are exactly the chosen categories that exist and qualify
    (zeros included); unknown ids and categories of the other treatment are ignored, so a stale
    filter never breaks the report.
    """
    wanted = ReportStep(step)
    keys = period_keys(start, end, wanted)
    if not keys:
        return CategorySeries((), (), (), Money(0, currency))
    state = _accumulate(
        entries,
        categories,
        keys=keys,
        step=wanted,
        start=start,
        end=end,
        currency=currency,
        include_savings=include_savings,
        selected=selected,
        with_ledger=False,
        with_categories=True,
    )
    rows = _selected_rows(
        state.amounts,
        categories,
        keys=keys,
        currency=currency,
        include_savings=include_savings,
        selected=selected,
    )
    return _category_result(rows, keys, currency)


def ledger_and_category_series(
    entries: Iterable[LedgerEntry],
    categories: Mapping[UUID, ReportCategory],
    *,
    start: date,
    end: date,
    step: ReportStep | str,
    currency: str,
    include_savings: bool = False,
) -> tuple[list[PeriodTotals], CategorySeries]:
    """Both F10.1's ledger series and F10.3's full category series from one pass.

    The Reports page needs both sections from the same read. Sharing the loop halves the
    per-entry work (and coverage instrumentation) compared with calling ``ledger_series`` and
    ``category_series`` one after the other. The category series is unfiltered here; a category
    filter narrows it afterwards with ``select_categories``.
    """
    wanted = ReportStep(step)
    keys = period_keys(start, end, wanted)
    if not keys:
        return [], CategorySeries((), (), (), Money(0, currency))
    state = _accumulate(
        entries,
        categories,
        keys=keys,
        step=wanted,
        start=start,
        end=end,
        currency=currency,
        include_savings=include_savings,
        selected=None,
        with_ledger=True,
        with_categories=True,
    )
    rows = _selected_rows(
        state.amounts,
        categories,
        keys=keys,
        currency=currency,
        include_savings=include_savings,
        selected=None,
    )
    return _period_totals(state, keys, currency), _category_result(rows, keys, currency)


def select_categories(
    series: CategorySeries,
    categories: Mapping[UUID, ReportCategory],
    *,
    include_savings: bool = False,
    selected: Collection[UUID],
) -> CategorySeries:
    """Narrow an already-computed category series to exactly the chosen categories.

    No second pass over the entries: the rows carry their per-period sums. Chosen categories
    without spending still get a zero row; unknown ids and categories that do not fit the option
    are ignored. The result's totals are the chosen categories' sums.
    """
    wanted_ids = set(selected)
    if not wanted_ids:
        return series
    amounts = {
        row.category_id: [amount.amount_minor for amount in row.amounts]
        for row in series.rows
        if row.category_id is not None
    }
    rows = _selected_rows(
        amounts,
        categories,
        keys=series.periods,
        currency=series.total.currency,
        include_savings=include_savings,
        selected=wanted_ids,
    )
    return _category_result(rows, series.periods, series.total.currency)


def top_categories(series: CategorySeries, *, limit: int = TOP_CATEGORIES) -> CategorySeries:
    """Keep the ``limit`` rows with the largest range totals and combine the rest as "Other".

    The remainder has no category id, name or colour; the view names it with the translated
    "Other" label and a theme colour. The per-period and range totals are unchanged, so the
    chart's stacked column totals still equal the ledger's spending.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if len(series.rows) <= limit:
        return series
    kept = series.rows[:limit]
    remainder = series.rows[limit:]
    amounts = tuple(
        Money(
            sum(row.amounts[index].amount_minor for row in remainder),
            series.total.currency,
        )
        for index in range(len(series.periods))
    )
    other = CategorySpend(
        category_id=None,
        name="",
        color="",
        amounts=amounts,
        total=Money(sum(amount.amount_minor for amount in amounts), series.total.currency),
    )
    return CategorySeries(
        periods=series.periods,
        rows=(*kept, other),
        totals=series.totals,
        total=series.total,
    )


REPORT_ROLLING_MONTHS = 12


def savings_rate(earned: int, saved: int) -> SavingsRate:
    """``saved / earned``, each representation rounded once (F2.4's rule).

    ``None`` in both fields when ``earned`` is 0. The rounding lives in F2.4's
    ``savings_rate_bp`` / ``savings_rate_tenths`` (shared with the month summary), so the
    report and the Month KPI use the same definition: the share of earnings moved to savings
    and investments, not money merely left over.
    """
    return SavingsRate(
        bp=savings_rate_bp(earned, saved),
        tenths=savings_rate_tenths(earned, saved),
    )


def rolling_savings_rates(
    series: Sequence[PeriodTotals], *, months: int = REPORT_ROLLING_MONTHS
) -> tuple[SavingsRate | None, ...]:
    """The rolling ``months``-period rate at each point of a **monthly** series.

    Every point is computed from that window's summed integers, never by averaging the monthly
    rates. The first ``months - 1`` points are ``None`` (not enough history); afterwards a
    ``None`` rate means the window earned nothing. The running sums make this one pass.
    """
    if months < 1:
        raise ValueError("months must be at least 1")
    result: list[SavingsRate | None] = []
    earned = saved = 0
    for index, period in enumerate(series):
        earned += period.earned.amount_minor
        saved += period.saved.amount_minor
        if index >= months:
            leaving = series[index - months]
            earned -= leaving.earned.amount_minor
            saved -= leaving.saved.amount_minor
        result.append(savings_rate(earned, saved) if index >= months - 1 else None)
    return tuple(result)


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
    "REPORT_ROLLING_MONTHS",
    "TOP_CATEGORIES",
    "CategorySeries",
    "CategorySpend",
    "LedgerEntry",
    "PeriodKey",
    "PeriodTotals",
    "ReportCategory",
    "ReportStep",
    "ReportTotals",
    "SavingsRate",
    "category_series",
    "ledger_and_category_series",
    "ledger_series",
    "period_keys",
    "rolling_savings_rates",
    "savings_rate",
    "select_categories",
    "top_categories",
    "totals_of",
]
