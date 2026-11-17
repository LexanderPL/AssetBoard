"""Portfolio value changes for the Home dashboard (F9.1).

This is a read-only composition of F6.7's historical valuation: one ``load_history`` read
produces values at yesterday, the last day of the previous month and today; the changes and
their percentages are simple differences of those already-valued amounts. No valuation formula
is repeated here, and no quantity or price is aggregated in SQL.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.domain.money import Money
from assetboard.domain.summary import ratio_tenths
from assetboard.services.context import Context, Role, require_role
from assetboard.services.portfolio_history import (
    HistoricalPortfolio,
    HistoricalValues,
    historical_values,
    load_history,
)

__all__ = [
    "PortfolioChanges",
    "PortfolioMover",
    "ValueChange",
    "portfolio_changes",
]

MAJOR_MOVERS = 3


@dataclass(frozen=True, slots=True)
class ValueChange:
    amount: Money | None
    percent_tenths: int | None

    @property
    def available(self) -> bool:
        return self.amount is not None


@dataclass(frozen=True, slots=True)
class PortfolioMover:
    instrument_id: UUID
    name: str
    change: ValueChange


@dataclass(frozen=True, slots=True)
class PortfolioChanges:
    currency: str
    previous_day: date
    month_start: date
    since_previous_day: ValueChange
    since_month_start: ValueChange
    movers: tuple[PortfolioMover, ...]
    has_data: bool


def _total(data: HistoricalPortfolio, values: HistoricalValues, day: date) -> Money | None:
    total = Money(0, data.currency)
    for instrument in data.instruments:
        value = values.instrument(instrument.id, day)
        if value is None:
            return None
        total += value
    return total


def _change(previous: Money | None, current: Money | None) -> ValueChange:
    if previous is None or current is None:
        return ValueChange(amount=None, percent_tenths=None)
    amount = current - previous
    return ValueChange(
        amount=amount,
        percent_tenths=ratio_tenths(amount.amount_minor, previous.amount_minor),
    )


def portfolio_changes(factory: sessionmaker[Session], ctx: Context) -> PortfolioChanges:
    """Value change since yesterday and since the previous month-end, plus the largest movers."""
    require_role(ctx, Role.VIEWER)
    today = ctx.clock.today(ctx.workspace.timezone)
    previous_day = today - timedelta(days=1)
    month_start = today.replace(day=1)
    month_baseline = month_start - timedelta(days=1)
    with factory() as session:
        data = load_history(
            session,
            ctx,
            observation_dates={previous_day, month_baseline, today},
            end=today,
        )
        values = historical_values(data, {previous_day, month_baseline, today})
    if not data.transactions:
        return PortfolioChanges(
            currency=data.currency,
            previous_day=previous_day,
            month_start=month_start,
            since_previous_day=ValueChange(None, None),
            since_month_start=ValueChange(None, None),
            movers=(),
            has_data=False,
        )

    previous_total = _total(data, values, previous_day)
    month_total = _total(data, values, month_baseline)
    current_total = _total(data, values, today)
    movers: list[PortfolioMover] = []
    for instrument in data.instruments:
        before = values.instrument(instrument.id, previous_day)
        now = values.instrument(instrument.id, today)
        if before is None or now is None:
            continue
        change = _change(before, now)
        if change.amount is not None and change.amount.amount_minor != 0:
            movers.append(
                PortfolioMover(
                    instrument_id=instrument.id,
                    name=instrument.name,
                    change=change,
                )
            )
    movers.sort(
        key=lambda row: (
            -abs(row.change.amount.amount_minor if row.change.amount is not None else 0),
            row.name.casefold(),
            str(row.instrument_id),
        )
    )
    return PortfolioChanges(
        currency=data.currency,
        previous_day=previous_day,
        month_start=month_start,
        since_previous_day=_change(previous_total, current_total),
        since_month_start=_change(month_total, current_total),
        movers=tuple(movers[:MAJOR_MOVERS]),
        has_data=True,
    )
