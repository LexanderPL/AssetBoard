"""The Reports page's view model (F10.1–F10.3): formatted rows, chart JSON and notes.

Amounts, rates and labels are formatted here by the server's locale helpers; the browser only
draws the integers it receives. Chart colours are CSS token names (read by
``static/js/charts.js``) or a category's ``#rrggbb`` colour, so both themes keep their contrast.
The data block is the same JSON shape every chart uses (``<script type="application/json">``
read by the static module); each table carries the same figures for assistive technology and
for visitors without JavaScript.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from fastapi import Request

from assetboard.domain.money import Money
from assetboard.domain.months import YearMonth
from assetboard.domain.reports import (
    REPORT_ROLLING_MONTHS,
    CategorySeries,
    CategorySpend,
    PeriodKey,
    PeriodTotals,
    ReportStep,
    ReportTotals,
    SavingsRate,
    rolling_savings_rates,
    savings_rate,
)
from assetboard.i18n import gettext, ngettext
from assetboard.i18n.dates import format_month
from assetboard.i18n.numbers import format_money, format_rate_tenths
from assetboard.services.reports import CategoryOption, FilterError, ReportsData
from assetboard.web.chart_axis import money_axis, rate_axis
from assetboard.web.rendering import scoped_url


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


FILTER_ERRORS: dict[FilterError, str] = {
    FilterError.INVALID_YEAR: _("Enter a year between 1900 and 2199."),
    FilterError.FROM_AFTER_TO: _("The first year must be the same as or before the last year."),
    FilterError.TOO_MANY_YEARS: _("Choose at most 30 years."),
}
CHART_TITLE = _("Income vs expenses")
SO_FAR = _("so far")
TOTAL = _("Total")
TABLE_CAPTION = _("Income, expenses and savings by period")
RANGE_END_NOTE = _("The range ends with the current month.")
EARNED = _("Earned")
SPENT = _("Spent")
SAVED = _("Saved")
NET = _("Net")
SAVINGS_RATE = _("Savings rate")
ROLLING_RATE = _("12-month rolling rate")
NOT_AVAILABLE = _("Not available: nothing earned")
NOT_ENOUGH_HISTORY = _("Not available: fewer than 12 months of data.")
CATEGORY_CHART_TITLE = _("Expenses by category over time")
CATEGORY_TABLE_CAPTION = _("Spending by category and period")
CATEGORY_EMPTY = _("No expenses in this range.")
CATEGORY_EMPTY_SAVINGS = _("No expenses or savings in this range.")
CATEGORY_EMPTY_SELECTED = _("No spending for the selected categories.")
CATEGORY_FILTER = _("Categories")
CATEGORY_FILTER_HELP = _(
    "Leave empty to show the 8 largest categories and the rest as Other (combined)."
)
CATEGORY_FILTER_ACTIVE = _("Showing the selected categories only.")
CATEGORY_TOP_NOTE = _(
    "The 8 largest categories are shown; the rest are combined as Other (combined)."
)
INCLUDE_SAVINGS = _("Include saved / invested")
INCLUDE_SAVINGS_HELP = _("Saved and invested amounts get their own category series.")
OTHER = _("Other (combined)")
ARCHIVED = _("archived")


@dataclass(frozen=True, slots=True)
class ReportRow:
    label: str
    href: str | None
    so_far: bool
    earned: str
    spent: str
    saved: str
    net: str
    net_minor: int


@dataclass(frozen=True, slots=True)
class ReportTotal:
    label: str
    earned: str
    spent: str
    saved: str
    net: str
    net_minor: int


@dataclass(frozen=True, slots=True)
class RateRow:
    """One period's savings rate and (for monthly steps) its rolling 12-month rate.

    ``rate``/``rolling`` are ``None`` when the figure is undefined; ``rolling_note`` says why
    for screen readers ("nothing earned" or "fewer than 12 months of data").
    """

    label: str
    href: str | None
    so_far: bool
    rate: str | None
    rate_tenths: int | None
    rolling: str | None
    rolling_note: str | None


@dataclass(frozen=True, slots=True)
class RateTotal:
    label: str
    rate: str | None
    rate_tenths: int | None


@dataclass(frozen=True, slots=True)
class CategoryColumn:
    """One period column of the category table."""

    label: str
    href: str | None
    so_far: bool


@dataclass(frozen=True, slots=True)
class CategoryRow:
    """One category row: formatted cells, a row total and its swatch colour."""

    label: str
    color: str | None
    cells: tuple[str, ...]
    total: str


@dataclass(frozen=True, slots=True)
class CategoryTable:
    columns: tuple[CategoryColumn, ...]
    rows: tuple[CategoryRow, ...]
    totals: tuple[str, ...]
    total: str


@dataclass(frozen=True, slots=True)
class CategoryOptionView:
    id: UUID
    name: str
    selected: bool
    archived: bool = False


@dataclass(frozen=True, slots=True)
class ReportsView:
    data: ReportsData
    errors: dict[str, str]
    rows: tuple[ReportRow, ...]
    total: ReportTotal
    chart: dict[str, Any] | None
    rate_rows: tuple[RateRow, ...]
    rate_total: RateTotal
    rate_chart: dict[str, Any] | None
    show_rolling: bool
    category_chart: dict[str, Any] | None
    category_table: CategoryTable
    category_options: tuple[CategoryOptionView, ...]
    category_notes: tuple[str, ...]
    category_empty: str
    notes: tuple[str, ...]
    range_years: str

    @property
    def valid(self) -> bool:
        return not self.errors

    @property
    def has_data(self) -> bool:
        return self.data.has_data

    @property
    def show_currency_selector(self) -> bool:
        return self.data.show_currency_selector

    @property
    def include_savings(self) -> bool:
        return self.data.filters.savings

    @property
    def category_has_data(self) -> bool:
        # A selected category without spending still has a zero table row; the chart itself is
        # empty (no bars), as for any range with nothing to draw.
        return bool(self.category_table.rows)

    @property
    def category_filter_active(self) -> bool:
        return self.data.category_filter_active


def _period_label(period: PeriodKey, step: ReportStep, locale: str) -> str:
    if step is ReportStep.MONTH:
        assert isinstance(period, YearMonth)
        return format_month(period, locale)
    return str(period)


def _is_so_far(period: PeriodKey, step: ReportStep, today: date) -> bool:
    if step is ReportStep.MONTH:
        return period == YearMonth.from_date(today)
    return period == today.year


def _format_totals(totals: ReportTotals, locale: str) -> tuple[str, str, str, str, int]:
    return (
        format_money(totals.earned, locale),
        format_money(totals.spent, locale),
        format_money(totals.saved, locale),
        format_money(totals.net, locale),
        totals.net.amount_minor,
    )


def _period_row(
    period: PeriodTotals, step: ReportStep, today: date, locale: str, request: Request
) -> ReportRow:
    earned, spent, saved, net, net_minor = _format_totals(
        ReportTotals(period.earned, period.spent, period.saved, period.net), locale
    )
    href = None
    if step is ReportStep.MONTH:
        assert isinstance(period.period, YearMonth)
        href = scoped_url(request, "month", month=str(period.period))
    return ReportRow(
        label=_period_label(period.period, step, locale),
        href=href,
        so_far=_is_so_far(period.period, step, today),
        earned=earned,
        spent=spent,
        saved=saved,
        net=net,
        net_minor=net_minor,
    )


def _rate_text(rate: SavingsRate | None, locale: str) -> str | None:
    if rate is None or rate.tenths is None:
        return None
    return format_rate_tenths(rate.tenths, locale)


def _rolling_rates(data: ReportsData) -> tuple[SavingsRate | None, ...]:
    if data.filters.step is not ReportStep.MONTH or not data.series:
        return (None,) * len(data.series)
    return rolling_savings_rates(data.series)


def _rate_rows(
    data: ReportsData,
    rolling: tuple[SavingsRate | None, ...],
    *,
    show_rolling: bool,
    locale: str,
    request: Request,
) -> tuple[RateRow, ...]:
    rows: list[RateRow] = []
    for index, period in enumerate(data.series):
        rate = period.savings_rate
        rolling_rate = rolling[index] if show_rolling else None
        rolling_text: str | None = None
        rolling_note: str | None = None
        if show_rolling:
            rolling_text = _rate_text(rolling_rate, locale)
            if rolling_text is None:
                rolling_note = gettext(
                    NOT_ENOUGH_HISTORY if index < REPORT_ROLLING_MONTHS - 1 else NOT_AVAILABLE,
                    locale,
                )
        href = None
        if data.filters.step is ReportStep.MONTH:
            assert isinstance(period.period, YearMonth)
            href = scoped_url(request, "month", month=str(period.period))
        rows.append(
            RateRow(
                label=_period_label(period.period, data.filters.step, locale),
                href=href,
                so_far=_is_so_far(period.period, data.filters.step, data.today),
                rate=_rate_text(rate, locale),
                rate_tenths=rate.tenths,
                rolling=rolling_text,
                rolling_note=rolling_note,
            )
        )
    return tuple(rows)


def _rate_total(data: ReportsData, locale: str) -> RateTotal:
    rate = savings_rate(data.totals.earned.amount_minor, data.totals.saved.amount_minor)
    return RateTotal(TOTAL, _rate_text(rate, locale), rate.tenths)


def _rate_chart(
    data: ReportsData,
    rolling: tuple[SavingsRate | None, ...],
    *,
    show_rolling: bool,
    locale: str,
) -> dict[str, Any] | None:
    if not data.has_data or not data.series:
        return None
    rates = [period.savings_rate for period in data.series]
    values = [rate.tenths for rate in rates if rate.tenths is not None]
    rolling_values = (
        [rate.tenths for rate in rolling if rate is not None and rate.tenths is not None]
        if show_rolling
        else []
    )
    available = [*values, *rolling_values]
    if not available:
        return None
    low, high, step, axis_labels = rate_axis(available, locale)
    labels = [_period_label(period.period, data.filters.step, locale) for period in data.series]
    tooltips: list[list[str]] = []
    for period, rate in zip(data.series, rates, strict=True):
        lines: list[str] = []
        if rate.tenths is None:
            lines.append(gettext(NOT_AVAILABLE, locale))
        if _is_so_far(period.period, data.filters.step, data.today):
            lines.append(gettext(SO_FAR, locale))
        tooltips.append(lines)

    def text(rate: SavingsRate | None) -> str:
        return _rate_text(rate, locale) or ""

    series: list[dict[str, Any]] = [
        {
            "key": "rate",
            "name": gettext(SAVINGS_RATE, locale),
            "color": "primary-ink",
            "values": [rate.tenths for rate in rates],
            "amounts": [text(rate) for rate in rates],
        }
    ]
    if show_rolling:
        series.append(
            {
                "key": "rolling",
                "name": gettext(ROLLING_RATE, locale),
                "color": "muted",
                "values": [rate.tenths if rate is not None else None for rate in rolling],
                "amounts": [text(rate) for rate in rolling],
            }
        )
    return {
        "labels": labels,
        "y_min": low,
        "y_max": high,
        "y_interval": step,
        "y_labels": axis_labels,
        "series": series,
        "tooltips": tooltips,
    }


def _chart(data: ReportsData, locale: str) -> dict[str, Any] | None:
    if not data.has_data or not data.series:
        return None
    currency = data.filters.currency
    labels = [_period_label(period.period, data.filters.step, locale) for period in data.series]
    earned = [period.earned.amount_minor for period in data.series]
    spent = [period.spent.amount_minor for period in data.series]
    saved = [period.saved.amount_minor for period in data.series]
    low, high, step, axis_labels = money_axis((*earned, *spent, *saved), currency, locale)

    def money(minor: int) -> str:
        return format_money(Money(minor, currency), locale)

    tooltips: list[list[str]] = []
    for period in data.series:
        lines = [
            f"{gettext(EARNED, locale)}: {money(period.earned.amount_minor)}",
            f"{gettext(SPENT, locale)}: {money(period.spent.amount_minor)}",
            f"{gettext(SAVED, locale)}: {money(period.saved.amount_minor)}",
            f"{gettext(NET, locale)}: {money(period.net.amount_minor)}",
        ]
        if _is_so_far(period.period, data.filters.step, data.today):
            lines.append(gettext(SO_FAR, locale))
        tooltips.append(lines)
    return {
        "labels": labels,
        "y_min": low,
        "y_max": high,
        "y_interval": step,
        "y_labels": axis_labels,
        "series": [
            {
                "key": "earned",
                "name": gettext(EARNED, locale),
                "color": "success",
                "values": earned,
            },
            {
                "key": "spent",
                "name": gettext(SPENT, locale),
                "color": "danger",
                "values": spent,
            },
            {
                "key": "saved",
                "name": gettext(SAVED, locale),
                "color": "primary-ink",
                "values": saved,
            },
        ],
        "tooltips": tooltips,
    }


def _category_name(row: CategorySpend, locale: str) -> str:
    if row.other:
        return gettext(OTHER, locale)
    name = row.name
    if row.archived:
        name = f"{name} ({gettext(ARCHIVED, locale)})"
    return name


def _category_columns(
    series: CategorySeries,
    data: ReportsData,
    locale: str,
    request: Request,
) -> tuple[CategoryColumn, ...]:
    columns: list[CategoryColumn] = []
    for period in series.periods:
        href = None
        if data.filters.step is ReportStep.MONTH:
            assert isinstance(period, YearMonth)
            href = scoped_url(request, "month", month=str(period))
        columns.append(
            CategoryColumn(
                label=_period_label(period, data.filters.step, locale),
                href=href,
                so_far=_is_so_far(period, data.filters.step, data.today),
            )
        )
    return tuple(columns)


def _category_table(data: ReportsData, locale: str, request: Request) -> CategoryTable:
    series = data.category_series
    rows = tuple(
        CategoryRow(
            label=_category_name(row, locale),
            color=row.color or None,
            cells=tuple(format_money(amount, locale) for amount in row.amounts),
            total=format_money(row.total, locale),
        )
        for row in series.rows
    )
    return CategoryTable(
        columns=_category_columns(series, data, locale, request),
        rows=rows,
        totals=tuple(format_money(total, locale) for total in series.totals),
        total=format_money(series.total, locale),
    )


def _category_chart(data: ReportsData, locale: str) -> dict[str, Any] | None:
    series = data.category_series
    if not series.rows or series.total.amount_minor <= 0:
        return None
    currency = data.filters.currency
    labels = [_period_label(period, data.filters.step, locale) for period in series.periods]
    low, high, step, axis_labels = money_axis(
        (total.amount_minor for total in series.totals), currency, locale
    )
    tooltips: list[list[str]] = []
    for index, period in enumerate(series.periods):
        lines = [
            f"{_category_name(row, locale)}: {format_money(row.amounts[index], locale)}"
            for row in series.rows
            if row.amounts[index].amount_minor != 0
        ]
        lines.append(f"{gettext(TOTAL, locale)}: {format_money(series.totals[index], locale)}")
        if _is_so_far(period, data.filters.step, data.today):
            lines.append(gettext(SO_FAR, locale))
        tooltips.append(lines)
    return {
        "labels": labels,
        "y_min": low,
        "y_max": high,
        "y_interval": step,
        "y_labels": axis_labels,
        "stack": "spending",
        "series": [
            {
                "key": str(row.category_id) if row.category_id is not None else "other",
                "name": _category_name(row, locale),
                "color": row.color or "line-strong",
                "values": [amount.amount_minor for amount in row.amounts],
                "amounts": [format_money(amount, locale) for amount in row.amounts],
            }
            for row in series.rows
        ],
        "tooltips": tooltips,
    }


def _category_options(data: ReportsData) -> tuple[CategoryOptionView, ...]:
    selected = set(data.filters.category_ids)
    options: tuple[CategoryOption, ...] = data.category_options
    return tuple(
        CategoryOptionView(
            id=option.id,
            name=option.name,
            selected=option.id in selected,
            archived=option.archived,
        )
        for option in options
    )


def _category_notes(data: ReportsData, locale: str) -> tuple[str, ...]:
    if data.category_filter_active:
        return (gettext(CATEGORY_FILTER_ACTIVE, locale),)
    if any(row.other for row in data.category_series.rows):
        return (gettext(CATEGORY_TOP_NOTE, locale),)
    return ()


def _notes(data: ReportsData, locale: str) -> tuple[str, ...]:
    notes: list[str] = []
    if data.future_count:
        notes.append(
            ngettext(
                "%(num)d entry is dated after today and is not charted.",
                "%(num)d entries are dated after today and are not charted.",
                data.future_count,
                locale,
            )
            % {"num": data.future_count}
        )
    if data.other_currency_count:
        notes.append(
            ngettext(
                "%(num)d entry in another currency is left out; reports do not convert.",
                "%(num)d entries in another currency are left out; reports do not convert.",
                data.other_currency_count,
                locale,
            )
            % {"num": data.other_currency_count}
        )
    if data.filters.end_year > data.today.year:
        notes.append(gettext(RANGE_END_NOTE, locale))
    return tuple(notes)


def build_reports_view(request: Request, data: ReportsData, locale: str) -> ReportsView:
    """The formatted page from the service's data."""
    step = data.filters.step
    rows = tuple(_period_row(period, step, data.today, locale, request) for period in data.series)
    earned, spent, saved, net, net_minor = _format_totals(data.totals, locale)
    total = ReportTotal(TOTAL, earned, spent, saved, net, net_minor)
    errors = {
        field: gettext(FILTER_ERRORS[error], locale) for field, error in data.filters.errors.items()
    }
    range_years = (
        f"{data.filters.start_year} – {data.filters.end_year}"
        if data.filters.start_year != data.filters.end_year
        else str(data.filters.start_year)
    )
    rolling = _rolling_rates(data)
    show_rolling = step is ReportStep.MONTH and len(data.series) >= REPORT_ROLLING_MONTHS
    if data.filters.category_ids:
        category_empty = CATEGORY_EMPTY_SELECTED
    elif data.filters.savings:
        category_empty = CATEGORY_EMPTY_SAVINGS
    else:
        category_empty = CATEGORY_EMPTY
    return ReportsView(
        data=data,
        errors=errors,
        rows=rows,
        total=total,
        chart=_chart(data, locale),
        rate_rows=_rate_rows(
            data, rolling, show_rolling=show_rolling, locale=locale, request=request
        ),
        rate_total=_rate_total(data, locale),
        rate_chart=_rate_chart(data, rolling, show_rolling=show_rolling, locale=locale),
        show_rolling=show_rolling,
        category_chart=_category_chart(data, locale),
        category_table=_category_table(data, locale, request),
        category_options=_category_options(data),
        category_notes=_category_notes(data, locale),
        category_empty=gettext(category_empty, locale),
        notes=_notes(data, locale),
        range_years=range_years,
    )
