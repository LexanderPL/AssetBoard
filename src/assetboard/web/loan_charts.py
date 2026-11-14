"""Loan overview chart views (F7.3).

The server formats every label, amount and rate. The browser receives minor-unit integers and
pre-formatted strings; ``static/js/charts.js`` draws the lines and stacked bars. Rate-change
markers are vertical dashed lines with the rate in the label and in the axis tooltip.
"""

from __future__ import annotations

from dataclasses import dataclass

from assetboard.domain.loans import Schedule, ScheduleRow
from assetboard.domain.money import Money
from assetboard.i18n import gettext
from assetboard.i18n.dates import format_day
from assetboard.i18n.numbers import format_money, format_rate_short
from assetboard.services.loan_scenarios import LoanScenarioComparison
from assetboard.web.chart_axis import money_axis


@dataclass(frozen=True, slots=True)
class BalancePoint:
    date: str
    plan: str
    baseline: str


@dataclass(frozen=True, slots=True)
class PaymentPoint:
    date: str
    principal: str
    interest: str
    costs: str
    overpayment: str
    total: str


@dataclass(frozen=True, slots=True)
class LoanCharts:
    balance: dict[str, object] | None
    balance_points: tuple[BalancePoint, ...]
    payments: dict[str, object] | None
    payment_points: tuple[PaymentPoint, ...]
    has_baseline: bool
    has_overpayments: bool


def _amount(value: int | None, currency: str, locale: str) -> str:
    if value is None:
        return "—"
    return format_money(Money(value, currency), locale)


def _rate_markers(
    rows: tuple[ScheduleRow, ...], *, offset: int, locale: str
) -> list[dict[str, object]]:
    markers: list[dict[str, object]] = []
    previous: int | None = None
    for index, row in enumerate(rows):
        if previous is not None and row.annual_rate_bp != previous:
            rate = format_rate_short(row.annual_rate_bp, locale)
            markers.append(
                {
                    "index": index + offset,
                    "label": rate,
                    "text": gettext("Rate change: %(rate)s", locale) % {"rate": rate},
                }
            )
        previous = row.annual_rate_bp
    return markers


def _balance_data(
    schedule: Schedule, baseline: Schedule, locale: str
) -> tuple[dict[str, object], tuple[BalancePoint, ...], bool]:
    has_baseline = schedule is not baseline
    labels_rows = baseline.rows if len(baseline.rows) >= len(schedule.rows) else schedule.rows
    max_rows = len(labels_rows)
    labels = [format_day(schedule.anchor_date, locale)] + [
        format_day(row.date, locale) for row in labels_rows
    ]
    plan_values: list[int | None] = [schedule.starting_balance.amount_minor]
    baseline_values: list[int | None] = [baseline.starting_balance.amount_minor]
    for index in range(max_rows):
        plan_values.append(
            schedule.rows[index].closing.amount_minor if index < len(schedule.rows) else None
        )
        baseline_values.append(
            baseline.rows[index].closing.amount_minor if index < len(baseline.rows) else None
        )
    complete = [value for value in (*plan_values, *baseline_values) if value is not None]
    low, high, step, axis_labels = money_axis(complete, schedule.currency, locale)
    tooltips: list[list[str]] = [[]]
    for row in schedule.rows:
        extra: list[str] = []
        if row.n > 1:
            previous = schedule.rows[row.n - 2]
            if previous.annual_rate_bp != row.annual_rate_bp:
                extra.append(
                    gettext("Rate change: %(rate)s", locale)
                    % {"rate": format_rate_short(row.annual_rate_bp, locale)}
                )
        tooltips.append(extra)
    while len(tooltips) < len(labels):
        tooltips.append([])
    series: list[dict[str, object]] = [
        {
            "name": gettext("Configured plan", locale),
            "values": plan_values,
            "amounts": [_amount(value, schedule.currency, locale) for value in plan_values],
            "color": "primary-ink",
        }
    ]
    if has_baseline:
        series.append(
            {
                "name": gettext("Without overpayments", locale),
                "values": baseline_values,
                "amounts": [_amount(value, schedule.currency, locale) for value in baseline_values],
                "color": "muted",
            }
        )
    data: dict[str, object] = {
        "labels": labels,
        "series": series,
        "y_min": low,
        "y_max": high,
        "y_interval": step,
        "y_labels": axis_labels,
        "tooltips": tooltips,
        "markers": _rate_markers(schedule.rows, offset=1, locale=locale),
    }
    points = tuple(
        BalancePoint(
            date=labels[index],
            plan=_amount(plan_values[index], schedule.currency, locale),
            baseline=(
                _amount(baseline_values[index], schedule.currency, locale) if has_baseline else "—"
            ),
        )
        for index in range(len(labels))
    )
    return data, points, has_baseline


