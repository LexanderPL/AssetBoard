"""View models for net-worth history (F5.3): chart data, table rows and source badges."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from assetboard.domain.money import Money
from assetboard.domain.months import YearMonth
from assetboard.domain.networth_history import HISTORY_RANGES, month_end
from assetboard.i18n import gettext, ngettext
from assetboard.i18n.dates import format_day, format_month
from assetboard.i18n.numbers import format_money
from assetboard.services.networth_snapshots import (
    HistoryCategory,
    HistorySnapshot,
    NetWorthHistoryData,
)


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


RANGE_LABELS = {
    "1y": _("1 year"),
    "3y": _("3 years"),
    "5y": _("5 years"),
    "all": _("All time"),
}
SOURCE_LABELS = {
    "manual": _("Manual"),
    "auto": _("Automatic"),
    "backfill": _("From history"),
}
SERIES_COLORS = {
    "net": "primary-ink",
    "assets": "success",
    "liabilities": "danger",
}


@dataclass(frozen=True, slots=True)
class RangeOption:
    code: str
    label: str
    active: bool


@dataclass(frozen=True, slots=True)
class BreakdownRow:
    name: str
    side: str
    amount: str
    color: str | None


@dataclass(frozen=True, slots=True)
class HistoryRow:
    as_of: date
    as_of_text: str
    source: str
    source_label: str
    net: str
    assets: str
    liabilities: str
    currency: str
    charted: bool
    breakdown: tuple[BreakdownRow, ...]
    not_counted: tuple[str, ...]
    estimated_note: str | None


@dataclass(frozen=True, slots=True)
class HistoryView:
    base_currency: str
    range_code: str
    range_options: tuple[RangeOption, ...]
    rows: tuple[HistoryRow, ...]
    chart: dict[str, object] | None
    other_currency_count: int
    can_edit: bool
    notice: str | None
    error: str | None

    @property
    def has_chart(self) -> bool:
        return self.chart is not None


def _not_counted(point: HistorySnapshot, locale: str) -> tuple[str, ...]:
    lines: list[str] = []
    if point.breakdown.non_base:
        count = point.breakdown.non_base
        lines.append(
            ngettext(
                "%(num)d item in another currency was not counted.",
                "%(num)d items in another currency were not counted.",
                count,
                locale,
            )
            % {"num": count}
        )
    if point.breakdown.no_value:
        count = point.breakdown.no_value
        lines.append(
            ngettext(
                "%(num)d item had no value yet and was not counted.",
                "%(num)d items had no value yet and were not counted.",
                count,
                locale,
            )
            % {"num": count}
        )
    if point.breakdown.unavailable:
        count = point.breakdown.unavailable
        lines.append(
            ngettext(
                "%(num)d linked item was unavailable and was not counted.",
                "%(num)d linked items were unavailable and were not counted.",
                count,
                locale,
            )
            % {"num": count}
        )
    return tuple(lines)


def _estimated_note(point: HistorySnapshot, locale: str) -> str | None:
    if point.breakdown.estimated <= 0:
        return None
    return ngettext(
        "includes %(num)d estimated value",
        "includes %(num)d estimated values",
        point.breakdown.estimated,
        locale,
    ) % {"num": point.breakdown.estimated}


def _breakdown(
    point: HistorySnapshot,
    categories: dict[UUID, HistoryCategory],
    locale: str,
) -> tuple[BreakdownRow, ...]:
    rows: list[BreakdownRow] = []
    for total in point.breakdown.categories:
        category: HistoryCategory | None = categories.get(total.category_id)
        name = category.name if category is not None else gettext("Deleted category", locale)
        side = category.side.value if category is not None else total.side.value
        side_label = "Assets" if side == "asset" else "Liabilities"
        rows.append(
            BreakdownRow(
                name=name,
                side=gettext(side_label, locale),
                amount=format_money(Money(total.total_minor, point.currency), locale),
                color=category.color if category is not None else None,
            )
        )
    return tuple(rows)


def _point_label(day: date, locale: str) -> str:
    """A month-end says its month; any other date says the day, so labels stay distinct."""
    if day == month_end(day):
        return format_month(YearMonth.from_date(day), locale)
    return format_day(day, locale)


def _nice_step(span: int) -> int:
    raw = max(1, span // 5)
    magnitude = 10 ** (len(str(raw)) - 1)
    for factor in (1, 2, 5, 10):
        step = factor * magnitude
        if step >= raw:
            return step
    return 10 * magnitude


def _axis(values: list[int], currency: str, locale: str) -> tuple[int, int, int, dict[str, str]]:
    """Nice integer ticks with labels formatted on the server, so no money is parsed in JS."""
    low_value = min(0, *values)
    high_value = max(0, *values)
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


def _chart(
    points: tuple[HistorySnapshot, ...],
    categories: dict[UUID, HistoryCategory],
    *,
    base_currency: str,
    locale: str,
) -> dict[str, object] | None:
    if not points:
        return None
    net = [point.net.amount_minor for point in points]
    assets = [point.assets.amount_minor for point in points]
    liabilities = [point.liabilities.amount_minor for point in points]
    low, high, step, axis_labels = _axis(net + assets + liabilities, base_currency, locale)
    tooltips = []
    for point in points:
        lines = [
            f"{row.side} · {row.name}: {row.amount}"
            for row in _breakdown(point, categories, locale)
        ]
        lines.extend(_not_counted(point, locale))
        estimated_note = _estimated_note(point, locale)
        if estimated_note is not None:
            lines.append(estimated_note)
        tooltips.append(lines)
    return {
        "labels": [_point_label(point.as_of, locale) for point in points],
        "y_min": low,
        "y_max": high,
        "y_interval": step,
        "y_labels": axis_labels,
        "tooltips": tooltips,
        "series": [
            {
                "key": "net",
                "name": gettext("Net worth", locale),
                "color": SERIES_COLORS["net"],
                "values": net,
                "amounts": [format_money(point.net, locale) for point in points],
            },
            {
                "key": "assets",
                "name": gettext("Assets", locale),
                "color": SERIES_COLORS["assets"],
                "values": assets,
                "amounts": [format_money(point.assets, locale) for point in points],
            },
            {
                "key": "liabilities",
                "name": gettext("Liabilities", locale),
                "color": SERIES_COLORS["liabilities"],
                "values": liabilities,
                "amounts": [format_money(point.liabilities, locale) for point in points],
            },
        ],
    }


def build_history_view(
    data: NetWorthHistoryData,
    locale: str,
    *,
    can_edit: bool,
    notice: str | None = None,
    error: str | None = None,
) -> HistoryView:
    rows = tuple(
        HistoryRow(
            as_of=point.as_of,
            as_of_text=_point_label(point.as_of, locale),
            source=point.source,
            source_label=gettext(SOURCE_LABELS.get(point.source, point.source), locale),
            net=format_money(point.net, locale),
            assets=format_money(point.assets, locale),
            liabilities=format_money(point.liabilities, locale),
            currency=point.currency,
            charted=point.charted,
            breakdown=_breakdown(point, data.categories, locale),
            not_counted=_not_counted(point, locale),
            estimated_note=_estimated_note(point, locale),
        )
        for point in data.snapshots
    )
    return HistoryView(
        base_currency=data.base_currency,
        range_code=data.range_code,
        range_options=tuple(
            RangeOption(
                code=code,
                label=gettext(RANGE_LABELS[code], locale),
                active=code == data.range_code,
            )
            for code in HISTORY_RANGES
        ),
        rows=rows,
        chart=_chart(
            data.charted,
            data.categories,
            base_currency=data.base_currency,
            locale=locale,
        ),
        other_currency_count=data.other_currency_count,
        can_edit=can_edit,
        notice=notice,
        error=error,
    )
