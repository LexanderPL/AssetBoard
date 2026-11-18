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
    CategoryRepository,
    CurrentWorkspaceRepository,
    TransactionRepository,
)
from assetboard.domain.money import Money
from assetboard.domain.months import MAX_YEAR, MIN_YEAR
from assetboard.domain.reports import (
    CategorySeries,
    LedgerEntry,
    PeriodTotals,
    ReportCategory,
    ReportStep,
    ReportTotals,
    ledger_and_category_series,
    select_categories,
    top_categories,
    totals_of,
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
    return ReportFilters(
        start_year=start_year,
        end_year=end_year,
        step=step,
        currency="",
        from_text=raw_from.strip() or str(start_year),
        to_text=raw_to.strip() or str(end_year),
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
    today: date,
    currency: str,
    stats: _StreamStats,
) -> Iterator[LedgerEntry]:
    """One stream: count excluded rows and yield the rows the series counts."""
    for row in repository.iter_ledger_columns(start=start, end=end):
        if row.day > today:
            stats.future += 1
            continue
        if row.currency != currency:
            stats.other_currency += 1
            continue
        category = categories.get(row.category_id)
        if category is not None and _counts(category):
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
) -> ReportsData:
    """The Reports page's filters and series for the current workspace."""
    require_role(ctx, Role.VIEWER)
    today = ctx.clock.today(ctx.workspace.timezone)
    include_savings = savings == "1"
    with factory() as session:
        base_currency = CurrentWorkspaceRepository(session, ctx).get().base_currency
        ordered = CategoryRepository(session, ctx).ordered()
        categories = {
            row.id: ReportCategory(row.id, row.kind, row.treatment, row.name, row.color)
            for row in ordered
        }
        filters = _resolve_filters(raw_from=from_year, raw_to=to_year, raw_step=step, today=today)
        if filters.errors:
            return ReportsData(
                filters=replace(filters, currency=base_currency, savings=include_savings),
                series=(),
                totals=totals_of((), currency=base_currency),
                category_series=_empty_category_series(base_currency),
                category_options=(),
                available_currencies=(base_currency,),
                show_currency_selector=False,
                future_count=0,
                other_currency_count=0,
                has_data=False,
                today=today,
                series_start=None,
                series_end=None,
            )
        read_start = date(filters.start_year, 1, 1)
        read_end = date(filters.end_year, 12, 31)
        series_start = read_start
        series_end = min(today, read_end)
        observed = (
            TransactionRepository(session, ctx).currencies_in_window(
                start=read_start, end=series_end
            )
            if series_end >= read_start
            else ()
        )
        available = tuple(dict.fromkeys((base_currency, *observed)))
        selected_currency = _select_currency(currency, available, base_currency)
        filters = replace(filters, currency=selected_currency, savings=include_savings)
        stats = _StreamStats()
        stream = _entries(
            TransactionRepository(session, ctx),
            categories,
            start=read_start,
            end=read_end,
            today=today,
            currency=selected_currency,
            stats=stats,
        )
        # One read and one pure pass feed both reports: no second iteration over the rows.
        series, all_categories = ledger_and_category_series(
            stream,
            categories,
            start=series_start,
            end=series_end,
            step=filters.step,
            currency=selected_currency,
            include_savings=include_savings,
        )
        if not series:
            # A future-only range has no period keys, so the pure function returns before the
            # generator runs; consume it so the future/other-currency notes are still counted.
            for _entry in stream:
                pass
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
            if row.id in eligible_ids and (row.archived_at is None or row.id in spending_ids)
        )
    return ReportsData(
        filters=filters,
        series=tuple(series),
        totals=totals_of(series, currency=selected_currency),
        category_series=displayed_categories,
        category_options=category_options,
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
