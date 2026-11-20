"""Home widget preferences and the dashboard's missing-data scan (F9.1).

Preferences are a per-user, per-workspace UI setting. Reading them never touches financial
data; saving one is a small write through the scoped repository. The missing-data scan composes
the existing portfolio, income and import reads; it does not recalculate any of them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, cast
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.engine import run_immediate
from assetboard.db.repositories import DashboardPreferenceRepository, ImportBatchRepository
from assetboard.domain.dashboard import (
    DEFAULT_NET_WORTH_RANGE,
    DEFAULT_PORTFOLIO_RANGE,
    DEFAULT_UPCOMING_LIMIT,
    DEFAULT_WIDGET_KEYS,
    WIDGET_KEYS,
    clean_net_worth_range,
    clean_portfolio_range,
    clean_upcoming_limit,
    clean_widgets,
)
from assetboard.domain.networth_history import normalize_history_range
from assetboard.services.context import Context, Role, has_role, require_role, require_role_now
from assetboard.services.home_cache import cache_generation, cache_get, cache_put
from assetboard.services.income import missing_work_logs
from assetboard.services.months import current_month
from assetboard.services.networth_history import NetWorthHistoryData, networth_history
from assetboard.services.performance import (
    VALUE_RANGES,
    PerformancePage,
    normalize_value_range,
    performance_page,
)
from assetboard.services.portfolio import (
    PortfolioPage,
    current_portfolio_data_in_session,
    portfolio_page,
)

__all__ = [
    "DashboardPreferences",
    "MissingData",
    "MissingPrice",
    "MissingWorkLog",
    "PlannedImport",
    "home_net_worth_history",
    "home_portfolio_history",
    "home_portfolio_page",
    "missing_data",
    "read_dashboard_preferences",
    "reset_dashboard_preferences",
    "save_dashboard_preferences",
]


@dataclass(frozen=True, slots=True)
class DashboardPreferences:
    widgets: tuple[str, ...]
    customised: bool
    upcoming_limit: int
    portfolio_range: str
    net_worth_range: str


@dataclass(frozen=True, slots=True)
class MissingPrice:
    kind: Literal["price", "fx"]
    state: Literal["stale", "missing"]
    name: str
    as_of: date | None


@dataclass(frozen=True, slots=True)
class MissingWorkLog:
    source_id: UUID
    source_name: str
    work_month: date


@dataclass(frozen=True, slots=True)
class PlannedImport:
    id: UUID
    filename: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MissingData:
    stale_prices: tuple[MissingPrice, ...]
    missing_work_logs: tuple[MissingWorkLog, ...]
    planned_imports: tuple[PlannedImport, ...]

    @property
    def empty(self) -> bool:
        return not (self.stale_prices or self.missing_work_logs or self.planned_imports)


def read_dashboard_preferences(
    factory: sessionmaker[Session],
    ctx: Context,
    *,
    known: tuple[str, ...] = WIDGET_KEYS,
    defaults: tuple[str, ...] = DEFAULT_WIDGET_KEYS,
) -> DashboardPreferences:
    """The current user's Home widgets; no row means the default catalogue."""
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        row = DashboardPreferenceRepository(session, ctx).for_user(ctx.user.id)
        if row is None:
            return DashboardPreferences(
                widgets=tuple(defaults),
                customised=False,
                upcoming_limit=DEFAULT_UPCOMING_LIMIT,
                portfolio_range=DEFAULT_PORTFOLIO_RANGE,
                net_worth_range=DEFAULT_NET_WORTH_RANGE,
            )
        stored = cast(object, row.widgets)
        if not isinstance(stored, list | tuple):
            widgets = tuple(defaults)
        else:
            widgets = clean_widgets(cast("Sequence[object]", stored), known)
        return DashboardPreferences(
            widgets=widgets,
            customised=True,
            upcoming_limit=clean_upcoming_limit(cast(object, row.upcoming_limit)),
            portfolio_range=clean_portfolio_range(cast(object, row.portfolio_range)),
            net_worth_range=clean_net_worth_range(cast(object, row.net_worth_range)),
        )


def save_dashboard_preferences(
    factory: sessionmaker[Session],
    ctx: Context,
    widgets: list[str] | tuple[str, ...],
    *,
    upcoming_limit: int = DEFAULT_UPCOMING_LIMIT,
    portfolio_range: str = DEFAULT_PORTFOLIO_RANGE,
    net_worth_range: str = DEFAULT_NET_WORTH_RANGE,
    known: tuple[str, ...] = WIDGET_KEYS,
) -> None:
    """Store the enabled widgets and widget settings; unknown keys are ignored."""
    require_role(ctx, Role.VIEWER)
    cleaned = clean_widgets(list(widgets), known)
    limit = clean_upcoming_limit(upcoming_limit)
    range_code = clean_portfolio_range(portfolio_range)
    net_worth_code = clean_net_worth_range(net_worth_range)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.VIEWER)
        DashboardPreferenceRepository(session, ctx).upsert(
            user_id=ctx.user.id,
            widgets=list(cleaned),
            upcoming_limit=limit,
            portfolio_range=range_code,
            net_worth_range=net_worth_code,
            customised_at=ctx.clock.now(),
        )

    run_immediate(factory, write)


