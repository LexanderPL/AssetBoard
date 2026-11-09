"""Portfolio overview and allocation (F6.6): read-only, viewers and up.

One read collects the workspace's classes, instruments, allocations and live transactions;
holdings and realized P&L come from the pure holdings pass (``domain.holdings``), prices and FX
from one ``latest_prices`` and one ``latest_fx_rates`` call at the workspace's today (no
per-instrument queries), and the valuation from ``domain.portfolio.value_portfolio``.

The chart's default scope is the active asset class seeded as Stocks: its ``name_key`` is
compared with the seed's name in every supported language, because class names are editable
and localised. When there is no such class, or nothing is held in it, the default is the whole
portfolio. A scope that is not one of this workspace's classes falls back to the default.

``current_portfolio_data_in_session`` exposes the same load to the F6.8 net-worth links, so a
linked item shows exactly this page's class or instrument value for the workspace's today.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.models.investments import AssetClass, Instrument
from assetboard.db.repositories import (
    AssetClassRepository,
    CurrentWorkspaceRepository,
    InstrumentAllocationRepository,
    InstrumentRepository,
    InvestmentTransactionRepository,
)
from assetboard.domain.categories import name_key
from assetboard.domain.charts import Slice
from assetboard.domain.holdings import (
    CostBasisMethod,
    Holding,
    HoldingTransaction,
    compute_holdings,
    realized_events,
)
from assetboard.domain.instruments import ASSET_CLASS_SEED
from assetboard.domain.portfolio import (
    ClassTotal,
    PortfolioAllocation,
    PortfolioHolding,
    PortfolioPrice,
    PortfolioValuation,
    RealizedTotals,
    portfolio_slices,
    realized_totals,
    value_portfolio,
)
from assetboard.i18n import gettext
from assetboard.i18n.locale import SUPPORTED_LOCALES
from assetboard.services.context import Context, Role, require_role
from assetboard.services.holding_rows import cost_basis_method, holding_transaction
from assetboard.services.prices import (
    FxAt,
    PriceAt,
    latest_fx_rates_in_session,
    latest_prices_in_session,
)
from assetboard.services.risk import PortfolioRisk, risk_for_portfolio

ALL_SCOPE = "all"


@dataclass(frozen=True, slots=True)
class PortfolioInstrument:
    id: UUID
    name: str
    symbol: str | None
    type: str
    unit_label: str
    quote_currency: str
    archived: bool


@dataclass(frozen=True, slots=True)
class PortfolioClass:
    id: UUID
    name: str
    color: str
    archived: bool
    investable: bool = True  # False: not counted in FIRE (M8); the portfolio counts it anyway


@dataclass(frozen=True, slots=True)
class PortfolioClassRow:
    asset_class: PortfolioClass | None  # None: instruments without any allocation
    total: ClassTotal


@dataclass(frozen=True, slots=True)
class CurrentPortfolioData:
    """The F6.6 current valuation, loaded in an existing session for link resolvers."""

    currency: str
    today: date
    classes: tuple[AssetClass, ...]
    instruments: tuple[Instrument, ...]
    holdings: Mapping[UUID, Holding]
    allocations: tuple[PortfolioAllocation, ...]
    prices: dict[UUID, PriceAt]
    fx: dict[str, FxAt]
    valuation: PortfolioValuation
    transactions: list[HoldingTransaction]
    method: CostBasisMethod


@dataclass(frozen=True, slots=True)
class PortfolioPage:
    currency: str
    today: date
    valuation: PortfolioValuation
    instruments: dict[UUID, PortfolioInstrument]
    prices: dict[UUID, PriceAt]
    fx: dict[str, FxAt]
    class_rows: tuple[PortfolioClassRow, ...]
    realized: RealizedTotals
    scope: PortfolioClass | None  # None: the whole portfolio
    scope_options: tuple[PortfolioClass, ...]
    slices: tuple[Slice, ...]
    risk: PortfolioRisk


def default_scope_keys() -> frozenset[str]:
    """The name keys of the default-scope seed class in every supported language."""
    return frozenset(
        name_key(gettext(seed.name, locale))
        for seed in ASSET_CLASS_SEED
        if seed.portfolio_default
        for locale in SUPPORTED_LOCALES
    )


def _class(row: AssetClass) -> PortfolioClass:
    return PortfolioClass(
        row.id, row.name, row.color, row.archived_at is not None, investable=row.investable
    )


def _resolve_scope(
    requested: str,
    classes: list[AssetClass],
    valuation: PortfolioValuation,
) -> AssetClass | None:
    by_id = {row.id: row for row in classes}
    if requested == ALL_SCOPE:
        return None
    try:
        chosen = by_id.get(UUID(requested)) if requested else None
    except ValueError:
        chosen = None
    if chosen is not None:
        return chosen
    keys = default_scope_keys()
    default = next(
        (row for row in classes if row.archived_at is None and row.name_key in keys), None
    )
    if default is None or valuation.class_value(default.id) <= 0:
        return None
    return default


def current_portfolio_data_in_session(session: Session, ctx: Context) -> CurrentPortfolioData:
    """The exact F6.6 current valuation, including undated legacy lots."""
    workspace = CurrentWorkspaceRepository(session, ctx).get()
    base = workspace.base_currency
    method = cost_basis_method(workspace)
    today = ctx.clock.today(ctx.workspace.timezone)
    classes = tuple(AssetClassRepository(session, ctx).ordered())
    instrument_rows = tuple(InstrumentRepository(session, ctx).ordered())
    allocation_rows = InstrumentAllocationRepository(session, ctx).list()
    transactions = [
        holding_transaction(row) for row in InvestmentTransactionRepository(session, ctx).live()
    ]
    holdings = {
        holding.instrument_id: holding
        for holding in compute_holdings(transactions, method, None, base_currency=base)
        if holding.quantity > 0
    }
    held = [row for row in instrument_rows if row.id in holdings]
    prices = latest_prices_in_session(session, ctx, [row.id for row in held], today) if held else {}
    currencies = {row.quote_currency for row in held} - {base}
    fx = latest_fx_rates_in_session(session, ctx, currencies, today) if currencies else {}
    class_order = {row.id: row.sort_order for row in classes}
    allocations = tuple(
        PortfolioAllocation(
            row.instrument_id,
            row.asset_class_id,
            row.weight_bp,
            class_order.get(row.asset_class_id, 0),
        )
        for row in allocation_rows
        if row.instrument_id in holdings
    )
    valuation = value_portfolio(
        [
            PortfolioHolding(
                instrument_id=row.id,
                quantity=holdings[row.id].quantity,
                cost_basis=holdings[row.id].cost_basis_base,
                quote_currency=row.quote_currency,
            )
            for row in held
        ],
        [
            PortfolioPrice(row.id, prices[row.id].price if row.id in prices else None)
            for row in held
        ],
        {currency: rate.rate for currency, rate in fx.items() if rate.rate is not None},
        allocations,
        today,
        base_currency=base,
    )
    return CurrentPortfolioData(
        currency=base,
        today=today,
        classes=classes,
        instruments=instrument_rows,
        holdings=holdings,
        allocations=allocations,
        prices=prices,
        fx=fx,
        valuation=valuation,
        transactions=transactions,
        method=method,
    )


def portfolio_page(
    factory: sessionmaker[Session], ctx: Context, *, scope: str = ""
) -> PortfolioPage:
    """The portfolio at the workspace's today; ``scope`` is ``"all"``, a class id or empty."""
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        data = current_portfolio_data_in_session(session, ctx)
        risk = risk_for_portfolio(session, ctx, data)
    classes = data.classes
    valuation = data.valuation
    totals = {total.asset_class_id: total for total in valuation.classes}
    class_rows = [
        PortfolioClassRow(_class(row), totals[row.id])
        for row in classes
        if row.id in totals
        and (totals[row.id].value.amount_minor or totals[row.id].cost.amount_minor)
    ]
    if None in totals:
        class_rows.append(PortfolioClassRow(None, totals[None]))
    chosen = _resolve_scope(scope, list(classes), valuation)
    realized = realized_totals(
        realized_events(data.transactions, data.method, base_currency=data.currency),
        data.today,
        base_currency=data.currency,
    )
    return PortfolioPage(
        currency=data.currency,
        today=data.today,
        valuation=valuation,
        instruments={
            row.id: PortfolioInstrument(
                id=row.id,
                name=row.name,
                symbol=row.symbol,
                type=row.type,
                unit_label=row.unit_label,
                quote_currency=row.quote_currency,
                archived=row.archived_at is not None,
            )
            for row in data.instruments
            if row.id in data.holdings
        },
        prices=data.prices,
        fx=data.fx,
        class_rows=tuple(class_rows),
        realized=realized,
        scope=_class(chosen) if chosen is not None else None,
        scope_options=tuple(row.asset_class for row in class_rows if row.asset_class is not None),
        slices=portfolio_slices(valuation, chosen.id if chosen is not None else None),
        risk=risk,
    )
