"""View models for the net-worth summary (F5.2): big figure, bar, tables and donuts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from assetboard.domain.charts import (
    DEFAULT_MAX_SLICES,
    CategoryAmount,
    category_slices,
)
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
ASSET_EMPTY = _("No assets with a value yet")
LIABILITY_EMPTY = _("No liabilities with a value yet")
OTHER_CATEGORIES = _("Other categories")
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
    donut: DonutView | None


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
    asset_side: SummarySide
    liability_side: SummarySide
    non_base_items: tuple[WarningItem, ...]
    no_value_items: tuple[WarningItem, ...]
    excluded_items: tuple[WarningItem, ...]

    @property
    def has_donut(self) -> bool:
        return self.asset_side.donut is not None or self.liability_side.donut is not None


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


def _donut(
    *,
    key: NetWorthDonutKey,
    heading: str,
    empty: str,
    categories: tuple[CategoryTotal, ...],
    metadata: dict[UUID, SummaryCategory],
    currency: str,
    total: str,
    locale: str,
) -> DonutView | None:
    slices = category_slices(
        (CategoryAmount(category.category_id, category.total) for category in categories),
        currency=currency,
        max_slices=DEFAULT_MAX_SLICES,
    )
    if not slices:
        return None
    return DonutView(
        key=key,
        heading=gettext(heading, locale),
        empty=gettext(empty, locale),
        currency=currency,
        total=total,
        slices=tuple(
            DonutSlice(
                name=(
                    gettext(OTHER_CATEGORIES, locale)
                    if item.category_id is None
                    else metadata[item.category_id].name
                ),
                color=(None if item.category_id is None else metadata[item.category_id].color),
                amount_minor=item.amount.amount_minor,
                amount=format_money(item.amount, locale),
                share=format_rate_tenths(item.share_tenths, locale),
            )
            for item in slices
        ),
    )


def _side(
    *,
    key: NetWorthDonutKey,
    heading: str,
    empty: str,
    categories: tuple[CategoryTotal, ...],
    metadata: dict[UUID, SummaryCategory],
    total: Money,
    currency: str,
    locale: str,
) -> SummarySide:
    rows = tuple(_row(category, metadata[category.category_id], locale) for category in categories)
    total_text = format_money(total, locale)
    return SummarySide(
        key=key,
        heading=gettext(heading, locale),
        total=total_text,
        total_share=(format_rate_tenths(1000, locale) if total.amount_minor > 0 else None),
        rows=rows,
        donut=_donut(
            key=key,
            heading=heading,
            empty=empty,
            categories=categories,
            metadata=metadata,
            currency=currency,
            total=total_text,
            locale=locale,
        ),
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
        asset_side=_side(
            key="assets",
            heading=ASSET_HEADING,
            empty=ASSET_EMPTY,
            categories=summary.asset_categories,
            metadata=data.categories,
            total=summary.assets,
            currency=summary.currency,
            locale=locale,
        ),
        liability_side=_side(
            key="liabilities",
            heading=LIABILITY_HEADING,
            empty=LIABILITY_EMPTY,
            categories=summary.liability_categories,
            metadata=data.categories,
            total=summary.liabilities,
            currency=summary.currency,
            locale=locale,
        ),
        non_base_items=tuple(_warning(item, locale) for item in data.non_base_items),
        no_value_items=tuple(_warning(item, locale) for item in data.no_value_items),
        excluded_items=tuple(_warning(item, locale) for item in data.excluded_items),
    )
