"""FIRE result charts (F8.3): server-formatted series, axes, markers and plotted-data rows.

``static/js/charts.js`` only draws what this module prepares. The net-worth chart follows the
page's nominal/real choice; the expenses-versus-gain chart is always nominal because the FIRE rule
compares nominal values. Every label, amount and tooltip line is translated and formatted here.
"""

from __future__ import annotations

from dataclasses import dataclass

from assetboard.domain.fire import Projection, ProjectionRow
from assetboard.domain.money import Money
from assetboard.i18n import gettext
from assetboard.i18n.numbers import format_money
from assetboard.services.fire_comparison import FireComparison, FireComparisonRow
from assetboard.web.chart_axis import money_axis


@dataclass(frozen=True, slots=True)
class NetWorthPoint:
    year: str
    age: str
    net_worth: str


@dataclass(frozen=True, slots=True)
class ExpensesGainPoint:
    year: str
    age: str
    expenses: str
    gain: str


@dataclass(frozen=True, slots=True)
class FireCharts:
    basis: str
    net_worth: dict[str, object] | None
    net_worth_points: tuple[NetWorthPoint, ...]
    expenses_gain: dict[str, object] | None
    expenses_gain_points: tuple[ExpensesGainPoint, ...]
    fire_index: int | None
    fire_year: int | None
    fire_age: int | None
    depletion_age: int | None


def _amount(value: int | None, currency: str, locale: str) -> str:
    if value is None:
        return "—"
    return format_money(Money(value, currency), locale)


def _marker(index: int, label: str, text: str) -> dict[str, object]:
    return {"index": index, "label": label, "text": text}


def _first_income_stop(rows: tuple[ProjectionRow, ...]) -> int | None:
    return next((index for index, row in enumerate(rows) if row.income_stopped), None)


def _depletion_index(rows: tuple[ProjectionRow, ...], depletion_age: int | None) -> int | None:
    if depletion_age is None:
        return None
    return next(
        (
            index
            for index, row in enumerate(rows)
            if row.age == depletion_age and row.net_worth.amount_minor < 0
        ),
        None,
    )


def _markers(projection: Projection, locale: str) -> list[dict[str, object]]:
    entries: list[tuple[int, str, str]] = []
    if projection.fire_index is not None:
        assert projection.fire_year is not None and projection.fire_age is not None
        entries.append(
            (
                projection.fire_index,
                gettext("FIRE", locale),
                gettext("FIRE at age %(age)d (%(year)d)", locale)
                % {"age": projection.fire_age, "year": projection.fire_year},
            )
        )
    stop_index = _first_income_stop(projection.rows)
    if stop_index is not None:
        entries.append(
            (
                stop_index,
                gettext("Income stops", locale),
                gettext("Income stops at age %(age)d", locale)
                % {"age": projection.rows[stop_index].age},
            )
        )
    depletion_index = _depletion_index(projection.rows, projection.depletion_age)
    if depletion_index is not None:
        entries.append(
            (
                depletion_index,
                gettext("Depleted", locale),
                gettext("Portfolio depleted at age %(age)d", locale)
                % {"age": projection.rows[depletion_index].age},
            )
        )
    entries.sort(key=lambda entry: entry[0])
    return [_marker(index, label, text) for index, label, text in entries]


def _net_worth_data(
    projection: Projection, basis: str, locale: str
) -> tuple[dict[str, object], tuple[NetWorthPoint, ...]]:
    currency = projection.inputs.currency
    real = basis == "real"
    labels = [str(row.year) for row in projection.rows]
    values = [
        row.real_net_worth.amount_minor if real else row.net_worth.amount_minor
        for row in projection.rows
    ]
    amounts = [_amount(value, currency, locale) for value in values]
    low, high, step, axis_labels = money_axis(values, currency, locale)
    tooltips: list[list[str]] = []
    for row in projection.rows:
        extra: list[str] = []
        if row.is_fire:
            extra.append(gettext("FIRE year", locale))
        if row.income_stopped:
            extra.append(gettext("Income stopped", locale))
        if projection.depletion_age is not None and row.age == projection.depletion_age:
            extra.append(gettext("Portfolio depleted", locale))
        tooltips.append(extra)
    mark_points = []
    if projection.fire_index is not None:
        mark_points.append(
            {
                "index": projection.fire_index,
                "value": values[projection.fire_index],
                "label": gettext("FIRE", locale),
                "text": gettext("FIRE at age %(age)d (%(year)d)", locale)
                % {"age": projection.fire_age, "year": projection.fire_year},
            }
        )
    data: dict[str, object] = {
        "labels": labels,
        "series": [
            {
                "name": gettext("Net worth in today's money", locale)
                if real
                else gettext("Net worth", locale),
                "values": values,
                "amounts": amounts,
                "color": "primary-ink",
            }
        ],
        "y_min": low,
        "y_max": high,
        "y_interval": step,
        "y_labels": axis_labels,
        "tooltips": tooltips,
        "markers": _markers(projection, locale),
        "mark_points": mark_points,
    }
    points = tuple(
        NetWorthPoint(
            year=labels[index],
            age=str(projection.rows[index].age),
            net_worth=amounts[index],
        )
        for index in range(len(labels))
    )
    return data, points


