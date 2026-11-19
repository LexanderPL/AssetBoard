"""The Reports page's data (F10.1–F10.3): filters, one ledger read, the income/expense series
and the category breakdown.

Every member, viewers included, may read the page. The service resolves the year range, the
granularity, the category filter and the savings option, reads the workspace's categories, the
currencies present in the range and one column-only stream of transactions through the scoped
``TransactionRepository``, then runs the pure ``domain.reports`` functions. Amounts stay integer
minor units; no SQL aggregate or ``DecimalType`` column is involved and nothing is converted
between currencies.

Open and closed months count alike. Rows dated after today (in the workspace's time zone) are
never charted; their count is returned for the page's note. Rows in another currency than the
selected one are left out and counted too (Phase A never converts).

The category filter is repeatable and bookmarkable (``?category=<id>&category=<id>``). Unknown
ids and ids from another workspace are ignored; if none of the requested ids belong to an
eligible expense category, the chart falls back to its default (the largest eight and Other).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.repositories import (
    BudgetLineRepository,
    CategoryRepository,
    CurrentWorkspaceRepository,
    TransactionRepository,
)
from assetboard.domain.money import Money
from assetboard.domain.months import MAX_YEAR, MIN_YEAR, YearMonth
from assetboard.domain.reports import (
    TREND_MONTHS_DEFAULT,
    CategorySeries,
    CategoryTrends,
    LedgerEntry,
    PeriodTotals,
    PlanAccuracy,
    PlannedMonth,
    ReportCategory,
    ReportStep,
    ReportTotals,
    YearOverYear,
    category_series,
    ledger_and_category_series,
    plan_accuracy,
    rolling_category_averages,
    select_categories,
    top_categories,
    totals_of,
    year_grid,
    year_over_year,
)
from assetboard.domain.summary import block_of
from assetboard.services.context import Context, Role, require_role

DEFAULT_YEARS = 3
MAX_YEARS = 30
MAX_MONTHLY_ONLY_YEARS = 3
_YEAR = re.compile(r"[0-9]{4}")
_CURRENCY = re.compile(r"[A-Z]{3}")


class FilterError(StrEnum):
    """A filter problem the route renders beside its field; no chart is drawn."""

    INVALID_YEAR = "invalid_year"
    FROM_AFTER_TO = "from_after_to"
    TOO_MANY_YEARS = "too_many_years"


@dataclass(frozen=True, slots=True)
class ReportFilters:
    """The resolved form values (``*_text`` keeps what the visitor typed)."""

    start_year: int
    end_year: int
    step: ReportStep
    currency: str
    from_text: str
    to_text: str
    category_ids: tuple[UUID, ...] = ()
    savings: bool = False
    year: int = 0
    trend_months: int = TREND_MONTHS_DEFAULT
    yoy_category_id: UUID | None = None
    errors: Mapping[str, FilterError] = field(default_factory=dict[str, FilterError])


@dataclass(frozen=True, slots=True)
class CategoryOption:
    """One expense category the category chart can show or filter by."""

    id: UUID
    name: str
    archived: bool


@dataclass(frozen=True, slots=True)
class ReportsData:
    filters: ReportFilters
    series: tuple[PeriodTotals, ...]
    totals: ReportTotals
    category_series: CategorySeries
    category_options: tuple[CategoryOption, ...]
    year_options: tuple[int, ...]
    year_grid: CategorySeries
    category_trends: CategoryTrends
    yoy: YearOverYear | None
    plan_accuracy: PlanAccuracy
    yoy_category_options: tuple[CategoryOption, ...]
    available_currencies: tuple[str, ...]
    show_currency_selector: bool
    future_count: int
    other_currency_count: int
    has_data: bool
    today: date
    series_start: date | None
    series_end: date | None

    @property
    def category_filter_active(self) -> bool:
        """Whether a valid category filter narrowed the category chart."""
        return bool(self.filters.category_ids)


@dataclass(slots=True)
class _StreamStats:
    future: int = 0
    other_currency: int = 0
    counted: int = 0


def _parse_year(raw: str) -> int | None:
    text = raw.strip()
    if _YEAR.fullmatch(text) is None:
        return None
    year = int(text)
    return year if MIN_YEAR <= year <= MAX_YEAR else None


def _default_step(start_year: int, end_year: int) -> ReportStep:
    years = end_year - start_year + 1
    return ReportStep.MONTH if years <= MAX_MONTHLY_ONLY_YEARS else ReportStep.YEAR


def _resolve_filters(
    *,
    raw_from: str,
    raw_to: str,
    raw_step: str,
    raw_year: str,
    raw_trend: str,
    today: date,
) -> ReportFilters:
    current_year = today.year
    default_from = current_year - (DEFAULT_YEARS - 1)
    errors: dict[str, FilterError] = {}
    start_year = _parse_year(raw_from) if raw_from.strip() else default_from
    end_year = _parse_year(raw_to) if raw_to.strip() else current_year
    if start_year is None:
        errors["from"] = FilterError.INVALID_YEAR
        start_year = default_from
    if end_year is None:
        errors["to"] = FilterError.INVALID_YEAR
        end_year = current_year
    if not errors:
        if start_year > end_year:
            errors["to"] = FilterError.FROM_AFTER_TO
        elif end_year - start_year + 1 > MAX_YEARS:
            errors["to"] = FilterError.TOO_MANY_YEARS
    defaulted = _default_step(start_year, end_year)
    step = ReportStep(raw_step) if raw_step in ("month", "year") else defaulted
    # The section year is not a range filter: an unknown or malformed value is ignored and the
    # report's end year (but never a future one) is used, so an old bookmark still opens.
    default_year = min(end_year, current_year)
    year = _parse_year(raw_year) if raw_year.strip() else default_year
    if year is None:
        year = default_year
    trend_months = int(raw_trend) if raw_trend in ("3", "12") else TREND_MONTHS_DEFAULT
    return ReportFilters(
        start_year=start_year,
        end_year=end_year,
        step=step,
        currency="",
        from_text=raw_from.strip() or str(start_year),
        to_text=raw_to.strip() or str(end_year),
        year=year,
        trend_months=trend_months,
        errors=errors,
    )


def _select_currency(requested: str, available: tuple[str, ...], base_currency: str) -> str:
    wanted = requested.strip().upper()
    if _CURRENCY.fullmatch(wanted) and wanted in available:
        return wanted
    return base_currency


def _parse_category_ids(raw: Iterable[str]) -> tuple[UUID, ...]:
    """The distinct UUIDs among the repeatable ``category`` parameters (junk is ignored)."""
    ids: list[UUID] = []
    for value in raw:
        try:
            category_id = UUID(value)
        except ValueError:
            continue
        if category_id not in ids:
            ids.append(category_id)
    return tuple(ids)


def _category_blocks(include_savings: bool) -> set[str]:
    return {"expense", "saved"} if include_savings else {"expense"}


def _resolve_yoy_category(
    raw: str, categories: Mapping[UUID, ReportCategory], *, include_savings: bool
) -> UUID | None:
    """The single year-over-year category, or ``None`` for the whole category series.

    The value is one ``yoy_category`` parameter (not the repeatable ``category`` filter). An
    unknown, malformed or other-workspace id, or a category that does not fit the current
    savings option, is ignored and falls back to the total.
    """
    try:
        category_id = UUID(raw)
    except ValueError:
        return None
    category = categories.get(category_id)
    if category is None:
        return None
    if block_of(category.kind, category.treatment) not in _category_blocks(include_savings):
        return None
    return category.id


def _year_options(today: date, filters: ReportFilters) -> tuple[int, ...]:
    """The year selector's choices: the last 30 years, the report range and the selected year."""
    years = {
        *range(max(MIN_YEAR, today.year - MAX_YEARS + 1), today.year + 1),
        *range(filters.start_year, filters.end_year + 1),
        filters.year,
    }
    return tuple(sorted(year for year in years if MIN_YEAR <= year <= MAX_YEAR))


