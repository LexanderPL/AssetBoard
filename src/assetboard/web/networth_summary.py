"""View models for the net-worth summary (F5.2): big figure, bar, tables and one combined donut."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from assetboard.domain.charts import share_tenths
from assetboard.domain.money import Money
from assetboard.domain.networth_summary import CategoryTotal, NetWorthSummary
from assetboard.i18n import gettext
from assetboard.i18n.numbers import format_money, format_rate_tenths
from assetboard.services.networth import ItemView
from assetboard.services.networth_summary import NetWorthSummaryData, SummaryCategory
from assetboard.web.charts import DonutSlice, DonutView, NetWorthDonutKey

NetWorthTone = Literal["favourable", "unfavourable", "neutral"]


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


ASSET_HEADING = _("Assets by category")
LIABILITY_HEADING = _("Liabilities by category")
COMBINED_HEADING = _("Assets and liabilities")
COMBINED_EMPTY = _("No assets or liabilities yet")
ASSET_SLICE_COLOR = "#0277bd"  # the palette's primary blue
LIABILITY_SLICE_COLOR = "#c62828"  # the palette's danger red
BAR_SCALE = 1000


@dataclass(frozen=True, slots=True)
class CategoryRow:
    id: str
    name: str
    color: str
    total: str
    share: str | None


@dataclass(frozen=True, slots=True)
class SummarySide:
    key: NetWorthDonutKey
    heading: str
    total: str
    total_share: str | None
    rows: tuple[CategoryRow, ...]


@dataclass(frozen=True, slots=True)
class SummaryBar:
    assets_width: int
    liabilities_width: int
    scale: int = BAR_SCALE

    @property
    def empty(self) -> bool:
        return self.assets_width == 0 and self.liabilities_width == 0


@dataclass(frozen=True, slots=True)
class WarningItem:
    name: str
    category: str
    value: str
    currency: str


@dataclass(frozen=True, slots=True)
class NetWorthSummaryView:
    currency: str
    assets: str
    liabilities: str
    net: str
    net_tone: NetWorthTone
    bar: SummaryBar
    combined_donut: DonutView | None
    asset_side: SummarySide
    liability_side: SummarySide
    non_base_items: tuple[WarningItem, ...]
    no_value_items: tuple[WarningItem, ...]
    excluded_items: tuple[WarningItem, ...]
    unavailable_items: tuple[WarningItem, ...]

    @property
    def has_donut(self) -> bool:
        return bool(self.combined_donut and self.combined_donut.slices)


def _bar(summary: NetWorthSummary) -> SummaryBar:
    whole = max(summary.assets.amount_minor, summary.liabilities.amount_minor)
    if whole <= 0:
        return SummaryBar(0, 0)
    assets_width = (
        max(1, summary.assets.amount_minor * BAR_SCALE // whole)
        if summary.assets.amount_minor > 0
        else 0
    )
    liabilities_width = (
        max(1, summary.liabilities.amount_minor * BAR_SCALE // whole)
        if summary.liabilities.amount_minor > 0
        else 0
    )
    return SummaryBar(
        assets_width=assets_width,
        liabilities_width=liabilities_width,
    )


def _row(total: CategoryTotal, category: SummaryCategory, locale: str) -> CategoryRow:
    return CategoryRow(
        id=str(total.category_id),
        name=category.name,
        color=category.color,
        total=format_money(total.total, locale),
        share=(
            format_rate_tenths(total.share_tenths, locale)
            if total.share_tenths is not None
            else None
        ),
    )


def _combined_donut(summary: NetWorthSummary, locale: str) -> DonutView:
    """Assets and liabilities as slices of the gross total, not of net worth."""
    gross = summary.assets.amount_minor + summary.liabilities.amount_minor
    slices = tuple(
        DonutSlice(
            name=gettext(name, locale),
            color=color,
            amount_minor=amount.amount_minor,
            amount=format_money(amount, locale),
            share=format_rate_tenths(share_tenths(amount.amount_minor, gross), locale),
        )
        for name, amount, color in (
            (_("Assets"), summary.assets, ASSET_SLICE_COLOR),
            (_("Liabilities"), summary.liabilities, LIABILITY_SLICE_COLOR),
        )
        if amount.amount_minor > 0
    )
    return DonutView(
        key="combined",
        heading=gettext(COMBINED_HEADING, locale),
        empty=gettext(COMBINED_EMPTY, locale),
        currency=summary.currency,
        total=format_money(Money(gross, summary.currency), locale),
        slices=slices,
    )


def _side(
    *,
    key: NetWorthDonutKey,
    heading: str,
    categories: tuple[CategoryTotal, ...],
    metadata: dict[UUID, SummaryCategory],
    total: Money,
    locale: str,
) -> SummarySide:
    rows = tuple(_row(category, metadata[category.category_id], locale) for category in categories)
    return SummarySide(
        key=key,
        heading=gettext(heading, locale),
        total=format_money(total, locale),
        total_share=(format_rate_tenths(1000, locale) if total.amount_minor > 0 else None),
        rows=rows,
    )


def _warning(item: ItemView, locale: str) -> WarningItem:
    return WarningItem(
        name=item.name,
        category=item.category_name,
        value=format_money(item.current, locale) if item.current is not None else "—",
        currency=item.currency,
    )


def build_summary_view(data: NetWorthSummaryData, locale: str) -> NetWorthSummaryView:
    summary = data.summary
    net_tone: NetWorthTone
    if summary.net.amount_minor > 0:
        net_tone = "favourable"
    elif summary.net.amount_minor < 0:
        net_tone = "unfavourable"
    else:
        net_tone = "neutral"
    return NetWorthSummaryView(
        currency=summary.currency,
        assets=format_money(summary.assets, locale),
        liabilities=format_money(summary.liabilities, locale),
        net=format_money(summary.net, locale),
        net_tone=net_tone,
        bar=_bar(summary),
        combined_donut=_combined_donut(summary, locale),
        asset_side=_side(
            key="assets",
            heading=ASSET_HEADING,
            categories=summary.asset_categories,
            metadata=data.categories,
            total=summary.assets,
            locale=locale,
        ),
        liability_side=_side(
            key="liabilities",
            heading=LIABILITY_HEADING,
            categories=summary.liability_categories,
            metadata=data.categories,
            total=summary.liabilities,
            locale=locale,
        ),
        non_base_items=tuple(_warning(item, locale) for item in data.non_base_items),
        no_value_items=tuple(_warning(item, locale) for item in data.no_value_items),
        excluded_items=tuple(_warning(item, locale) for item in data.excluded_items),
        unavailable_items=tuple(_warning(item, locale) for item in data.unavailable_items),
    )
