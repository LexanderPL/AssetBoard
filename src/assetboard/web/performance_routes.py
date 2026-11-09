"""Performance page (F6.7): chart and contribution-adjusted period returns.

The view formats money, percentages and dates with the locale helpers; the server sends minor
units and pre-formatted labels to the chart module. The page is read-only, so there is no CSRF
dependency. Range and class are GET parameters.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from assetboard.domain.money import Money
from assetboard.i18n import gettext
from assetboard.i18n.dates import format_day
from assetboard.i18n.numbers import (
    format_money,
    format_rate_basis_points,
    format_rate_tenths,
)
from assetboard.services.context import Context, Role, has_role
from assetboard.services.performance import (
    RANGES,
    PerformancePage,
    normalize_range,
    performance_page,
)
from assetboard.web.context import CurrentContext
from assetboard.web.errors import is_htmx_fragment_request
from assetboard.web.rendering import render_template

router = APIRouter()


def _(message: str) -> str:
    """Marks a message for extraction; it is translated where it is rendered."""
    return message


RANGE_LABELS = {
    "ytd": _("Year to date"),
    "1y": _("1 year"),
    "3y": _("3 years"),
    "5y": _("5 years"),
    "all": _("All time"),
}
PERIOD_LABELS = {
    "1m": _("1 month"),
    "3m": _("3 months"),
    "ytd": _("Year to date"),
    "1y": _("1 year"),
    "all": _("All time"),
}
WHOLE_PORTFOLIO = _("Whole portfolio")


@dataclass(frozen=True, slots=True)
class RangeOptionView:
    code: str
    label: str
    active: bool


@dataclass(frozen=True, slots=True)
class ClassOptionView:
    value: str
    label: str
    selected: bool


@dataclass(frozen=True, slots=True)
class ChartPointView:
    date: str
    value: str
    invested: str
    incomplete: bool


@dataclass(frozen=True, slots=True)
class PeriodRowView:
    code: str
    label: str
    start: str
    end: str
    start_value: str
    end_value: str
    change: str
    change_pct: str | None
    net_flows: str
    change_excluding_flows: str
    return_value: str | None
    return_label: str
    incomplete: bool


@dataclass(frozen=True, slots=True)
class PerformanceView:
    currency: str
    today: str
    range_code: str
    range_options: tuple[RangeOptionView, ...]
    class_options: tuple[ClassOptionView, ...]
    periods: tuple[PeriodRowView, ...]
    chart: dict[str, Any] | None
    points: tuple[ChartPointView, ...]
    undated_count: int
    incomplete_points: int
    has_data: bool

    @property
    def has_chart(self) -> bool:
        return self.chart is not None


def _rate_text(rate: Decimal | None, locale: str) -> str | None:
    if rate is None:
        return None
    with localcontext() as context:
        context.prec = max(context.prec, 80)
        basis_points = int((rate * Decimal(10_000)).to_integral_value(rounding=ROUND_HALF_UP))
    return format_rate_basis_points(basis_points, locale)


def _nice_step(span: int) -> int:
    raw = max(1, span // 5)
    magnitude = 10 ** (len(str(raw)) - 1)
    for factor in (1, 2, 5, 10):
        step = factor * magnitude
        if step >= raw:
            return step
    return 10 * magnitude


def _axis(values: list[int], currency: str, locale: str) -> tuple[int, int, int, dict[str, str]]:
    low_value = min((0, *values))
    high_value = max((0, *values))
    if high_value == low_value:
        high_value = low_value + 1
    step = _nice_step(high_value - low_value)
    low = (low_value // step) * step
    high = -((-high_value) // step) * step
    if high == low:
        high = low + step
    labels = {
        str(tick): format_money(Money(tick, currency), locale)
        for tick in range(low, high + 1, step)
    }
    return low, high, step, labels


def _chart(page: PerformancePage, locale: str) -> dict[str, Any] | None:
    if not page.chart:
        return None
    values: list[int | None] = [
        None if point.incomplete else point.value.amount_minor for point in page.chart
    ]
    invested: list[int | None] = [
        None if point.incomplete else point.net_invested.amount_minor for point in page.chart
    ]
    gain: list[int | None] = [
        None if value is None or cost is None else value - cost
        for value, cost in zip(values, invested, strict=True)
    ]
    complete = [value for value in (*values, *invested) if value is not None]
    low, high, step, axis_labels = _axis(complete, page.currency, locale)
    dash = "—"
    incomplete_text = gettext("Incomplete: an instrument has no price or FX rate.", locale)
    tooltips: list[list[str]] = []
    for point in page.chart:
        if point.incomplete:
            tooltips.append([incomplete_text])
            continue
        gain_minor = point.value.amount_minor - point.net_invested.amount_minor
        tooltips.append(
            [
                f"{gettext('Value', locale)}: {format_money(point.value, locale)}",
                f"{gettext('Invested capital', locale)}: "
                f"{format_money(point.net_invested, locale)}",
                f"{gettext('Gain', locale)}: "
                f"{format_money(Money(gain_minor, page.currency), locale)}",
            ]
        )
    return {
        "labels": [format_day(point.date, locale) for point in page.chart],
        "y_min": low,
        "y_max": high,
        "y_interval": step,
        "y_labels": axis_labels,
        "value": values,
        "invested": invested,
        "gain": gain,
        "value_amounts": [
            dash if amount is None else format_money(Money(amount, page.currency), locale)
            for amount in values
        ],
        "invested_amounts": [
            dash if amount is None else format_money(Money(amount, page.currency), locale)
            for amount in invested
        ],
        "gain_amounts": [
            dash if amount is None else format_money(Money(amount, page.currency), locale)
            for amount in gain
        ],
        "tooltips": tooltips,
        "legend": {
            "value": gettext("Value", locale),
            "invested": gettext("Invested capital", locale),
        },
    }


def build_view(request: Request, page: PerformancePage, locale: str) -> PerformanceView:
    periods: list[PeriodRowView] = []
    for row in page.periods:
        periods.append(
            PeriodRowView(
                code=row.code,
                label=gettext(PERIOD_LABELS[row.code], locale),
                start=format_day(row.start, locale),
                end=format_day(row.end, locale),
                start_value=format_money(row.start_value, locale),
                end_value=format_money(row.end_value, locale),
                change=format_money(row.change, locale),
                change_pct=(
                    format_rate_tenths(row.change_tenths, locale)
                    if row.change_tenths is not None
                    else None
                ),
                net_flows=format_money(row.net_flows, locale),
                change_excluding_flows=format_money(row.change_excluding_flows, locale),
                return_value=_rate_text(row.display_rate, locale),
                return_label=(
                    gettext("Annualised XIRR", locale)
                    if row.annualized
                    else gettext("Period return", locale)
                ),
                incomplete=row.incomplete,
            )
        )
    range_options = tuple(
        RangeOptionView(
            code=code, label=gettext(RANGE_LABELS[code], locale), active=code == page.range_code
        )
        for code in RANGES
    )
    class_options = [
        ClassOptionView("", gettext(WHOLE_PORTFOLIO, locale), page.class_scope is None)
    ]
    class_options.extend(
        ClassOptionView(
            str(row.id), row.name, page.class_scope is not None and row.id == page.class_scope.id
        )
        for row in page.class_options
    )
    points = tuple(
        ChartPointView(
            date=format_day(point.date, locale),
            value="—" if point.incomplete else format_money(point.value, locale),
            invested="—" if point.incomplete else format_money(point.net_invested, locale),
            incomplete=point.incomplete,
        )
        for point in page.chart
    )
    return PerformanceView(
        currency=page.currency,
        today=format_day(page.today, locale),
        range_code=normalize_range(page.range_code),
        range_options=range_options,
        class_options=tuple(class_options),
        periods=tuple(periods),
        chart=_chart(page, locale),
        points=points,
        undated_count=page.undated_count,
        incomplete_points=page.incomplete_points,
        has_data=page.first_date is not None,
    )


def _render(request: Request, ctx: Context, range_code: str, class_id: str) -> Response:
    try:
        parsed_class = UUID(class_id) if class_id else None
    except ValueError:
        parsed_class = None
    page = performance_page(
        request.app.state.session_factory,
        ctx,
        range_code=range_code,
        asset_class_id=parsed_class,
    )
    view = build_view(request, page, request.state.locale)
    context = {
        "view": view,
        "can_edit": has_role(ctx, Role.EDITOR),
        "can_manage_providers": has_role(ctx, Role.OWNER),
    }
    template = (
        "investments/_performance_body.html"
        if is_htmx_fragment_request(request)
        else "investments/performance.html"
    )
    response = render_template(request, template, context=context)
    response.headers["Vary"] = "HX-Request"
    return response


@router.get("/investments/performance", name="investments_performance")
async def performance(
    request: Request, ctx: CurrentContext, range: str = "", class_id: str = ""
) -> Response:
    return await run_in_threadpool(lambda: _render(request, ctx, range.strip(), class_id.strip()))