def _read_window(
    *,
    report_start: date,
    report_end: date,
    grid_start: date,
    grid_end: date,
    yoy_start: date,
    yoy_end: date,
    today: date,
) -> tuple[date, date]:
    """The union of the report range and the section years that can hold past data.

    The section years only extend the read up to today: future-dated entries are never charted.
    The report range is read in full even when it lies in the future, so the existing future-row
    note still counts those rows.
    """
    starts = [report_start]
    ends = [report_end]
    if grid_start <= today:
        starts.append(grid_start)
        ends.append(min(today, grid_end))
    if yoy_start <= today:
        starts.append(yoy_start)
        ends.append(min(today, yoy_end))
    return min(starts), max(ends)


def _empty_category_series(currency: str) -> CategorySeries:
    return CategorySeries((), (), (), Money(0, currency))


def _counts(category: ReportCategory) -> bool:
    """Whether an entry of this category contributes to earned, spent or saved."""
    return block_of(category.kind, category.treatment) in {"expense", "saved", "income"}


def _entries(
    repository: TransactionRepository,
    categories: Mapping[UUID, ReportCategory],
    *,
    start: date,
    end: date,
    note_start: date,
    note_end: date,
    today: date,
    currency: str,
    stats: _StreamStats,
) -> Iterator[LedgerEntry]:
    """One stream: count excluded report rows and yield the rows the series may count.

    ``start``/``end`` are the read window: it can be wider than the report range when a grid or
    year-over-year year lies outside it. The notes describe only the report range
    (``note_start``/``note_end``), so a row from a section year is never added to them.
    """
    for row in repository.iter_ledger_columns(start=start, end=end):
        in_notes = note_start <= row.day <= note_end
        if row.day > today:
            if in_notes:
                stats.future += 1
            continue
        if row.currency != currency:
            if in_notes:
                stats.other_currency += 1
            continue
        category = categories.get(row.category_id)
        if in_notes and category is not None and _counts(category):
            stats.counted += 1
        yield LedgerEntry(row.day, row.category_id, Money(row.amount_minor, row.currency))


