"""Portfolio overview and allocation page (F6.6): read-only, viewers and up.

``services.portfolio`` values the portfolio; this module formats it for the template with the
locale helpers (money in the currency's native style, quantities and prices in the interface
locale, shares and gains in tenths of a percent). The donut reuses ``macros/charts.html``:
instrument slices take the categorical palette by position (instruments of one class would all
share the class colour), "Other" takes the theme's neutral colour, and the table stays as the
accessible, no-JavaScript version. The scope selector is a GET form, so no CSRF is involved.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, localcontext
from uuid import UUID

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from assetboard.domain.categories import PALETTE
from assetboard.domain.money import Money
from assetboard.i18n import gettext
from assetboard.i18n.dates import format_day
from assetboard.i18n.numbers import format_money, format_quantity_input, format_rate_tenths
from assetboard.services.context import Context, Role, has_role
from assetboard.services.portfolio import ALL_SCOPE, PortfolioPage, portfolio_page
from assetboard.services.price_sources import provider_enabled
from assetboard.web.charts import DonutSlice, DonutView
from assetboard.web.context import CurrentContext
from assetboard.web.errors import is_htmx_fragment_request
from assetboard.web.rendering import render_template, scoped_url

router = APIRouter()

PRICE_DISPLAY_DECIMALS = 6  # a model or provider price can carry 18 decimals; show at most 6


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


OTHER = _("Other")
UNALLOCATED = _("Unallocated")
WHOLE_PORTFOLIO = _("Whole portfolio")


@dataclass(frozen=True, slots=True)
class HoldingRowView:
    id: UUID
    name: str
    symbol: str | None
    url: str
    archived: bool
    quantity: str
    unit_label: str
    avg_cost: str
    price: str | None
    price_date: str | None
    price_stale: bool
    terms_missing: bool
    fx_stale: bool
    quote_currency: str
    missing: str | None  # "price" or "fx": excluded from the totals
    value: str | None
    cost: str
    gain: str | None
    gain_pct: str | None
    gain_negative: bool
    share: str | None


@dataclass(frozen=True, slots=True)
class ClassRowView:
    name: str
    color: str | None
    archived: bool
    value: str
    cost: str
    gain: str
    gain_pct: str | None
    gain_negative: bool


@dataclass(frozen=True, slots=True)
class ScopeOption:
    value: str
    label: str
    selected: bool


@dataclass(frozen=True, slots=True)
class PortfolioView:
    currency: str
    as_of: str
    rows: tuple[HoldingRowView, ...]
    classes: tuple[ClassRowView, ...]
    value: str
    cost: str
    gain: str
    gain_pct: str | None
    gain_negative: bool
    realized_ytd: str
    realized_all: str
    year: int
    excluded: int
    scope_options: tuple[ScopeOption, ...]
    donut: DonutView

    @property
    def has_rows(self) -> bool:
        return bool(self.rows)

    @property
    def has_chart(self) -> bool:
        return bool(self.donut.slices)


def _price_text(value: Decimal, locale: str) -> str:
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and -exponent > PRICE_DISPLAY_DECIMALS:
        with localcontext() as context:
            context.prec = max(context.prec, 80)
            value = value.quantize(Decimal(1).scaleb(-PRICE_DISPLAY_DECIMALS), ROUND_HALF_UP)
    return format_quantity_input(value, locale)


def _pct(tenths: int | None, locale: str) -> str | None:
    return format_rate_tenths(tenths, locale) if tenths is not None else None


def _day(value: date | None, locale: str) -> str | None:
    return format_day(value, locale) if value is not None else None


def build_view(request: Request, page: PortfolioPage, locale: str) -> PortfolioView:
    valuation = page.valuation
    rows: list[HoldingRowView] = []
    for row in valuation.rows:
        instrument = page.instruments[row.instrument_id]
        price_at = page.prices.get(row.instrument_id)
        fx_at = page.fx.get(instrument.quote_currency)
        rows.append(
            HoldingRowView(
                id=instrument.id,
                name=instrument.name,
                symbol=instrument.symbol,
                url=scoped_url(request, "investments_instrument", instrument_id=str(instrument.id)),
                archived=instrument.archived,
                quantity=format_quantity_input(row.quantity, locale),
                unit_label=instrument.unit_label,
                avg_cost=format_money(Money.from_decimal(row.avg_cost, page.currency), locale),
                price=_price_text(row.price, locale) if row.price is not None else None,
                price_date=_day(price_at.as_of if price_at else None, locale),
                price_stale=bool(price_at and price_at.stale and row.price is not None),
                terms_missing=bool(price_at and price_at.terms_missing),
                fx_stale=bool(fx_at and fx_at.stale and fx_at.rate is not None),
                quote_currency=instrument.quote_currency,
                missing=row.missing.value if row.missing is not None else None,
                value=format_money(row.value, locale) if row.value is not None else None,
                cost=format_money(row.cost_basis, locale),
                gain=format_money(row.gain, locale) if row.gain is not None else None,
                gain_pct=_pct(row.gain_tenths, locale),
                gain_negative=bool(row.gain and row.gain.amount_minor < 0),
                share=_pct(row.share_tenths, locale),
            )
        )
    classes = tuple(
        ClassRowView(
            name=(
                item.asset_class.name
                if item.asset_class is not None
                else gettext(UNALLOCATED, locale)
            ),
            color=item.asset_class.color if item.asset_class is not None else None,
            archived=bool(item.asset_class and item.asset_class.archived),
            value=format_money(item.total.value, locale),
            cost=format_money(item.total.cost, locale),
            gain=format_money(item.total.gain, locale),
            gain_pct=_pct(item.total.gain_tenths, locale),
            gain_negative=item.total.gain.amount_minor < 0,
        )
        for item in page.class_rows
    )
    selected = str(page.scope.id) if page.scope is not None else ALL_SCOPE
    options = [ScopeOption(ALL_SCOPE, gettext(WHOLE_PORTFOLIO, locale), selected == ALL_SCOPE)]
    known = {option.id for option in page.scope_options}
    scope_classes = list(page.scope_options)
    if page.scope is not None and page.scope.id not in known:
        scope_classes.append(page.scope)
    options.extend(
        ScopeOption(str(option.id), option.name, selected == str(option.id))
        for option in scope_classes
    )
    return PortfolioView(
        currency=page.currency,
        as_of=format_day(page.today, locale),
        rows=tuple(rows),
        classes=classes,
        value=format_money(valuation.value, locale),
        cost=format_money(valuation.cost, locale),
        gain=format_money(valuation.gain, locale),
        gain_pct=_pct(valuation.gain_tenths, locale),
        gain_negative=valuation.gain.amount_minor < 0,
        realized_ytd=format_money(page.realized.year_to_date, locale),
        realized_all=format_money(page.realized.all_time, locale),
        year=page.today.year,
        excluded=valuation.excluded,
        scope_options=tuple(options),
        donut=_donut(page, locale),
    )


def _donut(page: PortfolioPage, locale: str) -> DonutView:
    slices: list[DonutSlice] = []
    position = 0
    for item in page.slices:
        if item.category_id is None:
            name, color = gettext(OTHER, locale), None
        else:
            name, color = page.instruments[item.category_id].name, PALETTE[position % len(PALETTE)]
            position += 1
        slices.append(
            DonutSlice(
                name=name,
                color=color,
                amount_minor=item.amount.amount_minor,
                amount=format_money(item.amount, locale),
                share=format_rate_tenths(item.share_tenths, locale),
            )
        )
    total = sum(item.amount.amount_minor for item in page.slices)
    if page.scope is None:
        heading = gettext(_("Holdings in the whole portfolio"), locale)
        empty = gettext(_("Nothing is valued in the portfolio yet."), locale)
    else:
        heading = gettext(_("Holdings in %(class)s"), locale) % {"class": page.scope.name}
        empty = gettext(_("Nothing is held in this asset class."), locale)
    return DonutView(
        key="portfolio",
        heading=heading,
        empty=empty,
        currency=page.currency,
        total=format_money(Money(total, page.currency), locale),
        slices=tuple(slices),
    )


def _render(request: Request, ctx: Context, scope: str) -> Response:
    factory = request.app.state.session_factory
    locale = request.state.locale
    page = portfolio_page(factory, ctx, scope=scope)
    context = {
        "view": build_view(request, page, locale),
        "can_manage_providers": has_role(ctx, Role.OWNER),
        "coingecko_enabled": provider_enabled(factory, ctx, "coingecko"),
        "can_edit": has_role(ctx, Role.EDITOR),
    }
    template = (
        "investments/_portfolio_body.html"
        if is_htmx_fragment_request(request)
        else "investments/portfolio.html"
    )
    response = render_template(request, template, context=context)
    response.headers["Vary"] = "HX-Request"
    return response


@router.get("/investments/portfolio", name="investments_portfolio")
async def portfolio(request: Request, ctx: CurrentContext, scope: str = "") -> Response:
    return await run_in_threadpool(lambda: _render(request, ctx, scope.strip()))
