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
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from assetboard.domain.categories import PALETTE
from assetboard.domain.instruments import RISK_TIER_LABELS, RISK_TIER_SUBTITLES
from assetboard.domain.money import Money
from assetboard.domain.risk import (
    RISK_POLICY_MODE_LABELS,
    InvalidRiskPolicy,
    RiskPolicyMode,
)
from assetboard.i18n import gettext
from assetboard.i18n.dates import format_day
from assetboard.i18n.numbers import (
    format_money,
    format_quantity_input,
    format_rate_short,
    format_rate_tenths,
)
from assetboard.services.context import Context, Role, has_role
from assetboard.services.portfolio import ALL_SCOPE, PortfolioPage, portfolio_page
from assetboard.services.price_sources import provider_enabled
from assetboard.services.risk import PortfolioRisk, save_risk_policy
from assetboard.web.charts import DonutSlice, DonutView
from assetboard.web.context import CurrentContext
from assetboard.web.errors import is_htmx_fragment_request
from assetboard.web.rendering import render_template, scoped_url
from assetboard.web.security import require_csrf

router = APIRouter(dependencies=[Depends(require_csrf)])
FormStr = Annotated[str, Form()]

PRICE_DISPLAY_DECIMALS = 6  # a model or provider price can carry 18 decimals; show at most 6


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


OTHER = _("Other")
UNALLOCATED = _("Unallocated")
WHOLE_PORTFOLIO = _("Whole portfolio")
PORTFOLIO_NOTICES = {"risk_policy_saved": _("Risk targets saved.")}
RISK_ERRORS = {
    "invalid_mode": _("Choose a target mode."),
    "invalid_percent": _("Enter a percentage between 0 and 100 with at most two decimals."),
    "targets_total": _("The fixed targets must total exactly 100%."),
    "birth_year_required": _("Set your birth year in Profile before using age-based targets."),
}


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
    not_in_fire: bool
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
class RiskTierRowView:
    label: str
    subtitle: str
    value: str
    current_pct: str | None
    target_pct: str | None
    difference_pct: str | None
    difference: str | None
    flagged: bool


@dataclass(frozen=True, slots=True)
class RiskHintView:
    from_label: str
    to_label: str
    amount: str


@dataclass(frozen=True, slots=True)
class RiskView:
    rows: tuple[RiskTierRowView, ...]
    assigned: str
    unassigned: str
    unassigned_share: str | None
    hint: RiskHintView | None
    mode: str
    mode_label: str
    saved: bool
    targets_available: bool
    age: int | None
    birth_year: int | None
    life_expectancy: int
    age_modes_enabled: bool
    age_mode_without_birth_year: bool
    form_values: dict[str, str]
    errors: dict[str, str]


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
    risk: RiskView

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


def _bp_text(value: int | None, locale: str) -> str | None:
    return format_rate_short(value, locale) if value is not None else None


def _signed_bp_text(value: int | None, locale: str) -> str | None:
    if value is None:
        return None
    text = format_rate_short(abs(value), locale)
    if value > 0:
        return f"+{text}"
    if value < 0:
        return f"-{text}"
    return text


def _bp_input(value: int, locale: str) -> str:
    return format_quantity_input(Decimal(value).scaleb(-2), locale)


def _risk_form_values(risk: PortfolioRisk, locale: str) -> dict[str, str]:
    policy = risk.policy
    saved_x = _bp_input(policy.speculative_bp, locale)
    return {
        "mode": policy.mode.value,
        "safe": _bp_input(policy.safe_bp, locale),
        "growth": _bp_input(policy.growth_bp, locale),
        "fixed_speculative": _bp_input(policy.speculative_bp, locale),
        "age_scaled_speculative": saved_x if risk.saved else _bp_input(1000, locale),
        "age_in_bonds_speculative": saved_x if risk.saved else _bp_input(0, locale),
        "tolerance": _bp_input(policy.tolerance_bp, locale),
    }