def _expenses_gain_data(
    projection: Projection, locale: str
) -> tuple[dict[str, object], tuple[ExpensesGainPoint, ...]]:
    currency = projection.inputs.currency
    labels = [str(row.year) for row in projection.rows]
    expenses = [row.expenses.amount_minor for row in projection.rows]
    gains = [row.gain.amount_minor for row in projection.rows]
    expense_amounts = [_amount(value, currency, locale) for value in expenses]
    gain_amounts = [_amount(value, currency, locale) for value in gains]
    low, high, step, axis_labels = money_axis((*expenses, *gains), currency, locale)
    tooltips: list[list[str]] = [
        [gettext("FIRE year", locale)] if row.is_fire else [] for row in projection.rows
    ]
    markers = []
    if projection.fire_index is not None:
        markers.append(
            _marker(
                projection.fire_index,
                gettext("FIRE", locale),
                gettext("FIRE at age %(age)d (%(year)d)", locale)
                % {"age": projection.fire_age, "year": projection.fire_year},
            )
        )
    data: dict[str, object] = {
        "labels": labels,
        "series": [
            {
                "name": gettext("Expenses", locale),
                "values": expenses,
                "amounts": expense_amounts,
                "color": "warning-strong",
            },
            {
                "name": gettext("Investment gain", locale),
                "values": gains,
                "amounts": gain_amounts,
                "color": "success",
            },
        ],
        "y_min": low,
        "y_max": high,
        "y_interval": step,
        "y_labels": axis_labels,
        "tooltips": tooltips,
        "markers": markers,
    }
    points = tuple(
        ExpensesGainPoint(
            year=labels[index],
            age=str(projection.rows[index].age),
            expenses=expense_amounts[index],
            gain=gain_amounts[index],
        )
        for index in range(len(labels))
    )
    return data, points


@dataclass(frozen=True, slots=True)
class FireComparisonSeries:
    name: str
    color: str


@dataclass(frozen=True, slots=True)
class FireComparisonPoint:
    year: str
    values: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FireComparisonChart:
    data: dict[str, object] | None
    series: tuple[FireComparisonSeries, ...]
    points: tuple[FireComparisonPoint, ...]
    basis: str
    mixed_start_years: bool


# Fixed tokens, one per selected scenario (the comparison is capped at three).
COMPARISON_COLORS = ("primary-ink", "success", "warning-strong")


def _comparison_label(row: FireComparisonRow, locale: str, used: set[str]) -> str:
    """The scenario name with an archived suffix and a free duplicate suffix if needed."""
    base = row.scenario.name
    if row.scenario.archived:
        base = gettext("%(name)s (archived)", locale) % {"name": base}
    candidate = base
    number = 2
    while candidate in used:
        candidate = f"{base} ({number})"
        number += 1
    used.add(candidate)
    return candidate


