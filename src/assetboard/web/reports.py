"""The Reports page's view model (F10.1): formatted rows, chart JSON and notes.

Amounts and labels are formatted here by the server's locale helpers; the browser only draws
the integers it receives. Chart colours are CSS token names (read by ``static/js/charts.js``),
so both themes keep their contrast. The data block is the same JSON shape every chart uses
(``<script type="application/json">`` read by the static module); the table carries the same
figures for assistive technology and for visitors without JavaScript.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from fastapi import Request

from assetboard.domain.money import Money
from assetboard.domain.months import YearMonth
from assetboard.domain.reports import PeriodKey, PeriodTotals, ReportStep, ReportTotals
from assetboard.i18n import gettext, ngettext
from assetboard.i18n.dates import format_month
from assetboard.i18n.numbers import format_money
from assetboard.services.reports import FilterError, ReportsData
from assetboard.web.chart_axis import money_axis
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
class ReportsView:
    data: ReportsData
    errors: dict[str, str]
    rows: tuple[ReportRow, ...]
    total: ReportTotal
    chart: dict[str, Any] | None
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
    return ReportsView(
        data=data,
        errors=errors,
        rows=rows,
        total=total,
        chart=_chart(data, locale),
        notes=_notes(data, locale),
        range_years=range_years,
    )
