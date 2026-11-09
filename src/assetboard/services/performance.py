"""Performance series, ranges and XIRR (F6.7): read-only, viewers and up.

One read builds sorted price and FX observations for the whole history, then the pure
``portfolio_series``/``portfolio_value_at`` functions answer the chart and period rows. The chart
defaults to one year; the class filter applies F6.6 look-through weights to both value and net
invested. Undated legacy lots are excluded from the series and counted in a page note.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.models.investments import (
    AssetClass,
    BondTerms,
    Instrument,
    InvestmentTransaction,
)
from assetboard.db.models.prices import FxRate, PricePoint
from assetboard.db.repositories import (
    AssetClassRepository,
    BondTermsRepository,
    CurrentWorkspaceRepository,
    FxRateRepository,
    InstrumentAllocationRepository,
    InstrumentRepository,
    InvestmentTransactionRepository,
    PricePointRepository,
)
from assetboard.domain.bonds import bond_value
from assetboard.domain.holdings import HoldingTransaction
from assetboard.domain.money import Money
from assetboard.domain.performance import (
    PriceObservation,
    RateObservation,
    SeriesPoint,
    Step,
    XirrFailure,
    XirrResult,
    add_months,
    cashflows_for_period,
    period_return,
    portfolio_series,
    portfolio_value_at,
    step_dates,
    xirr,
    years_before,
)
from assetboard.domain.portfolio import PortfolioAllocation
from assetboard.domain.prices import source_rank
from assetboard.domain.summary import ratio_tenths
from assetboard.services.bonds import remaining_lots, terms_from_row
from assetboard.services.context import Context, Role, require_role
from assetboard.services.holding_rows import holding_transaction

DEFAULT_RANGE = "1y"
RANGES: tuple[str, ...] = ("ytd", "1y", "3y", "5y", "all")
PERIODS: tuple[str, ...] = ("1m", "3m", "ytd", "1y", "all")


@dataclass(frozen=True, slots=True)
class PerformancePeriod:
    code: str
    start: date
    end: date
    start_value: Money
    end_value: Money
    change: Money
    change_tenths: int | None
    net_flows: Money
    change_excluding_flows: Money
    xirr: XirrResult
    display_rate: Decimal | None
    annualized: bool
    incomplete: bool


@dataclass(frozen=True, slots=True)
class PerformancePage:
    currency: str
    today: date
    range_code: str
    range_start: date
    class_scope: AssetClass | None
    class_options: tuple[AssetClass, ...]
    chart: tuple[SeriesPoint, ...]
    periods: tuple[PerformancePeriod, ...]
    undated_count: int
    incomplete_points: int
    first_date: date | None


def normalize_range(value: str) -> str:
    return value if value in RANGES else DEFAULT_RANGE


def _range_start(code: str, today: date, first_date: date | None) -> date:
    if code == "ytd":
        return date(today.year, 1, 1)
    if code == "1y":
        return years_before(today, 1)
    if code == "3y":
        return years_before(today, 3)
    if code == "5y":
        return years_before(today, 5)
    return (first_date - timedelta(days=1)) if first_date is not None else today


def _period_start(code: str, today: date, first_date: date | None) -> date:
    if code == "1m":
        return add_months(today, -1)
    if code == "3m":
        return add_months(today, -3)
    if code == "ytd":
        return date(today.year, 1, 1)
    if code == "1y":
        return years_before(today, 1)
    return (first_date - timedelta(days=1)) if first_date is not None else today


def _step_for(code: str, start: date, today: date) -> Step:
    span = (today - start).days
    if span <= 90:
        return Step.DAY
    if code == "all" and span > 5 * 365:
        return Step.MONTH
    return Step.WEEK


def _source_candidate(source: str, created_at: object, row_id: object) -> tuple[object, ...]:
    return (-source_rank(source), created_at, str(row_id))


def _effective_price_observations(
    price_rows: Sequence[PricePoint],
    transaction_rows: Sequence[InvestmentTransaction],
) -> dict[UUID, list[PriceObservation]]:
    best: dict[tuple[UUID, date], tuple[tuple[object, ...], Decimal]] = {}
    for row in price_rows:
        key = (row.instrument_id, row.date)
        candidate = _source_candidate(row.source, row.created_at, row.id)
        previous = best.get(key)
        if previous is None or candidate > previous[0]:
            best[key] = (candidate, row.price)
    for row in transaction_rows:
        if row.date is None:
            continue
        key = (row.instrument_id, row.date)
        candidate = _source_candidate("transaction", row.created_at, row.id)
        previous = best.get(key)
        if previous is None or candidate > previous[0]:
            best[key] = (candidate, row.unit_price)
    result: dict[UUID, list[PriceObservation]] = {}
    for (instrument_id, day), (_candidate, price) in best.items():
        result.setdefault(instrument_id, []).append(PriceObservation(day, price))
    for rows in result.values():
        rows.sort(key=lambda row: row.date)
    return result


def _effective_fx_observations(
    fx_rows: Sequence[FxRate],
    transaction_rows: Sequence[InvestmentTransaction],
    base_currency: str,
) -> dict[str, list[RateObservation]]:
    best: dict[tuple[str, date], tuple[tuple[object, ...], Decimal]] = {}
    for row in fx_rows:
        key = (row.quote_currency, row.date)
        candidate = _source_candidate(row.source, row.created_at, row.id)
        previous = best.get(key)
        if previous is None or candidate > previous[0]:
            best[key] = (candidate, row.rate)
    for row in transaction_rows:
        if row.date is None or row.quote_currency == base_currency:
            continue
        rate = row.fx_to_base if row.fx_to_base is not None else Decimal(1)
        if rate <= 0:
            continue
        key = (row.quote_currency, row.date)
        candidate = _source_candidate("transaction", row.created_at, row.id)
        previous = best.get(key)
        if previous is None or candidate > previous[0]:
            best[key] = (candidate, rate)
    result: dict[str, list[RateObservation]] = {}
    for (currency, day), (_candidate, rate) in best.items():
        result.setdefault(currency, []).append(RateObservation(day, rate))
    for rows in result.values():
        rows.sort(key=lambda row: row.date)
    return result


def _bond_observations(
    instrument: Instrument,
    terms: BondTerms,
    transaction_rows: Sequence[InvestmentTransaction],
    dates: Collection[date],
) -> list[PriceObservation]:
    term_value = terms_from_row(terms, instrument)
    rows = [
        row
        for row in transaction_rows
        if row.instrument_id == instrument.id and row.date is not None
    ]
    observations: list[PriceObservation] = []
    for day in sorted(set(dates)):
        held = [row for row in rows if row.date is not None and row.date <= day]
        lots = remaining_lots(held, term_value, current=False)
        quantity = sum((lot.quantity for lot in lots), Decimal(0))
        if quantity <= 0:
            continue
        valuation = bond_value(term_value, lots, day)
        observations.append(PriceObservation(day, valuation.net.to_decimal() / quantity))
    return observations


def performance_page(
    factory: sessionmaker[Session],
    ctx: Context,
    *,
    range_code: str = "",
    asset_class_id: UUID | None = None,
) -> PerformancePage:
    require_role(ctx, Role.VIEWER)
    today = ctx.clock.today(ctx.workspace.timezone)
    chosen_range = normalize_range(range_code)
    with factory() as session:
        workspace = CurrentWorkspaceRepository(session, ctx).get()
        base = workspace.base_currency
        classes = AssetClassRepository(session, ctx).ordered()
        instrument_rows = InstrumentRepository(session, ctx).ordered()
        transaction_rows = list(InvestmentTransactionRepository(session, ctx).live())
        transaction_ids = {row.instrument_id for row in transaction_rows}
        instruments = [row for row in instrument_rows if row.id in transaction_ids]
        allocation_rows = [
            row
            for row in InstrumentAllocationRepository(session, ctx).list()
            if row.instrument_id in transaction_ids
        ]
        bond_rows = {
            row.instrument_id: row
            for row in BondTermsRepository(session, ctx).for_instruments(transaction_ids)
        }
        price_rows = PricePointRepository(session, ctx).for_instruments_on_or_before(
            transaction_ids, today
        )
        fx_rows = FxRateRepository(session, ctx).for_base_on_or_before(base, today)
    dated_rows = [row for row in transaction_rows if row.date is not None]
    undated_count = len(transaction_rows) - len(dated_rows)
    first_date = min((row.date for row in dated_rows if row.date is not None), default=None)
    range_start = _range_start(chosen_range, today, first_date)
    step = _step_for(chosen_range, range_start, today)
    period_starts = {_period_start(code, today, first_date) for code in PERIODS}
    chart_dates = set(step_dates(range_start, today, step))
    observation_dates = chart_dates | period_starts | {today}

    prices = _effective_price_observations(price_rows, dated_rows)
    fx = _effective_fx_observations(fx_rows, dated_rows, base)
    class_order = {row.id: row.sort_order for row in classes}
    allocations: dict[UUID, list[PortfolioAllocation]] = {}
    for row in allocation_rows:
        allocations.setdefault(row.instrument_id, []).append(
            PortfolioAllocation(
                row.instrument_id,
                row.asset_class_id,
                row.weight_bp,
                class_order.get(row.asset_class_id, 0),
            )
        )
    allocation_map = {key: tuple(value) for key, value in allocations.items()}
    for instrument in instruments:
        if instrument.type == "currency":
            prices[instrument.id] = [PriceObservation(date(1900, 1, 1), Decimal(1))]
            continue
        terms = bond_rows.get(instrument.id)
        if terms is not None:
            prices[instrument.id] = _bond_observations(
                instrument, terms, dated_rows, observation_dates
            )

    class_scope = next((row for row in classes if row.id == asset_class_id), None)
    transactions = [holding_transaction(row) for row in dated_rows]
    chart = (
        portfolio_series(
            transactions,
            prices,
            fx,
            range_start,
            today,
            step,
            base_currency=base,
            allocations=allocation_map,
            asset_class_id=class_scope.id if class_scope is not None else None,
        )
        if dated_rows
        else ()
    )
    periods = tuple(
        _period(
            code, today, first_date, transactions, prices, fx, base, allocation_map, class_scope
        )
        for code in PERIODS
    )
    return PerformancePage(
        currency=base,
        today=today,
        range_code=chosen_range,
        range_start=range_start,
        class_scope=class_scope,
        class_options=tuple(row for row in classes if row.archived_at is None),
        chart=chart,
        periods=periods,
        undated_count=undated_count,
        incomplete_points=sum(1 for point in chart if point.incomplete),
        first_date=first_date,
    )


def _period(
    code: str,
    today: date,
    first_date: date | None,
    transactions: Sequence[HoldingTransaction],
    prices: Mapping[UUID, Sequence[PriceObservation]],
    fx: Mapping[str, Sequence[RateObservation]],
    base_currency: str,
    allocations: Mapping[UUID, Sequence[PortfolioAllocation]],
    class_scope: AssetClass | None,
) -> PerformancePeriod:
    start = _period_start(code, today, first_date)
    scope_id = class_scope.id if class_scope is not None else None
    start_point = portfolio_value_at(
        transactions,
        prices,
        fx,
        start,
        base_currency=base_currency,
        allocations=allocations,
        asset_class_id=scope_id,
    )
    end_point = portfolio_value_at(
        transactions,
        prices,
        fx,
        today,
        base_currency=base_currency,
        allocations=allocations,
        asset_class_id=scope_id,
    )
    change = end_point.value - start_point.value
    net_flows = end_point.net_invested - start_point.net_invested
    change_excluding_flows = change - net_flows
    incomplete = start_point.incomplete or end_point.incomplete
    if incomplete:
        result = XirrResult(None, XirrFailure.INCOMPLETE)
    elif not transactions:
        result = XirrResult(None, XirrFailure.NO_CASHFLOWS)
    else:
        result = xirr(
            cashflows_for_period(
                transactions,
                start,
                today,
                start_value=start_point.value,
                end_value=end_point.value,
                base_currency=base_currency,
                allocations=allocations,
                asset_class_id=scope_id,
            )
        )
    days = (today - start).days
    annualized = days >= 365
    display_rate = result.rate
    if result.rate is not None and not annualized and days < 365:
        display_rate = period_return(result.rate, days)
    return PerformancePeriod(
        code=code,
        start=start,
        end=today,
        start_value=start_point.value,
        end_value=end_point.value,
        change=change,
        change_tenths=ratio_tenths(change.amount_minor, start_point.value.amount_minor),
        net_flows=net_flows,
        change_excluding_flows=change_excluding_flows,
        xirr=result,
        display_rate=display_rate,
        annualized=annualized,
        incomplete=incomplete,
    )