def build_fire_comparison_chart(
    comparison: FireComparison, basis: str, locale: str
) -> FireComparisonChart:
    """Overlay the selected scenarios' net worth on the union of their calendar years.

    Blocked plans are omitted (the page keeps them in the table). A series has ``None`` outside
    its own start year and horizon, so the chart shows a gap rather than a zero. The tooltip for
    a FIRE year names each scenario that reaches FIRE in that year. The nominal/real choice only
    changes the plotted values; the accessible plotted-data rows carry the same formatted
    amounts, and every label is translated and formatted here.
    """
    normalized = "real" if basis == "real" else "nominal"
    entries = [row for row in comparison.rows if row.projection is not None and row.projection.rows]
    if not entries:
        return FireComparisonChart(
            data=None,
            series=(),
            points=(),
            basis=normalized,
            mixed_start_years=comparison.mixed_start_years,
        )
    start_year = min(
        entry.projection.inputs.start_year for entry in entries if entry.projection is not None
    )
    end_year = max(
        entry.projection.rows[-1].year for entry in entries if entry.projection is not None
    )
    years = list(range(start_year, end_year + 1))
    used: set[str] = set()
    series: list[dict[str, object]] = []
    series_meta: list[FireComparisonSeries] = []
    amounts_by_index: list[list[str]] = [[] for _ in years]
    all_values: list[int] = []
    currency = entries[0].scenario.currency
    for index, entry in enumerate(entries):
        projection = entry.projection
        assert projection is not None
        name = _comparison_label(entry, locale, used)
        color = COMPARISON_COLORS[index % len(COMPARISON_COLORS)]
        by_year = {row.year: row for row in projection.rows}
        values: list[int | None] = []
        for year in years:
            row = by_year.get(year)
            if row is None:
                values.append(None)
                continue
            values.append(
                row.real_net_worth.amount_minor
                if normalized == "real"
                else row.net_worth.amount_minor
            )
        amounts = [_amount(value, currency, locale) for value in values]
        for offset, amount in enumerate(amounts):
            amounts_by_index[offset].append(amount)
        all_values.extend(value for value in values if value is not None)
        series.append(
            {
                "name": name,
                "values": values,
                "amounts": amounts,
                "color": color,
            }
        )
        series_meta.append(FireComparisonSeries(name=name, color=color))

    tooltips: list[list[str]] = [[] for _ in years]
    index_by_year = {year: index for index, year in enumerate(years)}
    for series_index, entry in enumerate(entries):
        projection = entry.projection
        assert projection is not None
        label = series_meta[series_index].name
        if projection.fire_year is not None and projection.fire_age is not None:
            tooltips[index_by_year[projection.fire_year]].append(
                gettext("%(name)s: FIRE at age %(age)d", locale)
                % {"name": label, "age": projection.fire_age}
            )

    low, high, step, axis_labels = money_axis(all_values, currency, locale)
    data: dict[str, object] = {
        "labels": [str(year) for year in years],
        "series": series,
        "y_min": low,
        "y_max": high,
        "y_interval": step,
        "y_labels": axis_labels,
        "tooltips": tooltips,
        "markers": [],
    }
    points = tuple(
        FireComparisonPoint(
            year=str(years[index]),
            values=tuple(amounts_by_index[index]),
        )
        for index in range(len(years))
    )
    return FireComparisonChart(
        data=data,
        series=tuple(series_meta),
        points=points,
        basis=normalized,
        mixed_start_years=comparison.mixed_start_years,
    )


def build_fire_charts(projection: Projection | None, basis: str, locale: str) -> FireCharts:
    """Prepare both charts and their accessible tables for one projection."""
    if projection is None or not projection.rows:
        return FireCharts(
            basis=basis,
            net_worth=None,
            net_worth_points=(),
            expenses_gain=None,
            expenses_gain_points=(),
            fire_index=None,
            fire_year=None,
            fire_age=None,
            depletion_age=None,
        )
    normalized = "real" if basis == "real" else "nominal"
    net_worth, net_worth_points = _net_worth_data(projection, normalized, locale)
    expenses_gain, expenses_gain_points = _expenses_gain_data(projection, locale)
    return FireCharts(
        basis=normalized,
        net_worth=net_worth,
        net_worth_points=net_worth_points,
        expenses_gain=expenses_gain,
        expenses_gain_points=expenses_gain_points,
        fire_index=projection.fire_index,
        fire_year=projection.fire_year,
        fire_age=projection.fire_age,
        depletion_age=projection.depletion_age,
    )


__all__ = [
    "COMPARISON_COLORS",
    "ExpensesGainPoint",
    "FireCharts",
    "FireComparisonChart",
    "FireComparisonPoint",
    "FireComparisonSeries",
    "NetWorthPoint",
    "build_fire_charts",
    "build_fire_comparison_chart",
]