def _payments_data(
    schedule: Schedule, has_overpayments: bool, locale: str
) -> tuple[dict[str, object] | None, tuple[PaymentPoint, ...], bool]:
    rows = schedule.rows
    if not rows:
        return None, (), False
    labels = [format_day(row.date, locale) for row in rows]
    principal = [row.principal.amount_minor for row in rows]
    interest = [row.interest.amount_minor for row in rows]
    costs = [row.costs.amount_minor for row in rows]
    overpayment = [row.overpayment.amount_minor for row in rows]
    show_overpayment = has_overpayments and any(overpayment)
    series: list[dict[str, object]] = [
        {
            "name": gettext("Principal", locale),
            "values": principal,
            "color": "primary-ink",
        },
        {
            "name": gettext("Interest", locale),
            "values": interest,
            "color": "success",
        },
        {
            "name": gettext("Costs", locale),
            "values": costs,
            "color": "warning-strong",
        },
    ]
    if show_overpayment:
        series.append(
            {
                "name": gettext("Overpayment", locale),
                "values": overpayment,
                "color": "accent",
            }
        )
    totals = [
        principal[i] + interest[i] + costs[i] + (overpayment[i] if show_overpayment else 0)
        for i in range(len(rows))
    ]
    low, high, step, axis_labels = money_axis(totals, schedule.currency, locale)
    tooltips = []
    for row in rows:
        lines = [
            f"{gettext('Principal', locale)}: {format_money(row.principal, locale)}",
            f"{gettext('Interest', locale)}: {format_money(row.interest, locale)}",
            f"{gettext('Costs', locale)}: {format_money(row.costs, locale)}",
        ]
        if show_overpayment:
            lines.append(
                f"{gettext('Overpayment', locale)}: {format_money(row.overpayment, locale)}"
            )
        lines.append(f"{gettext('Total', locale)}: {format_money(row.installment_total, locale)}")
        if row.n > 1 and rows[row.n - 2].annual_rate_bp != row.annual_rate_bp:
            lines.append(
                gettext("Rate change: %(rate)s", locale)
                % {"rate": format_rate_short(row.annual_rate_bp, locale)}
            )
        tooltips.append(lines)
    data: dict[str, object] = {
        "labels": labels,
        "series": series,
        "y_min": low,
        "y_max": high,
        "y_interval": step,
        "y_labels": axis_labels,
        "tooltips": tooltips,
        "markers": _rate_markers(rows, offset=0, locale=locale),
    }
    points = tuple(
        PaymentPoint(
            date=labels[index],
            principal=format_money(rows[index].principal, locale),
            interest=format_money(rows[index].interest, locale),
            costs=format_money(rows[index].costs, locale),
            overpayment=(
                format_money(rows[index].overpayment, locale) if show_overpayment else "—"
            ),
            total=format_money(rows[index].installment_total, locale),
        )
        for index in range(len(rows))
    )
    return data, points, show_overpayment


def build_loan_charts(
    schedule: Schedule | None,
    baseline: Schedule | None,
    locale: str,
) -> LoanCharts:
    if schedule is None or baseline is None or not schedule.rows:
        return LoanCharts(None, (), None, (), False, False)
    balance, balance_points, has_baseline = _balance_data(schedule, baseline, locale)
    payments, payment_points, has_overpayments = _payments_data(
        schedule,
        has_overpayments=has_baseline,
        locale=locale,
    )
    return LoanCharts(
        balance=balance,
        balance_points=balance_points,
        payments=payments,
        payment_points=payment_points,
        has_baseline=has_baseline,
        has_overpayments=has_overpayments,
    )


