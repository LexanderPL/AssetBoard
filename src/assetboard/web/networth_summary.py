"""View models for the net-worth summary (F5.2): big figure, combined bar and category tables."""

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

NetWorthTone = Literal["favourable", "unfavourable", "neutral"]
NetWorthSideKey = Literal["assets", "liabilities"]


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


ASSET_HEADING = _("Assets by category")
LIABILITY_HEADING = _("Liabilities by category")
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
    key: NetWorthSideKey
    heading: str
    total: str
    total_share: str | None
    rows: tuple[CategoryRow, ...]


@dataclass(frozen=True, slots=True)
class SummaryBar:
    assets_width: int
    liabilities_width: int
    caption: str
    scale: int = BAR_SCALE

    @property
    def empty(self) -> bool:
        return self.assets_width == 0 and self.liabilities_width == 0

    @property
    def liabilities_start(self) -> int:
        return max(0, self.assets_width - self.liabilities_width)

    @property
    def covered_width(self) -> int:
        return min(self.assets_width, self.liabilities_width)

    @property
    def deficit_width(self) -> int:
        return max(0, self.liabilities_width - self.assets_width)


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
    unavailable_items: tuple[WarningItem, ...]


def _bar(summary: NetWorthSummary, locale: str) -> SummaryBar:
    """One bar on the larger side's scale: green assets, red liabilities, hatched deficit."""
    assets = summary.assets.amount_minor
    liabilities = summary.liabilities.amount_minor
    whole = max(assets, liabilities)
    if whole <= 0:
        return SummaryBar(0, 0, gettext(_("No assets or liabilities yet."), locale))
    assets_width = max(1, assets * BAR_SCALE // whole) if assets > 0 else 0
    liabilities_width = max(1, liabilities * BAR_SCALE // whole) if liabilities > 0 else 0
    if liabilities == 0:
        caption = gettext(_("No liabilities; the whole bar is net worth."), locale)
    elif assets == 0:
        caption = gettext(_("Liabilities exceed assets by %(amount)s."), locale) % {
            "amount": format_money(Money(liabilities - assets, summary.currency), locale)
        }
    elif assets == liabilities:
        caption = gettext(_("Liabilities equal assets; net worth is zero."), locale)
    elif liabilities < assets:
        caption = gettext(
            _("Liabilities are %(rate)s of assets; the green part is net worth."), locale
        ) % {"rate": format_rate_tenths(share_tenths(liabilities, assets), locale)}
    else:
        caption = gettext(
            _("Liabilities are %(rate)s of assets; they exceed assets by %(amount)s."), locale
        ) % {
            "rate": format_rate_tenths(share_tenths(liabilities, assets), locale),
            "amount": format_money(Money(liabilities - assets, summary.currency), locale),
        }
    return SummaryBar(
        assets_width=assets_width,
        liabilities_width=liabilities_width,
        caption=caption,
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


def _side(
    *,
    key: NetWorthSideKey,
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
        bar=_bar(summary, locale),
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