def reports_data(
    factory: sessionmaker[Session],
    ctx: Context,
    *,
    from_year: str = "",
    to_year: str = "",
    step: str = "",
    currency: str = "",
    category: Iterable[str] = (),
    savings: str = "",
    year: str = "",
    trend: str = "",
    yoy_category: str = "",
) -> ReportsData:
    """The Reports page's filters and every section's data for the current workspace."""
    require_role(ctx, Role.VIEWER)
    today = ctx.clock.today(ctx.workspace.timezone)
    include_savings = savings == "1"
    with factory() as session:
        base_currency = CurrentWorkspaceRepository(session, ctx).get().base_currency
        ordered = CategoryRepository(session, ctx).ordered()
        categories = {
            row.id: ReportCategory(
                row.id,
                row.kind,
                row.treatment,
                row.name,
                row.color,
                archived=row.archived_at is not None,
            )
            for row in ordered
        }
        filters = _resolve_filters(
            raw_from=from_year,
            raw_to=to_year,
            raw_step=step,
            raw_year=year,
            raw_trend=trend,
            today=today,
        )
        if filters.errors:
            return ReportsData(
                filters=replace(filters, currency=base_currency, savings=include_savings),
                series=(),
                totals=totals_of((), currency=base_currency),
                category_series=_empty_category_series(base_currency),
                category_options=(),
                year_options=_year_options(today, filters),
                year_grid=_empty_category_series(base_currency),
                category_trends=CategoryTrends((), filters.trend_months, ()),
                yoy=None,
                plan_accuracy=PlanAccuracy((), 0, 0, None),
                yoy_category_options=(),
                available_currencies=(base_currency,),
                show_currency_selector=False,
                future_count=0,
                other_currency_count=0,
                has_data=False,
                today=today,
                series_start=None,
                series_end=None,
            )
        report_start = date(filters.start_year, 1, 1)
        report_end = date(filters.end_year, 12, 31)
        series_start = report_start
        series_end = min(today, report_end)
        grid_start = date(filters.year, 1, 1)
        grid_end = date(filters.year, 12, 31)
        yoy_start = date(max(MIN_YEAR, filters.year - 1), 1, 1)
        yoy_end = grid_end
        read_start, read_end = _read_window(
            report_start=report_start,
            report_end=report_end,
            grid_start=grid_start,
            grid_end=grid_end,
            yoy_start=yoy_start,
            yoy_end=yoy_end,
            today=today,
        )
        observed = (
            TransactionRepository(session, ctx).currencies_in_window(
                start=report_start, end=series_end
            )
            if series_end >= report_start
            else ()
        )
        available = tuple(dict.fromkeys((base_currency, *observed)))
        selected_currency = _select_currency(currency, available, base_currency)
        yoy_id = _resolve_yoy_category(yoy_category, categories, include_savings=include_savings)
        filters = replace(
            filters,
            currency=selected_currency,
            savings=include_savings,
            yoy_category_id=yoy_id,
        )
        stats = _StreamStats()
        stream = _entries(
            TransactionRepository(session, ctx),
            categories,
            start=read_start,
            end=read_end,
            note_start=report_start,
            note_end=report_end,
            today=today,
            currency=selected_currency,
            stats=stats,
        )
        # One scoped read feeds every section. The rows are materialised once, then each pure
        # aggregation walks them locally; no section re-reads the database.
        entries = list(stream)
        series, all_categories = ledger_and_category_series(
            entries,
            categories,
            start=series_start,
            end=series_end,
            step=filters.step,
            currency=selected_currency,
            include_savings=include_savings,
        )
        eligible_blocks = _category_blocks(include_savings)
        eligible_ids = {
            category_id
            for category_id, row in categories.items()
            if block_of(row.kind, row.treatment) in eligible_blocks
        }
        requested = _parse_category_ids(category)
        chosen = tuple(category_id for category_id in requested if category_id in eligible_ids)
        filters = replace(filters, category_ids=chosen)
        if chosen:
            displayed_categories = select_categories(
                all_categories,
                categories,
                include_savings=include_savings,
                selected=chosen,
            )
        else:
            displayed_categories = top_categories(all_categories)
        spending_ids = {
            row.category_id for row in all_categories.rows if row.category_id is not None
        }
        category_options = tuple(
            CategoryOption(row.id, row.name, archived=row.archived_at is not None)
            for row in ordered
            if row.id in eligible_ids
            and (row.archived_at is None or row.id in spending_ids or row.id in chosen)
        )
        plans: tuple[PlannedMonth, ...] = ()
        if series and series_end >= series_start:
            plan_start = YearMonth.from_date(series_start)
            plan_end = YearMonth.from_date(series_end)
            plans = tuple(
                PlannedMonth(
                    line.category_id,
                    YearMonth.from_date(line.month),
                    Money(line.planned_minor, line.currency),
                )
                for line in BudgetLineRepository(session, ctx).for_range(plan_start, plan_end)
                if line.currency == selected_currency
            )
        yoy_series = category_series(
            entries,
            categories,
            start=yoy_start,
            end=yoy_end,
            step=ReportStep.YEAR,
            currency=selected_currency,
            include_savings=include_savings,
            selected=(yoy_id,) if yoy_id is not None else None,
        )
        yoy_spending_ids = {
            row.category_id for row in yoy_series.rows if row.category_id is not None
        }
        yoy_category_options = tuple(
            CategoryOption(row.id, row.name, archived=row.archived_at is not None)
            for row in ordered
            if row.id in eligible_ids
            and (row.archived_at is None or row.id in yoy_spending_ids or row.id == yoy_id)
        )
    if not series or series_end < series_start:
        accuracy_series = _empty_category_series(selected_currency)
    elif filters.step is ReportStep.MONTH:
        accuracy_series = all_categories
    else:
        accuracy_series = category_series(
            entries,
            categories,
            start=series_start,
            end=series_end,
            step=ReportStep.MONTH,
            currency=selected_currency,
            include_savings=include_savings,
        )
    grid = year_grid(
        entries,
        categories,
        year=filters.year,
        currency=selected_currency,
        include_savings=include_savings,
        selected=chosen or None,
    )
    trends = (
        rolling_category_averages(displayed_categories, months=filters.trend_months)
        if filters.step is ReportStep.MONTH
        else CategoryTrends((), filters.trend_months, ())
    )
    return ReportsData(
        filters=filters,
        series=tuple(series),
        totals=totals_of(series, currency=selected_currency),
        category_series=displayed_categories,
        category_options=category_options,
        year_options=_year_options(today, filters),
        year_grid=grid,
        category_trends=trends,
        yoy=year_over_year(yoy_series, year=filters.year, category_id=yoy_id),
        plan_accuracy=plan_accuracy(accuracy_series, plans, categories),
        yoy_category_options=yoy_category_options,
        available_currencies=available,
        show_currency_selector=len(available) > 1,
        future_count=stats.future,
        other_currency_count=stats.other_currency,
        has_data=stats.counted > 0 and bool(series),
        today=today,
        series_start=series_start if series else None,
        series_end=series_end if series else None,
    )


__all__ = [
    "DEFAULT_YEARS",
    "MAX_YEARS",
    "CategoryOption",
    "FilterError",
    "ReportFilters",
    "ReportsData",
    "reports_data",
]