@dataclass(frozen=True, slots=True)
class ScenarioChartSeries:
    name: str
    color: str


@dataclass(frozen=True, slots=True)
class ScenarioChartPoint:
    date: str
    values: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ScenarioChart:
    data: dict[str, object] | None
    series: tuple[ScenarioChartSeries, ...]
    points: tuple[ScenarioChartPoint, ...]


SCENARIO_COLORS = ("success", "warning-strong", "accent", "line-strong", "ink")


def build_scenario_chart(comparison: LoanScenarioComparison, locale: str) -> ScenarioChart:
    """Overlay the baseline, current plan and every scenario's closing balance."""
    entries = [
        (row.label, row.is_baseline, row.is_current, row.schedule)
        for row in comparison.rows
        if row.schedule is not None and row.schedule.rows
    ]
    if not entries:
        return ScenarioChart(None, (), ())
    source = max((entry[3] for entry in entries), key=lambda schedule: len(schedule.rows))
    assert source is not None
    max_rows = len(source.rows)
    labels = [format_day(source.anchor_date, locale)] + [
        format_day(row.date, locale) for row in source.rows
    ]
    series: list[dict[str, object]] = []
    series_meta: list[ScenarioChartSeries] = []
    scenario_index = 0
    points_by_index: list[list[str]] = [[] for _ in labels]
    all_values: list[int] = []
    for label, is_baseline, is_current, schedule in entries:
        assert schedule is not None
        if is_baseline:
            color = "muted"
        elif is_current:
            color = "primary-ink"
        else:
            color = SCENARIO_COLORS[scenario_index % len(SCENARIO_COLORS)]
            scenario_index += 1
        values: list[int | None] = [schedule.starting_balance.amount_minor]
        values.extend(row.closing.amount_minor for row in schedule.rows)
        values.extend([None] * (max_rows - len(schedule.rows)))
        all_values.extend(value for value in values if value is not None)
        amounts = [_amount(value, schedule.currency, locale) for value in values]
        for index, amount in enumerate(amounts):
            points_by_index[index].append(amount)
        series.append(
            {
                "name": label,
                "values": values,
                "amounts": amounts,
                "color": color,
            }
        )
        series_meta.append(ScenarioChartSeries(name=label, color=color))
    low, high, step, axis_labels = money_axis(all_values, source.currency, locale)

    marker_entries: list[tuple[int, str, str]] = []
    seen: set[tuple[int, int]] = set()
    marker_lines: dict[int, list[str]] = {}
    for _label, _baseline, _current, schedule in entries:
        assert schedule is not None
        previous: int | None = None
        for index, row in enumerate(schedule.rows):
            if previous is not None and row.annual_rate_bp != previous:
                key = (index + 1, row.annual_rate_bp)
                if key not in seen:
                    seen.add(key)
                    rate = format_rate_short(row.annual_rate_bp, locale)
                    text = gettext("Rate change: %(rate)s", locale) % {"rate": rate}
                    marker_entries.append((index + 1, rate, text))
                    marker_lines.setdefault(index + 1, []).append(text)
            previous = row.annual_rate_bp
    marker_entries.sort(key=lambda marker: marker[0])
    markers = [
        {"index": index, "label": rate, "text": text} for index, rate, text in marker_entries
    ]
    tooltips = [marker_lines.get(index, []) for index in range(len(labels))]
    data: dict[str, object] = {
        "labels": labels,
        "series": series,
        "y_min": low,
        "y_max": high,
        "y_interval": step,
        "y_labels": axis_labels,
        "tooltips": tooltips,
        "markers": markers,
    }
    points = tuple(
        ScenarioChartPoint(
            date=labels[index],
            values=tuple(points_by_index[index]),
        )
        for index in range(len(labels))
    )
    return ScenarioChart(data, tuple(series_meta), points)


__all__ = [
    "BalancePoint",
    "LoanCharts",
    "PaymentPoint",
    "ScenarioChart",
    "ScenarioChartPoint",
    "ScenarioChartSeries",
    "build_loan_charts",
    "build_scenario_chart",
]
