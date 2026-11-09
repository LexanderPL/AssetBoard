"""Historical portfolio values for the F6.8 net-worth resolvers.

One load reads the workspace's instruments, allocations, dated transactions, price points, FX
rates and bond terms, reusing the F6.7 observation builders. Values at past dates use the F6.7
rules: dated lots only, prices and FX carried forward, and the F6.5 model value for bond
instruments. Current values deliberately use the separate F6.6 path in ``services.portfolio``
because that path includes undated legacy lots.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
from uuid import UUID

from sqlalchemy.orm import Session

from assetboard.db.models.investments import Instrument
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
from assetboard.domain.holdings import (
    HoldingTransaction,
    InvestmentTransactionType,
    quantize_quantity,
    split_factor,
)
from assetboard.domain.money import Money
from assetboard.domain.performance import (
    Observations,
    PriceObservation,
    RateObservation,
)
from assetboard.domain.portfolio import (
    PortfolioAllocation,
    PortfolioHolding,
    PortfolioPrice,
    value_portfolio,
)
from assetboard.services.context import Context
from assetboard.services.holding_rows import holding_transaction
from assetboard.services.performance import (
    bond_observations,
    fx_observations,
    price_observations,
)


@dataclass(frozen=True, slots=True)
class HistoricalPortfolio:
    currency: str
    instruments: tuple[Instrument, ...]
    allocations: Mapping[UUID, tuple[PortfolioAllocation, ...]]
    transactions: tuple[HoldingTransaction, ...]
    prices: Mapping[UUID, Observations]
    fx: Mapping[str, Observations]


@dataclass(frozen=True, slots=True)
class HistoricalValues:
    currency: str
    instruments: Mapping[tuple[UUID, date], Money | None]
    classes: Mapping[tuple[UUID, date], Money | None]

    def instrument(self, instrument_id: UUID, day: date) -> Money | None:
        return self.instruments.get((instrument_id, day), Money(0, self.currency))

    def asset_class(self, asset_class_id: UUID, day: date) -> Money | None:
        return self.classes.get((asset_class_id, day), Money(0, self.currency))


def load_history(
    session: Session,
    ctx: Context,
    *,
    observation_dates: Collection[date],
    end: date,
) -> HistoricalPortfolio:
    workspace = CurrentWorkspaceRepository(session, ctx).get()
    base = workspace.base_currency
    classes = tuple(AssetClassRepository(session, ctx).ordered())
    instrument_rows = tuple(InstrumentRepository(session, ctx).ordered())
    transaction_rows = tuple(InvestmentTransactionRepository(session, ctx).live())
    dated = [row for row in transaction_rows if row.date is not None]
    transaction_ids = {row.instrument_id for row in transaction_rows}
    instruments = tuple(row for row in instrument_rows if row.id in transaction_ids)
    allocations_rows = [
        row
        for row in InstrumentAllocationRepository(session, ctx).list()
        if row.instrument_id in transaction_ids
    ]
    class_order = {row.id: row.sort_order for row in classes}
    by_instrument: dict[UUID, list[PortfolioAllocation]] = {}
    for row in allocations_rows:
        by_instrument.setdefault(row.instrument_id, []).append(
            PortfolioAllocation(
                row.instrument_id,
                row.asset_class_id,
                row.weight_bp,
                class_order.get(row.asset_class_id, 0),
            )
        )
    bond_rows = {
        row.instrument_id: row
        for row in BondTermsRepository(session, ctx).for_instruments(transaction_ids)
    }
    price_rows = PricePointRepository(session, ctx).for_instruments_on_or_before(
        transaction_ids, end
    )
    fx_rows = FxRateRepository(session, ctx).for_base_on_or_before(base, end)
    prices: dict[UUID, Observations] = {
        instrument_id: _observations(rows)
        for instrument_id, rows in price_observations(price_rows, dated).items()
    }
    fx: dict[str, Observations] = {
        currency: _observations(rows)
        for currency, rows in fx_observations(fx_rows, dated, base).items()
    }
    for instrument in instruments:
        if instrument.type == "currency":
            prices[instrument.id] = Observations((date(1900, 1, 1),), (Decimal(1),))
            continue
        terms = bond_rows.get(instrument.id)
        if terms is not None:
            prices[instrument.id] = _observations(
                bond_observations(instrument, terms, list(dated), observation_dates)
            )
    return HistoricalPortfolio(
        currency=base,
        instruments=instruments,
        allocations={key: tuple(value) for key, value in by_instrument.items()},
        transactions=tuple(holding_transaction(row) for row in dated),
        prices=prices,
        fx=fx,
    )


def historical_values(data: HistoricalPortfolio, dates: Collection[date]) -> HistoricalValues:
    wanted = sorted(set(dates))
    instrument_values: dict[tuple[UUID, date], Money | None] = {}
    class_values: dict[tuple[UUID, date], Money | None] = {}
    rows = sorted(
        data.transactions,
        key=lambda row: (row.date or date.min, row.created_at, str(row.id)),
    )
    splits_by_instrument: dict[UUID, list[HoldingTransaction]] = {}
    for row in data.transactions:
        if InvestmentTransactionType(row.type) is InvestmentTransactionType.SPLIT:
            splits_by_instrument.setdefault(row.instrument_id, []).append(row)
    quantities: dict[UUID, Decimal] = {}
    pointer = 0
    class_ids = {
        allocation.asset_class_id for values in data.allocations.values() for allocation in values
    }
    for day in wanted:
        while pointer < len(rows):
            row = rows[pointer]
            row_date = row.date
            assert row_date is not None
            if row_date > day:
                break
            pointer += 1
            kind = InvestmentTransactionType(row.type)
            if kind is InvestmentTransactionType.BUY:
                quantities[row.instrument_id] = (
                    quantities.get(row.instrument_id, Decimal(0)) + row.quantity
                )
            elif kind is InvestmentTransactionType.SELL:
                quantities[row.instrument_id] = (
                    quantities.get(row.instrument_id, Decimal(0)) - row.quantity
                )
            elif kind is InvestmentTransactionType.SPLIT:
                with localcontext() as context:
                    context.prec = max(context.prec, 80)
                    ratio = row.quantity / row.unit_price
                    quantities[row.instrument_id] = quantize_quantity(
                        quantities.get(row.instrument_id, Decimal(0)) * ratio
                    )

        held = [
            instrument
            for instrument in data.instruments
            if quantities.get(instrument.id, Decimal(0)) > 0
        ]
        holdings = [
            PortfolioHolding(
                instrument_id=instrument.id,
                quantity=quantities[instrument.id],
                cost_basis=Money(0, data.currency),
                quote_currency=instrument.quote_currency,
            )
            for instrument in held
        ]
        price_map: list[PortfolioPrice] = []
        for instrument in held:
            series = data.prices.get(instrument.id)
            observation = series.latest_with_date(day) if series is not None else None
            if observation is None:
                price_map.append(PortfolioPrice(instrument.id, None))
                continue
            factor = split_factor(splits_by_instrument.get(instrument.id, ()), observation[0], day)
            price_map.append(PortfolioPrice(instrument.id, observation[1] * factor))
        rates: dict[str, Decimal] = {}
        for instrument in held:
            if instrument.quote_currency == data.currency:
                continue
            series = data.fx.get(instrument.quote_currency)
            rate = series.latest(day) if series is not None else None
            if rate is not None:
                rates[instrument.quote_currency] = rate
        valuation = value_portfolio(
            holdings,
            price_map,
            rates,
            [
                allocation
                for instrument in held
                for allocation in data.allocations.get(instrument.id, ())
            ],
            day,
            base_currency=data.currency,
        )
        row_by_id = {row.instrument_id: row for row in valuation.rows}
        for instrument in data.instruments:
            row = row_by_id.get(instrument.id)
            if row is None:
                instrument_values[(instrument.id, day)] = Money(0, data.currency)
            elif row.missing is not None:
                instrument_values[(instrument.id, day)] = None
            else:
                instrument_values[(instrument.id, day)] = row.value
        for class_id in class_ids:
            missing = any(
                row.missing is not None
                and any(
                    allocation.asset_class_id == class_id
                    for allocation in data.allocations.get(row.instrument_id, ())
                )
                for row in valuation.rows
            )
            class_values[(class_id, day)] = (
                None if missing else Money(valuation.class_value(class_id), data.currency)
            )
    return HistoricalValues(
        currency=data.currency,
        instruments=instrument_values,
        classes=class_values,
    )


def _observations(rows: Sequence[PriceObservation | RateObservation]) -> Observations:
    ordered = sorted(rows, key=lambda row: row.date)
    return Observations(
        dates=tuple(row.date for row in ordered),
        values=tuple(
            row.price if isinstance(row, PriceObservation) else row.rate for row in ordered
        ),
    )