def _risk_view(
    page: PortfolioPage,
    locale: str,
    *,
    form_values: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
) -> RiskView:
    risk = page.risk
    balance = risk.balance
    rows = tuple(
        RiskTierRowView(
            label=gettext(RISK_TIER_LABELS[row.tier], locale),
            subtitle=gettext(RISK_TIER_SUBTITLES.get(row.tier, ""), locale),
            value=format_money(row.value, locale),
            current_pct=_bp_text(row.current_bp, locale),
            target_pct=_bp_text(row.target_bp, locale),
            difference_pct=_signed_bp_text(row.difference_bp, locale),
            difference=format_money(row.difference, locale) if row.difference else None,
            flagged=row.flagged,
        )
        for row in balance.tiers
    )
    hint = (
        RiskHintView(
            from_label=gettext(RISK_TIER_LABELS[balance.hint.from_tier], locale),
            to_label=gettext(RISK_TIER_LABELS[balance.hint.to_tier], locale),
            amount=format_money(balance.hint.amount, locale),
        )
        if balance.hint is not None
        else None
    )
    return RiskView(
        rows=rows,
        assigned=format_money(balance.assigned, locale),
        unassigned=format_money(balance.unassigned, locale),
        unassigned_share=_bp_text(balance.unassigned_bp, locale),
        hint=hint,
        mode=risk.policy.mode.value,
        mode_label=gettext(RISK_POLICY_MODE_LABELS[risk.policy.mode], locale),
        saved=risk.saved,
        targets_available=balance.targets_available,
        age=risk.age,
        birth_year=risk.birth_year,
        life_expectancy=risk.life_expectancy,
        age_modes_enabled=risk.age is not None,
        age_mode_without_birth_year=(
            risk.saved and risk.policy.mode is not RiskPolicyMode.FIXED and risk.age is None
        ),
        form_values=form_values or _risk_form_values(risk, locale),
        errors=errors or {},
    )


def build_view(
    request: Request,
    page: PortfolioPage,
    locale: str,
    *,
    risk_form_values: dict[str, str] | None = None,
    risk_errors: dict[str, str] | None = None,
) -> PortfolioView:
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
            not_in_fire=bool(item.asset_class and not item.asset_class.investable),
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
        risk=_risk_view(
            page,
            locale,
            form_values=risk_form_values,
            errors=risk_errors,
        ),
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


def _render(
    request: Request,
    ctx: Context,
    scope: str,
    *,
    status_code: int = 200,
    notice: str | None = None,
    risk_form_values: dict[str, str] | None = None,
    risk_errors: dict[str, str] | None = None,
) -> Response:
    factory = request.app.state.session_factory
    locale = request.state.locale
    page = portfolio_page(factory, ctx, scope=scope)
    context = {
        "view": build_view(
            request,
            page,
            locale,
            risk_form_values=risk_form_values,
            risk_errors=(
                {
                    field: gettext(RISK_ERRORS.get(code, code), locale)
                    for field, code in (risk_errors or {}).items()
                }
                if risk_errors
                else None
            ),
        ),
        "notice": gettext(PORTFOLIO_NOTICES[notice], locale) if notice else None,
        "can_manage_providers": has_role(ctx, Role.OWNER),
        "coingecko_enabled": provider_enabled(factory, ctx, "coingecko"),
        "can_edit": has_role(ctx, Role.EDITOR),
    }
    template = (
        "investments/_portfolio_body.html"
        if is_htmx_fragment_request(request)
        else "investments/portfolio.html"
    )
    response = render_template(request, template, context=context, status_code=status_code)
    response.headers["Vary"] = "HX-Request"
    return response


@router.get("/investments/portfolio", name="investments_portfolio")
async def portfolio(
    request: Request, ctx: CurrentContext, scope: str = "", notice: str = ""
) -> Response:
    return await run_in_threadpool(
        lambda: _render(request, ctx, scope.strip(), notice=notice or None)
    )


@router.post("/investments/portfolio/risk-targets", name="investments_risk_policy")
async def risk_policy_save(
    request: Request,
    ctx: CurrentContext,
    mode: FormStr = "",
    safe: FormStr = "",
    growth: FormStr = "",
    fixed_speculative: FormStr = "",
    age_scaled_speculative: FormStr = "",
    age_in_bonds_speculative: FormStr = "",
    tolerance: FormStr = "",
    scope: FormStr = "",
) -> Response:
    locale = request.state.locale
    speculative = {
        "age_scaled": age_scaled_speculative,
        "age_in_bonds": age_in_bonds_speculative,
    }.get(mode, fixed_speculative)
    values = {
        "mode": mode,
        "safe": safe,
        "growth": growth,
        "fixed_speculative": fixed_speculative,
        "age_scaled_speculative": age_scaled_speculative,
        "age_in_bonds_speculative": age_in_bonds_speculative,
        "tolerance": tolerance,
    }
    try:
        await run_in_threadpool(
            lambda: save_risk_policy(
                request.app.state.session_factory,
                ctx,
                mode=mode,
                safe=safe,
                growth=growth,
                speculative=speculative,
                tolerance=tolerance,
                locale=locale,
            )
        )
    except InvalidRiskPolicy as exc:
        return await run_in_threadpool(
            lambda error=exc: _render(
                request,
                ctx,
                scope.strip(),
                status_code=400,
                risk_form_values=values,
                risk_errors={error.field: error.code},
            )
        )
    if is_htmx_fragment_request(request):
        return await run_in_threadpool(
            lambda: _render(
                request,
                ctx,
                scope.strip(),
                notice="risk_policy_saved",
            )
        )
    query = "?notice=risk_policy_saved"
    if scope.strip():
        query += f"&scope={scope.strip()}"
    return RedirectResponse(
        request.url_for("investments_portfolio").path + query,
        status_code=303,
    )
