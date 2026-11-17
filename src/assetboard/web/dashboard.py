"""The Home widget registry and view models (F9.1).

Every widget is one HTMX fragment. The registry is the single presentation catalogue; the pure
key list lives in ``domain.dashboard`` so services and tests can validate stored preferences
without importing the web layer. Each builder calls the existing service that owns the figures;
no finance math is repeated here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import Request

from assetboard.domain.charts import ChartBlock
from assetboard.domain.dashboard import (
    PORTFOLIO_RANGES,
    UPCOMING_LIMIT_MAX,
    UPCOMING_LIMIT_MIN,
    WIDGET_KEYS,
    clean_portfolio_range,
    clean_upcoming_limit,
    complete_order,
    move_widget,
)
from assetboard.domain.months import YearMonth
from assetboard.i18n import gettext
from assetboard.i18n.dates import format_day, format_month, format_month_heading
from assetboard.i18n.numbers import (
    format_amount_input,
    format_money,
    format_rate_short,
    format_rate_tenths,
)
from assetboard.services import occurrences
from assetboard.services.context import Context, Role, has_role
from assetboard.services.dashboard import MissingData, missing_data, read_dashboard_preferences
from assetboard.services.fire_projection import results_for_detail
from assetboard.services.fire_scenarios import get_fire_plan_if_any
from assetboard.services.loans import LoanPage, list_loans
from assetboard.services.months import current_month, get_month
from assetboard.services.networth_history import networth_history
from assetboard.services.networth_summary import networth_summary
from assetboard.services.performance import VALUE_RANGES, normalize_value_range, performance_page
from assetboard.services.portfolio import portfolio_page
from assetboard.services.portfolio_changes import ValueChange, portfolio_changes
from assetboard.web.charts import build_class_donut, build_donut, build_performance_chart
from assetboard.web.networth_history import build_trend_view
from assetboard.web.networth_summary import build_summary_view
from assetboard.web.plan import SummaryView, build_summary
from assetboard.web.rendering import scoped_url

WidgetWidth = Literal["full", "half"]
WidgetBuilder = Callable[[Request, Context], dict[str, object]]
ModuleUrl = Callable[[Request, Context], str]


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


PORTFOLIO_RANGE_LABELS: dict[str, str] = {
    "1m": _("1 month"),
    "3m": _("3 months"),
    "6m": _("6 months"),
    "1y": _("1 year"),
    "all": _("All time"),
}


@dataclass(frozen=True, slots=True)
class Widget:
    key: str
    label: str
    description: str
    template: str
    width: WidgetWidth
    module_url: ModuleUrl
    build: WidgetBuilder


def _month_url(request: Request, ctx: Context) -> str:
    return scoped_url(request, "month", month=str(current_month(ctx)))


def _route_url(route_name: str) -> ModuleUrl:
    def url(request: Request, _ctx: Context) -> str:
        return scoped_url(request, route_name)

    return url


def _missing_data_url(request: Request, ctx: Context) -> str:
    route = "workspace_data" if has_role(ctx, Role.EDITOR) else "investments_portfolio"
    return scoped_url(request, route)


# View models shared by the Home shell and the Customise page.


@dataclass(frozen=True, slots=True)
class HomeWidgetView:
    key: str
    title: str
    fragment_url: str
    module_url: str
    width: WidgetWidth


@dataclass(frozen=True, slots=True)
class HomeView:
    month: str
    month_heading: str
    widgets: tuple[HomeWidgetView, ...]
    customised: bool


@dataclass(frozen=True, slots=True)
class CustomiseWidgetView:
    key: str
    title: str
    description: str
    enabled: bool
    can_move_up: bool
    can_move_down: bool


@dataclass(frozen=True, slots=True)
class CustomiseView:
    widgets: tuple[CustomiseWidgetView, ...]
    order_csv: str
    customised: bool
    selected_count: int
    upcoming_limit: int
    upcoming_limit_min: int
    upcoming_limit_max: int
    portfolio_range: str
    portfolio_range_options: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class PortfolioHistoryRow:
    date: str
    value: str
    invested: str


@dataclass(frozen=True, slots=True)
class PortfolioHistoryView:
    chart: dict[str, object] | None
    rows: tuple[PortfolioHistoryRow, ...]
    has_data: bool
    range_label: str
    incomplete_points: int


@dataclass(frozen=True, slots=True)
class ChangeView:
    amount: str | None
    percent: str | None
    positive: bool
    negative: bool


@dataclass(frozen=True, slots=True)
class MoverView:
    name: str
    amount: str
    percent: str | None
    positive: bool
    negative: bool


@dataclass(frozen=True, slots=True)
class PortfolioChangesView:
    previous_day: str
    month_start: str
    since_previous_day: ChangeView
    since_month_start: ChangeView
    movers: tuple[MoverView, ...]
    has_data: bool


@dataclass(frozen=True, slots=True)
class LoanRowView:
    name: str
    remaining: str
    payoff: str
    percent: str | None


@dataclass(frozen=True, slots=True)
class LoansView:
    total: str
    rows: tuple[LoanRowView, ...]
    more: int
    has_data: bool


@dataclass(frozen=True, slots=True)
class MissingPriceView:
    kind_label: str
    name: str
    as_of: str


@dataclass(frozen=True, slots=True)
class MissingWorkLogView:
    name: str
    month: str
    url: str


@dataclass(frozen=True, slots=True)
class PlannedImportView:
    filename: str
    created_at: str


@dataclass(frozen=True, slots=True)
class MissingDataView:
    stale_prices: tuple[MissingPriceView, ...]
    missing_work_logs: tuple[MissingWorkLogView, ...]
    planned_imports: tuple[PlannedImportView, ...]
    empty: bool


# Builders. Each one returns the fragment's context; the route adds the widget heading/link.


def _summary(request: Request, ctx: Context) -> SummaryView:
    view = get_month(request.app.state.session_factory, ctx, current_month(ctx))
    return build_summary(request.app.state.session_factory, ctx, request, view)


def build_month_in_out(request: Request, ctx: Context) -> dict[str, object]:
    return {"summary": _summary(request, ctx)}


def _build_month_donut(request: Request, ctx: Context, block: ChartBlock) -> dict[str, object]:
    summary = _summary(request, ctx)
    donut = (
        build_donut(summary.source, block, request.state.locale)
        if summary.source is not None
        else None
    )
    return {"summary": summary, "donut": donut, "block": block}


def build_month_expenses(request: Request, ctx: Context) -> dict[str, object]:
    return _build_month_donut(request, ctx, "expense")


def build_month_income(request: Request, ctx: Context) -> dict[str, object]:
    return _build_month_donut(request, ctx, "income")


def build_month_plan(request: Request, ctx: Context) -> dict[str, object]:
    return {"summary": _summary(request, ctx)}


def build_upcoming(request: Request, ctx: Context) -> dict[str, object]:
    factory = request.app.state.session_factory
    locale = request.state.locale
    preferences = read_dashboard_preferences(factory, ctx)
    limit = preferences.upcoming_limit
    days = occurrences.window_days(ctx, request.app.state.settings.upcoming_days)
    weeks = occurrences.upcoming(factory, ctx, days=days)
    items = sorted((o for w in weeks for o in w.occurrences), key=lambda o: o.due_date)
    return {
        "upcoming": items[:limit],
        "upcoming_more": len(items) > limit,
        "upcoming_days": days,
        "upcoming_limit": limit,
        "today": ctx.clock.today(ctx.workspace.timezone),
        "can_edit": has_role(ctx, Role.EDITOR),
        "format_day": format_day,
        "amount_input": lambda amount: format_amount_input(amount, locale),
    }


def build_net_worth(request: Request, ctx: Context) -> dict[str, object]:
    factory = request.app.state.session_factory
    locale = request.state.locale
    data = networth_summary(factory, ctx)
    trend = build_trend_view(networth_history(factory, ctx, "1y"), locale)
    return {"view": build_summary_view(data, locale), "trend": trend}


def build_portfolio(request: Request, ctx: Context) -> dict[str, object]:
    factory = request.app.state.session_factory
    locale = request.state.locale
    preferences = read_dashboard_preferences(factory, ctx)
    page = portfolio_page(factory, ctx)
    history_page = performance_page(
        factory,
        ctx,
        range_code=normalize_value_range(preferences.portfolio_range),
        ranges=VALUE_RANGES,
        with_periods=False,
    )
    chart = build_performance_chart(history_page, locale)
    history = PortfolioHistoryView(
        chart=chart,
        rows=tuple(
            PortfolioHistoryRow(
                date=format_day(point.date, locale),
                value="—" if point.incomplete else format_money(point.value, locale),
                invested=("—" if point.incomplete else format_money(point.net_invested, locale)),
            )
            for point in history_page.chart
        ),
        has_data=chart is not None,
        range_label=gettext(
            PORTFOLIO_RANGE_LABELS[history_page.range_code],
            locale,
        ),
        incomplete_points=history_page.incomplete_points,
    )
    return {
        "page": page,
        "donut": build_class_donut(page, locale),
        "value": format_money(page.valuation.value, locale),
        "gain": format_money(page.valuation.gain, locale),
        "gain_pct": (
            format_rate_tenths(page.valuation.gain_tenths, locale)
            if page.valuation.gain_tenths is not None
            else None
        ),
        "has_data": bool(page.valuation.value.amount_minor or page.valuation.cost.amount_minor),
        "history": history,
        "has_history_data": history_page.first_date is not None,
    }


def _change_view(change: ValueChange, locale: str) -> ChangeView:
    amount = format_money(change.amount, locale) if change.amount is not None else None
    percent = (
        format_rate_tenths(change.percent_tenths, locale)
        if change.percent_tenths is not None
        else None
    )
    return ChangeView(
        amount=amount,
        percent=percent,
        positive=change.amount is not None and change.amount.amount_minor > 0,
        negative=change.amount is not None and change.amount.amount_minor < 0,
    )


def build_portfolio_changes(request: Request, ctx: Context) -> dict[str, object]:
    factory = request.app.state.session_factory
    locale = request.state.locale
    changes = portfolio_changes(factory, ctx)
    movers = tuple(
        MoverView(
            name=mover.name,
            amount=format_money(mover.change.amount, locale)
            if mover.change.amount is not None
            else "",
            percent=(
                format_rate_tenths(mover.change.percent_tenths, locale)
                if mover.change.percent_tenths is not None
                else None
            ),
            positive=mover.change.amount is not None and mover.change.amount.amount_minor > 0,
            negative=mover.change.amount is not None and mover.change.amount.amount_minor < 0,
        )
        for mover in changes.movers
    )
    return {
        "changes": changes,
        "view": PortfolioChangesView(
            previous_day=format_day(changes.previous_day, locale),
            month_start=format_day(changes.month_start, locale),
            since_previous_day=_change_view(changes.since_previous_day, locale),
            since_month_start=_change_view(changes.since_month_start, locale),
            movers=movers,
            has_data=changes.has_data,
        ),
    }


def build_loans(request: Request, ctx: Context) -> dict[str, object]:
    factory = request.app.state.session_factory
    locale = request.state.locale
    page: LoanPage = list_loans(factory, ctx, include_archived=False)
    rows = tuple(
        LoanRowView(
            name=row.loan.name,
            remaining=(
                format_money(row.figures.remaining_principal, locale)
                if row.figures.remaining_principal is not None
                else "—"
            ),
            payoff=(
                format_day(row.figures.payoff_date, locale)
                if row.figures.payoff_date is not None
                else "—"
            ),
            percent=(
                format_rate_short(row.figures.percent_repaid_bp, locale)
                if row.figures.percent_repaid_bp is not None
                else None
            ),
        )
        for row in page.active[:3]
    )
    return {
        "page": page,
        "total": format_money(page.summary.total_remaining, locale),
        "rows": rows,
        "more": max(0, len(page.active) - len(rows)),
        "has_data": bool(page.active),
    }


def build_fire(request: Request, ctx: Context) -> dict[str, object]:
    factory = request.app.state.session_factory
    locale = request.state.locale
    detail = get_fire_plan_if_any(factory, ctx)
    if detail is None:
        return {"has_plan": False, "blocked": None, "progress": None, "fire_age": None}
    results = results_for_detail(factory, ctx, detail)
    projection = results.projection
    return {
        "has_plan": True,
        "results": results,
        "blocked": results.blocked,
        "progress": (
            format_rate_short(projection.plan_progress_bp, locale)
            if projection is not None and projection.plan_progress_bp is not None
            else None
        ),
        "fire_age": projection.fire_age if projection is not None else None,
        "fire_year": projection.fire_year if projection is not None else None,
        "progress_bp": projection.plan_progress_bp if projection is not None else None,
    }


def build_missing_data(request: Request, ctx: Context) -> dict[str, object]:
    locale = request.state.locale
    data: MissingData = missing_data(request.app.state.session_factory, ctx)
    prices = tuple(
        MissingPriceView(
            kind_label=(
                gettext("Missing price", locale)
                if item.state == "missing" and item.kind == "price"
                else gettext("Missing FX rate", locale)
                if item.state == "missing"
                else gettext("Stale price", locale)
                if item.kind == "price"
                else gettext("Stale FX rate", locale)
            ),
            name=item.name,
            as_of=format_day(item.as_of, locale) if item.as_of is not None else "—",
        )
        for item in data.stale_prices
    )
    logs = tuple(
        MissingWorkLogView(
            name=item.source_name,
            month=format_month(YearMonth.from_date(item.work_month), locale),
            url=scoped_url(
                request,
                "income_calendar",
                source_id=str(item.source_id),
            )
            + f"?month={item.work_month.year:04d}-{item.work_month.month:02d}",
        )
        for item in data.missing_work_logs
    )
    imports = tuple(
        PlannedImportView(
            filename=item.filename,
            created_at=format_day(
                item.created_at.astimezone(ZoneInfo(ctx.workspace.timezone)).date(),
                locale,
            ),
        )
        for item in data.planned_imports
    )
    return {
        "view": MissingDataView(
            stale_prices=prices,
            missing_work_logs=logs,
            planned_imports=imports,
            empty=data.empty,
        ),
        "portfolio_url": scoped_url(request, "investments_portfolio"),
        "income_url": scoped_url(request, "income_sources"),
        "imports_url": (
            scoped_url(request, "workspace_data") if has_role(ctx, Role.EDITOR) else None
        ),
    }


WIDGETS: tuple[Widget, ...] = (
    Widget(
        "month-in-out",
        _("This month: in vs out"),
        _("The current month's income, spending and what is left."),
        "dashboard/widgets/month_in_out.html",
        "full",
        _month_url,
        build_month_in_out,
    ),
    Widget(
        "month-expenses",
        _("This month: expenses by category"),
        _("Where this month's spending went."),
        "dashboard/widgets/month_expenses.html",
        "half",
        _month_url,
        build_month_expenses,
    ),
    Widget(
        "month-income",
        _("This month: income by category"),
        _("Where this month's income came from."),
        "dashboard/widgets/month_income.html",
        "half",
        _month_url,
        build_month_income,
    ),
    Widget(
        "month-plan",
        _("This month: planned vs actual"),
        _("Spending and income against the plan, with the savings rate and forecast."),
        "dashboard/widgets/month_plan.html",
        "full",
        _month_url,
        build_month_plan,
    ),
    Widget(
        "upcoming",
        _("Upcoming payments"),
        _("Overdue items and payments due in the Upcoming window."),
        "dashboard/widgets/upcoming.html",
        "full",
        _route_url("scheduled_upcoming"),
        build_upcoming,
    ),
    Widget(
        "net-worth",
        _("Net worth"),
        _("The current value and its 12-month trend."),
        "dashboard/widgets/net_worth.html",
        "full",
        _route_url("networth_summary"),
        build_net_worth,
    ),
    Widget(
        "portfolio",
        _("Portfolio"),
        _("The portfolio value, gain and allocation."),
        "dashboard/widgets/portfolio.html",
        "half",
        _route_url("investments_portfolio"),
        build_portfolio,
    ),
    Widget(
        "portfolio-changes",
        _("Portfolio changes"),
        _("Changes since yesterday and this month, with the largest movers."),
        "dashboard/widgets/portfolio_changes.html",
        "half",
        _route_url("investments_portfolio"),
        build_portfolio_changes,
    ),
    Widget(
        "loans",
        _("Loans"),
        _("Remaining principal, payoff dates and how much is repaid."),
        "dashboard/widgets/loans.html",
        "half",
        _route_url("loans"),
        build_loans,
    ),
    Widget(
        "fire",
        _("FIRE"),
        _("Plan progress and the age at which the plan reaches independence."),
        "dashboard/widgets/fire.html",
        "half",
        _route_url("fire_scenarios"),
        build_fire,
    ),
    Widget(
        "missing-data",
        _("Missing data"),
        _("Stale prices, missing work logs and imports waiting to be applied."),
        "dashboard/widgets/missing_data.html",
        "full",
        _missing_data_url,
        build_missing_data,
    ),
)
WIDGETS_BY_KEY: dict[str, Widget] = {widget.key: widget for widget in WIDGETS}


def home_context(request: Request, ctx: Context) -> dict[str, object]:
    """The Home shell: the ordered placeholders, not the widget data (blocking)."""
    locale = request.state.locale
    preferences = read_dashboard_preferences(request.app.state.session_factory, ctx)
    month = current_month(ctx)
    widgets = []
    for key in preferences.widgets:
        widget = WIDGETS_BY_KEY.get(key)
        if widget is None:
            continue
        widgets.append(
            HomeWidgetView(
                key=widget.key,
                title=gettext(widget.label, locale),
                fragment_url=scoped_url(request, "dashboard_widget", widget_key=widget.key),
                module_url=widget.module_url(request, ctx),
                width=widget.width,
            )
        )
    notices = {
        "saved": _("Home saved."),
        "reset": _("Home reset to the default widgets."),
    }
    notice = next(
        (
            gettext(message, locale)
            for code, message in notices.items()
            if request.query_params.get(code)
        ),
        None,
    )
    return {
        "workspace": ctx.workspace,
        "home": HomeView(
            month=str(month),
            month_heading=format_month_heading(month, locale),
            widgets=tuple(widgets),
            customised=preferences.customised,
        ),
        "notice": notice,
    }


def customise_context(
    request: Request,
    ctx: Context,
    *,
    order: tuple[str, ...] | None = None,
    enabled: set[str] | None = None,
    upcoming_limit: int | None = None,
    portfolio_range: str | None = None,
) -> dict[str, object]:
    """The Customise page: every known widget, widget settings and the draft order."""
    locale = request.state.locale
    preferences = read_dashboard_preferences(request.app.state.session_factory, ctx)
    draft = complete_order(order if order is not None else preferences.widgets, WIDGET_KEYS)
    checked = set(enabled if enabled is not None else preferences.widgets)
    limit = (
        clean_upcoming_limit(upcoming_limit)
        if upcoming_limit is not None
        else preferences.upcoming_limit
    )
    range_code = (
        clean_portfolio_range(portfolio_range)
        if portfolio_range is not None
        else preferences.portfolio_range
    )
    rows = []
    for index, key in enumerate(draft):
        widget = WIDGETS_BY_KEY[key]
        rows.append(
            CustomiseWidgetView(
                key=widget.key,
                title=gettext(widget.label, locale),
                description=gettext(widget.description, locale),
                enabled=key in checked,
                can_move_up=index > 0,
                can_move_down=index < len(draft) - 1,
            )
        )
    return {
        "view": CustomiseView(
            widgets=tuple(rows),
            order_csv=",".join(draft),
            customised=preferences.customised,
            selected_count=len(checked & set(WIDGET_KEYS)),
            upcoming_limit=limit,
            upcoming_limit_min=UPCOMING_LIMIT_MIN,
            upcoming_limit_max=UPCOMING_LIMIT_MAX,
            portfolio_range=range_code,
            portfolio_range_options=tuple(
                (code, gettext(PORTFOLIO_RANGE_LABELS[code], locale)) for code in PORTFOLIO_RANGES
            ),
        )
    }


def widget_context(request: Request, ctx: Context, widget: Widget) -> dict[str, object]:
    """The fragment's context plus its heading and module link."""
    locale = request.state.locale
    return {
        **widget.build(request, ctx),
        "widget_key": widget.key,
        "widget_title": gettext(widget.label, locale),
        "widget_template": widget.template,
        "widget_width": widget.width,
        "module_url": widget.module_url(request, ctx),
    }


