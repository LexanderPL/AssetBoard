"""The category donuts of the Home and Month pages (F2.7): view models.

``domain.charts.category_breakdown`` builds the slices from the month summary; this module adds
the category names and colours and the labels formatted by the server's locale helpers. The
template (``macros/charts.html``) renders the data as a ``<script type="application/json">``
block read by ``static/js/charts.js`` and as a table (visible without JavaScript, sr-only once
the chart is drawn). Amounts travel as minor-unit integers with the currency; the browser never
parses or rounds money.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from assetboard.domain.charts import ChartBlock, category_breakdown, share_tenths
from assetboard.i18n import gettext
from assetboard.i18n.numbers import format_money, format_rate_tenths
from assetboard.services.portfolio import PortfolioPage
from assetboard.services.summary import SummaryData

PortfolioDonutKey = Literal["portfolio", "portfolio-classes"]
DonutKey = ChartBlock | PortfolioDonutKey


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


HEADINGS: dict[ChartBlock, str] = {
    "expense": _("Expenses by category"),
    "income": _("Income by category"),
}
EMPTY: dict[ChartBlock, str] = {
    "expense": _("No expenses yet this month"),
    "income": _("No income yet this month"),
}
OTHER = _("Other")
UNALLOCATED = _("Unallocated")


@dataclass(frozen=True, slots=True)
class DonutSlice:
    name: str
    color: str | None  # "#rrggbb"; None: the "Other" slice (a theme colour, chosen in charts.js)
    amount_minor: int
    amount: str  # formatted with the user's locale
    share: str  # e.g. "33.3%" in the user's locale


@dataclass(frozen=True, slots=True)
class DonutView:
    key: DonutKey
    heading: str
    empty: str
    currency: str
    total: str
    slices: tuple[DonutSlice, ...]

    @property
    def data(self) -> dict[str, object]:
        """The JSON the chart module reads (rendered with Jinja's ``tojson``)."""
        return {
            "key": self.key,
            "label": self.heading,
            "currency": self.currency,
            "total": self.total,
            "slices": [
                {
                    "name": s.name,
                    "color": s.color,
                    "value": s.amount_minor,
                    "amount": s.amount,
                    "share": s.share,
                }
                for s in self.slices
            ],
        }


def build_donut(data: SummaryData, block: ChartBlock, locale: str) -> DonutView:
    """The donut of ``block`` (``expense`` or ``income``) of a month's summary."""
    summary = data.summary
    slices = []
    for s in category_breakdown(summary, block):
        if s.category_id is None:
            name, color = gettext(OTHER, locale), None
        else:
            category = data.categories[s.category_id]
            name, color = category.name, category.color
        slices.append(
            DonutSlice(
                name=name,
                color=color,
                amount_minor=s.amount.amount_minor,
                amount=format_money(s.amount, locale),
                share=format_rate_tenths(s.share_tenths, locale),
            )
        )
    return DonutView(
        key=block,
        heading=gettext(HEADINGS[block], locale),
        empty=gettext(EMPTY[block], locale),
        currency=summary.currency,
        total=format_money(summary.block(block).actual, locale),
        slices=tuple(slices),
    )


def build_class_donut(page: PortfolioPage, locale: str) -> DonutView:
    """The look-through value of every asset class with a positive value, plus Unallocated.

    Shared by the portfolio page and the Home mini-donut: both use the same F6.6 valuation.
    """
    totals = {total.asset_class_id: total for total in page.valuation.classes}
    total_minor = page.valuation.value.amount_minor
    slices: list[DonutSlice] = []
    for row in page.class_rows:
        class_id = row.asset_class.id if row.asset_class is not None else None
        total = totals.get(class_id)
        if total is None or total.value.amount_minor <= 0:
            continue
        if row.asset_class is None:
            name, color = gettext(UNALLOCATED, locale), None
        else:
            name, color = row.asset_class.name, row.asset_class.color
        slices.append(
            DonutSlice(
                name=name,
                color=color,
                amount_minor=total.value.amount_minor,
                amount=format_money(total.value, locale),
                share=format_rate_tenths(
                    share_tenths(total.value.amount_minor, total_minor), locale
                ),
            )
        )
    return DonutView(
        key="portfolio-classes",
        heading=gettext(_("Asset classes"), locale),
        empty=gettext(_("Nothing is valued in the portfolio yet."), locale),
        currency=page.currency,
        total=format_money(page.valuation.value, locale),
        slices=tuple(slices),
    )