def reset_dashboard_preferences(factory: sessionmaker[Session], ctx: Context) -> None:
    """Delete the saved list; the next read returns the default catalogue."""
    require_role(ctx, Role.VIEWER)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.VIEWER)
        DashboardPreferenceRepository(session, ctx).delete_for_user(ctx.user.id)

    run_immediate(factory, write)


def home_net_worth_history(
    factory: sessionmaker[Session], ctx: Context, range_code: str
) -> NetWorthHistoryData:
    """The Home net-worth trend for one range, cached per workspace/date/range.

    The range is applied before the read: a one-year widget loads one year of valuations plus
    each item's carry-forward value, never the all-time series. Any committed net-worth write
    invalidates the cache before the next request (``services.home_cache``).
    """
    require_role(ctx, Role.VIEWER)
    code = normalize_history_range(range_code)
    today = ctx.clock.today(ctx.workspace.timezone)
    key = ("net-worth", ctx.workspace.id, today, code)
    cached = cache_get(key, now=ctx.clock.now())
    if isinstance(cached, NetWorthHistoryData):
        return cached
    generation = cache_generation()
    data = networth_history(factory, ctx, code)
    cache_put(key, data, generation=generation, now=ctx.clock.now())
    return data


def home_portfolio_history(
    factory: sessionmaker[Session], ctx: Context, range_code: str
) -> PerformancePage:
    """The Home portfolio history for one range, cached per workspace/date/range.

    The chart-only read (no period table) loads prices and FX from the range start plus each
    series' latest observation before it. Any committed investment write invalidates the cache.
    """
    require_role(ctx, Role.VIEWER)
    code = normalize_value_range(range_code)
    today = ctx.clock.today(ctx.workspace.timezone)
    key = ("portfolio-history", ctx.workspace.id, today, code)
    cached = cache_get(key, now=ctx.clock.now())
    if isinstance(cached, PerformancePage):
        return cached
    generation = cache_generation()
    page = performance_page(
        factory,
        ctx,
        range_code=code,
        ranges=VALUE_RANGES,
        with_periods=False,
    )
    cache_put(key, page, generation=generation, now=ctx.clock.now())
    return page


def home_portfolio_page(factory: sessionmaker[Session], ctx: Context) -> PortfolioPage:
    """The Home portfolio valuation for one workspace/date, cached until an investment write.

    The risk block is viewer-specific (age and policy), so the key includes the user even though
    the Home widget only renders value and gain.
    """
    require_role(ctx, Role.VIEWER)
    today = ctx.clock.today(ctx.workspace.timezone)
    key = ("portfolio", ctx.workspace.id, ctx.user.id, today)
    cached = cache_get(key, now=ctx.clock.now())
    if isinstance(cached, PortfolioPage):
        return cached
    generation = cache_generation()
    page = portfolio_page(factory, ctx)
    cache_put(key, page, generation=generation, now=ctx.clock.now())
    return page


def missing_data(
    factory: sessionmaker[Session],
    ctx: Context,
    *,
    work_log_within_days: int = 10,
) -> MissingData:
    """Stale portfolio prices/FX, missing work logs and imports planned but not applied.

    Planned imports are only visible to editors and owners, like the data page itself. Viewers
    still see stale prices and missing work logs, which their module pages show.
    """
    require_role(ctx, Role.VIEWER)
    today = ctx.clock.today(ctx.workspace.timezone)
    with factory() as session:
        portfolio = current_portfolio_data_in_session(session, ctx, today=today)
        names = {instrument.id: instrument.name for instrument in portfolio.instruments}
        stale: list[MissingPrice] = []
        for instrument_id, price in portfolio.prices.items():
            if price.price is None:
                stale.append(
                    MissingPrice(
                        kind="price",
                        state="missing",
                        name=names.get(instrument_id, ""),
                        as_of=price.as_of,
                    )
                )
            elif price.stale:
                stale.append(
                    MissingPrice(
                        kind="price",
                        state="stale",
                        name=names.get(instrument_id, ""),
                        as_of=price.as_of,
                    )
                )
        for currency, fx in portfolio.fx.items():
            if fx.rate is None:
                stale.append(
                    MissingPrice(kind="fx", state="missing", name=currency, as_of=fx.as_of)
                )
            elif fx.stale:
                stale.append(MissingPrice(kind="fx", state="stale", name=currency, as_of=fx.as_of))
        imports: list[PlannedImport] = []
        if has_role(ctx, Role.EDITOR):
            imports = [
                PlannedImport(id=batch.id, filename=batch.filename, created_at=batch.created_at)
                for batch in ImportBatchRepository(session, ctx).awaiting_apply()
                if batch.status == "planned"
            ]
    logs = missing_work_logs(
        factory,
        ctx,
        current_month(ctx).first_day,
        within_days=work_log_within_days,
    )
    return MissingData(
        stale_prices=tuple(
            sorted(
                stale,
                key=lambda item: (
                    item.state,
                    item.kind,
                    item.name.casefold(),
                    item.as_of or today,
                ),
            )
        ),
        missing_work_logs=tuple(
            MissingWorkLog(source_id=source_id, source_name=name, work_month=work_month)
            for source_id, name, work_month in sorted(
                logs, key=lambda row: (row[2], row[1].casefold(), str(row[0]))
            )
        ),
        planned_imports=tuple(
            sorted(imports, key=lambda item: (item.created_at, str(item.id)), reverse=True)
        ),
    )