def parse_order(value: str) -> tuple[str, ...]:
    """A posted comma-separated order, completed with every known widget."""
    return complete_order(tuple(part for part in value.split(",") if part), WIDGET_KEYS)


def parse_move(value: str) -> tuple[str, str] | None:
    key, separator, direction = value.partition(":")
    if not separator or direction not in {"up", "down"} or key not in WIDGETS_BY_KEY:
        return None
    return key, direction


def moved_order(order: tuple[str, ...], key: str, direction: str) -> tuple[str, ...]:
    return move_widget(order, key, direction, WIDGET_KEYS)


__all__ = [
    "ChangeView",
    "CustomiseView",
    "CustomiseWidgetView",
    "HomeView",
    "HomeWidgetView",
    "LoanRowView",
    "LoansView",
    "MissingDataView",
    "MissingPriceView",
    "MissingWorkLogView",
    "MoverView",
    "PlannedImportView",
    "PortfolioChangesView",
    "PortfolioHistoryRow",
    "PortfolioHistoryView",
    "WIDGETS",
    "WIDGETS_BY_KEY",
    "Widget",
    "build_fire",
    "build_loans",
    "build_missing_data",
    "build_month_expenses",
    "build_month_in_out",
    "build_month_income",
    "build_month_plan",
    "build_net_worth",
    "build_portfolio",
    "build_portfolio_changes",
    "build_upcoming",
    "customise_context",
    "home_context",
    "moved_order",
    "parse_move",
    "parse_order",
    "widget_context",
]
